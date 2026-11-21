import { Terminal } from "@xterm/xterm";
import { expect, it } from "vitest";
import { TextContrast } from "./textContrast";

/* The cell xterm.js holds at row 0, column `x` once `chunks` went through the lift (split as given): its foreground
   as xterm.js keeps it, and whether it draws faint. */
async function cellAfter(x: number, ...chunks: string[]) {
  const lift = new TextContrast(() => ({ theme: { background: "#ffffff", foreground: "#0e1716" }, ratio: 4.5 }));
  const term = new Terminal({ allowProposedApi: true });
  for (const chunk of chunks) await new Promise<void>((done) => term.write(lift.feed(new TextEncoder().encode(chunk)), done));
  const cell = term.buffer.active.getLine(term.buffer.active.cursorY)!.getCell(x)!;
  const drawn = { rgb: cell.isFgRGB(), palette: cell.isFgPalette(), colour: cell.getFgColor(), dim: cell.isDim() !== 0, bold: cell.isBold() !== 0 };
  term.dispose();
  return drawn;
}

// xterm.js's saved attributes go with the active screen buffer, are cleared by a soft reset (DECSTR) and are the
// defaults where nothing was saved: the lift follows all three, so the faint X after each lands in the colour the same
// text gets with no history, never one lifted from a grey xterm.js no longer holds.
it.each([
  ["a soft reset clears what was saved", "\x1b[38;5;246m\x1b7\x1b[!p\x1b8", ""],
  ["a restore with nothing saved gives the defaults", "\x1b[38;5;246m\x1b8", ""],
  ["the alternate buffer's save is its own", "\x1b[38;5;246m\x1b[?1049h\x1b[39m\x1b7\x1b[?1049l", "\x1b[38;5;246m"],
])("%s", async (_case, history, control) => {
  expect(await cellAfter(0, history, "\x1b[2mX")).toEqual(await cellAfter(0, control, "\x1b[2mX"));
});

// Faint text is drawn in a colour that reaches the ratio, not at xterm.js's half strength; bold survives the SGR 22
// that takes faint off.
it("draws faint text in a readable colour, keeping bold", async () => {
  const cell = await cellAfter(0, "\x1b[1;2mX");
  expect([cell.dim, cell.bold, cell.rgb]).toEqual([false, true, true]);
});
