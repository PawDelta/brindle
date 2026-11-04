#!/usr/bin/env bash
# Records the desktop app's demo build (the real UI over the in-memory fake
# daemon) as the README's animated GIF: assets/brindle-app-demo.gif.
#
#   scripts/record_app_demo.sh [--frames <dir>]   # --frames also keeps sample PNGs
#
# Builds app/demo/dist/index.html (npm run build:demo), drives it with
# Playwright's video recording (light theme, the floating Demo switcher
# hidden), then converts with ffmpeg using a palette pass. Needs node, npm,
# ffmpeg and Playwright's Chromium (fetched on first run).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/assets/brindle-app-demo.gif"
FRAMES=""
if [ "${1:-}" = "--frames" ]; then FRAMES="${2:?--frames needs a directory}"; fi

WIDTH=1200      # GIF width in pixels
FPS=10
TRIM_START=1.0  # seconds of page load to drop from the front
DURATION=30     # seconds of GIF

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$ROOT/app"
[ -d node_modules ] || npm ci --silent
npm run build:demo --silent
npx playwright install chromium >/dev/null

APP_ROOT="$ROOT/app" VIDEO_DIR="$WORK" FRAMES_DIR="$FRAMES" node --input-type=module - <<'EOF'
import { pathToFileURL } from "node:url";
import { mkdirSync, readdirSync, renameSync } from "node:fs";
import { join } from "node:path";

const appRoot = process.env.APP_ROOT;
const { chromium } = await import(pathToFileURL(join(appRoot, "node_modules/playwright-core/index.mjs")).href);
const videoDir = process.env.VIDEO_DIR;
const framesDir = process.env.FRAMES_DIR;
if (framesDir) mkdirSync(framesDir, { recursive: true });

const SIZE = { width: 1440, height: 900 };
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: SIZE,
  colorScheme: "light",
  recordVideo: { dir: videoDir, size: SIZE },
});
const page = await context.newPage();
await page.goto(pathToFileURL(join(appRoot, "demo/dist/index.html")).href);
const t0 = Date.now();
// No Demo switcher, and a small cursor dot so viewers can follow the clicks.
await page.addStyleTag({
  content: `[data-testid="demo-switcher"]{display:none!important}
    #rec-cursor{position:fixed;z-index:99999;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;
      background:rgba(90,70,200,.35);border:2px solid rgba(90,70,200,.9);pointer-events:none;transition:transform .1s}
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
await click(button(/^Inbox/));                      // every decision in one place
await at(8.5);   await snap("inbox");
await click(button(/^Allow once$/));                // a permission answered
await at(10.5);  await snap("inbox-answered");
await click(button(/b31c9f02/));                    // the allowed worker carries on, printing
await at(16.5);  await snap("second-worker");
await click(button(/a7aaa807/));                    // while the first is still at work
await at(19.7);
await click(button(/sup-42 · supervisor/));
await at(21.5);  await snap("milestones-green");    // every milestone passed at 20s
await at(25.5);
await click(button(/^Inbox/));                      // brindle says the branch is ready
await at(26.5);  await snap("pr-ready");
await click(button(/Review & open PR/));
await at(28);
await click(button(/Review changes/));              // the branch's diff
await at(29.5);  await snap("diff");
await at(32);

await context.close();   // flushes the video
await browser.close();
const webm = readdirSync(videoDir).find((f) => f.endsWith(".webm"));
renameSync(join(videoDir, webm), join(videoDir, "app.webm"));
EOF

# Palette pass: one optimal 256-colour palette for the whole clip, light dithering.
FILTERS="fps=$FPS,scale=$WIDTH:-1:flags=lanczos"
ffmpeg -v error -y -ss "$TRIM_START" -t "$DURATION" -i "$WORK/app.webm" \
  -vf "$FILTERS,palettegen=max_colors=96:stats_mode=diff" "$WORK/palette.png"
ffmpeg -v error -y -ss "$TRIM_START" -t "$DURATION" -i "$WORK/app.webm" -i "$WORK/palette.png" \
  -lavfi "$FILTERS [x]; [x][1:v] paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 "$OUT"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
