#!/usr/bin/env node
/* Screenshots of the plan reader in a browser (dev only), over the fake daemon and the plan in
   shots/visuals.tsx:
     node shots/plan-shots.mjs --out <dir> [--channel chrome]
   (--channel chrome uses the Chrome already installed, where Playwright's own isn't downloaded.)
   Five states x light/dark at 1440px: the overview, a decision's option cards, another option
   picked with the ask being corrected, the split's task cards, and the approved live board. */

import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import { createServer } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const out = resolve(args[args.indexOf("--out") + 1]);
mkdirSync(out, { recursive: true });

const server = await createServer({
  root: resolve(here, ".."),
  configFile: resolve(here, "..", "vite.config.ts"),
  logLevel: "warn",
  server: { port: 4194, strictPort: false, host: "127.0.0.1", open: false },
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, "");
const browser = await chromium.launch(args.includes("--channel") ? { channel: args[args.indexOf("--channel") + 1] } : {});
try {
  for (const theme of ["light", "dark"]) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => console.log("pageerror", e.message));
    await page.goto(`${base}/shots/visuals.html?theme=${theme}`);
    await page.waitForSelector("html[data-ready]");
    const shot = async (name) => {
      // Let the step's own transitions end before the picture.
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      await page.screenshot({ path: resolve(out, `${name}-${theme}.png`) });
      console.log(`${name}-${theme}.png`);
    };
    const next = () => page.click('[data-testid="plan-verdict"] button:has-text("Next")');
    await page.click('[data-testid="visuals-button"]');
    await page.click('[data-testid="visual-row-v_31aa"]');
    await page.waitForSelector('[data-testid="plan-ask"]');
    await shot("1-plan-overview");
    await next();
    await page.waitForSelector('[role="radiogroup"]');
    await shot("2-plan-decision");
    await page.click('[role="radio"]:has-text("B · The existing socket")');
    await page.click('button:has-text("Correct the ask")');
    await page.fill('textarea[aria-label="Comment on What I understood"]', "Teams on a shared key too.");
    await shot("3-plan-picked-corrected");
    await next();
    await next();
    await page.waitForSelector('[data-testid="task-w1"]');
    await shot("4-plan-split");
    await page.click('button:has-text("Approve")');
    await page.waitForSelector('[data-testid="plan-board-progress"]');
    await shot("5-plan-board");
    await ctx.close();
  }
} finally {
  await browser.close();
  await server.close();
}
