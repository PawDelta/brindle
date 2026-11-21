/* Theme fixtures shared by the pane and contrast tests: the app's own colour tokens (styles.css), and how xterm.js
   draws a row, read back from its renderer. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Terminal } from "@xterm/xterm";

const css = readFileSync(resolve(import.meta.dirname, "../styles.css"), "utf8");
const block = (selector: string) => css.slice(css.indexOf(`${selector} {`), css.indexOf("}", css.indexOf(`${selector} {`)) + 1);

/* The app's colour tokens, dark by default and light under data-theme="light", as a <style> the document carries
   until it is removed. */
export function appThemeSheet(): HTMLStyleElement {
  const style = document.createElement("style");
  style.textContent = block(":root") + block(':root[data-theme="light"]');
  document.head.append(style);
  return style;
}

/* A token's value as the current theme resolves it. */
export const token = (name: string): string => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* The OSC 11 answer tmux reads for a terminal whose ground is this hex colour. */
export const asGround = (hex: string): string => `\x1b]11;rgb:${[1, 3, 5].map((i) => hex.slice(i, i + 2).repeat(2)).join("/")}\x1b\\`;

export interface Core {
  _themeService: { colors: { background: { css: string }; foreground: { css: string }; ansi: { css: string }[] } };
  _renderService: { _renderer: { value: { _rowFactory: { createRow(...args: unknown[]): HTMLSpanElement[] }; _widthCache: unknown } } };
}

/* The colour xterm's DOM renderer draws each span of a row in: its inline colour where it set one (truecolour, or
   adjusted for contrast), else the palette slot its xterm-fg-N class names. */
export function drawnColours(terminal: Terminal, row: number): { text: string; css: string; ground: string; dim: boolean }[] {
  const core = (terminal as unknown as { _core: Core })._core;
  const renderer = core._renderService._renderer.value;
  const line = (terminal.buffer.active as unknown as { _buffer: { lines: { get(i: number): unknown } } })._buffer.lines.get(row);
  const spans = renderer._rowFactory.createRow(line, row, false, undefined, undefined, 0, false, 9, renderer._widthCache, -1, -1);
  return spans.filter(span => span.textContent!.trim()).map(span => {
    const style = span.getAttribute("style") ?? "";
    const inline = /(?:^|;)\s*color:\s*([^;]+)/.exec(style)?.[1].trim();
    const slot = /xterm-fg-(\d+)/.exec(span.className)?.[1];
    const ground = /background-color:\s*([^;]+)/.exec(style)?.[1].trim() ?? /xterm-bg-(\d+)/.exec(span.className)?.[1];
    return {
      text: span.textContent!,
      dim: span.classList.contains("xterm-dim"),
      css: inline ?? (slot === undefined ? core._themeService.colors.foreground.css : core._themeService.colors.ansi[Number(slot)].css),
      ground: ground === undefined ? core._themeService.colors.background.css : ground.startsWith("#") || ground.startsWith("rgb") ? ground : core._themeService.colors.ansi[Number(ground)].css,
    };
  });
}

function luminance(css: string): number {
  const channels = css.startsWith("#")
    ? [1, 3, 5].map(i => parseInt(css.slice(i, i + 2), 16))
    : css.match(/\d+/g)!.slice(0, 3).map(Number);
  const [r, g, b] = channels.map(c => c / 255)
    .map(c => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/* WCAG contrast ratio of two colours, each a hex or rgb() string. */
export const contrast = (a: string, b: string): number => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
