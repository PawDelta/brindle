#!/usr/bin/env node
/* The organisation settings mockups (shots/admin-settings.tsx), dev only:
     node shots/admin-settings-shots.mjs [--out <dir>] [--channel chrome]
   Two layouts, each as the admin's screen and the member's, light and dark, as <frame>-<theme>.png.
   <dir> defaults to ~/Documents/brindle-design-docs/admin-settings; its index.html is written by hand. */

import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import { createServer } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const option = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const out = resolve(option("--out", resolve(homedir(), "Documents/brindle-design-docs/admin-settings")));
mkdirSync(out, { recursive: true });

const FRAMES = ["a-admin", "a-member", "b-admin", "b-admin-edit", "b-member"];

const server = await createServer({
  root: resolve(here, ".."),
  configFile: resolve(here, "..", "vite.config.ts"),
  logLevel: "warn",
  server: { port: 4203, strictPort: false, host: "127.0.0.1", open: false },
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, "");
const browser = await chromium.launch(args.includes("--channel") ? { channel: option("--channel") } : {});
try {
  for (const theme of ["light", "dark"]) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1100 }, colorScheme: theme, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => console.log("pageerror", e.message));
    for (const frame of FRAMES) {
      await page.goto(`${base}/shots/admin-settings.html?theme=${theme}&f=${frame}`);
      await page.waitForSelector("html[data-ready]");
      await page.evaluate(() => document.fonts.ready);
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      const file = resolve(out, `${frame}-${theme}.png`);
      await page.locator("#frame").screenshot({ path: file });
      console.log(file);
    }
    await ctx.close();
  }
} finally {
  await browser.close();
  await server.close();
}
