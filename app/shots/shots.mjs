#!/usr/bin/env node
/* Renderings of the app (dev only): serves shots/preview.html with Vite,
   over the fake daemon and fixtures.ts, and screenshots each scene in both
   themes as full-page PNGs with Playwright's CLI (fetched by npx; it is not
   a dependency of the app).

     node shots/shots.mjs [--step <name>] [--out <dir>] [--scene <a,b>] [--theme light,dark] [--browser chromium|webkit]

   PNGs land in <out>/<step>/<scene>-<theme>.png; <out> defaults to
   shots/out. The first run downloads the browser (Chromium unless
   --browser says WebKit, the engine Tauri runs on macOS) for Playwright. */

import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PLAYWRIGHT = "playwright@1.49.1";
const SCENES = [
  "new", "session", "session-waiting", "session-paused", "session-failed", "session-done", "inbox", "agent-supervisor", "agent-working", "agent-waiting", "agent-done", "agent-reviewer", "agent-failed", "agents", "agents-make",
  "knowledge", "knowledge-record", "knowledge-search", "knowledge-empty", "knowledge-add", "settings", "settings-edit", "settings-key", "settings-signin", "settings-health", "account", "account-team", "account-enterprise", "account-free", "account-signed-out",
];
const THEMES = ["dark", "light"];
const VIEWPORT = "1440,960";
/* How long the page gets to fetch its snapshot, draw the terminal and play
   its scene's presses before the shot (preview.tsx: ~1.5s after the first
   snapshot, longer on a busy machine). */
const SETTLE_MS = 5000;

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const step = opt("step", "latest");
const out = resolve(opt("out", resolve(here, "out")), step);
const scenes = opt("scene", SCENES.join(",")).split(",");
const themes = opt("theme", THEMES.join(",")).split(",");
const browser = opt("browser", "chromium");

/* Runs a command to its end without blocking this process: the Vite server
   below serves the page from this same event loop. */
const run = (cmd, cmdArgs) =>
  new Promise((resolve, reject) => {
    const child = spawn(cmd, cmdArgs, { stdio: "inherit" });
    child.on("error", reject);
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`${cmd} ${cmdArgs.join(" ")} failed (${code})`))));
  });

mkdirSync(out, { recursive: true });
await run("npx", ["-y", PLAYWRIGHT, "install", browser]);

const server = await createServer({
  root: resolve(here, ".."),
  configFile: resolve(here, "..", "vite.config.ts"),
  logLevel: "warn",
  server: { port: 4178, strictPort: false, host: "127.0.0.1", open: false },
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, "");

try {
  for (const scene of scenes) {
    for (const theme of themes) {
      const url = `${base}/shots/preview.html?scene=${scene}&theme=${theme}`;
      const file = resolve(out, `${scene}-${theme}.png`);
      await run("npx", [
        "-y", PLAYWRIGHT, "screenshot",
        "--browser", browser,
        "--viewport-size", VIEWPORT,
        "--color-scheme", theme,
        "--full-page",
        "--wait-for-timeout", String(SETTLE_MS),
        url, file,
      ]);
      console.log(file);
    }
  }
} finally {
  await server.close();
}
