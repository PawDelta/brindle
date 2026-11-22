#!/usr/bin/env node
/* The Library and sharing UX review's pictures (dev only):
     node shots/library-ux-shots.mjs [--out <dir>] [--only current|mockups] [--channel chrome]
   - current/: today's app on demo data, light and dark: Settings › Library in four states
     (shots/libraryUx.ts), the share panel's states and a teammate's reader (shots/panels.tsx), the
     Library and the Inbox (shots/preview.tsx).
   - mockups/: the improved flows (shots/library-ux.tsx), light and dark.
   <dir> defaults to ~/Documents/brindle-design-docs/library-ux; its index.html is written by hand. */

import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import { createServer } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const option = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const out = resolve(option("--out", resolve(homedir(), "Documents/brindle-design-docs/library-ux")));
const only = option("--only", "");

/* Today's screens: [file, page, what to capture ("view": the window; else a selector), a step before the shot]. */
const CURRENT = [
  ["settings-library", "preview.html?scene=settings-library", '[data-testid="doc-switches"]'],
  ["settings-library-page", "preview.html?scene=settings-library", "view"],
  ["settings-library-servers", "preview.html?scene=settings-library-servers", '[data-testid="doc-switches"]'],
  ["settings-library-locked", "preview.html?scene=settings-library-locked", '[data-testid="doc-switches"]'],
  ["settings-library-admin", "preview.html?scene=settings-library-admin", "view"],
  ["library", "preview.html?scene=library", "view"],
  ["inbox", "preview.html?scene=inbox", "view"],
  ["share-first", "panels.html?panel=share&state=first", '[data-testid="plan-share"]', async (page) => {
    await page.getByTestId("share-team-group-platform").waitFor();
  }],
  ["share-people", "panels.html?panel=share&state=people", '[data-testid="plan-share"]'],
  ["share-search", "panels.html?panel=share&state=search", '[data-testid="plan-share"]', async (page) => {
    await page.getByTestId("share-search").focus();
    await page.getByTestId("share-result-lee").waitFor();
    await page.getByTestId("share-result-lee").hover();
  }],
  ["share-person-menu", "panels.html?panel=share&state=people", '[data-testid="plan-share"]', async (page) => {
    await page.getByTestId("share-person-sam").getByRole("button", { name: "What Sam Rivera can do" }).click();
  }],
  ["reader-teammate", "panels.html?panel=share&state=reviewer", "view"],
];

/* The improved flows: [file, frame of shots/library-ux.tsx]. */
const MOCKUPS = [
  "location-a", "location-a-moved", "location-b", "location-b-moved", "location-locked", "admin",
  "share-a", "share-a-add", "share-b", "share-b-add", "share-first", "recipient-library", "recipient-inbox", "recipient-reader",
];

const server = await createServer({
  root: resolve(here, ".."),
  configFile: resolve(here, "..", "vite.config.ts"),
  logLevel: "warn",
  server: { port: 4201, strictPort: false, host: "127.0.0.1", open: false },
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, "");
const browser = await chromium.launch(args.includes("--channel") ? { channel: option("--channel") } : {});
const settled = async (page) => {
  await page.waitForSelector("html[data-ready]");
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
};
try {
  for (const theme of ["light", "dark"]) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme, deviceScaleFactor: 2 });
    if (only !== "mockups") {
      mkdirSync(resolve(out, "current"), { recursive: true });
      for (const [name, path, what, step] of CURRENT) {
        const page = await ctx.newPage();
        page.on("pageerror", (e) => console.log("pageerror", name, e.message));
        // The share panel is as tall as its window: a shorter one keeps its picture to what it holds.
        if (name.startsWith("share-")) await page.setViewportSize({ width: 1440, height: 640 });
        await page.goto(`${base}/shots/${path}&theme=${theme}`);
        await settled(page);
        if (step) await step(page);
        await settled(page);
        const file = resolve(out, "current", `${name}-${theme}.png`);
        if (what === "view") await page.screenshot({ path: file });
        else await page.locator(what).first().screenshot({ path: file });
        console.log(file);
        await page.close();
      }
    }
    if (only !== "current") {
      mkdirSync(resolve(out, "mockups"), { recursive: true });
      const page = await ctx.newPage();
      page.on("pageerror", (e) => console.log("pageerror", e.message));
      for (const frame of MOCKUPS) {
        await page.goto(`${base}/shots/library-ux.html?theme=${theme}&f=${frame}`);
        await settled(page);
        const file = resolve(out, "mockups", `${frame}-${theme}.png`);
        await page.locator("#frame").screenshot({ path: file });
        console.log(file);
      }
      await page.close();
    }
    await ctx.close();
  }
} finally {
  await browser.close();
  await server.close();
}
