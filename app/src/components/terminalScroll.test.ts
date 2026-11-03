import { act, cleanup, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { PtyViewHandlers, PtyViewOpener } from "../api/ptyView";
import { PtyTerminal } from "./PtyTerminal";
import { GESTURE_GAP, notchPx, scrollByWheel, WheelLines, wheelReports } from "./terminalScroll";

/* The real xterm.js, recorded (as PtyTerminal.test.tsx). */
const xterms = vi.hoisted(() => [] as import("@xterm/xterm").Terminal[]);
vi.mock("@xterm/xterm", async (original) => {
  const real = await original<typeof import("@xterm/xterm")>();
  class Recorded extends real.Terminal {
    constructor(...args: ConstructorParameters<typeof real.Terminal>) {
      super(...args);
      xterms.push(this);
    }
  }
  return { ...real, Terminal: Recorded };
});

beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup();
  xterms.length = 0;
  vi.unstubAllGlobals();
});

const at = (deltaY: number, timeStamp: number, deltaMode = 0) => ({ deltaY, deltaMode, timeStamp });

it("counts a trackpad's pixels a line of text a report, keeping what's left of a line for the gesture's next event", () => {
  const lines = new WheelLines(40);
  // 16 px cells: 6 + 6 px is no line yet, 4 more makes one; 50 px more is three, 2 px left over.
  expect([lines.add(at(-6, 0), 16, 40), lines.add(at(-6, 8), 16, 40), lines.add(at(-4, 16), 16, 40)]).toEqual([0, 0, -1]);
  expect(lines.add(at(-50, 24), 16, 40)).toBe(-3);
  expect(lines.add(at(-14, 32), 16, 40)).toBe(-1);
  // xterm.js's own: one report an event at most, a pixel's worth scaled by 0.3 under 50 px: one report for these 80 px.
});

it("sends a report for each of a mouse notch's lines, one at least: WebKit's 40 px, Chromium's 100, a line or a page", () => {
  const webkit = new WheelLines(notchPx(false));
  expect(webkit.add(at(-40, 0), 16, 40)).toBe(-1);
  expect(webkit.add(at(-40, 300), 16, 40)).toBe(-1);
  // Spun: macOS's accelerated deltaY (4 lines), 160 px.
  expect(webkit.add(at(-160, 316), 16, 40)).toBe(-4);
  expect(webkit.add(at(80, 600), 16, 40)).toBe(2);
  expect(new WheelLines(notchPx(true)).add(at(100, 0), 16, 40)).toBe(1);
  expect(new WheelLines(40).add(at(-3, 0, 1), 16, 40)).toBe(-3);
  expect(new WheelLines(40).add(at(0.2, 0, 1), 16, 40)).toBe(1);
  expect(new WheelLines(40).add(at(1, 0, 2), 16, 40)).toBe(40);
});

it("encodes wheel reports as SGR, xterm.js's own way, the modifiers held", () => {
  expect(wheelReports(true, 2, 10, 5)).toBe("\x1b[<64;10;5M\x1b[<64;10;5M");
  expect(wheelReports(false, 1, 1, 1, { shiftKey: true, altKey: true, ctrlKey: true })).toBe("\x1b[<93;1;1M");
});

/* An xterm.js with tmux's mouse on (button events, SGR), its screen 800 x 400 px: 80 x 25 cells of 10 x 16. */
async function mouseTerm() {
  const { Terminal } = await import("@xterm/xterm");
  const term = new Terminal({ cols: 80, rows: 25 });
  const host = document.createElement("div");
  document.body.append(host);
  term.open(host);
  await new Promise<void>((r) => term.write("\x1b[?1000h\x1b[?1006h", r));
  const screen = term.element!.querySelector(".xterm-screen")!;
  screen.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400, x: 0, y: 0, toJSON() {} });
  return { term, screen };
}

/* A frame at a time, as the test says: what `scrollByWheel` asks to run at the next paint. */
function frames() {
  const booked: (() => void)[] = [];
  return { frame: (step: () => void) => booked.push(step), paint: () => booked.splice(0).forEach((step) => step()), booked };
}

/* What xterm.js's own wheel would see, at the given point of its screen; and whether its default was prevented. */
function wheel(target: Element, deltaY: number, x = 405, y = 85): boolean {
  const ev = new WheelEvent("wheel", { deltaY, deltaMode: 0, clientX: x, clientY: y, bubbles: true, cancelable: true });
  target.dispatchEvent(ev);
  return ev.defaultPrevented;
}

it("sends a gesture's first reports at once, then at most one write a frame with all that came since", async () => {
  const { term, screen } = await mouseTerm();
  const written: string[] = [];
  const xtermOwn: string[] = [];
  term.onData((d) => xtermOwn.push(d));
  const { frame, paint, booked } = frames();
  const scroll = scrollByWheel(term, (d) => written.push(d), frame);
  // 20 px up over cell 41, 6: a line, sent as it comes, at the cell under the pointer.
  expect(wheel(screen, -20)).toBe(true);
  expect(written).toEqual([wheelReports(true, 1, 41, 6)]);
  // Two events within the frame (a 120 Hz trackpad's): nothing until it is painted, then one write of their four lines.
  wheel(screen, -30);
  wheel(screen, -30);
  expect(written).toHaveLength(1);
  paint();
  expect(written).toEqual([wheelReports(true, 1, 41, 6), wheelReports(true, 4, 41, 6)]);
  // A frame with nothing: no write, and no frame booked until the next event.
  paint();
  expect(booked).toHaveLength(0);
  expect(written).toHaveLength(2);
  // xterm.js's own reports never went.
  expect(xtermOwn).toEqual([]);
  scroll.dispose();
  term.dispose();
});

it("turning the other way within a frame drops what hadn't gone the first way; never more than a screen's rows of reports at once", async () => {
  const { term, screen } = await mouseTerm();
  const written: string[] = [];
  const { frame, paint } = frames();
  scrollByWheel(term, (d) => written.push(d), frame);
  wheel(screen, -16);
  wheel(screen, -48);
  wheel(screen, 32);
  paint();
  expect(written).toEqual([wheelReports(true, 1, 41, 6), wheelReports(false, 2, 41, 6)]);
  wheel(screen, -16 * 400);
  paint();
  expect(written.at(-1)).toBe(wheelReports(true, 25, 41, 6));
  term.dispose();
});

