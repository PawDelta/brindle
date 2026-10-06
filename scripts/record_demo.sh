#!/usr/bin/env bash
# Re-record assets/brindle-demo.gif: `brindle demo` from this checkout, captured
# with asciinema, cut when both milestones are verified, rendered with agg in
# the Brindle palette.
#
#   scripts/record_demo.sh [OUT.gif]      (default: assets/brindle-demo.gif)
#
# Needs asciinema, agg and tmux on PATH, plus a working Claude Code (the demo's
# workers and reviewers run on it). The demo runs in a throwaway brindle home
# on a private tmux server, so your own sessions are never touched or shown;
# it starts a real supervisor and two workers: expect it to take a few minutes
# and to use your Claude subscription. Env knobs:
#
#   DEMO_COLS / DEMO_ROWS   terminal size recorded (default 150x45)
#   DEMO_TIMEOUT            hard stop, seconds (default 1200)
#   DEMO_SPEED              agg playback speed (default 4)
#   DEMO_FONT_SIZE          agg font size in px (default 13: 150 cols ~ 1190 px wide)
#   DEMO_SETTLE             seconds to keep recording once the goal is reached (default 8)
#   DEMO_TMUX_SOCKET        name of the private tmux server used (default brindle-demo-rec)
#   DEMO_KEEP_CAST=1        keep the .cast next to the GIF
#   DEMO_RENDER_ONLY=1      skip recording; re-render the kept .cast (to tune agg flags)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-"$ROOT/assets/brindle-demo.gif"}
COLS=${DEMO_COLS:-150}
ROWS=${DEMO_ROWS:-45}
TIMEOUT=${DEMO_TIMEOUT:-1200}
SPEED=${DEMO_SPEED:-4}
FONT_SIZE=${DEMO_FONT_SIZE:-13}
# Settle time after the goal is reached, so the dashboard repaints with both
# milestones green before the session is closed (recorded seconds).
SETTLE=${DEMO_SETTLE:-8}

for tool in asciinema agg tmux uv; do
    command -v "$tool" >/dev/null || { echo "record_demo: $tool is not installed" >&2; exit 1; }
done

# Brindle palette (see THEME in src/brindle/tmux.py and PALETTE_256 in
# src/brindle/watch.py): bg, fg, then ANSI 0-7 and 8-15.
THEME=17130f,ece4d9
THEME+=,17130f,c75c4a,87af87,d7af87,8a9bb0,afafff,8fb5a8,c4b8aa
THEME+=,8f8173,ff875f,a3c98a,e8c9a3,a3b4c8,c9bfff,a3c8bc,ece4d9

CAST="${OUT%.gif}.cast"
mkdir -p "$(dirname "$OUT")"
if [[ ${DEMO_RENDER_ONLY:-0} == 1 ]]; then
    [[ -s $CAST ]] || { echo "record_demo: DEMO_RENDER_ONLY needs $CAST" >&2; exit 1; }
else
rm -f "$CAST"

# A throwaway brindle home for the demo, so nothing it does touches your real
# ~/.brindle: not its database (a demo running on a private tmux server would
# otherwise take your own sessions' agents, which it can't see, for dead and
# stop them), not its locks, not its worktrees. Only what signing in and the
# global config need is copied across; the whole directory goes at the end.
REAL_HOME=${BRINDLE_HOME:-$HOME/.brindle}
export BRINDLE_HOME
BRINDLE_HOME=$(mktemp -d -t brindle-demo-rec)
for item in config.json agents legacy-warned.json; do
    [[ -e "$REAL_HOME/$item" ]] && cp -R "$REAL_HOME/$item" "$BRINDLE_HOME/$item"
done
if [[ -d "$REAL_HOME/pro" ]]; then
    mkdir -p "$BRINDLE_HOME/pro"
    find "$REAL_HOME/pro" -maxdepth 1 -type f ! -name '*.lock' -exec cp {} "$BRINDLE_HOME/pro/" \;
fi
DEMO_HOME="$BRINDLE_HOME/demo"
mkdir -p "$DEMO_HOME"

# A private tmux server for the demo (brindle honours BRINDLE_TMUX_SOCKET, as
# its test suite does), so the recording never touches your own tmux sessions
# and nothing of yours shows up in it. Its windows are born at the recorded
# size: with the default server, a new window takes the size of your latest
# client, and the sidebar dies when the attach then shrinks it mid-draw.
export BRINDLE_TMUX_SOCKET=${DEMO_TMUX_SOCKET:-brindle-demo-rec}
TMUX_CMD=(tmux -L "$BRINDLE_TMUX_SOCKET")
"${TMUX_CMD[@]}" kill-server 2>/dev/null || true
# A placeholder session keeps the server (and so the option) alive until the
# demo's own sessions exist: a tmux server with no sessions exits.
"${TMUX_CMD[@]}" new-session -d -s holder -x "$COLS" -y "$ROWS" "sleep $((TIMEOUT + 120))"
"${TMUX_CMD[@]}" set-option -g default-size "${COLS}x${ROWS}"

# The demo repo is textkit-<stamp>, new every run: whatever appears under
# ~/.brindle/demo after we start is ours.
before=$(ls -1 "$DEMO_HOME" 2>/dev/null || true)
new_demo_dir() {
    comm -13 <(printf '%s\n' "$before" | sort) <(ls -1 "$DEMO_HOME" | sort) | head -n 1
}

cleanup() {
    [[ -n ${REC_PID:-} ]] && kill "$REC_PID" 2>/dev/null || true
    # The whole private server: the demo's workers and reviewers run in
    # their own sessions on it, and nothing else does.
    "${TMUX_CMD[@]}" kill-server 2>/dev/null || true
    rm -rf "$BRINDLE_HOME"
}
trap cleanup EXIT INT TERM

echo "record_demo: recording ${COLS}x${ROWS} to $CAST"
# `brindle demo` creates the repo, starts the supervisor with the watch
# sidebar and attaches tmux; the recording ends when that attach does.
# Headless: asciinema drives its own pty, so this works from any terminal
# (or none). TERM/COLORTERM so tmux paints the theme in full colour. TMUX
# unset: inside a tmux pane brindle would switch the current client to the
# demo instead of attaching, and the recording would end at once.
env -u TMUX -u TMUX_PANE TERM=xterm-256color COLORTERM=truecolor \
    asciinema rec --headless --quiet --overwrite \
    --window-size "${COLS}x${ROWS}" \
    --title "brindle demo" \
    --command "uv run --project '$ROOT' brindle demo" \
    "$CAST" &
REC_PID=$!

# Find the demo repo and its tmux session.
DEMO=""
for _ in $(seq 1 120); do
    name=$(new_demo_dir)
    if [[ -n $name ]]; then DEMO="$DEMO_HOME/$name"; break; fi
    kill -0 "$REC_PID" 2>/dev/null || { echo "record_demo: asciinema exited before the demo started" >&2; exit 1; }
    sleep 1
done
[[ -n $DEMO ]] || { echo "record_demo: no demo repo appeared under $DEMO_HOME" >&2; exit 1; }
SESSION="brindle_${name//./_}_root"
echo "record_demo: demo repo $DEMO (tmux session $SESSION)"

progress() {
    (cd "$DEMO" && uv run --project "$ROOT" brindle autopilot 2>/dev/null) || true
}

# Poll the goal until every milestone is verified, or the hard timeout hits.
start=$(date +%s)
done_at=""
while kill -0 "$REC_PID" 2>/dev/null; do
    now=$(date +%s)
    if (( now - start > TIMEOUT )); then
        echo "record_demo: timeout after ${TIMEOUT}s; closing the demo session" >&2
        break
    fi
    p=$(progress)
    if [[ -z $done_at ]]; then
        total=$(printf '%s\n' "$p" | sed -n 's/^\([0-9]*\) of \([0-9]*\) milestones verified\./\1 \2/p')
        if [[ -n $total ]] && [[ ${total% *} == "${total#* }" ]]; then
            echo "record_demo: goal reached ($total); settling ${SETTLE}s"
            done_at=$now
        fi
    elif (( now - done_at >= SETTLE )); then
        break
    fi
    sleep 3
done

# Killing the session ends the attach, so `brindle demo` and the recording end.
if kill -0 "$REC_PID" 2>/dev/null; then
    "${TMUX_CMD[@]}" kill-session -t "=$SESSION" 2>/dev/null || true
    for _ in $(seq 1 30); do
        kill -0 "$REC_PID" 2>/dev/null || break
        sleep 1
    done
    kill "$REC_PID" 2>/dev/null || true
fi
wait "$REC_PID" 2>/dev/null || true
REC_PID=""
[[ -s $CAST ]] || { echo "record_demo: $CAST is empty" >&2; exit 1; }
fi  # DEMO_RENDER_ONLY

# End the GIF on the finished dashboard, not on the shell text `brindle demo`
# prints after the session closes: drop everything after tmux leaves the
# alternate screen (its client detaching), and the checkout's path from the
# header, which agg never draws but which has no place in a published file.
python3 - "$CAST" <<'EOF'
import json, sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines()
header = json.loads(lines[0])
header.pop("command", None)
events = [json.loads(l) for l in lines[1:] if l.strip()]
last = max((i for i, e in enumerate(events) if e[1] == "o" and "\x1b[?1049l" in e[2]), default=None)
if last is not None:
    events = events[:last]
with open(path, "w", encoding="utf-8") as f:
    f.write(json.dumps(header, ensure_ascii=False) + "\n")
    for e in events:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
EOF

echo "record_demo: rendering $OUT"
agg --quiet \
    --theme "$THEME" \
    --font-size "$FONT_SIZE" \
    --speed "$SPEED" \
    --idle-time-limit 2 \
    --fps-cap 15 \
    --last-frame-duration 4 \
    "$CAST" "$OUT"

size=$(wc -c <"$OUT" | tr -d ' ')
echo "record_demo: wrote $OUT ($((size / 1024)) KB)"
if [[ ${DEMO_KEEP_CAST:-0} != 1 ]]; then rm -f "$CAST"; fi
