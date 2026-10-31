import { act, cleanup, render } from "@testing-library/react";
import type { ILink, ILinkProvider } from "@xterm/xterm";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { PtyViewHandlers, PtyViewOpener } from "../api/ptyView";
import { COPIED_SHOWN, COPY_KEY, COPY_OR_INTERRUPT, PASTE_WAIT, PtyTerminal, selectionIn } from "./PtyTerminal";

/* The real xterm.js, recorded to read its buffer (as Terminal.test.tsx). */
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

/* `write`: how the app's clipboard takes a copy (settled, or failed where it can't be written). */
function mount(paste?: () => Promise<string>, write: (text: string) => Promise<void> = () => Promise.resolve()) {
  const written: string[] = [];
  const copied: string[] = [];
  const opened: { at: unknown; handlers?: PtyViewHandlers; closed: boolean; focused: number } = { at: null, closed: false, focused: 0 };
  const open: PtyViewOpener = (at, _size, handlers) => {
    opened.at = at;
    opened.handlers = handlers;
    return { write: (d) => written.push(d), resize() {}, focus: () => void opened.focused++, close: () => void (opened.closed = true) };
  };
  const clipboard = { text: "" };
  const view = render(<PtyTerminal at={{ server: "brindle", target: "s:@1" }} open={open} copy={(text) => (copied.push(text), write(text))} paste={paste ?? (() => Promise.resolve(clipboard.text))} />);
  return { written, copied, clipboard, opened, view, term: () => xterms.at(-1)! };
}

const parsed = (term: import("@xterm/xterm").Terminal) => act(() => new Promise<void>((r) => term.write("", r)));

it("sends Caps Lock letters on keydown exactly once without invoking clipboard shortcuts", async () => {
  const { written, copied, term } = mount();
  const textarea = term().textarea!;
  const press = (type: string, key: string, keyCode = key.charCodeAt(0)) => {
    const e = new KeyboardEvent(type, { key, keyCode, charCode: type === "keypress" ? key.charCodeAt(0) : 0, bubbles: true, cancelable: true });
    Object.defineProperty(e, "getModifierState", { value: (name: string) => name === "CapsLock" });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  for (const key of "ACV") {
    expect(press("keydown", key).defaultPrevented).toBe(true);
    // A webview that also emits keypress must not send the same letter twice.
    press("keypress", key);
    press("keyup", key);
  }
  expect(written).toEqual(["A", "C", "V"]);
  expect(copied).toEqual([]);
  // A dead key survives its keyup; xterm must receive the composed letter
  // and clear its pending dead key before the following navigation key.
  press("keydown", "Dead", 222);
  press("keyup", "Dead", 222);
  // Protocol replies use onData too, but do not complete the pending dead key.
  act(() => term().write("\x1b[6n"));
  await parsed(term());
  press("keydown", "A");
  press("keypress", "Á");
  press("keyup", "A");
  press("keydown", "ArrowLeft", 37);
  expect(written).toEqual(["A", "C", "V", "\x1b[1;1R", "Á", "\x1b[D"]);
  press("keydown", "B");
  expect(written.at(-1)).toBe("B");
});

it("draws the client's bytes as they come and sends what xterm.js says, as is", async () => {
  const { written, opened, term } = mount();
  expect(opened.at).toEqual({ server: "brindle", target: "s:@1" });
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[2J\x1b[Hhello \x1b[1mtmux\x1b[0m")));
  await parsed(term());
  expect(term().buffer.active.getLine(0)?.translateToString(true)).toBe("hello tmux");
  // Keys, escape sequences and a mouse report: never turned into tmux key names, never filtered.
  act(() => {
    term().input("a", true);
    term().input("\x1b[A", true);
    term().input("\x1b[<64;10;5M", false);
  });
  expect(written).toEqual(["a", "\x1b[A", "\x1b[<64;10;5M"]);
});

it("puts an OSC 52 write on the app's clipboard, and never tells a program what the clipboard holds", async () => {
  const { written, copied, opened, term } = mount();
  const copy = btoa(String.fromCharCode(...new TextEncoder().encode("copied in tmux ✓")));
  act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b]52;c;${copy}\x07`)));
  await parsed(term());
  await act(() => new Promise((r) => setTimeout(r, 0)));
  expect(copied).toEqual(["copied in tmux ✓"]);
  // A program asks what's on the clipboard: the answer is empty.
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b]52;c;?\x07")));
  await parsed(term());
  await act(() => new Promise((r) => setTimeout(r, 0)));
  expect(written.every((w) => !/\x1b\]52;c;[^\x07]+\x07/.test(w))).toBe(true);
  expect(copied).toEqual(["copied in tmux ✓"]);
});

/* The webview's own paste (its Edit menu's, as ⌘V's default brings it), on xterm.js's textarea. */
function webviewPaste(target: EventTarget, text: string | null): Event {
  const e = new Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(e, "clipboardData", { value: text === null ? null : { getData: (type: string) => (type === "text/plain" ? text : "") } });
  act(() => void target.dispatchEvent(e));
  return e;
}

const settled = (ms = 0) => act(() => new Promise((r) => setTimeout(r, ms)));

it("off a Mac, ⌘V pastes the app's clipboard (bracketed where asked), and ⌘C copies xterm.js's selection onto it, taking the keys", async () => {
  const { written, copied, clipboard, opened, term } = mount();
  const textarea = term().textarea!;
  const press = (key: string) => {
    const e = new KeyboardEvent("keydown", { key, metaKey: true, bubbles: true, cancelable: true });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  // A program that asked for bracketed paste.
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?2004hhello pty")));
  await parsed(term());
  clipboard.text = "from the clipboard ✓";
  // jsdom's platform is no Mac's: ⌘V taken (no webview paste follows), the app's clipboard read.
  expect(press("v").defaultPrevented, "the webview's own paste doesn't follow").toBe(true);
  await settled();
  expect(written).toEqual(["\x1b[200~from the clipboard ✓\x1b[201~"]);

  term().select(0, 0, 5);
  expect(press("c").defaultPrevented).toBe(true);
  expect(copied).toEqual(["hello"]);
  // A paste can't end itself early: no escape in it survives (a marker, one nested in another, an 8-bit CSI's), so
  // its Enter stays inside the paste.
  clipboard.text = "ls\x1b[201~a\x1b[20\x1b[201~1~b\x9b201~rm -rf ~\n";
  press("v");
  await settled();
  expect(written.at(-1)).toBe("\x1b[200~ls[201~a[20[201~1~b201~rm -rf ~\r\x1b[201~");
  written.length = 1;
  // An empty clipboard pastes nothing.
  clipboard.text = "";
  press("v");
  await settled();
  expect(written).toEqual(["\x1b[200~from the clipboard ✓\x1b[201~"]);
});

it("keeps what's typed after ⌘V behind the paste while the clipboard is read, pastes in the order pressed, and drops a paste that can't be read", async () => {
  const reads: { ok(text: string): void; fail(): void }[] = [];
  const { written, opened, view, term } = mount(() => new Promise<string>((ok, fail) => reads.push({ ok, fail: () => fail(new Error("no clipboard")) })));
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?2004h")));
  await parsed(term());
  const textarea = term().textarea!;
  const press = () => act(() => void textarea.dispatchEvent(new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true, cancelable: true })));
  press();
  act(() => term().input("\r", true));
  press();
  act(() => term().input("x", true));
  press();
  await settled();
  expect(reads).toHaveLength(3);
  // Nothing past the first ⌘V goes before its paste.
  expect(written).toEqual([]);
  // Read out of order: still sent in the order pressed; the read that fails is dropped, what's behind it goes on.
  reads[1].ok("second");
  reads[2].fail();
  await settled();
  expect(written).toEqual([]);
  reads[0].ok("first");
  await settled();
  expect(written).toEqual(["\x1b[200~first\x1b[201~", "\r", "\x1b[200~second\x1b[201~", "x"]);
  // The line empty again: typing goes straight out.
  act(() => term().input("y", true));
  expect(written.at(-1)).toBe("y");

  // Unmounted with a read pending: nothing more goes to the pane.
  press();
  act(() => term().input("z", true));
  view.unmount();
  reads[3].ok("late");
  await settled();
  expect(written.at(-1)).toBe("y");
});

it("drag-select then ⌘C copies the highlighted text: xterm.js's selection, or tmux's (its copy key, the copy back as OSC 52), never a key to the pane", async () => {
  const { written, copied, opened, term } = mount();
  const textarea = term().textarea!;
  const press = () => {
    const e = new KeyboardEvent("keydown", { key: "c", metaKey: true, bubbles: true, cancelable: true });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  // tmux's selection: a plain drag over a pane (tmux's mouse, copy mode). ⌘C asks tmux for it, the key taken.
  expect(press().defaultPrevented).toBe(true);
  expect(written).toEqual([COPY_KEY]);
  expect(copied).toEqual([]);
  // tmux copies it as it copies a drag's end: OSC 52 with no selection named (`52;;`), onto the app's clipboard.
  const b64 = (text: string) => btoa(String.fromCharCode(...new TextEncoder().encode(text)));
  act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b]52;;${b64("dragged in tmux ✓")}\x07`)));
  await parsed(term());
  await settled();
  expect(copied).toEqual(["dragged in tmux ✓"]);
  // A program's own drag (Claude Code's, the mouse its own) copies on release the same way, through tmux.
  act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b]52;;${b64("dragged in Claude Code")}\x07`)));
  await parsed(term());
  await settled();
  expect(copied.at(-1)).toBe("dragged in Claude Code");

  // xterm.js's own selection (Option-drag): ⌘C copies it, nothing to tmux.
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[2J\x1b[Hhighlighted text")));
  await parsed(term());
  act(() => term().select(0, 0, 11));
  expect(press().defaultPrevented).toBe(true);
  expect(copied.at(-1)).toBe("highlighted");
  expect(written).toEqual([COPY_KEY]);
});

it("select to copy: xterm.js's own selection (a drag, a double-click's word, a triple-click's line) goes onto the app's clipboard as the mouse lets go, no ⌘C", async () => {
  const { written, copied, opened, view, term } = mount();
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[2J\x1b[Hselect me please\r\nsecond line")));
  await parsed(term());
  const host = view.getByTestId("terminal");
  // The mouse pressed in the view, the selection made while it is held, let go (off the view, as a drag can end).
  const gesture = (select: () => void) => {
    act(() => void host.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    act(select);
    act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
  };
  gesture(() => term().select(0, 0, 9));
  expect(copied).toEqual(["select me"]);
  gesture(() => term().selectLines(1, 1));
  expect(copied.at(-1)).toBe("second line");
  // A click that selects nothing copies nothing; nor does a mouseup with no press in the view.
  gesture(() => term().clearSelection());
  act(() => term().select(0, 0, 6));
  act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
  expect(copied).toEqual(["select me", "second line"]);
  // Nothing goes to the pane for it.
  expect(written).toEqual([]);
  // Unmounted: a late let-go copies nothing.
  act(() => void host.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
  view.unmount();
  act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
  expect(copied).toHaveLength(2);
});

it("says \"Copied\" over the view for a moment whenever a copy reaches the app's clipboard: the program's (Claude Code's, through tmux, as OSC 52 with the mouse reported), tmux's, or xterm.js's selection let go", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const { written, copied, opened, view, term } = mount();
    const shownCopied = () => view.queryByTestId("terminal-copied");
    const b64 = (text: string) => btoa(String.fromCharCode(...new TextEncoder().encode(text)));
    // tmux reports the mouse (Claude Code has it): a drag is Claude Code's, which copies it on release with
    // `tmux load-buffer -w`, back to the view as OSC 52 with no selection named. No ⌘C, no Ctrl+C.
    act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1002h\x1b[?1006hselect me please")));
    await parsed(term());
    expect(term().modes.mouseTrackingMode).toBe("drag");
    expect(shownCopied()).toBeNull();
    act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b]52;;${b64("selected in Claude Code")}\x07`)));
    await parsed(term());
    await settled();
    expect(copied).toEqual(["selected in Claude Code"]);
    expect(shownCopied()?.textContent).toBe("Copied");
    expect(shownCopied()?.getAttribute("role")).toBe("status");
    // Gone after a moment.
    act(() => void vi.advanceTimersByTime(COPIED_SHOWN));
    expect(shownCopied()).toBeNull();
    // xterm.js's own selection (Option-drag) let go: copied, and said so.
    const host = view.getByTestId("terminal");
    act(() => void host.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    act(() => term().select(0, 0, 6));
    act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
    expect(copied.at(-1)).toBe("select");
    await settled();
    expect(shownCopied()?.textContent).toBe("Copied");
    // A second copy keeps it up its full time from then.
    act(() => void vi.advanceTimersByTime(COPIED_SHOWN - 100));
    act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b]52;;${b64("copied in tmux")}\x07`)));
    await parsed(term());
    await settled();
    act(() => void vi.advanceTimersByTime(200));
    expect(shownCopied()?.textContent).toBe("Copied");
    act(() => void vi.advanceTimersByTime(COPIED_SHOWN));
    expect(shownCopied()).toBeNull();
    // An empty copy says nothing; nothing went to the pane for any of it.
    act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b]52;;\x07")));
    await parsed(term());
    await settled();
    expect(shownCopied()).toBeNull();
    expect(written).toEqual([]);
  } finally {
    vi.useRealTimers();
  }
});

it("says nothing for a copy the app's clipboard couldn't take, and \"Copied\" only once one it could has landed", async () => {
  let fails = true;
  let land: () => void = () => {};
  const { copied, opened, view, term } = mount(undefined, () => (fails ? Promise.reject(new Error("no clipboard")) : new Promise<void>((ok) => (land = ok))));
  const shownCopied = () => view.queryByTestId("terminal-copied");
  const b64 = (text: string) => btoa(String.fromCharCode(...new TextEncoder().encode(text)));
  // The mouse reported, Claude Code's copy back through tmux as OSC 52; the write refused.
  act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b[?1002h\x1b[?1006h\x1b]52;;${b64("not copied")}\x07`)));
  await parsed(term());
  await settled();
  expect(copied).toEqual(["not copied"]);
  expect(shownCopied()).toBeNull();
  // xterm.js's selection let go, refused too: still nothing.
  act(() => opened.handlers!.onData(new TextEncoder().encode("select me")));
  await parsed(term());
  act(() => void view.getByTestId("terminal").dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
  act(() => term().select(0, 0, 6));
  act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
  await settled();
  expect(copied.at(-1)).toBe("select");
  expect(shownCopied()).toBeNull();
  // A write that works: nothing while it's out, "Copied" once the clipboard has it.
  fails = false;
  act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b]52;;${b64("copied")}\x07`)));
  await parsed(term());
  await settled();
  expect(copied.at(-1)).toBe("copied");
  expect(shownCopied()).toBeNull();
  await act(async () => land());
  expect(shownCopied()?.textContent).toBe("Copied");
});

it("select to copy with columns cropped: only the shown columns go onto the app's clipboard", async () => {
  const { copied, opened, view, term } = mount();
  act(() => opened.handlers!.onCrop!({ cols: 11, left: 11 }));
  act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b[2J\x1b[H${"side".padEnd(10)}│agent text`)));
  await parsed(term());
  const host = view.getByTestId("terminal");
  act(() => void host.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
  act(() => term().selectLines(0, 0));
  act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
  expect(copied).toEqual(["agent text"]);
});

it("Ctrl+C on Windows and Linux copies xterm.js's selection or tmux's, else is ^C (tmux's key decides); on a Mac it is always ^C", async () => {
  const ctrlC = (textarea: HTMLTextAreaElement) => {
    const e = new KeyboardEvent("keydown", { key: "c", keyCode: 67, ctrlKey: true, bubbles: true, cancelable: true });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("Win32");
  try {
    const { written, copied, opened, term } = mount();
    act(() => opened.handlers!.onData(new TextEncoder().encode("selected words")));
    await parsed(term());
    act(() => term().select(0, 0, 8));
    expect(ctrlC(term().textarea!).defaultPrevented).toBe(true);
    expect(copied).toEqual(["selected"]);
    expect(written).toEqual([]);
    // The copy clears the selection, as Windows Terminal's does. With none in xterm.js, tmux's key: it copies tmux's
    // copy-mode selection (leaving copy mode), or with none sends C-c to the pane (pty_view.rs, real tmux).
    expect(term().hasSelection()).toBe(false);
    expect(ctrlC(term().textarea!).defaultPrevented).toBe(true);
    expect(written).toEqual([COPY_OR_INTERRUPT]);
    // tmux had a selection: its copy comes back as OSC 52, onto the app's clipboard.
    act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b]52;;${btoa("in tmux")}\x07`)));
    await parsed(term());
    await settled();
    expect(copied).toEqual(["selected", "in tmux"]);
    cleanup();

    platform.mockReturnValue("MacIntel");
    const mac = mount();
    act(() => mac.opened.handlers!.onData(new TextEncoder().encode("selected words")));
    await parsed(mac.term());
    act(() => mac.term().select(0, 0, 8));
    ctrlC(mac.term().textarea!);
    expect(mac.written).toEqual(["\x03"]);
    expect(mac.copied).toEqual([]);
  } finally {
    platform.mockRestore();
  }
});

it("on a Mac, ⌘V pastes what the webview's own paste holds, in the ⌘V's place, once, without reading the app's clipboard", async (ctx) => {
  const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  ctx.onTestFinished(() => platform.mockRestore());
  let reads = 0;
  const { written, opened, view, term } = mount(() => {
    reads++;
    return Promise.resolve("the app's clipboard");
  });
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?2004h")));
  await parsed(term());
  const textarea = term().textarea!;
  const press = () => {
    const e = new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true, cancelable: true });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  // The first ⌘V's paste comes late, after the wait: pasted once, then; the app's clipboard never read for it.
  expect(press().defaultPrevented).toBe(false);
  await settled(PASTE_WAIT + 50);
  expect(written).toEqual([]);
  webviewPaste(textarea, "delayed");
  await settled();
  expect(written).toEqual(["\x1b[200~delayed\x1b[201~"]);
  expect(reads).toBe(0);
  written.length = 0;

  // ⌘V's default goes on: the webview's paste (its Edit menu's), taken from xterm.js, typing behind it.
  expect(press().defaultPrevented).toBe(false);
  act(() => term().input("x", true));
  expect(written).toEqual([]);
  const e = webviewPaste(textarea, "pasted ✓\nline two");
  expect(e.defaultPrevented, "nothing goes into the textarea for xterm.js to paste again").toBe(true);
  await settled();
  expect(written).toEqual(["\x1b[200~pasted ✓\rline two\x1b[201~", "x"]);
  await settled(PASTE_WAIT + 50);
  expect(reads).toBe(0);

  // Edit > Paste clicked (no ⌘V): pasted as it comes.
  webviewPaste(textarea, "from the menu");
  await settled();
  expect(written.at(-1)).toBe("\x1b[200~from the menu\x1b[201~");
  // A webview paste with no text: the app's clipboard instead.
  press();
  webviewPaste(textarea, null);
  await settled();
  expect(reads).toBe(1);
  expect(written.at(-1)).toBe("\x1b[200~the app's clipboard\x1b[201~");
  // A ⌘V whose paste hasn't come is given up (typing behind it let go), the app's clipboard not read in its place;
  // its paste, coming later, is pasted then, once.
  const count = written.length;
  press();
  act(() => term().input("y", true));
  await settled(PASTE_WAIT + 50);
  expect(written.slice(count)).toEqual(["y"]);
  expect(reads).toBe(1);
  webviewPaste(textarea, "late");
  await settled();
  expect(written.slice(count)).toEqual(["y", "\x1b[200~late\x1b[201~"]);
  // Unmounted while a ⌘V waits: nothing read for it.
  press();
  view.unmount();
  await settled(PASTE_WAIT + 50);
  expect(reads).toBe(1);
});

it("off a Mac, ⌘V pastes the app's clipboard, and an Edit > Paste after it pastes too, never dropped or doubled", async () => {
  const { written, clipboard, opened, term } = mount();
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?2004h")));
  await parsed(term());
  const textarea = term().textarea!;
  const press = () => {
    const e = new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true, cancelable: true });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  // ⌘V taken (no webview paste follows it to double it), the app's clipboard read at once.
  clipboard.text = "fallback";
  expect(press().defaultPrevented).toBe(true);
  await settled();
  expect(written).toEqual(["\x1b[200~fallback\x1b[201~"]);
  // Edit > Paste right after: its own paste, pasted.
  webviewPaste(textarea, "menu paste");
  await settled();
  expect(written).toEqual(["\x1b[200~fallback\x1b[201~", "\x1b[200~menu paste\x1b[201~"]);
  // And nothing more, however long on.
  await settled(PASTE_WAIT + 50);
  expect(written).toHaveLength(2);
});

it("pastes after a press anywhere in the view, its margin too: the press gives xterm.js the keyboard, so ⌘V's paste reaches the pane", async () => {
  const { written, opened, view, term } = mount();
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?2004h")));
  await parsed(term());
  // A press on the view outside xterm.js's own element (its margin, the strip under its last row, the mask): the
  // webview would leave the focus on the page, where the paste went, pasting nothing.
  const press = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
  act(() => void view.getByTestId("terminal").dispatchEvent(press));
  // ⌘V's paste, on whatever has the focus.
  webviewPaste(document.activeElement!, "pasted ✓");
  await settled();
  expect(written).toEqual(["\x1b[200~pasted ✓\x1b[201~"]);
  expect(press.defaultPrevented, "the press's default doesn't take the focus off the view again").toBe(true);
});

it("opens a link Ctrl-clicked (⌘-clicked on a Mac) in the system browser, a URL the pane prints or a program's OSC 8 hyperlink; a plain click opens nothing", async () => {
  const urls: string[] = [];
  let handlers: PtyViewHandlers | undefined;
  const open: PtyViewOpener = (_at, _size, h) => ((handlers = h), { write() {}, resize() {}, focus() {}, close() {} });
  render(<PtyTerminal at={{ server: "brindle", target: "s:@1" }} open={open} openUrl={(url) => (urls.push(url), Promise.resolve())} />);
  const term = xterms.at(-1)!;
  act(() => handlers!.onData(new TextEncoder().encode("see https://example.com/docs?a=1.\r\n\x1b]8;;https://example.com/osc\x07a link\x1b]8;;\x07")));
  await parsed(term);
  // The links xterm.js finds on a row (1-based), as hovering it would.
  const providers = (term as unknown as { _core: { _linkProviderService: { linkProviders: ILinkProvider[] } } })._core._linkProviderService.linkProviders;
  const linksOn = (y: number) => Promise.all(providers.map((p) => new Promise<ILink[]>((ok) => p.provideLinks(y, (l) => ok(l ?? []))))).then((l) => l.flat());
  const click = (link: ILink, mods: MouseEventInit = {}) => link.activate(new MouseEvent("mouseup", mods), link.text);
  const [url] = await linksOn(1);
  expect(url.text).toBe("https://example.com/docs?a=1");
  click(url);
  expect(urls).toEqual([]);
  click(url, { ctrlKey: true });
  const [osc] = await linksOn(2);
  click(osc, { ctrlKey: true });
  expect(urls).toEqual(["https://example.com/docs?a=1", "https://example.com/osc"]);
});

it("tells the view it was focused or clicked, so the window takes its size, typing nothing", () => {
  const { written, opened, view, term } = mount();
  act(() => term().focus());
  expect(opened.focused).toBe(1);
  // Clicked while focused already: no focus event, the click says it.
  act(() => void view.getByTestId("terminal").querySelector(".xterm-screen")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
  expect(opened.focused).toBe(2);
  expect(written).toEqual([]);
  view.unmount();
  term().textarea?.dispatchEvent(new FocusEvent("focus"));
  expect(opened.focused).toBe(2);
});

it("says its size once open and on each change, as its client asks tmux for it", () => {
  const sizes: [number, number][] = [];
  const resized: [number, number][] = [];
  const open: PtyViewOpener = () => ({ write() {}, resize: (c, r) => void resized.push([c, r]), focus() {}, close() {} });
  render(<PtyTerminal at={{ server: "brindle", target: "s:@1" }} open={open} onSize={(c, r) => sizes.push([c, r])} />);
  const term = xterms.at(-1)!;
  expect(sizes).toEqual([[term.cols, term.rows]]);
  act(() => term.resize(90, 30));
  expect(sizes.at(-1)).toEqual([90, 30]);
  expect(resized.at(-1)).toEqual([90, 30]);
});

it("copies a selection across rows only from the columns shown: never a cropped sidebar's or its border", async () => {
  const { opened, term } = mount();
  // A 10-column sidebar and its border at the left, the agent's rows after (columns 11 on).
  const row = (n: number) => `${`side ${n}`.padEnd(10)}│agent row ${n}`;
  act(() => opened.handlers!.onData(new TextEncoder().encode(`\x1b[2J\x1b[H${[1, 2, 3].map(row).join("\r\n")}`)));
  await parsed(term());
  // From "row 1" on the first row to "agent" on the third.
  act(() => term().select(17, 0, 2 * term().cols + 16 - 17));
  // xterm.js's own selection (what its copy takes) has the sidebar's columns of every row after the first.
  expect(term().getSelection()).toBe("row 1\nside 2    │agent row 2\nside 3    │agent");
  expect(selectionIn(term(), 11, 80)).toBe("row 1\nagent row 2\nagent");
  // Columns from 14 on hidden (a right sidebar's): no row has them, the ends kept where they are.
  expect(selectionIn(term(), 0, 14)).toBe("\nside 2    │age\nside 3    │age");
});

it("keeps no scrollback of its own (the history is tmux's) and closes the client when unmounted", () => {
  const { opened, view, term } = mount();
  expect(term().options.scrollback).toBe(0);
  view.unmount();
  expect(opened.closed).toBe(true);
});
