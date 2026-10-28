#!/usr/bin/env bash
# dev.sh — run this checkout's brindle (uncommitted edits included) under the
# desktop app, so staff can test local changes without the in-app buttons.
# Works from any brindle checkout or worktree. docs: app/README.md,
# "Testing local changes".
#
# Usage: bash app/scripts/dev.sh [--bundle] [--help]
#
#   (default)  stop the running daemon, start this checkout's engine
#              (.venv/bin/brindle daemon), then run the app in dev mode
#              (`npm --prefix app run tauri dev`) against it. On Ctrl-C the
#              local daemon keeps running; the message printed on exit says
#              how to go back.
#   --bundle   build the app with its engine (smoke_bundle.sh --engine), and
#              if that passes, open the built brindle.app from target/. It is
#              built from committed code only, and it is never copied into
#              /Applications.
#
# Both modes: the installed brindle app refuses a daemon started by hand
# (an engine conflict), so quit it first.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash app/scripts/dev.sh [--bundle] [--help]

Run this checkout's brindle (uncommitted edits included) under the desktop app.

Default mode:
  1. stops the running daemon (brindle daemon stop);
  2. starts this checkout's engine (.venv/bin/brindle daemon) in the background,
     logging to ~/.brindle/dev-daemon.log (or $BRINDLE_HOME);
  3. runs `npm --prefix app run tauri dev` with BRINDLE_BIN set to that engine.
     Ctrl-C stops the app and leaves the local daemon running. To go back:
     `brindle daemon stop`, then open the installed app.

--bundle:
  runs `bash app/scripts/smoke_bundle.sh --engine`, and if it passes, opens the
  built app from target/ (src-tauri/target/release/bundle/macos/brindle.app).
  Only committed code is built, and the app is not copied into /Applications.

Before either mode: quit the installed brindle app. It refuses a daemon that
was started by hand (an engine conflict).
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --bundle) MODE=bundle ;;
  "") MODE=dev ;;
  *) echo "dev.sh: unknown option '$1' (see --help)" >&2; exit 2 ;;
esac

APP="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$APP/.." && pwd)"
CARGO_OUT="${CARGO_TARGET_DIR:-$APP/src-tauri/target}"

echo "dev.sh: quit the installed brindle app first; it refuses a daemon started by hand (engine conflict)." >&2

if [ "$MODE" = bundle ]; then
  echo "dev.sh: --bundle builds only committed code, not your uncommitted edits." >&2
  bash "$APP/scripts/smoke_bundle.sh" --engine
  BUNDLE_APP="$CARGO_OUT/release/bundle/macos/brindle.app"
  if [ ! -d "$BUNDLE_APP" ]; then
    echo "dev.sh: no app at $BUNDLE_APP after the smoke check" >&2
    exit 1
  fi
  echo "dev.sh: opening $BUNDLE_APP"
  open "$BUNDLE_APP"
  exit 0
fi

BRINDLE="$ROOT/.venv/bin/brindle"
if [ ! -x "$BRINDLE" ]; then
  echo "dev.sh: no $BRINDLE. Run \`uv sync\` in $ROOT first." >&2
  exit 1
fi

LOG_DIR="${BRINDLE_HOME:-$HOME/.brindle}"
LOG="$LOG_DIR/dev-daemon.log"
mkdir -p "$LOG_DIR"

"$BRINDLE" daemon stop || true

# The daemon serves in the foreground, so it goes to the background; its
# SIGINT is ignored there, so Ctrl-C at this script leaves it running.
nohup "$BRINDLE" daemon >>"$LOG" 2>&1 &
for _ in $(seq 40); do
  if "$BRINDLE" daemon status >/dev/null 2>&1; then
    break
  fi
done
if ! "$BRINDLE" daemon status; then
  echo "dev.sh: the daemon did not come up; see $LOG" >&2
  exit 1
fi

finish() {
  echo
  echo "dev.sh: the local daemon is still running (this checkout's engine)."
  echo "  To go back: brindle daemon stop, then open the installed app."
}
trap finish EXIT

if [ ! -d "$APP/node_modules" ]; then
  npm --prefix "$APP" ci
fi

BRINDLE_BIN="$BRINDLE" npm --prefix "$APP" run tauri dev
