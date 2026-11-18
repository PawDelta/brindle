/* The page's side of visual zoom (src-tauri/src/visual_zoom.js, the very file the prelude embeds),
   run in real engines on a page far bigger than its window: wheel and keys inside the page become
   brindle.zoom messages with the pointer's place and never scroll or zoom the page themselves, a
   plain wheel is left alone, and the host's anchor message keeps the zoomed-around content under
   the pointer. The webview's own scaling (set_zoom) is the shell's and isn't in a browser. */

import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const script = readFileSync(new URL("../src-tauri/src/visual_zoom.js", import.meta.url), "utf8");

test("zoom input inside the page is forwarded around the pointer, and the host's anchor keeps it there", async ({ page }) => {
  await page.setContent(`<body style="margin:0"><div style="width:4000px;height:4000px"></div></body>`);
  await page.evaluate((source) => {
    const sent: unknown[] = [];
    Object.assign(window, { sent });
    (0, eval)(`(${source.trim()})`)((m: unknown) => sent.push(m));
    window.scrollTo(500, 500);
  }, script);
  const sent = () => page.evaluate(() => (window as unknown as { sent: Record<string, unknown>[] }).sent);
  const scroll = () => page.evaluate(() => [window.scrollX, window.scrollY]);

  // A plain wheel scrolls the page and says nothing.
  await page.mouse.move(200, 100);
  await page.mouse.wheel(0, 50);
  await expect.poll(scroll).toEqual([500, 550]);
  expect(await sent()).toEqual([]);

  // ⌘/Ctrl-wheel is zoom: forwarded with where the pointer is; the page doesn't scroll for it.
  await page.keyboard.down("Control");
  await page.mouse.wheel(0, -20);
  await page.keyboard.up("Control");
  await expect.poll(async () => (await sent()).length).toBe(1);
  expect((await sent())[0]).toMatchObject({ type: "brindle.zoom", dir: "in", x: 200, y: 100 });
  expect(((await sent())[0].factor as number)).toBeGreaterThan(1);
  expect(await scroll()).toEqual([500, 550]);

  await page.keyboard.press("Control+=");
  expect((await sent()).at(-1)).toEqual({ type: "brindle.zoom", dir: "in" });

  // The host zoomed 1 → 1.25 around (200, 100): the content under the pointer stays under it.
  await page.evaluate(() => window.dispatchEvent(new MessageEvent("message", { data: { type: "brindle.zoom.anchor", x: 200, y: 100, ratio: 0.8 }, origin: "brindle://host" })));
  await expect.poll(scroll).toEqual([540, 570]);
  // Only the host may ask: the same message from anywhere else does nothing.
  await page.evaluate(() => window.dispatchEvent(new MessageEvent("message", { data: { type: "brindle.zoom.anchor", x: 200, y: 100, ratio: 0.8 }, origin: "https://evil.example" })));
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
  expect(await scroll()).toEqual([540, 570]);
});
