#!/usr/bin/env node
/* Screenshots of a shared plan's queued changes (PlanDelivery.tsx) in a browser (dev only), over the
   fake daemon in shots/visuals.tsx (?doc=spine&shared=1&queued=...), light at 1440px:
     node shots/queued-shots.mjs --out <dir> [--channel chrome]
   Waiting to send, after sending (Sent), and a refused pick; each where the reply and comment are and
   where the pick is, with the greyed reply and the tagged card cropped at 2x. Then the author's own
   shared plan with its reply and comment unsent. It also prints boxes that spill out of their parent. */

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
  server: { port: 4197, strictPort: false, host: "127.0.0.1", open: false },
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, "");
const browser = await chromium.launch(args.includes("--channel") ? { channel: args[args.indexOf("--channel") + 1] } : {});

const open = async (query, scale) => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: "light", deviceScaleFactor: scale });
  const page = await context.newPage();
  page.on("pageerror", (e) => console.log("pageerror", e.message));
  await page.goto(`${base}/shots/visuals.html?theme=light&doc=spine&${query}`);
  // Sent fades after two seconds: hold it so it can be taken.
  await page.addStyleTag({ content: ".plan-delivery-sent { animation: none !important; }" });
  await page.waitForSelector("html[data-ready]");
  return { context, page };
};
const settle = (page) => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
const shot = async (page, name) => {
  await settle(page);
  await page.screenshot({ path: resolve(out, `${name}.png`) });
  console.log(resolve(out, `${name}.png`));
};
const crop = async (page, locator, name, pad = 14) => {
  await locator.scrollIntoViewIfNeeded();
  await settle(page);
  const box = await locator.boundingBox();
  await page.screenshot({ path: resolve(out, `${name}.png`), clip: { x: box.x - pad, y: box.y - pad, width: box.width + pad * 2, height: box.height + pad * 2 } });
  console.log(resolve(out, `${name}.png`));
};
const center = (page, id) => page.evaluate((s) => document.getElementById(s).scrollIntoView({ block: "center" }), id);

try {
  for (const [n, state] of [["1", "waiting"], ["2", "sent"], ["3", "refused"]]) {
    for (const scale of [1, 2]) {
      const { context, page } = await open(`shared=1&queued=${state}`, scale);
      const thread = page.locator('[data-testid="thread-10"]');
      const card = page.locator("#plan-decision-loud");
      await card.locator(`[data-state="${state}"]`).waitFor();
      if (scale === 1) {
        await page.evaluate(() => document.querySelector('[data-testid="plan-doc"]').scrollTo(0, 0));
        await shot(page, `${n}-${state}-top`);
        await center(page, "plan-proposed-design");
        await shot(page, `${n}-${state}-reply`);
        await center(page, "plan-decision-loud");
        await shot(page, `${n}-${state}-pick`);
      } else {
        if (state !== "refused") await crop(page, thread, `${n}-${state}-reply-2x`);
        if (state === "waiting") await crop(page, page.locator('article[data-state="waiting"]'), `${n}-${state}-comment-2x`);
        await crop(page, card, `${n}-${state}-card-2x`);
      }
      await context.close();
    }
  }
  const { context, page } = await open("queued=waiting", 1);
  await page.click('[data-testid="visuals-button"]');
  await page.click('[data-testid="visual-row-v_31aa"]');
  await page.locator('[data-testid="thread-10"] [data-state="waiting"]').first().waitFor();
  await shot(page, "4-author-waiting-top");
  await center(page, "plan-proposed-design");
  await shot(page, "4-author-waiting-reply");
  await context.close();
} finally {
  await browser.close();
  await server.close();
}
