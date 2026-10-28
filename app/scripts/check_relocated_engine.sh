#!/usr/bin/env bash
# check_relocated_engine.sh — prove a built engine still works after it moves.
#
# Copies <engine-dir> (app/src-tauri/engine, as build_engine.sh leaves it) to a
# fresh temp path, as installing the app does, and from there:
#   - fails if a script in bin/ or python/bin/ has a shebang naming an
#     interpreter outside the engine (a CI build path, say: PR #301's bug, where
#     python/bin/brindle made Codex's hook exit 126), other than the system's
#     own /bin and /usr/bin;
#   - runs every entry point hooks and agents use, with nothing from this
#     machine's environment, and fails if one exits non-zero:
#       bin/brindle --version
#       python/bin/brindle --version
#       python/bin/python3 -P -m brindle --version
#       the command codex_hook.hook_command() gives for that location
#       (its trailing "_hook <event>" replaced by --version, so it needs no
#       Codex or agent), which must also name the relocated engine.
#
# Usage: bash app/scripts/check_relocated_engine.sh <engine-dir>
set -euo pipefail

die() { echo "check_relocated_engine: $*" >&2; exit 1; }

[ $# = 1 ] && [ -d "$1" ] || die "usage: $0 <engine-dir>"
SRC="$(cd "$1" && pwd -P)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/relocated-engine.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
WORK="$(cd "$WORK" && pwd -P)"
DEST="$WORK/moved/engine"
mkdir -p "$WORK/moved" "$WORK/home"
cp -R "$SRC" "$DEST"

failed=0
fail() { echo "check_relocated_engine: $*" >&2; failed=1; }

# Shebangs: every script names an interpreter inside the engine or the system's.
for script in "$DEST"/bin/* "$DEST"/python/bin/*; do
  [ -f "$script" ] && [ ! -L "$script" ] || continue
  first="$(head -n 1 "$script" | LC_ALL=C tr -d '\r' | cut -c1-4096)"
  case "$first" in
    "#!"*) ;;
    *) continue ;;
  esac
  interp="${first#\#!}"
  interp="${interp# }"
  interp="${interp%% *}"
  case "$interp" in
    "$DEST"/*|/bin/*|/usr/bin/*) ;;
    *) fail "${script#"$DEST"/}: shebang '$first' names an interpreter outside the engine" ;;
  esac
done

# run LABEL CMD...: the command from outside the engine, with a bare environment.
run() {
  local label="$1"; shift
  local out
  if out="$(cd "$WORK" && env -i HOME="$WORK/home" PATH=/usr/bin:/bin "$@" 2>&1)"; then
    echo "check_relocated_engine: ok: $label: $(printf '%s' "$out" | tail -n 1)"
  else
    fail "$label exited non-zero: $out"
  fi
}

run "bin/brindle --version" "$DEST/bin/brindle" --version
run "python/bin/brindle --version" "$DEST/python/bin/brindle" --version
run "python3 -P -m brindle --version" "$DEST/python/bin/python3" -P -m brindle --version

# The hook command brindle would give Codex from this location.
hook="$(cd "$WORK" && env -i HOME="$WORK/home" PATH=/usr/bin:/bin "$DEST/python/bin/python3" -I -c \
  'from brindle import codex_hook; print(codex_hook.hook_command())' 2>&1)" \
  || { fail "hook_command() failed: $hook"; hook=""; }
if [ -n "$hook" ]; then
  suffix=" _hook codex-permission-request"
  case "$hook" in
    *"$suffix") ;;
    *) fail "unexpected hook command: $hook" ;;
  esac
  case "$hook" in
    *"$SRC"*) fail "hook command names the engine before it moved: $hook" ;;
    *"$DEST"*) ;;
    *) fail "hook command doesn't name the relocated engine: $hook" ;;
  esac
  version_cmd="${hook%"$suffix"} --version"
  if out="$(cd "$WORK" && env -i HOME="$WORK/home" PATH=/usr/bin:/bin /bin/sh -c "$version_cmd" 2>&1)"; then
    echo "check_relocated_engine: ok: codex hook command: $(printf '%s' "$out" | tail -n 1)"
  else
    fail "codex hook command exited non-zero ($version_cmd): $out"
  fi
fi

[ "$failed" = 0 ] || die "the engine doesn't survive being moved"
echo "check_relocated_engine: $SRC works from a new path"
