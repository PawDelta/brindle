import { describe, expect, it } from "vitest";
import { keptLink, READ_SIZE, readPane } from "./paneText";
import type { PtyViewHandlers, PtyViewOpener, PtyViewTarget } from "./ptyView";

/* An opener recording the one view it opens, the bytes it sends played by the test. */
function opener() {
  const got: { at?: PtyViewTarget; size?: { cols: number; rows: number }; handlers?: PtyViewHandlers; closed: boolean } = { closed: false };
  const open: PtyViewOpener = (at, size, handlers) => {
    Object.assign(got, { at, size, handlers });
    return { write() {}, resize() {}, focus() {}, close: () => void (got.closed = true) };
  };
  const draw = (text: string) => got.handlers!.onData(new TextEncoder().encode(text));
  return { open, got, draw };
}

/* Until xterm.js has parsed what was sent (it parses on a timer) and the reader has read it back. */
const parsed = () => new Promise((r) => setTimeout(r, 20));

const at = { server: "brindle", target: "%7" };

describe("readPane", () => {
  it("reads the pane's screen as a tmux client redraws it, a link drawn across rows read whole", async () => {
    const { open, got, draw } = opener();
    const texts: string[] = [];
    readPane(open, at, (text) => texts.push(text), () => {});
    expect(got.at).toEqual(at);
    expect(got.size).toEqual(READ_SIZE);

    // A redraw, as a client draws one: cursor moves, not newlines; a link longer than a row runs on into the next.
    const link = `https://claude.ai/oauth/authorize?code=true&client_id=${"x".repeat(READ_SIZE.cols)}&state=abc`;
    draw(`\x1b[H\x1b[2J\x1b[1;1HOpening your browser to sign in:\x1b[2;1H${link}\x1b[5;1HPaste code here if prompted > `);
    await parsed();
    // Rows 2 and 3 are the link's; row 4 is blank.
    expect(texts.at(-1)).toBe(`Opening your browser to sign in:\n${link}\n\nPaste code here if prompted > `);
    // Nothing new on screen: nothing read again.
    draw("\x1b[5;31H");
    await parsed();
    expect(texts).toHaveLength(1);
  });

  it("read at the width another client sizes the pane to, a link tmux redraws at that width reads whole", async () => {
    const { open, got, draw } = opener();
    const resizes: [number, number][] = [];
    const sized: PtyViewOpener = (at, size, handlers) => {
      const view = open(at, size, handlers);
      return { ...view, resize: (cols, rows) => resizes.push([cols, rows]) };
    };
    const texts: string[] = [];
    const reading = readPane(sized, at, (text) => texts.push(text), () => {});
    // An 80-column client shows the pane too (the details): tmux redraws the window at 80 columns,
    // each row put by a cursor move, so nothing marks the link's rows as one line but their width.
    const link = `https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a&state=${"s".repeat(100)}`;
    const redraw = `\x1b[H\x1b[2J\x1b[1;1HOpen this link:\x1b[2;1H${link.slice(0, 80)}\x1b[3;1H${link.slice(80, 160)}\x1b[4;1H${link.slice(160)}`;
    // Read at its own 200 columns, the rows read as separate lines: the link would be cut.
    draw(redraw);
    await parsed();
    expect(texts.at(-1)).toBe(`Open this link:\n${link.slice(0, 80)}\n${link.slice(80, 160)}\n${link.slice(160)}`);
    // Read at the details' size, its client asking tmux for it too: whole.
    reading.resize(80, 24);
    expect(resizes).toEqual([[80, 24]]);
    draw(redraw);
    await parsed();
    expect(texts.at(-1)).toBe(`Open this link:\n${link}`);
    // The same size again asks nothing; back to its own once the details go.
    reading.resize(80, 24);
    reading.resize(READ_SIZE.cols, READ_SIZE.rows);
    expect(resizes).toEqual([[80, 24], [READ_SIZE.cols, READ_SIZE.rows]]);
    reading.close();
    expect(got.closed).toBe(true);
  });

  it("ends once tmux says the pane is dead, or the client exits, and closes its view; says why one couldn't open", async () => {
    const dead = opener();
    const ends: (string | undefined)[] = [];
    readPane(dead.open, at, () => {}, (why) => ends.push(why));
    dead.draw("\x1b[H\x1b[2JSigned in.\r\n\r\nPane is dead (status 0, Tue Oct 20 09:12:00 2026)");
    await parsed();
    expect(ends).toEqual([undefined]);
    expect(dead.got.closed).toBe(true);
    // Once: nothing after the end is heard.
    dead.got.handlers!.onExit(0);
    expect(ends).toEqual([undefined]);

    const exits = opener();
    readPane(exits.open, at, () => {}, (why) => ends.push(why));
    exits.got.handlers!.onExit(0);
    expect(ends).toEqual([undefined, undefined]);

    const refused = opener();
    readPane(refused.open, at, () => {}, (why) => ends.push(why));
    refused.got.handlers!.onError("can't find pane: %7");
    expect(ends).toEqual([undefined, undefined, "can't find pane: %7"]);
    expect(refused.got.closed).toBe(true);
  });

  it("closed by its reader, reads nothing more", async () => {
    const { open, got, draw } = opener();
    const texts: string[] = [];
    const reading = readPane(open, at, (text) => texts.push(text), () => {});
    reading.close();
    expect(got.closed).toBe(true);
    draw("late");
    await parsed();
    expect(texts).toEqual([]);
  });
});

describe("keptLink", () => {
  it("keeps a link a redraw shows only the start of, and takes any other", () => {
    const link = "https://claude.ai/oauth/authorize?code=true&state=abc";
    expect(keptLink(null, link)).toBe(link);
    expect(keptLink(link, "https://claude.ai/oauth/authorize?code=tr")).toBe(link);
    expect(keptLink(link, link)).toBe(link);
    expect(keptLink(link, "https://claude.ai/other")).toBe("https://claude.ai/other");
    expect(keptLink("https://claude.ai/oauth", link)).toBe(link);
  });
});
