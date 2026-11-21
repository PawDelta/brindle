#!/usr/bin/env node
/* node shots/panel-shots.mjs [--out <dir>] [--channel chrome]
   Production panels, DOM terminal renderer, and WCAG sRGB contrast with ancestor opacity.
   Text uses 4.5, large text (24px, or bold 18.667px) and UI marks use 3.
   Disabled controls are measured too; failures are evidence, never CSS changes. */
import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import { createServer } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const out = resolve(option("--out", resolve(homedir(), "Documents/brindle-design-docs/theme-audit/panels")));

// Runs in the page so computed styles include the theme, xterm palette, and CSS color-mix.
function measure() {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const parse = (css) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = css;
    ctx.fillRect(0, 0, 1, 1);
    const p = ctx.getImageData(0, 0, 1, 1).data;
    return [p[0], p[1], p[2], p[3] / 255];
  };
  const over = (a, b) => {
    const alpha = a[3] + b[3] * (1 - a[3]);
    return alpha ? [0, 1, 2].map((i) => (a[i] * a[3] + b[i] * b[3] * (1 - a[3])) / alpha).concat(alpha) : [0, 0, 0, 0];
  };
  const luminance = (p) => p.slice(0, 3).map((v) => v / 255).map((v) => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
  const color = (p) => `rgb(${p.slice(0, 3).map((v) => Math.round(v)).join(", ")})`;
  const selector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.hasAttribute("data-testid")) return `[data-testid="${el.getAttribute("data-testid")}"]`;
    if (!el.parentElement) return el.localName;
    return `${selector(el.parentElement)} > ${el.localName}:nth-child(${[...el.parentElement.children].indexOf(el) + 1})`;
  };
  const visible = (el, rect) => {
    if (!rect.width || !rect.height || !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    let left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
    let top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
    for (let p = el.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p), r = p.getBoundingClientRect();
      if (s.overflowX !== "visible") { left = Math.max(left, r.left); right = Math.min(right, r.right); }
      if (s.overflowY !== "visible") { top = Math.max(top, r.top); bottom = Math.min(bottom, r.bottom); }
    }
    if (left >= right || top >= bottom) return false;
    const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    return !!hit && (el.contains(hit) || hit.contains(el));
  };
  const pairs = [];
  const record = (el, css, kind, text, style = getComputedStyle(el)) => {
    const ink = parse(css);
    if (kind === "placeholder") ink[3] *= Number(style.opacity);
    if (kind === "ui" && el instanceof SVGElement) ink[3] *= Number(css === style.stroke ? style.strokeOpacity : style.fillOpacity);
    if (!ink[3]) return;
    let foreground = ink, background = [0, 0, 0, 0];
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.backgroundImage !== "none") throw new Error(`Background image needs pixel sampling: ${selector(p)}`);
      const bg = parse(s.backgroundColor);
      foreground = over(foreground, bg);
      background = over(background, bg);
      const opacity = Number(s.opacity);
      foreground[3] *= opacity;
      background[3] *= opacity;
    }
    foreground = over(foreground, [255, 255, 255, 1]);
    background = over(background, [255, 255, 255, 1]);
    const a = luminance(foreground), b = luminance(background);
    const ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    const large = parseFloat(style.fontSize) >= 24 || (parseFloat(style.fontSize) >= 18.667 && Number(style.fontWeight) >= 700);
    const threshold = kind === "ui" || large ? 3 : 4.5;
    pairs.push({ selector: selector(el), kind, text, foreground: color(foreground), background: color(background), cssColor: css, ratio, threshold, pass: ratio >= threshold });
  };
  for (const el of document.querySelectorAll("body *")) {
    if (["SCRIPT", "STYLE", "OPTION", "TEXTAREA"].includes(el.tagName) || el.closest(".xterm-helpers")) continue;
    const style = getComputedStyle(el);
    for (const node of el.childNodes) {
      if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      if ([...range.getClientRects()].some((r) => visible(el, r))) record(el, style.color, "text", node.textContent.trim());
    }
    if (el.matches("input, select, textarea") && visible(el, el.getBoundingClientRect())) {
      const text = el.matches("select") ? el.selectedOptions[0]?.textContent : el.value;
      if (text) record(el, style.color, "text", text);
      else if (el.placeholder) { const pseudo = getComputedStyle(el, "::placeholder"); record(el, pseudo.color, "placeholder", el.placeholder, pseudo); }
    }
    if (el.matches("svg path, svg circle, svg polyline, svg line") && visible(el, el.getBoundingClientRect())) {
      for (const css of new Set([style.stroke, style.fill])) if (css !== "none") record(el, css, "ui", el.closest("button")?.getAttribute("aria-label") ?? "icon");
    }
    if (el.matches("button, input, select, [role=tab]") && visible(el, el.getBoundingClientRect())) {
      for (const side of ["Top", "Right", "Bottom", "Left"]) {
        if (parseFloat(style[`border${side}Width`]) && style[`border${side}Style`] !== "none") record(el, style[`border${side}Color`], "ui", `border-${side.toLowerCase()}`);
      }
    }
  }
  return pairs;
}

const server = await createServer({ root: resolve(here, ".."), configFile: resolve(here, "..", "vite.config.ts"), logLevel: "warn", server: { port: 4195, strictPort: false, host: "127.0.0.1", open: false } });
await server.listen();
let browser;
try {
  browser = await chromium.launch({ ...(args.includes("--channel") ? { channel: option("--channel") } : {}) });
  // An executable check of the auditor itself, before collecting production evidence.
  const calibration = await browser.newPage();
  await calibration.setContent('<body style="margin:0;background:white"><div style="opacity:.5"><span id="alpha" style="color:rgba(0,0,0,.5)">Alpha</span></div><div style="background:black;opacity:.5"><span id="group" style="color:white">Group</span></div><span id="large" style="color:black;font-size:24px">Large</span></body>');
  const checks = await calibration.evaluate(measure);
  const alpha = checks.find((p) => p.selector === "#alpha");
  const group = checks.find((p) => p.selector === "#group");
  const large = checks.find((p) => p.selector === "#large");
  if (alpha?.foreground !== "rgb(191, 191, 191)" || alpha.background !== "rgb(255, 255, 255)" || Math.abs(alpha.ratio - 1.84) > .02
    || group?.foreground !== "rgb(255, 255, 255)" || group.background !== "rgb(128, 128, 128)" || Math.abs(group.ratio - 3.98) > .02
    || large?.ratio !== 21 || large.threshold !== 3) throw new Error("Contrast calibration failed");
  await calibration.close();
  mkdirSync(out, { recursive: true });
  const results = { method: "Computed DOM colors, alpha and ancestor group opacity composited in sRGB; WCAG 2 contrast; DOM xterm renderer includes its own contrast adjustment. Native select popup options are outside the page and excluded. UI borders are measured against their inner background.", renders: [] };
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme });
    // xterm's production DOM fallback exposes every rendered cell's real colors for measurement.
    await context.addInitScript(() => {
      const original = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
        if (kind.includes("webgl")) return null;
        return original.call(this, kind, ...rest);
      };
    });
    for (const state of ["terminal", "people", "search", "scopes", "reviewer"]) {
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      const panel = state === "terminal" ? "terminal" : "share";
      await page.goto(`${server.resolvedUrls.local[0]}shots/panels.html?theme=${theme}&panel=${panel}&state=${state}`);
      await page.waitForSelector("html[data-ready]");
      if (state === "terminal") await page.getByTestId("terminal").locator(".xterm-rows").getByText("Build complete", { exact: false }).waitFor();
      if (state === "search") { await page.getByTestId("share-search").focus(); await page.getByTestId("share-result-lee").waitFor(); }
      if (state === "scopes") { await page.getByRole("button", { name: "Share for review", exact: true }).click(); await page.getByLabel("Share with", { exact: true }).waitFor(); }
      if (state !== "terminal" && state !== "reviewer") await page.getByLabel("Link audience").locator("option").filter({ hasText: "Platform engineering" }).waitFor({ state: "attached" });
      await page.evaluate(() => document.fonts.ready);
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      if (errors.length) throw new Error(errors.join("\n"));
      const pairs = await page.evaluate(measure);
      if (!pairs.length || (state === "terminal" && !pairs.some((p) => p.selector.includes("xterm") || p.text.includes("Build complete")))) throw new Error(`No measured content for ${state}`);
      const name = state === "terminal" ? "terminal-panel" : state === "people" ? "share-panel" : `share-panel-${state}`;
      const filename = `${name}-${theme}.png`;
      await page.screenshot({ path: resolve(out, filename) });
      const failures = pairs.filter((p) => !p.pass);
      results.renders.push({ panel, state, theme, filename, pairs, failures });
      for (const p of failures) console.log(`${filename} ${p.selector} ${p.kind} ${p.foreground} / ${p.background}: ${p.ratio.toFixed(2)}:1 (needs ${p.threshold}:1)`);
      console.log(`${filename}: ${pairs.length} pairs, ${failures.length} failures`);
      await page.close();
    }
    await context.close();
  }
  writeFileSync(resolve(out, "results.json"), JSON.stringify(results, null, 2) + "\n");
} finally {
  await browser?.close();
  await server.close();
}
