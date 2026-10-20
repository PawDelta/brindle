#!/usr/bin/env bash
# Re-record assets/brindle-demo.gif: `brindle demo` from this checkout, captured
# with asciinema, cut when both milestones are verified, rendered in the
# Brindle palette by vhs (scripts/record_demo.tape, xterm.js in headless
# Chrome) or, with DEMO_RENDERER=agg, by agg.
#
#   scripts/record_demo.sh [OUT.gif]      (default: assets/brindle-demo.gif)
#
# Needs asciinema, tmux and vhs (or agg) on PATH, plus a working Claude Code (the demo's
# workers and reviewers run on it). The demo runs in a throwaway brindle home
# on a private tmux server, so your own sessions are never touched or shown;
# it starts a real supervisor and two workers: expect it to take a few minutes
# and to use your Claude subscription. Env knobs:
#
#   DEMO_COLS / DEMO_ROWS   terminal size recorded (default 150x45)
#   DEMO_TIMEOUT            hard stop, seconds (default 1200)
#   DEMO_PROJECT            brindle checkout to run the demo from (default: this one)
#   DEMO_SECONDS            target GIF length in seconds (default 20); sets the speed
#   DEMO_RENDERER           vhs (default) or agg; agg garbles Claude Code's redraws
#   DEMO_SPEED              playback speed (default: whatever gives DEMO_SECONDS)
#   DEMO_FONTS              agg font list (default: a monospace font, then Apple Symbols,
#                           so Claude Code's ⏺ draws as a dot, not a colour emoji)
#   DEMO_FONT_SIZE          agg font size in px (default 13: 150 cols ~ 1190 px wide)
#   DEMO_SETTLE             seconds to keep recording once the goal is reached (default 8)
#   DEMO_TMUX_SOCKET        name of the private tmux server used (default brindle-demo-rec)
#   DEMO_KEEP_CAST=1        keep the .cast next to the GIF
#   DEMO_KEEP_HOME=1        keep the throwaway brindle home (its database and logs)
#   DEMO_RENDER_ONLY=1      skip recording; re-render the kept .cast (to tune agg flags)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-"$ROOT/assets/brindle-demo.gif"}
PROJECT=${DEMO_PROJECT:-$ROOT}
COLS=${DEMO_COLS:-150}
ROWS=${DEMO_ROWS:-45}
TIMEOUT=${DEMO_TIMEOUT:-1200}
SPEED=${DEMO_SPEED:-}
SECONDS_TARGET=${DEMO_SECONDS:-20}
FONT_SIZE=${DEMO_FONT_SIZE:-13}
FONTS=${DEMO_FONTS:-"JetBrains Mono,SF Mono,Menlo,DejaVu Sans Mono,Apple Symbols"}
# Settle time after the goal is reached, so the dashboard repaints with both
# milestones green before the session is closed (recorded seconds).
SETTLE=${DEMO_SETTLE:-8}

RENDERER=${DEMO_RENDERER:-vhs}
for tool in asciinema "$RENDERER" tmux uv; do
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

# A brand-new brindle home for the demo, so the recording shows what a new
# user sees and nothing it does touches your real ~/.brindle: not its database
# (a demo running on a private tmux server would otherwise take your own
# sessions' agents, which it can't see, for dead and stop them), not its
# locks, not its worktrees. Nothing is copied across: no config, no profiles,
# no brindle Pro sign-in (so no org or budget lines). quota.json is made a
# directory so brindle can't record plan usage: no "Claude at N% of its weekly
# limit" lines and no usage figures in the Costs section. The whole directory
# goes at the end.
export BRINDLE_HOME
BRINDLE_HOME=$(mktemp -d -t brindle-demo-rec)
mkdir -p "$BRINDLE_HOME/quota.json"
# Not signed in: Pro credentials are read from this home only, never from
# your keychain.
export BRINDLE_PRO_CREDENTIAL_STORE=file
# The one setting the demo needs: hand each milestone to its own worker at
# once (with the default "balanced", the supervisor may do both itself).
printf '{"delegation": "fast"}\n' >"$BRINDLE_HOME/config.json"

# Your own global git setup, plus a global excludes file for tool output that
# global agent hooks may leave in the demo repo (graphify-out/ and the like),
# which would otherwise block the first merge as "uncommitted changes".
export GIT_CONFIG_GLOBAL="$BRINDLE_HOME/gitconfig"
printf 'graphify-out/\n.graphify*/\n' >"$BRINDLE_HOME/gitignore"
{
    for f in "$HOME/.gitconfig" "${XDG_CONFIG_HOME:-$HOME/.config}/git/config"; do
        [[ -f $f ]] && printf '[include]\n\tpath = %s\n' "$f"
    done
    printf '[core]\n\texcludesFile = %s\n' "$BRINDLE_HOME/gitignore"
} >"$GIT_CONFIG_GLOBAL"
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
# $BRINDLE_HOME/demo (a throwaway home) after we start is ours.
before=$(ls -1 "$DEMO_HOME" 2>/dev/null || true)
new_demo_dir() {
    comm -13 <(printf '%s\n' "$before" | sort) <(ls -1 "$DEMO_HOME" | sort) | head -n 1
}

cleanup() {
    [[ -n ${REC_PID:-} ]] && kill "$REC_PID" 2>/dev/null || true
    # The whole private server: the demo's workers and reviewers run in
    # their own sessions on it, and nothing else does.
    "${TMUX_CMD[@]}" kill-server 2>/dev/null || true
    if [[ ${DEMO_KEEP_HOME:-0} == 1 ]]; then
        echo "record_demo: kept $BRINDLE_HOME" >&2
    else
        rm -rf "$BRINDLE_HOME"
    fi
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
    --command "uv run --project '$PROJECT' brindle demo" \
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
    (cd "$DEMO" && uv run --project "$PROJECT" brindle autopilot 2>/dev/null) || true
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
import json, os, re, sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines()
header = json.loads(lines[0])
header.pop("command", None)
events = [json.loads(l) for l in lines[1:] if l.strip()]
last = max((i for i, e in enumerate(events) if e[1] == "o" and "\x1b[?1049l" in e[2]), default=None)
if last is not None:
    events = events[:last]
# End on the goal reached: keep half a second after the sidebar first draws
# "N of N verified" (time for its repaint to finish), then cut, so the GIF
# ends on the finished dashboard rather than the supervisor's wrap-up.
done = re.compile(r"\b(\d+) of \1 verified")
t, stop = 0.0, None
for i, e in enumerate(events):
    t += e[0]
    if stop is None and e[1] == "o" and done.search(e[2]):
        stop = t + 0.5
    if stop is not None and t > stop:
        events = events[:i]
        break
# Drop any output that shows your home directory (a startup warning quoting
# your own ~/.claude/settings.json, say), keeping the timing: later frames
# repaint those cells anyway.
home = os.path.expanduser("~") + "/"
kept, carry = [], 0.0
for e in events:
    if e[1] == "o" and home in e[2]:
        carry += e[0]
        continue
    e[0] = round(e[0] + carry, 6)
    carry = 0.0
    kept.append(e)
events = kept
# Claude Code's banner names the recording account's plan ("Opus 5.5 · Claude
# Max"): blank the plan, keeping the width so the rest of the line stays put.
plan = re.compile(r" · Claude (?:Max|Pro|Team|Enterprise)\b")
for e in events:
    if e[1] == "o":
        e[2] = plan.sub(lambda m: " " * len(m.group(0)), e[2])
# agg draws a no-break space (Claude Code puts one after its prompt and tree
# glyphs) as a stray triangle; a plain space looks the same in a terminal.
for e in events:
    if e[1] == "o":
        e[2] = e[2].replace("\u00a0", " ")
# Open on the supervisor's banner: everything before it (the build, the tmux
# attach, start-up warnings from your own agent settings) becomes one event,
# so the first frame is the dashboard rather than a shell line.
first = next((i for i, e in enumerate(events) if e[1] == "o" and "Claude Code" in e[2]), None)
if first:
    head = "".join(e[2] for e in events[:first + 1] if e[1] == "o")
    events = [[0.0, "o", head]] + events[first + 1:]
with open(path, "w", encoding="utf-8") as f:
    f.write(json.dumps(header, ensure_ascii=False) + "\n")
    for e in events:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
EOF

# The speed that makes the GIF about DEMO_SECONDS long: idle gaps are capped
# at 2 s and the last frame is held for 4 s.
PLAYED=$(python3 - "$CAST" <<'PY'
import json, sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()[1:]
print(f"{sum(min(json.loads(l)[0], 2.0) for l in lines if l.strip()):.2f}")
PY
)
if [[ -z $SPEED ]]; then
    SPEED=$(python3 -c "import sys; p, t = map(float, sys.argv[1:]); print(f'{max(1.0, p / max(t - 4, 1)):.2f}')" \
        "$PLAYED" "$SECONDS_TARGET")
fi
echo "record_demo: rendering $OUT at ${SPEED}x with $RENDERER"
if [[ $RENDERER == vhs ]]; then
    # vhs's theme: the same palette as agg's THEME below, by name.
    VHS_THEME=$(python3 - "$THEME" <<'PY'
import json, sys
c = ["#" + x for x in sys.argv[1].split(",")]
names = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
t = {"name": "brindle", "background": c[0], "foreground": c[1], "cursor": c[1], "selection": c[10]}
for i, n in enumerate(names):
    t[n] = c[2 + i]
    t["bright" + n.capitalize()] = c[10 + i]
print(json.dumps(t))
PY
)
    HOLD=$(python3 -c "import sys; print(f'{float(sys.argv[1]) / float(sys.argv[2]) + 4:.1f}')" "$PLAYED" "$SPEED")
    TAPE_DIR=$(mktemp -d -t brindle-demo-tape)
    TAPE="$TAPE_DIR/demo.tape"
    python3 - "$ROOT/scripts/record_demo.tape" "$TAPE" "$OUT" "$CAST" "$SPEED" "$HOLD" "$VHS_THEME" <<'PY'
import sys
src, dst, out, cast, speed, hold, theme = sys.argv[1:]
text = open(src, encoding="utf-8").read()
for k, v in {"@OUT@": out, "@CAST@": cast, "@SPEED@": speed, "@HOLD@": hold, "@THEME@": theme}.items():
    text = text.replace(k, v)
open(dst, "w", encoding="utf-8").write(text)
PY
    vhs --quiet "$TAPE"
    rm -rf "$TAPE_DIR"
else
    agg --quiet \
        --theme "$THEME" \
        --font-family "$FONTS" \
        --font-size "$FONT_SIZE" \
        --speed "$SPEED" \
        --idle-time-limit 2 \
        --fps-cap 15 \
        --last-frame-duration 4 \
        "$CAST" "$OUT"
fi

size=$(wc -c <"$OUT" | tr -d ' ')
echo "record_demo: wrote $OUT ($((size / 1024)) KB)"
if [[ ${DEMO_KEEP_CAST:-0} != 1 ]]; then rm -f "$CAST"; fi
