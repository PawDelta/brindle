import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { groundLuminance, parseCss, toHsl } from "../../../components/textContrast";
import { ChatPane } from "./ChatPane";

const terminals = vi.hoisted(() => [] as import("@xterm/xterm").Terminal[]);
vi.mock("@xterm/xterm", async (original) => {
  const real = await original<typeof import("@xterm/xterm")>();
  class Recorded extends real.Terminal {
    constructor(...args: ConstructorParameters<typeof real.Terminal>) {
      super(...args);
      terminals.push(this);
    }
  }
  return { ...real, Terminal: Recorded };
});

beforeEach(() => {
  document.documentElement.dataset.theme = "light";
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup();
  terminals.length = 0;
  delete document.documentElement.dataset.theme;
  vi.unstubAllGlobals();
});

interface Core {
  _themeService: { colors: { background: { css: string }; foreground: { css: string }; ansi: { css: string }[] } };
  _renderService: { _renderer: { value: { _rowFactory: { createRow(...args: unknown[]): HTMLSpanElement[] }; _widthCache: unknown } } };
}

/* The colour xterm's DOM renderer draws each span of a row in: its inline colour where it set one (truecolour, or
   adjusted for contrast), else the palette slot its xterm-fg-N class names. */
function drawnColours(terminal: import("@xterm/xterm").Terminal, row: number): { text: string; css: string; ground: string; dim: boolean }[] {
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
const contrast = (a: string, b: string) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

/* Claude Code's light diff (2.1.330's colour-diff: its light theme's line and word grounds, GitHub's syntax colours,
   each token as a foreground SGR then a background SGR), in truecolour and as the 256-colour slots it sends without
   it (chalk's rgbToAnsi256, as captured from live panes: its dark grounds arrive as 65, 72, 95, 174): a removed line
   with its changed number, then the added one. */
const fg = (c: string) => `\x1b[38;${c}m`;
const bg = (c: string) => `\x1b[48;${c}m`;
const LIGHT_DIFF = {
  truecolour: { dec: "2;207;34;46", key: "2;167;29;93", ink: "2;51;51;51", num: "2;0;134;179", del: "2;255;168;180", delWord: "2;209;69;75", addDec: "2;36;138;61", add: "2;105;219;124", addWord: "2;47;157;68" },
  "256-colour": { dec: "5;167", key: "5;132", ink: "5;236", num: "5;38", del: "5;218", delWord: "5;167", addDec: "5;71", add: "5;114", addWord: "5;71" },
};
/* A ground reads as remove (red, not pink: within 15° of 0°) or add (green) by its hue. */
const reads = (css: string) => {
  const [h, s] = toHsl(parseCss(css)!);
  return s < 0.15 ? "grey" : h >= 345 || h < 15 ? "red" : h >= 80 && h < 165 ? "green" : h >= 270 ? "pink" : "other";
};

function lightDiff(c: typeof LIGHT_DIFF.truecolour): string {
  return [
    `${fg(c.dec)}${bg(c.del)}-${fg(c.key)}${bg(c.del)}const${fg(c.ink)}${bg(c.del)} min = ${fg(c.num)}${bg(c.delWord)}200${fg(c.ink)}${bg(c.del)};\x1b[0m`,
    `${fg(c.addDec)}${bg(c.add)}+${fg(c.key)}${bg(c.add)}const${fg(c.ink)}${bg(c.add)} min = ${fg(c.num)}${bg(c.addWord)}240${fg(c.ink)}${bg(c.add)};\x1b[0m`,
  ].join("\r\n");
}

/* The colour a "5;n" or "2;r;g;b" SGR colour names, as xterm draws it. */
function named(terminal: import("@xterm/xterm").Terminal, colour: string): string {
  const [kind, ...v] = colour.split(";").map(Number);
  return kind === 5 ? (terminal as unknown as { _core: Core })._core._themeService.colors.ansi[v[0]].css : `rgb(${v.join(", ")})`;
}

it("draws Claude's faint 256-colour and truecolour greys readably on the light pane", async () => {
  let pane: import("../../../api/ptyView").PtyViewHandlers | null = null;
  const props = {
    provider: "claude", prompt: { kind: "unknown" as const, rows: 0 }, composer: null,
    onDropFiles() {}, onComposeText() {},
    terminal: { at: { server: null, target: "%1" }, open: ((_at, _size, handlers) => { pane = handlers; return { write() {}, resize() {}, focus() {}, close() {} }; }) as import("../../../api/ptyView").PtyViewOpener },
  };
  const view = render(<ChatPane {...props} theme="light" />);
  const terminal = terminals.at(-1)!;
  // What the pane sends, through the view as it arrives (split anywhere), then xterm's write queue drained.
  const send = async (...chunks: string[]) => {
    for (const chunk of chunks) pane!.onData(new TextEncoder().encode(chunk));
    await act(() => new Promise<void>(resolve => terminal.write("", resolve)));
  };
  // Light: xterm.js's own floor is off; the pane's stream lifts every cell's text by its lightness.
  expect(terminal.options.minimumContrastRatio).toBe(1);
  // Claude's greys and orange on the field, and faint text, which is drawn in a colour rather than at half strength.
  await send("\x1b[38;5;246mfaint\x1b[0m \x1b[38;2;153;1", "53;153mgrey\x1b[0m \x1b[38;5;174morange\x1b[0m \x1b[2mdim\x1b[22m");
  const ground = "#ffffff";
  const drawn = drawnColours(terminal, 0);
  expect(drawn.map(d => [d.text.trim(), d.dim])).toEqual([["faint", false], ["grey", false], ["orange", false], ["dim", false]]);
  for (const { css } of drawn) expect(contrast(css, ground)).toBeGreaterThanOrEqual(4.5);
  // Claude's light diff: its grounds as sent; every token at 4.5 on its ground, in its own hue; keyword, text and
  // number still three colours on each ground, so the syntax still reads.
  const hue = (css: string) => toHsl(parseCss(css)!);
  for (const [form, c] of Object.entries(LIGHT_DIFF)) {
    const sent = lightDiff(c);
    const half = sent.indexOf("200") - 4; // mid-SGR, before the removed word's ground
    await send("\x1b[2J\x1b[H", sent.slice(0, half), sent.slice(half));
    const expected = [
      [[c.dec, c.del], [c.key, c.del], [c.ink, c.del], [c.num, c.delWord], [c.ink, c.del]],
      [[c.addDec, c.add], [c.key, c.add], [c.ink, c.add], [c.num, c.addWord], [c.ink, c.add]],
    ];
    for (const row of [0, 1]) {
      const drawn = drawnColours(terminal, row);
      // The line's ground as sent, red on the removed line and green on the added one.
      const line = named(terminal, expected[row][0][1]);
      expect(drawn.filter((_, i) => i !== 3).map(d => d.ground), `${form} row ${row} line grounds`).toEqual(Array(4).fill(line));
      expect(reads(line), `${form} row ${row} line`).toBe(row === 0 ? "red" : "green");
      // The changed word's mid-tone ground drawn lighter in its own hue: still red or green, and darker than its line.
      const [word, sentWord] = [drawn[3].ground, named(terminal, expected[row][3][1])];
      expect(reads(word), `${form} row ${row} word`).toBe(row === 0 ? "red" : "green");
      expect(Math.abs(((hue(word)[0] - hue(sentWord)[0] + 540) % 360) - 180), `${form} row ${row} word hue`).toBeLessThan(6);
      // A removed word's lighter ground stays a red, not a pastel pink.
      if (row === 0) expect(hue(word)[1], `${form} word saturation`).toBeGreaterThanOrEqual(0.8);
      expect(groundLuminance(parseCss(word)!)).toBeLessThan(groundLuminance(parseCss(line)!));
      expect(contrast(word, line), `${form} row ${row} word against its line`).toBeGreaterThan(1.1);
      // Every token at 4.5 on its ground, in its own hue.
      expected[row].forEach(([text], i) => {
        const [sentHue, sentSat] = hue(named(terminal, text));
        expect(contrast(drawn[i].css, drawn[i].ground), `${form} ${drawn[i].text}`).toBeGreaterThanOrEqual(4.5);
        if (sentSat > 0.2) expect(Math.abs(((hue(drawn[i].css)[0] - sentHue + 540) % 360) - 180), `${form} ${drawn[i].text} hue`).toBeLessThan(12);
      });
      const [, key, ink] = drawn;
      expect(new Set([key.css, ink.css]).size, `${form} row ${row} keyword vs text`).toBe(2);
    }
  }
  // Dark mode lifts every cell against its own ground, and keeps the CLI's own colours where they already read.
  view.rerender(<ChatPane {...props} theme="dark" />);
  expect(terminal.options.minimumContrastRatio).toBe(4.5);
  await send("\x1b[2J\x1b[H\x1b[38;5;246mfaint\x1b[0m \x1b[38;2;153;153;153mgrey\x1b[0m");
  expect(drawnColours(terminal, 0).map(d => d.css)).toEqual(["#949494", "rgb(153, 153, 153)"]);
  // Claude Code's dark diff, as it sends it in brindle's panes: an added line on slot 65 with its changed words on
  // 72, a removed word on 174, in the default text colour. As sent these measure 3.5, 2.3 and 2.3 to 1.
  await send("\r\n\x1b[48;5;65m const \x1b[48;5;72mhi\x1b[48;5;65m = 1;\x1b[48;5;174mlo\x1b[48;5;95m gone \x1b[49m");
  const diff = drawnColours(terminal, 1);
  expect(diff.map(d => d.text.trim())).toEqual(["const", "hi", "= 1;", "lo", "gone"]);
  for (const { text, css, ground } of diff) expect(contrast(css, ground), text).toBeGreaterThanOrEqual(4.5);
  // Its slot grounds read as add and remove: green for the added line and word, red for the removed.
  expect(diff.map(d => reads(d.ground))).toEqual(["green", "green", "green", "red", "red"]);
});
