#!/usr/bin/env node
/* Screenshots of the in-app visualizer in a browser (dev only), over the fake daemon:
     node shots/visuals-shots.mjs --out <dir>
   Eight states x light/dark at 1440px. The child webview exists only in the Tauri shell, so the
   reader shows its placeholder frame. The Export menu needs a snapshot the child webview would
   post; for its shots only, a second server swaps in a frame that posts a stub snapshot. The
   last one opens the page with ?free=1, where the policy answers 402 as it does off Pro. */

import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import { createServer } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const out = resolve(args[args.indexOf("--out") + 1]);
mkdirSync(out, { recursive: true });

const STUB = `
import { useEffect } from "react";
export const sameSource = (a, b) => a.kind === b.kind && a.id === b.id && (a.kind === "kept" || (b.kind === "session" && a.rev === b.rev));
export const visualSrc = (s) => s.kind === "kept" ? "brindle-visual://kept/" + s.id : "brindle-visual://" + s.id + "/r" + s.rev;
export function frameMessage(d) { return d && d.type ? d : null; }
export function VisualFrame({ source, title, snapshotRequest, onMessage }) {
  useEffect(() => { if (snapshotRequest) onMessage({ type: "brindle.snapshot", html: "<html><body>stub snapshot</body></html>" }, source); }, [snapshotRequest]);
  return <div className="flex min-h-0 flex-1 items-center justify-center p-s3 text-center text-sm text-ink-3" data-testid="visual-frame" aria-label={title}>This visual shows here once the sandboxed view is in this build. (Preview-only frame: posts a stub snapshot so the Export menu can open.)</div>;
}
export const PlaceholderFrame = VisualFrame;
`;

async function serve(stub) {
  const server = await createServer({
    root: resolve(here, ".."),
    configFile: resolve(here, "..", "vite.config.ts"),
    logLevel: "warn",
    server: { port: 4190, strictPort: false, host: "127.0.0.1", open: false },
    plugins: stub ? [{ name: "stub-frame", enforce: "pre", load: (id) => (id.split("?")[0].endsWith("/Visuals/frame.tsx") ? STUB : null) }] : [],
  });
  await server.listen();
  return { server, base: server.resolvedUrls.local[0].replace(/\/$/, "") };
}

async function open(browser, base, theme, extra = "") {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("pageerror", e.message));
  await page.goto(`${base}/shots/visuals.html?theme=${theme}${extra}`);
  await page.waitForSelector("html[data-ready]", { timeout: 30000 });
  await page.waitForSelector('[data-testid="visuals-button"]', { timeout: 15000 });
  return { ctx, page };
}

const browser = await chromium.launch();
try {
  for (const stub of [false, true]) {
    const { server, base } = await serve(stub);
    try {
      for (const theme of ["light", "dark"]) {
        const { ctx, page } = await open(browser, base, theme);
        const shot = (name) => page.screenshot({ path: resolve(out, `${name}-${theme}.png`) }).then(() => console.log(`${name}-${theme}.png`));
        const settle = () => page.waitForTimeout(400);
        if (!stub) {
          await shot("1-header-visuals-button");
          await page.click('[data-testid="visuals-button"]');
          await page.click('[data-testid="visuals-decided"]');
          await settle();
          await shot("5-decided-fold");
          await page.click('[data-testid="visual-row-v_8f2c"]');
          await page.waitForSelector('[data-testid="visual-reader"]');
          await settle();
          await shot("2-reader");
          await page.click('[role="radio"]:has-text("B · Gate on CI")');
          await page.fill('input[aria-label="Add a note"]', "CI is the gate we already trust");
          await settle();
          await shot("4-answer-chosen");
          await page.click('[data-testid="visual-keep"]');
          await settle();
          await shot("3-kept");
        } else {
          const box = (p) => p.$eval('[data-testid="visual-sandbox"]', (el) => JSON.stringify(el.getBoundingClientRect()));
          const exporting = async (p) => {
            await p.click('[data-testid="visuals-button"]');
            await p.click('[data-testid="visual-row-v_8f2c"]');
            await p.waitForSelector('[data-testid="visual-reader"]');
            const before = await box(p);
            await p.click('[data-testid="visual-export"]');
            await p.waitForSelector('[data-testid="export-menu"]', { timeout: 5000 });
            // The menu overlays the page: the frame's layout box must not move when it opens.
            const after = await box(p);
            if (before !== after) throw new Error(`Export menu moved the frame: ${before} -> ${after}`);
          };
          await exporting(page);
          await settle();
          await shot("6-export-menu");
          await page.click('button[aria-label="SharePoint"]');
          await page.fill('input[aria-label="Find a SharePoint site"]', "Release");
          await settle();
          await shot("7-export-sharepoint");
          // Off Pro: the online destinations greyed, with their Pro badge.
          const free = await open(browser, base, theme, "&free=1");
          await exporting(free.page);
          await settle();
          await free.page.screenshot({ path: resolve(out, `8-export-pro-${theme}.png`) });
          console.log(`8-export-pro-${theme}.png`);
          await free.ctx.close();
        }
        await ctx.close();
      }
    } finally {
      await server.close();
    }
  }
} finally {
  await browser.close();
}
