#!/usr/bin/env node
/* Screenshots of the planning doc (PlanDesignDoc.tsx) in a browser (dev only), over the fake daemon
   and the spine plan in shots/visuals.tsx (?doc=spine), to hold beside plan-doc-redesign.html:
     node shots/plan-doc-shots.mjs --out <dir> [--channel chrome]
   Four states x light/dark at 1440px: the top of the doc with its outline and margin notes, the
   first decision open, every decision picked (Approve on), and the approved bar. Then a question the
   supervisor is still replying to, with its margin thread cropped at 2x. Then (alone with
   --sign-off-only) a teammate's optional sign-off on a shared plan: the Sign off button and the
   sign-off sheet before and after they sign, each also cropped at 2x. */

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
  server: { port: 4196, strictPort: false, host: "127.0.0.1", open: false },
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, "");
const browser = await chromium.launch(args.includes("--channel") ? { channel: args[args.indexOf("--channel") + 1] } : {});
try {
  for (const theme of ["light", "dark"]) {
    // A teammate with comment access, not a reviewer, on a shared plan (?shared=1).
    for (const scale of [1, 2]) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme, deviceScaleFactor: scale });
      const doc = await context.newPage();
      doc.on("pageerror", (e) => console.log("pageerror", e.message));
      await doc.goto(`${base}/shots/visuals.html?theme=${theme}&doc=spine&shared=1`);
      await doc.waitForSelector("html[data-ready]");
      const sheet = doc.locator('[data-testid="plan-signoffs"]');
      const bar = doc.locator('[data-testid="plan-verdict"]');
      const own = sheet.locator('[data-testid="signoff-u_priya"][data-sign-off="optional"]');
      const snap = async (name, locator) => {
        await doc.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
        const file = `${name}${scale === 2 ? "-2x" : ""}-${theme}.png`;
        if (locator) await locator.screenshot({ path: resolve(out, file), animations: "disabled" });
        else await doc.screenshot({ path: resolve(out, file), animations: "disabled" });
        console.log(file);
      };
      await bar.locator('[data-testid="plan-sign-off"]:enabled').waitFor();
      await own.and(doc.locator('[data-state="waiting"]')).waitFor();
      if (scale === 1) await snap("8-sign-off-button");
      else {
        await snap("8-sign-off-button", bar);
        await snap("8-sign-off-sheet", sheet);
      }
      await bar.locator('[data-testid="plan-sign-off"]').click();
      await own.and(doc.locator('[data-state="signed"]')).waitFor();
      await snap(scale === 1 ? "9-signed" : "9-signed-sheet", scale === 1 ? undefined : sheet);
      await context.close();
    }
    if (args.includes("--sign-off-only")) continue;
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => console.log("pageerror", e.message));
    await page.goto(`${base}/shots/visuals.html?theme=${theme}&doc=spine`);
    await page.waitForSelector("html[data-ready]");
    const shot = async (name) => {
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      await page.screenshot({ path: resolve(out, `${name}-${theme}.png`) });
      console.log(`${name}-${theme}.png`);
    };
    await page.click('[data-testid="visuals-button"]');
    await page.click('[data-testid="visual-row-v_31aa"]');
    await page.waitForSelector('[data-testid="plan-design-doc"]');
    await shot("1-doc-top");
    await page.evaluate(() => document.getElementById("plan-decision-store").scrollIntoView({ block: "center" }));
    await shot("2-doc-decision");
    for (const id of ["store", "loud", "reach"]) await page.click(`#plan-decision-${id} [data-testid="plan-option"]`);
    await page.evaluate(() => document.getElementById("plan-decision-loud").scrollIntoView({ block: "center" }));
    await shot("3-all-decided");
    // The section shapes: Goals in two columns, Tech stack and Alternatives as rows, the rollout's steps.
    for (const id of ["goals", "tech-stack", "alternatives", "rollout"]) {
      await page.evaluate((s) => document.getElementById(`plan-${s}`).scrollIntoView({ block: "start" }), id);
      await shot(`5-${id}`);
    }
    await page.click('[data-testid="plan-build"]');
    await page.waitForSelector('[data-testid="plan-next"][data-state="approved"]');
    await shot("4-approved");
    await ctx.close();
    // A margin thread awaiting the supervisor (?replying=1): the doc, then the thread alone at 2x.
    for (const scale of [1, 2]) {
      const asked = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme, deviceScaleFactor: scale });
      const doc = await asked.newPage();
      doc.on("pageerror", (e) => console.log("pageerror", e.message));
      await doc.goto(`${base}/shots/visuals.html?theme=${theme}&doc=spine&replying=1`);
      await doc.waitForSelector("html[data-ready]");
      await doc.click('[data-testid="visuals-button"]');
      await doc.click('[data-testid="visual-row-v_31aa"]');
      const thread = doc.locator('[data-testid="thread-4"]');
      await thread.locator('[data-state="supervisor-replying"]').waitFor();
      await thread.scrollIntoViewIfNeeded();
      await doc.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      const name = `${scale === 1 ? "6-replying" : "7-replying-thread-2x"}-${theme}.png`;
      const box = await thread.boundingBox();
      const clip = scale === 1 ? undefined : { x: box.x - 24, y: box.y - 20, width: box.width + 48, height: box.height + 40 };
      // Disabled animations hold the three dots at full strength.
      await doc.screenshot({ path: resolve(out, name), animations: "disabled", clip });
      console.log(name);
      await asked.close();
    }
  }
} finally {
  await browser.close();
  await server.close();
}
