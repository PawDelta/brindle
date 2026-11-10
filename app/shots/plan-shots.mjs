#!/usr/bin/env node
/* Screenshots of the design doc in a browser (dev only), over the fake daemon and the plan in
   shots/visuals.tsx:
     node shots/plan-shots.mjs --out <dir> [--channel chrome]
   (--channel chrome uses the Chrome already installed, where Playwright's own isn't downloaded.)
   Seven states x light/dark at 1440px: the doc with its review column open, the next decision's
   table, a highlight with its Comment / Suggest edit / Ask the supervisor bar, the comment landed in the column, the column
   collapsed to its strip, Suggesting with a section's text in its box, and the signed-off doc
   under its live board. */

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
      // Let the screen's own transitions end before the picture.
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      await page.screenshot({ path: resolve(out, `${name}-${theme}.png`) });
      console.log(`${name}-${theme}.png`);
    };
    const bar = '[data-testid="plan-verdict"]';
    await page.click('[data-testid="visuals-button"]');
    await page.click('[data-testid="visual-row-v_31aa"]');
    await page.waitForSelector('[data-testid="plan-review"]');
    await shot("1-doc-column-open");
    // The next decision, where Jump to it lands: its table under the green divider.
    await page.evaluate(() => document.getElementById("plan-decision-where").scrollIntoView());
    await shot("2-doc-decision");
    // Highlight a phrase of the summary, as a drag over it would.
    await page.evaluate(() => {
      const doc = document.querySelector('[data-testid="plan-doc"]');
      doc.scrollTop = 0;
      const leaf = [...doc.querySelectorAll("[data-o]")].find((el) => el.textContent.includes("so code never passes"));
      const range = document.createRange();
      range.setStart(leaf.firstChild, leaf.textContent.indexOf("code never"));
      range.setEnd(leaf.firstChild, leaf.textContent.indexOf("PawDelta") + "PawDelta".length);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      doc.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    });
    await page.waitForSelector('[role="toolbar"]');
    await shot("3-doc-highlight");
    await page.click('[role="toolbar"] button:has-text("Comment")');
    await page.fill('[data-testid="thread-draft"] textarea', "Say which PawDelta services still see metadata.");
    await page.click('[data-testid="thread-draft"] button:has-text("Comment")');
    await page.waitForSelector('[data-testid="thread-10"]');
    await shot("4-doc-comment");
    await page.click('[data-testid="plan-review-collapse"]');
    await page.waitForSelector('[data-testid="plan-review-strip"]');
    await shot("5-doc-column-collapsed");
    await page.click('[data-testid="plan-review-strip"]');
    await page.click('[role="radiogroup"][aria-label="Mode"] button:has-text("Suggesting")');
    await page.click('button[aria-label="Suggest an edit to Summary"]');
    await shot("6-doc-suggesting");
    await page.click('button:has-text("Cancel")');
    // Take the two open decisions as recommended, then sign off: the live board.
    await page.click(`${bar} button:has-text("Confirm A")`);
    await page.waitForSelector('[data-testid="plan-todo"] button[data-state="current"]:has-text("lease")');
    await page.click(`${bar} button:has-text("Confirm A")`);
    await page.click(`${bar} button:has-text("Sign off")`);
    await page.waitForSelector('[data-testid="plan-board-progress"]');
    await shot("7-doc-signed-off");
    await ctx.close();
  }
} finally {
  await browser.close();
  await server.close();
}
