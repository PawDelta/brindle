#!/usr/bin/env bash
# Records the desktop app's demo build (the real UI over the in-memory fake
# daemon) as the README's animated GIF: assets/brindle-app-demo.gif.
#
#   scripts/record_app_demo.sh [--frames <dir>] [--work <dir>]
#     --frames also keeps sample PNGs; --work keeps the captured frames there, to try the encoding on
#
# Builds app/demo/dist/index.html (npm run build:demo), opens it in its
# recording mode (?record: a calm inbox, see app/demo/recording.ts), drives it in
# Playwright's Chromium (dark theme; THEME=light for light; the Demo switcher
# hidden), captures lossless screenshots, then converts with ffmpeg using a palette
# pass. Not Playwright's video: VP8 at its bitrate, in 4:2:0, smears colour off thin
# text and turns flat grounds into noise. Needs node, npm, ffmpeg and Playwright's
# Chromium (fetched on first run).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/assets/brindle-app-demo.gif"
FRAMES=""
WORK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --frames) FRAMES="${2:?--frames needs a directory}"; shift 2 ;;
    --work) WORK="${2:?--work needs a directory}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

FPS=10
TRIM_START=0.7  # seconds of first paint to drop from the front
DURATION=30     # seconds of GIF

if [ -n "$WORK" ]; then
  mkdir -p "$WORK"
else
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
fi

cd "$ROOT/app"
[ -d node_modules ] || npm ci --silent
npm run build:demo --silent
npx playwright install chromium >/dev/null

APP_ROOT="$ROOT/app" WORK_DIR="$WORK" FRAMES_DIR="$FRAMES" node --input-type=module - <<'EOF'
import { pathToFileURL } from "node:url";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const appRoot = process.env.APP_ROOT;
const { chromium } = await import(pathToFileURL(join(appRoot, "node_modules/playwright-core/index.mjs")).href);
const workDir = process.env.WORK_DIR;
const framesDir = process.env.FRAMES_DIR;
if (framesDir) mkdirSync(framesDir, { recursive: true });

// The GIF's size too: scaling the frames would blur thin text and lines off their colours, and painting smaller (a
// device scale under 1) scales xterm's canvas twice.
const SIZE = { width: 1440, height: 900 };
// sRGB whatever the display's profile, and no subpixel text: its colour fringes aren't the app's colours.
const browser = await chromium.launch({ args: ["--force-color-profile=srgb", "--disable-lcd-text"] });
const context = await browser.newContext({
  viewport: SIZE,
  colorScheme: process.env.THEME === "light" ? "light" : "dark",
});
const page = await context.newPage();
// The terminal's stack (components/PtyTerminal.tsx) comes to ui-monospace in the app's webview, SF Mono on a Mac, but
// Chromium has no ui-monospace and would fall on to Menlo: SF Mono under the stack's first name, before the app mounts.
const SF_MONO = "/System/Library/Fonts/SFNSMono.ttf";
if (existsSync(SF_MONO)) {
  await page.addInitScript((font) => {
    const bytes = Uint8Array.from(atob(font), (c) => c.charCodeAt(0));
    document.fonts.add(new FontFace("Geist Mono", bytes.buffer, { weight: "1 1000" }));
  }, readFileSync(SF_MONO).toString("base64"));
}
// ?record: the demo's recording mode (app/demo/recording.ts), a calm inbox and panes as the CLIs draw them.
await page.goto(pathToFileURL(join(appRoot, "demo/dist/index.html")).href + "?record");
const t0 = Date.now();
// No Demo switcher, and a small cursor dot so viewers can follow the clicks.
await page.addStyleTag({
  content: `[data-testid="demo-switcher"]{display:none!important}
    #rec-cursor{position:fixed;z-index:99999;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;
      background:var(--branch-soft);border:2px solid var(--branch);pointer-events:none;transition:transform .1s}
    #rec-cursor.down{transform:scale(.7)}`,
});
await page.evaluate(() => {
  const dot = document.createElement("div");
  dot.id = "rec-cursor";
  dot.style.left = "-50px";
  document.body.appendChild(dot);
  addEventListener("mousemove", (e) => { dot.style.left = e.clientX + "px"; dot.style.top = e.clientY + "px"; }, true);
  addEventListener("mousedown", () => dot.classList.add("down"), true);
  addEventListener("mouseup", () => dot.classList.remove("down"), true);
});

// The frames: lossless screenshots, one after another, each stamped as it starts.
const captured = [];
let recording = true;
const capturing = (async () => {
  while (recording) {
    const file = join(workDir, `frame-${String(captured.length).padStart(5, "0")}.png`);
    const startedAt = Date.now() / 1000;
    await page.screenshot({ path: file });
    captured.push({ file, at: startedAt });
  }
})();

const at = (s) => page.waitForTimeout(Math.max(0, s * 1000 - (Date.now() - t0)));
let shot = 0;
const snap = async (name) => { if (framesDir) await page.screenshot({ path: join(framesDir, `${String(++shot).padStart(2, "0")}-${name}.png`) }); };
const click = async (locator) => {
  const box = await locator.first().boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 12 });
  await page.waitForTimeout(250);
  await page.mouse.down();
  await page.waitForTimeout(80);
  await page.mouse.up();
};
const button = (name) => page.getByRole("button", { name });

// The page's own clock: the milestones go green at 20s, the PR is ready at 25s.
await page.mouse.move(700, 300);
await at(2.5);   await snap("session");
await click(button(/a7aaa807/));                    // a worker, terminal printing
await at(7);     await snap("worker");
await click(button(/^Inbox/));                      // what waits on you: a permission and a plan
await at(8.5);   await snap("inbox");
await click(button(/^Allow once$/));                // the permission answered
await at(9.8);   await snap("inbox-answered");
await click(button(/^Approve$/));                   // the plan approved: nothing left waiting
await at(11);    await snap("inbox-empty");
await click(button(/b31c9f02/));                    // the allowed worker carries on
await at(15.5);  await snap("second-worker");
await click(button(/e9a0b6c3/));                    // the plan-first worker starts on its plan
await at(18.5);  await snap("planned-worker");
await click(button(/sup-42 · supervisor/));         // the supervisor merges and checks at 20s
await at(21.5);  await snap("milestones-green");
await at(25.5);
await click(button(/^Inbox/));                      // brindle says the branch is ready
await at(26.5);  await snap("pr-ready");
await click(button(/Review & open PR/));
await at(28);
await click(button(/Review changes/));              // the branch's diff
await at(29.5);  await snap("diff");
await at(32);
recording = false;
await capturing;

// The colours the palette must hold exactly: the theme's solid tokens, and the panes' indexes as xterm.js's palette has them.
const { tokens, panes } = await page.evaluate(() => {
  // (Not the knowledge map's --group-*: the clip never shows them, and the slots are better spent.)
  const names = [...document.styleSheets].flatMap((s) => [...s.cssRules]).filter((r) => r.selectorText === ":root").flatMap((r) => [...r.style]).filter((n) => n.startsWith("--") && !n.startsWith("--group-"));
  const style = getComputedStyle(document.documentElement);
  return { tokens: names.map((n) => style.getPropertyValue(n).trim()), panes: window.paneColors };
});
const LEVELS = [0, 95, 135, 175, 215, 255];
const indexed = (n) => (n >= 232 ? [8 + (n - 232) * 10, 8 + (n - 232) * 10, 8 + (n - 232) * 10] : [LEVELS[Math.floor((n - 16) / 36)], LEVELS[Math.floor((n - 16) / 6) % 6], LEVELS[(n - 16) % 6]]);
const solid = tokens.filter((t) => /^#[0-9a-f]{6}$/i.test(t)).map((t) => [1, 3, 5].map((i) => parseInt(t.slice(i, i + 2), 16)));
writeFileSync(join(workDir, "accents.json"), JSON.stringify([...solid, ...panes.map(indexed)]));
await browser.close();

// The frames in time: each shown until the next, as ffmpeg's concat reads them (the last named twice, its duration kept).
const list = captured.flatMap((f, i) => [`file '${f.file}'`, `duration ${((captured[i + 1]?.at ?? f.at + 1) - f.at).toFixed(4)}`]);
writeFileSync(join(workDir, "frames.txt"), [...list, `file '${captured.at(-1).file}'`, ""].join("\n"));
console.log(`captured ${captured.length} frames`);
EOF

# Palette pass: one 256-colour palette for the whole clip, every pixel counted (not only the ones
# that change, or the still chrome loses its accents), no dithering (it would speckle flat colours).
ffmpeg -v error -y -ss "$TRIM_START" -f concat -safe 0 -i "$WORK/frames.txt" -t "$DURATION" \
  -vf "fps=$FPS,palettegen=max_colors=256:stats_mode=full" "$WORK/palette.png"
ffmpeg -v error -y -i "$WORK/palette.png" -f rawvideo -pix_fmt rgba "$WORK/palette.rgba"
# A status dot or a keyword is too few pixels to win a slot on its own: each accent takes the entry nearest it, exactly.
WORK_DIR="$WORK" node --input-type=module - <<'EOF'
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const dir = process.env.WORK_DIR;
const palette = readFileSync(join(dir, "palette.rgba"));
const taken = new Set();
for (const color of JSON.parse(readFileSync(join(dir, "accents.json"), "utf8"))) {
  const distance = (entry) => color.reduce((sum, c, i) => sum + (c - palette[entry * 4 + i]) ** 2, 0);
  const free = [...Array(256).keys()].filter((entry) => palette[entry * 4 + 3] === 255 && !taken.has(entry));
  const entry = free.reduce((best, e) => (distance(e) < distance(best) ? e : best));
  taken.add(entry);
  palette.set(color, entry * 4);
}
writeFileSync(join(dir, "palette.rgba"), palette);
EOF
ffmpeg -v error -y -f rawvideo -pix_fmt rgba -s 16x16 -i "$WORK/palette.rgba" -frames:v 1 "$WORK/palette.png"
ffmpeg -v error -y -ss "$TRIM_START" -f concat -safe 0 -i "$WORK/frames.txt" -i "$WORK/palette.png" -t "$DURATION" \
  -lavfi "fps=$FPS [x]; [x][1:v] paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 "$OUT"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
