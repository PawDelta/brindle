#!/usr/bin/env node
/* Mockups of an "Other" answer on a planning doc's decision card (shots/decision-other.tsx), dev only:
     node shots/decision-other-shots.mjs [--out shots/out/decision-other] [--channel chrome]
   Three treatments x five states (open, typing, saved, verdict, narrow) x light/dark, plus an
   index.html laying them side by side. */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import { createServer } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const out = resolve(args.includes("--out") ? args[args.indexOf("--out") + 1] : resolve(here, "out", "decision-other"));
mkdirSync(out, { recursive: true });

const TREATMENTS = [
  { id: "inline", name: "A · Inline row", lede: "“Other…” is the last option row. Picking it opens a short field in that row; saved, the row reads “Other: ‘…’” with the picker's face, like any option." },
  { id: "ghost", name: "B · Ghost link", lede: "A quiet “+ Your own answer” under the rows. It adds a row of the person's own, tagged “Your answer”, which teammates can then pick too." },
  { id: "under", name: "C · Field under the cards", lede: "A field is always there under the rows: “None of these?”. Typing in it is the pick; clearing it hands the pick back to the listed options." },
];
const STATES = [
  { id: "open", name: "1 · Open, Other offered" },
  { id: "typing", name: "2 · Other chosen, typing" },
  { id: "saved", name: "3 · Saved, as the team sees it" },
  { id: "verdict", name: "4 · Approve bar with a custom answer" },
  { id: "narrow", name: "5 · Narrow width (saved)" },
];

const server = await createServer({
  root: resolve(here, ".."),
  configFile: resolve(here, "..", "vite.config.ts"),
  logLevel: "warn",
  server: { port: 4198, strictPort: false, host: "127.0.0.1", open: false },
});
await server.listen();
const base = server.resolvedUrls.local[0].replace(/\/$/, "");
const browser = await chromium.launch(args.includes("--channel") ? { channel: args[args.indexOf("--channel") + 1] } : {});
try {
  for (const theme of ["light", "dark"]) {
    const ctx = await browser.newContext({ viewport: { width: 1240, height: 900 }, colorScheme: theme, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => console.log("pageerror", e.message));
    for (const tr of TREATMENTS) {
      for (const st of STATES) {
        const q = st.id === "narrow" ? "s=saved&w=narrow" : `s=${st.id}`;
        await page.goto(`${base}/shots/decision-other.html?theme=${theme}&t=${tr.id}&${q}`);
        await page.waitForSelector("html[data-ready]");
        await page.evaluate(() => document.fonts.ready);
        const name = `${tr.id}-${st.id}-${theme}.png`;
        await page.locator("#frame").screenshot({ path: resolve(out, name) });
        console.log(name);
      }
    }
    await ctx.close();
  }
} finally {
  await browser.close();
  await server.close();
}

const img = (tr, st, theme) => `<figure><img src="${tr}-${st}-${theme}.png" alt="${tr} ${st} ${theme}" loading="lazy"><figcaption>${theme}</figcaption></figure>`;
const section = (tr) => `
<section id="${tr.id}">
  <h2>${tr.name}</h2>
  <p class="lede">${tr.lede}</p>
  ${STATES.map((st) => `<h3>${st.name}</h3><div class="pair${st.id === "narrow" ? " narrow" : ""}">${img(tr.id, st.id, "light")}${img(tr.id, st.id, "dark")}</div>`).join("\n  ")}
</section>`;
writeFileSync(resolve(out, "index.html"), `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decision “Other” answer</title>
<style>
:root{--bg:#f7f7f5;--ink:#1d1d1b;--ink2:#55554f;--line:#e2e1dc;--sap:#b26a00}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--ink:#ecebe6;--ink2:#a3a29b;--line:#2c2c29;--sap:#e0a040}}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Geist",sans-serif}
main{max-width:1500px;margin:0 auto;padding:32px 24px 80px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:19px;margin:48px 0 4px;padding-top:20px;border-top:1px solid var(--line)}
h3{font-size:13px;font-weight:600;color:var(--ink2);margin:22px 0 8px;text-transform:uppercase;letter-spacing:.06em}
.lede{color:var(--ink2);margin:0 0 6px;max-width:760px}
nav a{color:var(--ink2);margin-right:16px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}.pair.narrow{grid-template-columns:repeat(2,minmax(0,300px))}
figure{margin:0}img{width:100%;border:1px solid var(--line);border-radius:8px;display:block}
figcaption{font-size:12px;color:var(--ink2);margin-top:4px}
.note{border-left:2px solid var(--sap);padding:10px 14px;margin-top:48px;max-width:760px}
@media (max-width:900px){.pair{grid-template-columns:1fr}}
</style></head><body><main>
<h1>An “Other” answer on decision cards</h1>
<p class="lede">When none of the listed options fit, the person (or a teammate on a shared plan) writes their own answer. Each treatment is shown open, chosen while typing, saved as the team sees it (“You: Other: ‘only on federal holidays’ · David: B”), in the Approve bar, and at a narrow width. Rendered from the real card's classes and the app's tokens; no product code changed.</p>
<nav>${TREATMENTS.map((tr) => `<a href="#${tr.id}">${tr.name}</a>`).join("")}<a href="#none">None of these</a></nav>
${TREATMENTS.map(section).join("\n")}
<div class="note" id="none"><b>None of these, keep exploring.</b> If no treatment is right yet, say what's off (where Other sits, how loud it is, whether teammates can pick someone's own answer) and another round follows. Other directions not drawn here: Other as a comment that the supervisor must answer before Approve; or “Request changes” on just this decision, asking the supervisor for new options instead of a free-text answer.</div>
</main></body></html>
`);
console.log(resolve(out, "index.html"));
