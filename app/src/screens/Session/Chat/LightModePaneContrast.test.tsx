import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
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
  _themeService: { colors: { background: { css: string }; ansi: { css: string }[] } };
  _renderService: { _renderer: { value: { _rowFactory: { createRow(...args: unknown[]): HTMLSpanElement[] }; _widthCache: unknown } } };
}

/* The colour xterm's DOM renderer draws each span of a row in: its inline colour where it set one (truecolour, or
   adjusted for contrast), else the palette slot its xterm-fg-N class names. */
function drawnColours(terminal: import("@xterm/xterm").Terminal, row: number): { text: string; css: string }[] {
  const core = (terminal as unknown as { _core: Core })._core;
  const renderer = core._renderService._renderer.value;
  const line = (terminal.buffer.active as unknown as { _buffer: { lines: { get(i: number): unknown } } })._buffer.lines.get(row);
  const spans = renderer._rowFactory.createRow(line, row, false, undefined, undefined, 0, false, 9, renderer._widthCache, -1, -1);
  return spans.filter(span => span.textContent!.trim()).map(span => {
    const inline = /(?:^|;)\s*color:\s*([^;]+)/.exec(span.getAttribute("style") ?? "")?.[1].trim();
    const slot = /xterm-fg-(\d+)/.exec(span.className)?.[1];
    return { text: span.textContent!, css: inline ?? core._themeService.colors.ansi[Number(slot)].css };
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

it("draws Claude's faint 256-colour and truecolour greys readably on the light pane", async () => {
  const props = {
    provider: "claude", prompt: { kind: "unknown" as const, rows: 0 }, composer: null,
    onDropFiles() {}, onComposeText() {},
    terminal: { at: { server: null, target: "%1" }, open: () => ({ write() {}, resize() {}, focus() {}, close() {} }) },
  };
  const view = render(<ChatPane {...props} theme="light" />);
  const terminal = terminals.at(-1)!;
  await act(() => new Promise<void>(resolve => terminal.write("\x1b[38;5;246mfaint\x1b[0m \x1b[38;2;153;153;153mgrey\x1b[0m", resolve)));
  const ground = "#ffffff";
  const drawn = drawnColours(terminal, 0);
  expect(drawn.map(d => d.text)).toEqual(["faint", "grey"]);
  for (const { css } of drawn) expect(contrast(css, ground)).toBeGreaterThanOrEqual(4.5);
  // Dark mode keeps the CLI's own colours untouched.
  view.rerender(<ChatPane {...props} theme="dark" />);
  expect(drawnColours(terminal, 0).map(d => d.css)).toEqual(["#949494", "rgb(153, 153, 153)"]);
});
