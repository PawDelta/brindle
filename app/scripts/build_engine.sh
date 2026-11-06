#!/usr/bin/env bash
# build_engine.sh — build the engine brindle.app carries, into
# app/src-tauri/engine/ (bundled as Contents/Resources/engine, see
# tauri.macos.conf.json, which runs this before every macOS `tauri build`):
#
#   python/       python-build-standalone's CPython, with brindle and its
#                 locked dependencies installed into its site-packages
#   bin/brindle   the launcher (scripts/engine/brindle):
#                 python/bin/python3 -I -B -X brindle-build=<BUILD> -m brindle
#   bin/tmux      tmux, its libevent linked in statically (it needs only
#                 macOS's own libraries)
#   BUILD         this build's id (its inputs' hash): the launcher puts it on
#                 every brindle's command line, so the app knows a daemon of
#                 this build from an older one's
#   licenses/     the licenses of Python, tmux, libevent and utf8proc, for
#                 the app's third-party notices (scripts/notices.mjs)
#
# and, beside it, the engine's helper app into app/src-tauri/engine-helper/
# (bundled as Contents/Library/LoginItems/brindle.app): what the daemon's
# launchd job runs, so macOS names brindle, with a reason, when the engine or
# an agent needs its permission (scripts/engine/helper). It is always signed
# (ad hoc without an identity): the app's signature takes nested code only
# signed.
#
# brindle comes from this same checkout (BRINDLE_SRC, default the repo's root)
# at BRINDLE_REF (default HEAD, so uncommitted engine changes aren't in it):
# only the engine's own paths, exported with `git archive`, and the checkout
# itself is only read. The build is keyed on those paths' trees, not the
# commit, so a commit that changes only the app rebuilds nothing. Downloads are
# pinned by sha256 and cached, as is the build, in target/engine-build/. A
# second run with the same inputs does nothing (engine/.stamp); --force
# rebuilds anyway. TAURI_ENV_TARGET_TRIPLE selects the architecture; Intel
# builds on Apple Silicon use Rosetta to run the target Python and smoke checks.
#
# Usage: bash app/scripts/build_engine.sh [--force]
set -euo pipefail

PY_VERSION=3.13.16
PY_RELEASE=20261003
PY_SHA256_aarch64=9e01f63bbb08576cd9c8bc2d0564d098cb30c8453a0cd4bcf6aef458f6d2a147
PY_SHA256_x86_64=b4dad38ba6a344555ccb71a1b08caad0a6c0dda88c5803658bc95bd7f04e9f5c
TMUX_VERSION=3.8
TMUX_SHA256=e79c699c7e949dccd0a4a125e17b8d1261e311b16979dbc8eb34542f3966d82e
LIBEVENT_VERSION=2.1.13-stable
LIBEVENT_SHA256=f7e9383b8c0baa81b687e5b5eecc01beefaf1b19b64151d95ed61647fe7a315c
UTF8PROC_VERSION=2.11.3
UTF8PROC_SHA256=abfed50b6d4da51345713661370290f4f4747263ee73dc90356299dfc7990c78
TMUX_URL="https://github.com/tmux/tmux/releases/download/$TMUX_VERSION/tmux-$TMUX_VERSION.tar.gz"
LIBEVENT_URL="https://github.com/libevent/libevent/releases/download/release-$LIBEVENT_VERSION/libevent-$LIBEVENT_VERSION.tar.gz"
UTF8PROC_URL="https://github.com/JuliaStrings/utf8proc/archive/refs/tags/v$UTF8PROC_VERSION.tar.gz"
# The oldest macOS the engine's binaries run on (python-build-standalone's own
# floor). An API newer than that is an error, not a weak import that crashes
# there: configure's checks then find it missing. The app declares the same
# minimum (tauri.macos.conf.json's minimumSystemVersion; smoke_bundle.sh
# --engine checks the two agree).
export MACOSX_DEPLOYMENT_TARGET=11.0
export CFLAGS="-O2 -Werror=unguarded-availability-new"

APP="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$APP/src-tauri/engine"
# The helper app (tauri.macos.conf.json bundles it into Library/LoginItems).
HELPER_OUT="$APP/src-tauri/engine-helper/brindle.app"
HELPER_SRC="$APP/scripts/engine/helper"
HELPER_ICON="$APP/src-tauri/icons/icon.icns"
# This checkout's own, never under CARGO_TARGET_DIR: worktrees share that
# (brindle.build_caches), and the build deletes and recreates its staging
# dirs here outside Cargo's lock.
WORK="$APP/src-tauri/target/engine-build"
# What the engine's wheel is built from: everything else in the repo is the app's.
ENGINE_PATHS=(src pyproject.toml uv.lock README.md LICENSE SCHEDULE-A)
FORCE=0
case "${1:-}" in
  --force) FORCE=1 ;;
  "") ;;
  -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

die() { echo "build_engine: $*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || die "builds the macOS engine; run it on a Mac"
case "${TAURI_ENV_TARGET_TRIPLE:-}" in
  ""|aarch64-apple-darwin|x86_64-apple-darwin) ;;
  *) die "unsupported target ${TAURI_ENV_TARGET_TRIPLE}" ;;
esac
case "$(uname -m)" in
  arm64) ARCH=aarch64 ;;
  x86_64) ARCH=x86_64 ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac
CONFIGURE_BUILD="$(uname -m)-apple-darwin"
if [ -n "${TAURI_ENV_TARGET_TRIPLE:-}" ]; then
  ARCH="${TAURI_ENV_TARGET_TRIPLE%-apple-darwin}"
fi
MACH_ARCH="$ARCH"
[ "$ARCH" != aarch64 ] || MACH_ARCH=arm64
CONFIGURE_HOST="$MACH_ARCH-apple-darwin"
export CC="clang -arch $MACH_ARCH"
export CFLAGS="$CFLAGS -arch $MACH_ARCH"
export LDFLAGS="-arch $MACH_ARCH"
command -v uv >/dev/null || die "needs uv (https://docs.astral.sh/uv) to build brindle's wheel"

BRINDLE_SRC="${BRINDLE_SRC:-$(git -C "$APP" rev-parse --show-toplevel)}" \
  || die "$APP is not in a git checkout (set BRINDLE_SRC)"
BRINDLE_REF="${BRINDLE_REF:-HEAD}"
[ -d "$BRINDLE_SRC" ] || die "no brindle checkout at $BRINDLE_SRC (set BRINDLE_SRC)"
COMMIT="$(git -C "$BRINDLE_SRC" rev-parse --verify "$BRINDLE_REF^{commit}")" \
  || die "no $BRINDLE_REF in $BRINDLE_SRC (set BRINDLE_REF)"
# The engine paths' object ids at that commit: the same engine, the same key.
ENGINE_TREE="$(git -C "$BRINDLE_SRC" ls-tree "$COMMIT" -- "${ENGINE_PATHS[@]}" | shasum -a 256 | cut -c1-16)"

TMUX_STAMP="tmux=$TMUX_VERSION libevent=$LIBEVENT_VERSION utf8proc=$UTF8PROC_VERSION macos=$MACOSX_DEPLOYMENT_TARGET arch=$ARCH compiler=$CC"
STAMP="python=$PY_VERSION+$PY_RELEASE $TMUX_STAMP arch=$ARCH brindle=$ENGINE_TREE"
ENTITLEMENTS="$APP/scripts/engine/engine.entitlements"
# The helper's: python's, and what macOS needs to ask on its behalf (the file says).
HELPER_ENTITLEMENTS="$HELPER_SRC/helper.entitlements"
STAMP="$STAMP sign=${APPLE_SIGNING_IDENTITY:-none}"
STAMP="$STAMP build=$(cat "$0" "$APP/scripts/engine/brindle" "$ENTITLEMENTS" "$HELPER_ENTITLEMENTS" \
  "$HELPER_SRC/main.c" "$HELPER_SRC/Info.plist" "$HELPER_ICON" | shasum -a 256 | cut -c1-16)"
if [ "$FORCE" = 0 ] && [ "$(cat "$OUT/.stamp" 2>/dev/null)" = "$STAMP" ] \
    && [ -x "$HELPER_OUT/Contents/MacOS/brindle" ]; then
  echo "build_engine: $OUT is up to date (brindle ${COMMIT:0:10})"
  exit 0
fi

mkdir -p "$WORK/downloads"

# fetch URL SHA256: the file, downloaded once into downloads/ and checked.
fetch() {
  local url="$1" sha="$2" file
  file="$WORK/downloads/$(basename "$url")"
  if [ ! -f "$file" ] || [ "$(shasum -a 256 "$file" | cut -d' ' -f1)" != "$sha" ]; then
    echo "build_engine: downloading $url" >&2
    # Any failure retried, a dropped connection too (plain --retry skips those).
    curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o "$file.part" "$url"
    [ "$(shasum -a 256 "$file.part" | cut -d' ' -f1)" = "$sha" ] \
      || { rm -f "$file.part"; die "$(basename "$url"): sha256 doesn't match the pinned one"; }
    mv "$file.part" "$file"
  fi
  echo "$file"
}

# ---- tmux, with libevent and utf8proc linked in -------------------------

build_tmux() {
  local deps="$WORK/deps-$ARCH" src="$WORK/src-$ARCH" built="$WORK/tmux-$TMUX_VERSION-$ARCH" jobs
  jobs="$(sysctl -n hw.ncpu)"
  if [ -x "$built" ] && [ "$(cat "$built.stamp" 2>/dev/null)" = "$TMUX_STAMP" ]; then
    return
  fi
  rm -rf "$deps" "$src"; mkdir -p "$deps/lib" "$deps/include" "$src"
  tar -xzf "$(fetch "$LIBEVENT_URL" "$LIBEVENT_SHA256")" -C "$src"
  tar -xzf "$(fetch "$UTF8PROC_URL" "$UTF8PROC_SHA256")" -C "$src"
  tar -xzf "$(fetch "$TMUX_URL" "$TMUX_SHA256")" -C "$src"
  (
    cd "$src/libevent-$LIBEVENT_VERSION"
    # pipe2 is in the SDK but only from macOS 27: configure's link test would find it.
    ac_cv_func_pipe2=no ./configure --host="$CONFIGURE_HOST" --build="$CONFIGURE_BUILD" --prefix="$deps" --disable-shared --enable-static \
      --disable-openssl --disable-samples --disable-libevent-regress --disable-debug-mode >/dev/null
    make -j"$jobs" >/dev/null
    make install >/dev/null
  )
  (
    # tmux's widths for emoji and the like: macOS's own wcwidth is out of date.
    cd "$src/utf8proc-$UTF8PROC_VERSION"
    make -j"$jobs" libutf8proc.a >/dev/null
    cp libutf8proc.a "$deps/lib/"
    cp utf8proc.h "$deps/include/"
  )
  (
    cd "$src/tmux-$TMUX_VERSION"
    # Only these libraries: pkg-config looks nowhere else (not in Homebrew's
    # prefix), and with no .dylib beside them the linker takes the .a files;
    # ncurses is then macOS's own, found without pkg-config. No jemalloc,
    # which tmux 3.8 asks for on macOS (docs/bundled-engine.md).
    PKG_CONFIG_LIBDIR="$deps/lib/pkgconfig" PKG_CONFIG_PATH="" \
      CPPFLAGS="-I$deps/include" LDFLAGS="$LDFLAGS -L$deps/lib" \
      ./configure --host="$CONFIGURE_HOST" --build="$CONFIGURE_BUILD" --prefix="$src/prefix" --enable-utf8proc --disable-jemalloc >/dev/null
    make -j"$jobs" >/dev/null
  )
  cp "$src/tmux-$TMUX_VERSION/tmux" "$built"
  echo "$TMUX_STAMP" > "$built.stamp"
}

# The libraries a Mach-O file links that aren't macOS's own.
foreign_libs() {
  otool -L "$1" | tail -n +2 | awk '{print $1}' | grep -v -E '^(/usr/lib/|/System/Library/)' || true
}

# The compilers' output goes to a log, shown when the build fails.
set +e
( set -e; build_tmux ) >"$WORK/tmux-build.log" 2>&1
status=$?
set -e
if [ "$status" != 0 ]; then
  tail -40 "$WORK/tmux-build.log" >&2
  die "building tmux failed (all of it in $WORK/tmux-build.log)"
fi
bad="$(foreign_libs "$WORK/tmux-$TMUX_VERSION-$ARCH")"
[ -z "$bad" ] || die "tmux links libraries outside macOS: $bad"

# ---- Python, with brindle installed -------------------------------------

STAGE="$WORK/stage"
rm -rf "$STAGE"; mkdir -p "$STAGE/bin"
py_sha_var="PY_SHA256_$ARCH"
tar -xzf "$(fetch "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/cpython-$PY_VERSION+$PY_RELEASE-$ARCH-apple-darwin-install_only_stripped.tar.gz" "${!py_sha_var}")" -C "$STAGE"
PY="$STAGE/python/bin/python3"
PYLIB="$STAGE/python/lib/python${PY_VERSION%.*}"

rm -rf "$WORK/brindle-src" "$WORK/wheel"; mkdir -p "$WORK/brindle-src"
git -C "$BRINDLE_SRC" archive --format=tar "$COMMIT" -- "${ENGINE_PATHS[@]}" | tar -x -C "$WORK/brindle-src"
uv build --quiet --wheel --out-dir "$WORK/wheel" "$WORK/brindle-src"
# brindle's dependencies as its uv.lock pins them (with their hashes), then brindle.
uv export --quiet --project "$WORK/brindle-src" --frozen --no-dev --no-emit-project \
  --no-header -o "$WORK/requirements.txt"
uv pip install --quiet --python "$PY" --break-system-packages -r "$WORK/requirements.txt"
uv pip install --quiet --python "$PY" --break-system-packages --no-deps "$WORK"/wheel/brindle-*.whl

# Console scripts installed by uv name the staging interpreter in their
# shebang. A shell/Python trampoline keeps every Python entry point usable
# after the engine moves into the app, including python/bin/brindle (Codex's
# stable hook command). -P keeps the caller's directory out of imports.
"$PY" -I - "$STAGE/python/bin" <<'PY'
import os
import sys
from pathlib import Path

bin_dir = Path(sys.argv[1])
header = b"""#!/bin/sh
'''exec' "$(/usr/bin/dirname "$0")/python3" -P "$0" "$@"
' '''
"""
for script in bin_dir.iterdir():
    if script.is_symlink() or not script.is_file():
        continue
    with script.open("rb") as f:
        first = f.readline(4096)
        if not first.startswith(b"#!"):
            continue
        body = f.read()
    # uv uses a shell trampoline itself for long paths or paths with spaces.
    if first == b"#!/bin/sh\n" and body.startswith(b"'''exec' "):
        command, end, body = body.split(b"\n", 2)
        if end != b"' '''" or os.fsencode(str(bin_dir / "python")) not in command:
            continue
    else:
        interpreter = Path(os.fsdecode(first[2:].split()[0]))
        if interpreter.parent != bin_dir or not interpreter.name.startswith("python"):
            continue
    script.write_bytes(header + body)
PY

# ---- The helper app ------------------------------------------------------

# Python in the helper's own process, from the engine's libpython (found by
# the bundle's layout, as main.c finds the engine), built against this
# Python's headers before they go. Its version is the engine's: an app update
# that leaves the engine as it was leaves the helper as it was.
HELPER="$WORK/helper/brindle.app"
rm -rf "$WORK/helper"; mkdir -p "$HELPER/Contents/MacOS" "$HELPER/Contents/Resources"
# shellcheck disable=SC2086  # CFLAGS is a list
cc $CFLAGS -Wall -Wextra -Werror -arch "$MACH_ARCH" \
  -isystem "$STAGE/python/include/python${PY_VERSION%.*}" "$HELPER_SRC/main.c" \
  -L"$STAGE/python/lib" -lpython"${PY_VERSION%.*}" \
  -Wl,-rpath,@executable_path/../../../../../Resources/engine/python/lib \
  -o "$HELPER/Contents/MacOS/brindle" || die "compiling the engine's helper failed"
bad="$(foreign_libs "$HELPER/Contents/MacOS/brindle" | grep -v -x "@rpath/libpython${PY_VERSION%.*}.dylib" || true)"
[ -z "$bad" ] || die "the engine's helper links libraries outside macOS and the engine: $bad"
cp "$HELPER_SRC/Info.plist" "$HELPER/Contents/Info.plist"
cp "$HELPER_ICON" "$HELPER/Contents/Resources/icon.icns"
engine_version="$("$PY" -I -c 'import importlib.metadata as m; print(m.version("brindle"))')"
for key in CFBundleShortVersionString CFBundleVersion; do
  /usr/libexec/PlistBuddy -c "Set :$key $engine_version" "$HELPER/Contents/Info.plist"
done

# What the engine never uses: Tk, IDLE, the bundled pip, headers, man pages.
rm -rf "$STAGE/python/include" "$STAGE/python/share" \
  "$STAGE"/python/lib/tcl* "$STAGE"/python/lib/tk* "$STAGE"/python/lib/itcl* \
  "$STAGE"/python/lib/thread* "$STAGE"/python/lib/libtcl* "$STAGE"/python/lib/libtk* \
  "$STAGE"/python/bin/idle* "$STAGE"/python/bin/pip* "$STAGE"/python/bin/pydoc* \
  "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" "$PYLIB/turtle.py" "$PYLIB/ensurepip" \
  "$PYLIB"/lib-dynload/_tkinter.* "$PYLIB"/site-packages/pip "$PYLIB"/site-packages/pip-*.dist-info

# All the bytecode, now: the launcher runs python -B, as writing __pycache__
# into the app would break its signature. unchecked-hash: valid whatever
# mtimes the copy into the .app gives the sources.
find "$STAGE/python/lib" -name __pycache__ -type d -prune -exec rm -rf {} +
"$PY" -I -m compileall -q -j0 --invalidation-mode unchecked-hash "$STAGE/python/lib" >/dev/null \
  || die "compiling the engine's bytecode failed"

cp "$WORK/tmux-$TMUX_VERSION-$ARCH" "$STAGE/bin/tmux"
cp "$APP/scripts/engine/brindle" "$STAGE/bin/brindle"
chmod 755 "$STAGE/bin/brindle" "$STAGE/bin/tmux"

# The licenses of what the engine carries besides brindle and its Python
# packages (those keep theirs in site-packages/*.dist-info), for the notices.
mkdir -p "$STAGE/licenses"
cp "$PYLIB/LICENSE.txt" "$STAGE/licenses/python.txt"
tar -xzOf "$(fetch "$TMUX_URL" "$TMUX_SHA256")" "tmux-$TMUX_VERSION/COPYING" > "$STAGE/licenses/tmux.txt"
tar -xzOf "$(fetch "$LIBEVENT_URL" "$LIBEVENT_SHA256")" "libevent-$LIBEVENT_VERSION/LICENSE" > "$STAGE/licenses/libevent.txt"
tar -xzOf "$(fetch "$UTF8PROC_URL" "$UTF8PROC_SHA256")" "utf8proc-$UTF8PROC_VERSION/LICENSE.md" > "$STAGE/licenses/utf8proc.txt"

# Signed for notarization when the app is (APPLE_SIGNING_IDENTITY, as tauri
# build reads it): tauri signs the app's own binary, not the Mach-O files in
# its Resources. Every library and extension module first, then the two
# executables with the hardened runtime's exceptions python needs
# (engine.entitlements). "-" signs ad hoc, to try the hardened runtime
# locally: without a timestamp, and without library validation, which
# refuses ad hoc signed modules (they have no team).
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
  ts=--timestamp
  if [ "$APPLE_SIGNING_IDENTITY" = - ]; then
    ts=--timestamp=none
    cp "$ENTITLEMENTS" "$WORK/adhoc.entitlements"
    cp "$HELPER_ENTITLEMENTS" "$WORK/adhoc-helper.entitlements"
    for adhoc in "$WORK/adhoc.entitlements" "$WORK/adhoc-helper.entitlements"; do
      /usr/libexec/PlistBuddy -c "Add :com.apple.security.cs.disable-library-validation bool true" "$adhoc"
    done
    ENTITLEMENTS="$WORK/adhoc.entitlements"
    HELPER_ENTITLEMENTS="$WORK/adhoc-helper.entitlements"
  fi
  # Apple's timestamp server fails now and then (rc.7's Intel build): try five times over
  # about two minutes, showing each failure, so the last one's error is codesign's own.
  sign() {
    local attempt out
    for attempt in 1 2 3 4 5; do
      out="$(codesign --force --options runtime "$ts" --sign "$APPLE_SIGNING_IDENTITY" "$@" 2>&1)" && return 0
      printf 'build_engine: codesign (try %s of 5): %s\n' "$attempt" "$out" >&2
      [ "$attempt" = 5 ] || sleep $((attempt * 10))
    done
    return 1
  }
  find "$STAGE" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 | while IFS= read -r -d '' lib; do
    sign "$lib" >/dev/null || die "codesign failed for $lib"
  done
  for exe in "$STAGE/bin/tmux" "$STAGE/python/bin/python${PY_VERSION%.*}"; do
    sign --entitlements "$ENTITLEMENTS" "$exe" >/dev/null || die "codesign failed for $exe"
  done
  # The helper as the bundle it is (its Info.plist bound, its identifier the bundle's).
  sign --entitlements "$HELPER_ENTITLEMENTS" "$HELPER" >/dev/null || die "codesign failed for $HELPER"
else
  # No identity: the helper alone is signed, ad hoc and without the hardened
  # runtime, whose library validation would refuse the engine's unsigned libpython.
  codesign --force --sign - --entitlements "$HELPER_ENTITLEMENTS" "$HELPER" 2>/dev/null \
    || die "codesign failed for $HELPER"
fi
# Whatever signed it: the entitlements macOS needs to ask on the helper's behalf are in the signature.
signed="$(codesign -d --entitlements - "$HELPER" 2>/dev/null || true)"
for entitlement in com.apple.security.automation.apple-events com.apple.security.device.audio-input; do
  case "$signed" in
    *"$entitlement"*) ;;
    *) die "the engine's helper app isn't signed with $entitlement" ;;
  esac
done

printf '%s' "$STAMP" | shasum -a 256 | cut -c1-16 > "$STAGE/BUILD"

# It runs with nothing from this machine's environment.
want="$("$PY" -I -c 'import importlib.metadata as m; print("brindle", m.version("brindle"))')"
got="$(env -i HOME="$WORK" PATH=/usr/bin:/bin "$STAGE/bin/brindle" --version 2>&1 | tail -1)"
case "$got" in
  *"${want#brindle }"*) ;;
  *) die "the staged engine's brindle --version printed '$got', wanted '$want'" ;;
esac
env -i PATH=/usr/bin:/bin "$STAGE/bin/tmux" -V >/dev/null || die "the staged tmux doesn't run"

rm -rf "$OUT"
mv "$STAGE" "$OUT"
# Exercise both launch paths after relocation, from outside the engine.
# Checking only in stage misses absolute shebangs pointing back into stage.
for launcher in "$OUT/bin/brindle" "$OUT/python/bin/brindle"; do
  got="$(cd "$WORK" && env -i HOME="$WORK" PATH=/usr/bin:/bin "$launcher" --version)"
  [ "$got" = "$want" ] || die "relocated $launcher printed '$got', wanted '$want'"
done

# The helper, where it is bundled from; and run as the daemon's job runs it,
# in the bundle's layout (target/engine-build/bundle, kept for trying it by
# hand): it finds the engine and its libpython by that layout alone.
rm -rf "$(dirname "$HELPER_OUT")"; mkdir -p "$(dirname "$HELPER_OUT")"
mv "$HELPER" "$HELPER_OUT"
codesign --verify --strict "$HELPER_OUT" || die "the engine's helper app isn't validly signed"
LAYOUT="$WORK/bundle/Contents"
rm -rf "$WORK/bundle"; mkdir -p "$LAYOUT/Resources" "$LAYOUT/Library/LoginItems"
ln -s "$OUT" "$LAYOUT/Resources/engine"
cp -R "$HELPER_OUT" "$LAYOUT/Library/LoginItems/brindle.app"
helper="$LAYOUT/Library/LoginItems/brindle.app/Contents/MacOS/brindle"
got="$(cd "$WORK" && env -i HOME="$WORK" PATH=/usr/bin:/bin "$helper" -I -B -X "brindle-build=$(cat "$OUT/BUILD")" -m brindle --version 2>&1 || true)"
[ "$got" = "$want" ] || die "the engine's helper printed '$got', wanted '$want'"
# Anything but brindle is refused: what holds brindle's grants runs only brindle.
if env -i "$helper" -I -B -c 'print(1)' >/dev/null 2>&1; then
  die "the engine's helper ran Python code that isn't brindle"
fi
echo "$STAMP" > "$OUT/.stamp"
echo "build_engine: $OUT: $want (${COMMIT:0:10}), Python $PY_VERSION, tmux $TMUX_VERSION, $(du -sh "$OUT" | cut -f1)"
