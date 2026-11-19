import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatPane } from "./ChatPane";
import sent from "./fixtures/claude-turn-4-idle.txt?raw";

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

it("keeps the light cursor visible while typing in Claude's prompt", async () => {
  render(<ChatPane provider="claude" theme="light" prompt={{ kind: "unknown", rows: 0 }} composer={null}
    onDropFiles={() => {}} onComposeText={() => {}}
    terminal={{ at: { server: null, target: "%1" }, open: (_at, _size, handlers) => ({
      write: text => handlers.onData(new TextEncoder().encode(text)), resize() {}, focus() {}, close() {},
    }) }} />);
  const input = screen.getByRole("textbox", { name: "Terminal input" });
  input.focus();
  fireEvent.keyDown(input, { key: "h", code: "KeyH", keyCode: 72 });
  const terminal = terminals.at(-1)!;
  await act(() => new Promise<void>(resolve => terminal.write("", resolve)));
  expect(terminal.buffer.active.getLine(0)!.translateToString(true)).toBe("h");
  const colours = (terminal as unknown as { _core: { _themeService: { colors: { cursor: { css: string }; cursorAccent: { css: string } } } } })._core._themeService.colors;
  expect(colours.cursor.css).toBe("#078a62");
  expect(colours.cursorAccent.css).toBe("#ffffff");
});

it("selects text on an opaque light ground the dark ink reads on, in light mode", () => {
  render(<ChatPane provider="claude" theme="light" prompt={{ kind: "unknown", rows: 0 }} composer={null}
    onDropFiles={() => {}} onComposeText={() => {}}
    terminal={{ at: { server: null, target: "%1" }, open: () => ({ write() {}, resize() {}, focus() {}, close() {} }) }} />);
  const terminal = terminals.at(-1)!;
  const theme = terminal.options.theme!;
  // Six-digit hex: no alpha for the WebGL renderer to draw near-solid.
  expect(theme.selectionBackground).toMatch(/^#[0-9a-f]{6}$/i);
  const lum = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(c => c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (lum(theme.selectionBackground!) + 0.05) / (lum(theme.foreground!) + 0.05);
  expect(ratio).toBeGreaterThanOrEqual(4.5);
  expect(lum(theme.selectionBackground!)).toBeGreaterThan(0.75);
});

it("renders captured sent-message cells on a light ground and restores their dark palette on switching", async () => {
  const props = {
    provider: "claude", prompt: { kind: "unknown" as const, rows: 0 }, composer: null,
    onDropFiles() {}, onComposeText() {},
    terminal: { at: { server: null, target: "%1" }, open: () => ({ write() {}, resize() {}, focus() {}, close() {} }) },
  };
  const view = render(<ChatPane {...props} theme="light" />);
  const terminal = terminals.at(-1)!;
  terminal.resize(80, 30);
  await act(() => new Promise<void>(resolve => terminal.write(sent.replace(/\n/g, "\r\n"), resolve)));
  const cell = terminal.buffer.active.getLine(14)!.getCell(0)!;
  expect(cell.getBgColor()).toBe(237);
  // Read the real renderer's resolved palette, not just the options passed to xterm.
  const colours = () => (terminal as unknown as { _core: { _themeService: { colors: { ansi: { css: string }[] } } } })._core._themeService.colors.ansi;
  expect(colours()[cell.getBgColor()].css).toBe("#e9eded");
  expect(colours()[231].css).toBe("#0e1716");
  view.rerender(<ChatPane {...props} theme="dark" />);
  expect(colours()[cell.getBgColor()].css).toBe("#3a3a3a");
  expect(colours()[231].css).toBe("#ffffff");
});
