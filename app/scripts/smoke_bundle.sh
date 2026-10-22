#!/usr/bin/env bash
# smoke_bundle.sh — build the desktop app's bundle without a Developer ID (on
# macOS signed ad hoc, see tauri.macos.conf.json) for this machine's
# architecture and check it. Exits 0 only if every check passes.
#
# macOS (the default): the .app and the .dmg exist, the bundle is named brindle
# at package.json's version, each .app (built, and in the mounted .dmg) passes
# `codesign --verify --deep --strict` (one that doesn't, macOS calls
# "damaged"), and its binary starts headless and prints that version.
#
# --linux: the AppImage and the .deb exist, the .deb's control file names the
# package brindle at package.json's version, and the AppImage and the .deb's
# binary each print that version for --version under xvfb-run. On Linux it
# builds here (Tauri's Linux build dependencies, Node and Rust installed); on
# a Mac it runs itself inside an Ubuntu 22.04 container (linux-build.Dockerfile
# beside this script; it starts Docker Desktop if the daemon isn't up), with
# cargo, npm, node_modules and target/ cached in docker volumes, and copies the
# two bundles out to src-tauri/target/linux-bundle/.
#
# Usage: bash app/scripts/smoke_bundle.sh [--skip-build] [--engine] [--linux]
#
#   --skip-build  check the bundles the last build left in target/ instead
#   --engine      (macOS only) also check the engine the .app carries
#                 (Contents/Resources/engine, docs/bundled-engine.md): its
#                 brindle, run from inside the .app under `env -i` (nothing
#                 from this machine's PATH or environment), starts
#                 `brindle daemon` and answers GET /v1/sessions with 200 over
#                 its Unix socket; and the app itself, under `env -i`, starts
#                 that engine's daemon (a release never takes one from PATH)
#                 and gets GET /v1/meta answered. Each runs in a fresh
#                 HOME and BRINDLE_HOME, with brindle Pro's credential store
#                 forced to a file there: it never touches this machine's
#                 daemon, tmux server or Keychain. Works from inside a brindle
#                 agent too (that home has no agents).
#   --linux       the AppImage / .deb build and checks instead of macOS's
set -euo pipefail

DESKTOP="$(cd "$(dirname "$0")/.." && pwd)"
BUILD=1
ENGINE=0
LINUX=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build) BUILD=0 ;;
    --engine) ENGINE=1 ;;
    --linux) LINUX=1 ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$LINUX" = 1 ] && [ "$ENGINE" = 1 ]; then
  echo "smoke_bundle.sh: --engine runs on macOS only, not with --linux" >&2; exit 2
fi

# No Developer ID (the .app is signed ad hoc) or updater key from this shell's
# environment, no updater artifacts (they need the release's minisign key).
unset APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD APPLE_SIGNING_IDENTITY \
      APPLE_API_KEY APPLE_API_ISSUER APPLE_API_KEY_PATH APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID \
      TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PATH TAURI_SIGNING_PRIVATE_KEY_PASSWORD
NO_UPDATER='{"bundle":{"createUpdaterArtifacts":false}}'

VERSION="$(node -p "require(process.argv[1]).version" "$DESKTOP/package.json")"
BUNDLE="${CARGO_TARGET_DIR:-$DESKTOP/src-tauri/target}/release/bundle"

FAILED=0
pass() { echo "ok    $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
finish() {
  if [ "$FAILED" = 0 ]; then
    echo "smoke_bundle: brindle $VERSION $1 OK"
  else
    echo "smoke_bundle: FAILED" >&2
  fi
  exit "$FAILED"
}

# Run a command, killed after 20 s if it never returns.
within_20s() {
  perl -e 'alarm 20; exec @ARGV or die "exec: $!"' "$@"
}

# check_version LABEL OUTPUT: the binary printed "brindle <version>".
check_version() {
  [ "$2" = "brindle $VERSION" ] && pass "$1 --version prints '$2'" \
    || fail "$1 --version printed '$2', wanted 'brindle $VERSION'"
}

# ---- Linux, from a Mac: the same script, --linux, in a container ----------

linux_in_docker() {
  command -v docker >/dev/null || { echo "smoke_bundle.sh --linux on a Mac needs Docker" >&2; exit 2; }
  if ! docker info >/dev/null 2>&1; then
    echo "starting Docker Desktop (waiting up to 3 minutes for its daemon)"
    open -a Docker
    local waited=0
    until docker info >/dev/null 2>&1; do
      [ "$waited" -lt 180 ] || { echo "the Docker daemon isn't up after 3 minutes" >&2; exit 2; }
      sleep 2; waited=$((waited + 2))
    done
  fi
  local image=brindle-desktop-linux-build repo out status=0
  docker build -q -t "$image" - < "$DESKTOP/scripts/linux-build.Dockerfile" >/dev/null
  repo="$(cd "$DESKTOP/.." && pwd)"
  out="src-tauri/target/linux-bundle"
  local args=(--linux)
  [ "$BUILD" = 1 ] || args+=(--skip-build)
  # node_modules is a volume over the mounted checkout's own (macOS) one;
  # target/ lives in a volume (CARGO_TARGET_DIR), the bundles are copied out.
  docker run --rm --init \
    -v "$repo:/src" -w /src \
    -v brindle-desktop-cargo-registry:/usr/local/cargo/registry \
    -v brindle-desktop-cargo-git:/usr/local/cargo/git \
    -v brindle-desktop-npm:/root/.npm \
    -v brindle-desktop-tauri-cache:/root/.cache/tauri \
    -v brindle-desktop-node-modules:/src/app/node_modules \
    -v brindle-desktop-target:/cache/target \
    -e CARGO_TARGET_DIR=/cache/target \
    "$image" bash -c '
      bash app/scripts/smoke_bundle.sh "$@"; status=$?
      rm -rf "app/'"$out"'"; mkdir -p "app/'"$out"'"
      cp /cache/target/release/bundle/appimage/*.AppImage /cache/target/release/bundle/deb/*.deb \
        "app/'"$out"'/" 2>/dev/null || true
      exit $status' smoke "${args[@]}" || status=$?
  [ "$status" != 0 ] || echo "bundles copied to $DESKTOP/$out/"
  exit "$status"
}

# ---- Linux: build and check the AppImage and the .deb ---------------------

linux_npm_install() {
  # node_modules may be a cached volume: reinstall when the lockfile changed.
  local stamp="$DESKTOP/node_modules/.smoke-lock" want
  want="$(sha256sum "$DESKTOP/package-lock.json" | cut -d' ' -f1)"
  if [ ! -x "$DESKTOP/node_modules/.bin/tauri" ] || [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
    bash "$DESKTOP/scripts/retry.sh" npm --prefix "$DESKTOP" ci --no-audit --no-fund
    echo "$want" > "$stamp"
  fi
}

smoke_linux() {
  if [ "$BUILD" = 1 ]; then
    linux_npm_install
    rm -rf "$BUNDLE/appimage" "$BUNDLE/deb"
    # linuxdeploy (an AppImage itself) runs without FUSE, as in a container.
    # Tauri downloads linuxdeploy and its plugins on a first build: tried twice.
    APPIMAGE_EXTRACT_AND_RUN=1 bash "$DESKTOP/scripts/retry.sh" -n 2 npm --prefix "$DESKTOP" run tauri -- build --ci \
      --bundles appimage,deb --config "$NO_UPDATER"
  fi

  local deb appimage name version exe scratch
  deb="$(ls "$BUNDLE"/deb/brindle_"$VERSION"_*.deb 2>/dev/null | head -1 || true)"
  if [ -z "$deb" ]; then
    fail ".deb: none for $VERSION in $BUNDLE/deb"
  else
    pass ".deb: $deb"
    name="$(dpkg-deb -f "$deb" Package 2>/dev/null || true)"
    [ "$name" = "brindle" ] && pass ".deb: Package is brindle" \
      || fail ".deb: Package is '$name', not brindle"
    version="$(dpkg-deb -f "$deb" Version 2>/dev/null || true)"
    [ "$version" = "$VERSION" ] && pass ".deb: Version $version matches package.json" \
      || fail ".deb: Version '$version', package.json says $VERSION"
    scratch="$(mktemp -d)"
    dpkg-deb -x "$deb" "$scratch"
    exe="$(sed -n 's/^Exec=\([^ ]*\).*/\1/p' "$scratch"/usr/share/applications/*.desktop 2>/dev/null | head -1)"
    if [ -n "$exe" ] && [ -x "$scratch/usr/bin/$exe" ]; then
      check_version ".deb: /usr/bin/$exe" "$(within_20s xvfb-run -a "$scratch/usr/bin/$exe" --version 2>&1 || true)"
    else
      fail ".deb: no /usr/bin binary named by its .desktop file (Exec='$exe')"
    fi
    rm -rf "$scratch"
  fi

  appimage="$(ls "$BUNDLE"/appimage/brindle_"$VERSION"_*.AppImage 2>/dev/null | head -1 || true)"
  if [ -z "$appimage" ]; then
    fail ".AppImage: none for $VERSION in $BUNDLE/appimage"
  elif [ ! -x "$appimage" ]; then
    fail ".AppImage: $appimage is not executable"
  else
    pass ".AppImage: $appimage"
    # Through the AppImage's own AppRun, extracted rather than FUSE-mounted.
    check_version ".AppImage:" \
      "$(APPIMAGE_EXTRACT_AND_RUN=1 within_20s xvfb-run -a "$appimage" --version 2>&1 || true)"
  fi
  finish "Linux bundles"
}

if [ "$LINUX" = 1 ]; then
  case "$(uname -s)" in
    Linux) smoke_linux ;;
    Darwin) linux_in_docker ;;
    *) echo "smoke_bundle.sh --linux runs on Linux, or on a Mac with Docker" >&2; exit 2 ;;
  esac
fi

# ---- macOS: build and check the .app and the .dmg -------------------------

[ "$(uname -s)" = "Darwin" ] || { echo "smoke_bundle.sh builds the macOS bundle; run it on a Mac (or pass --linux)" >&2; exit 2; }

# Tauri's bundle_dmg.sh builds the DMG through an interstitial rw.*.dmg it
# attaches; on some machines detaching it intermittently fails ("couldn't
# unmount diskN - Resource busy"), leaving it attached. Detach any such image
# under the target dir, before building, before a retry and on exit. Returns 1
# if one stays attached; its backing file is then kept, as are all rw files.
detach_rw_dmgs() {
  local target dev stuck=0
  target="$(dirname "$(dirname "$BUNDLE")")"
  for dev in $(hdiutil info 2>/dev/null | awk -v t="$target/" '
      /^=+$/ { mine = 0 }
      /^image-path/ { p = $0; sub(/^image-path *: */, "", p)
                      n = split(p, a, "/")
                      mine = index(p, t) == 1 && a[n] ~ /^rw\..*\.dmg$/ }
      mine && /^\/dev\/disk[0-9]+[ \t]/ { print $1; mine = 0 }'); do
    echo "detaching leftover rw DMG image $dev"
    if ! hdiutil detach -quiet "$dev" 2>/dev/null && ! hdiutil detach -quiet -force "$dev"; then
      echo "smoke_bundle: could not detach $dev (an rw DMG under $target)" >&2
      stuck=1
    fi
  done
  [ "$stuck" = 0 ] || return 1
  rm -f "$BUNDLE"/dmg/rw.*.dmg "$BUNDLE"/macos/rw.*.dmg
}

SHIM_DIR=""
ENGINE_DIR=""
# On exit: stop the --engine checks' daemons, detach leftovers (an image
# still attached fails an otherwise successful run) and remove the retry's
# hdiutil shim.
cleanup() {
  local status=$?
  ! declare -F stop_engines >/dev/null || stop_engines
  detach_rw_dmgs || [ "$status" != 0 ] || status=1
  [ -z "$SHIM_DIR" ] || rm -rf "$SHIM_DIR"
  exit "$status"
}
trap cleanup EXIT

# bundle_dmg.sh only waits out an EBUSY detach (exit 16); the busy volume
# (something still reading the freshly copied .app) fails it with another
# code. For the retry, an hdiutil on PATH (in SHIM_DIR) that retries `detach`
# with a growing wait and forces it last.
patient_hdiutil() {
  local dir
  dir="$(mktemp -d)"
  SHIM_DIR="$dir"
  cat > "$dir/hdiutil" <<'SHIM'
#!/bin/bash
if [ "$1" = detach ]; then
  shift
  for wait in 1 2 4 8; do
    /usr/bin/hdiutil detach "$@" && exit 0
    echo "hdiutil detach failed; retrying in ${wait}s" >&2
    sleep "$wait"
  done
  exec /usr/bin/hdiutil detach -force "$@"
fi
exec /usr/bin/hdiutil "$@"
SHIM
  chmod +x "$dir/hdiutil"
}

if [ "$BUILD" = 1 ]; then
  [ -x "$DESKTOP/node_modules/.bin/tauri" ] || npm --prefix "$DESKTOP" ci --no-audit --no-fund
  detach_rw_dmgs || exit 1
  # --ci skips the Finder styling of the DMG, which needs a GUI session.
  rm -rf "$BUNDLE/macos" "$BUNDLE/dmg"
  if ! npm --prefix "$DESKTOP" run tauri -- build --ci --bundles app,dmg --config "$NO_UPDATER"; then
    # Failed at the DMG stage (the .app is there, no .dmg): retry the bundling once.
    if [ -d "$BUNDLE/macos/brindle.app" ] && ! ls "$BUNDLE"/dmg/brindle_*.dmg >/dev/null 2>&1; then
      echo "smoke_bundle: the DMG step failed; detaching leftovers and retrying it once"
      detach_rw_dmgs || exit 1
      sleep 5
      patient_hdiutil
      # app,dmg rather than dmg: asked for the DMG alone, tauri deletes the .app
      # it made it from. The binary is already built, so this only re-bundles.
      PATH="$SHIM_DIR:$PATH" \
        npm --prefix "$DESKTOP" run tauri -- build --ci --bundles app,dmg --config "$NO_UPDATER"
    else
      exit 1
    fi
  fi
fi

# check_app LABEL PATH: the bundle's signature, name, version, and its
# binary's --version.
check_app() {
  local label="$1" app="$2" plist="$2/Contents/Info.plist" name version exe out
  if [ ! -d "$app" ]; then fail "$label: no $app"; return; fi
  pass "$label: $app"
  # The whole bundle signed and sealed (unsigned, it has only the linker's
  # ad-hoc signature on its binary, and no _CodeSignature).
  if out="$(codesign --verify --deep --strict "$app" 2>&1)"; then
    pass "$label: codesign --verify --deep --strict"
  else
    fail "$label: codesign --verify --deep --strict: $out"
  fi
  name="$(plutil -extract CFBundleName raw -o - "$plist" 2>/dev/null || true)"
  [ "$name" = "brindle" ] && pass "$label: CFBundleName is brindle" \
    || fail "$label: CFBundleName is '$name', not brindle"
  version="$(plutil -extract CFBundleShortVersionString raw -o - "$plist" 2>/dev/null || true)"
  [ "$version" = "$VERSION" ] && pass "$label: version $version matches package.json" \
    || fail "$label: version '$version', package.json says $VERSION"
  exe="$(plutil -extract CFBundleExecutable raw -o - "$plist" 2>/dev/null || true)"
  check_version "$label: $exe" "$(within_20s "$app/Contents/MacOS/$exe" --version 2>&1 || true)"
}

# ---- macOS --engine: the engine inside the .app ---------------------------

ENGINE_HOMES=()
# A fresh home for one engine check, in $ENGINE_HOME (not printed: in a
# $(...) subshell the exit trap would never learn of it): HOME, BRINDLE_HOME
# and tmux's sockets in it. mktemp's, not target/'s: the daemon's socket
# path must fit in 104 bytes.
engine_home() {
  ENGINE_HOME="$(mktemp -d)"
  ENGINE_HOMES+=("$ENGINE_HOME")
}

# `env -i` with only what keeps a check apart from this machine's brindle.
isolated() {
  local home="$1"
  shift
  env -i HOME="$home" BRINDLE_HOME="$home/brindle" TMUX_TMPDIR="$home" \
    BRINDLE_PRO_CREDENTIAL_STORE=file "$@"
}

# On exit: stop every daemon (and tmux server) a check started, remove its home.
stop_engines() {
  local home pid sock
  for home in ${ENGINE_HOMES[@]+"${ENGINE_HOMES[@]}"}; do
    pid="$(plutil -extract pid raw -o - "$home/brindle/daemon.json" 2>/dev/null || true)"
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
      kill -9 "$pid" 2>/dev/null || true
    fi
    for sock in "$home"/tmux-*/*; do
      if [ -S "$sock" ] && [ -n "$ENGINE_DIR" ]; then
        env -i "$ENGINE_DIR/bin/tmux" -S "$sock" kill-server 2>/dev/null || true
      fi
    done
    rm -rf "$home"
  done
}

# wait_for_daemon HOME PID: daemon.json written and its socket listening, up
# to 60 s (the first start compiles nothing, but imports a lot), while PID runs.
wait_for_daemon() {
  local home="$1" pid="$2" sock
  for _ in $(seq 1 120); do
    sock="$(plutil -extract socket raw -o - "$home/brindle/daemon.json" 2>/dev/null || true)"
    if [ -n "$sock" ] && [ -S "$sock" ]; then
      echo "$sock"
      return 0
    fi
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then return 1; fi
    sleep 0.5
  done
  return 1
}

# The oldest macOS the app says it runs on (LSMinimumSystemVersion) is no
# older than the newest minimum any Mach-O file in it was built for: macOS
# mustn't open an app whose engine can't run.
check_minimum_macos() {
  local app="$1" declared needed
  declared="$(plutil -extract LSMinimumSystemVersion raw -o - "$app/Contents/Info.plist" 2>/dev/null || true)"
  # minos (LC_BUILD_VERSION) or version (LC_VERSION_MIN_MACOSX), newest first.
  needed="$(find "$app/Contents" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) \
      -exec sh -c 'file -b "$1" | grep -q Mach-O && otool -l "$1"' _ {} \; 2>/dev/null \
    | awk '/cmd LC_BUILD_VERSION|cmd LC_VERSION_MIN_MACOSX/ { want = 1; next }
           want && ($1 == "minos" || $1 == "version") { print $2; want = 0 }' \
    | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
  if [ -z "$declared" ] || [ -z "$needed" ]; then
    fail "minimum macOS: declared '$declared', binaries need '$needed'"
  elif [ "$(printf '%s\n%s\n' "$declared" "$needed" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)" = "$declared" ]; then
    pass "minimum macOS: the app declares $declared, its binaries need $needed"
  else
    fail "minimum macOS: the app declares $declared (LSMinimumSystemVersion), its binaries need $needed"
  fi
}

# The daemon's process is the engine's own Python, naming the engine's build
# (BUILD) on its command line, which is how the app tells it's this build's.
check_engine_process() {
  local label="$1" home="$2" pid cmd build
  pid="$(plutil -extract pid raw -o - "$home/brindle/daemon.json" 2>/dev/null || true)"
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  build="$(cat "$ENGINE_DIR/BUILD" 2>/dev/null || true)"
  case "$cmd" in
    "$ENGINE_DIR/python/bin/python3 -I -B -X brindle-build=$build -m brindle daemon"*)
      [ -n "$build" ] && pass "$label: the daemon is the engine's, build $build: $cmd" \
        || fail "$label: the engine has no BUILD" ;;
    *) fail "$label: the daemon (pid '$pid') runs '$cmd', not $ENGINE_DIR/python/bin/python3 of build '$build'" ;;
  esac
}

check_engine() {
  local app="$1" exe home sock status out tmux pid
  exe="$(plutil -extract CFBundleExecutable raw -o - "$app/Contents/Info.plist" 2>/dev/null || true)"
  ENGINE_DIR="$(cd "$app/Contents/Resources/engine" 2>/dev/null && pwd -P || true)"
  if [ -z "$ENGINE_DIR" ]; then
    fail "engine: no Contents/Resources/engine in $app"
    return
  fi
  pass "engine: $ENGINE_DIR ($(du -sh "$ENGINE_DIR" | cut -f1))"
  for f in bin/brindle bin/tmux python/bin/python3; do
    [ -x "$ENGINE_DIR/$f" ] && pass "engine: $f" || fail "engine: no executable $f"
  done
  tmux="$(env -i "$ENGINE_DIR/bin/tmux" -V 2>&1 || true)"
  case "$tmux" in
    "tmux "*) pass "engine: bin/tmux runs under env -i: $tmux" ;;
    *) fail "engine: bin/tmux -V under env -i printed '$tmux'" ;;
  esac
  out="$(otool -L "$ENGINE_DIR/bin/tmux" | tail -n +2 | awk '{print $1}' | grep -v -E '^(/usr/lib/|/System/Library/)' || true)"
  [ -z "$out" ] && pass "engine: bin/tmux links only macOS's libraries" \
    || fail "engine: bin/tmux links $out"
  check_minimum_macos "$app"

  # Its brindle, from inside the .app, with nothing from this environment.
  engine_home
  home="$ENGINE_HOME"
  isolated "$home" "$ENGINE_DIR/bin/brindle" daemon --idle-exit >"$home/daemon.log" 2>&1 &
  pid=$!
  if ! sock="$(wait_for_daemon "$home" "$pid")"; then
    fail "engine: \`brindle daemon\` under env -i didn't start listening: $(tail -c 600 "$home/daemon.log")"
  else
    pass "engine: \`brindle daemon\` under env -i listens on $sock"
    # The token goes in a header file, not on curl's command line.
    printf 'Authorization: Bearer %s\n' "$(plutil -extract token raw -o - "$home/brindle/daemon.json")" \
      > "$home/auth-header"
    chmod 600 "$home/auth-header"
    # Sessions are per repo: a fresh one in that home, which has none yet.
    git init -q "$home/repo"
    status="$(env -i /usr/bin/curl -sS --max-time 20 --unix-socket "$sock" -H @"$home/auth-header" \
      -o "$home/sessions.json" -w '%{http_code}' --get --data-urlencode "repo=$home/repo" \
      http://localhost/v1/sessions 2>&1 || true)"
    if [ "$status" = 200 ] && [ "$(head -c 1 "$home/sessions.json")" = "[" ]; then
      pass "engine: GET /v1/sessions over the socket: 200 $(head -c 120 "$home/sessions.json")"
    else
      fail "engine: GET /v1/sessions over the socket: '$status' $(head -c 300 "$home/sessions.json" 2>/dev/null)"
    fi
    check_engine_process "engine" "$home"
  fi

  # The app, under env -i too: a release starts its own engine's daemon.
  engine_home
  home="$ENGINE_HOME"
  if out="$(isolated "$home" /usr/bin/perl -e 'alarm 60; exec @ARGV or die "exec: $!"' \
      "$app/Contents/MacOS/$exe" --headless-check 2>&1)"; then
    pass "app: --headless-check under env -i reached the daemon: $out"
    check_engine_process "app" "$home"
  else
    fail "app: --headless-check under env -i: $out"
  fi

  # Nothing outlives the checks (the exit trap stops them too, on failure).
  stop_engines
  ENGINE_HOMES=()
  out="$(pgrep -f "$ENGINE_DIR/" || true)"
  [ -z "$out" ] && pass "engine: the checks' daemons are stopped" \
    || fail "engine: processes still run from the engine: $out"
}

check_app ".app" "$BUNDLE/macos/brindle.app"
if [ "$ENGINE" = 1 ]; then
  check_engine "$BUNDLE/macos/brindle.app"
fi

DMG="$(ls "$BUNDLE"/dmg/brindle_"$VERSION"_*.dmg 2>/dev/null | head -1 || true)"
if [ -z "$DMG" ]; then
  fail ".dmg: none for $VERSION in $BUNDLE/dmg"
else
  pass ".dmg: $DMG"
  MOUNT="$(mktemp -d)"
  if hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MOUNT" "$DMG" >/dev/null 2>&1; then
    check_app ".dmg" "$MOUNT/brindle.app"
    hdiutil detach -quiet "$MOUNT" || hdiutil detach -quiet -force "$MOUNT" || true
  else
    fail ".dmg: hdiutil could not mount it"
  fi
  rmdir "$MOUNT" 2>/dev/null || true
fi

finish bundle
