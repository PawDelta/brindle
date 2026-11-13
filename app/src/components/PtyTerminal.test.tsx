import { act, cleanup, render } from "@testing-library/react";
import type { ILink, ILinkProvider } from "@xterm/xterm";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { PtyViewHandlers, PtyViewOpener } from "../api/ptyView";
import { DaemonClient } from "../api/client";
import { startupStep } from "../api/startup";
import { VisualLinkContext } from "../screens/Session/Visuals/link";
import { ATTACH_WAIT, COPIED_SHOWN, COPY_OR_INTERRUPT, LATE_PASTE_WINDOW, PASTE_WAIT, PtyTerminal, selectionIn } from "./PtyTerminal";

vi.mock("../api/startup", { spy: true });
const files = vi.hoisted(() => ({ read: vi.fn(), listen: vi.fn(), drop: (_event: unknown) => {} }));
vi.mock("../api/attachments", async (original) => ({
  ...await original<typeof import("../api/attachments")>(),
  clipboardAttachments: () => files.read(),
  listenForDrops: (handler: typeof files.drop) => { files.drop = handler; return files.listen(); },
}));
const native = vi.hoisted(() => ({ enabled: false }));
// The webview's, not the window's: the app's multi-webview feature makes the main webview a child, and Tauri sends a
// child's drags only to listeners on that webview. One listening on the window never hears them.
const webviewDrop = vi.hoisted(() => ({ handle: (_event: { payload: unknown }) => {} }));
vi.mock("@tauri-apps/api/webview", () => ({ getCurrentWebview: () => ({
  onDragDropEvent: async (handle: typeof webviewDrop.handle) => { webviewDrop.handle = handle; return () => {}; },
}) }));
vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => ({ onDragDropEvent: async () => () => {} }) }));
vi.mock("@tauri-apps/api/core", async original => ({
  ...await original<typeof import("@tauri-apps/api/core")>(),
  isTauri: () => native.enabled,
  invoke: async (command: string, args: { path: string }) => {
    if (command !== "pty_view_attachment_path") throw new Error("unexpected native command");
    return args.path.replace(/^C:\\/, "/windows/c/").replaceAll("\\", "/");
  },
}));

/* What goes to the debug log. */
const logs = vi.hoisted(() => [] as unknown[][]);
vi.mock("../api/debug", () => ({ logDebug: (...args: unknown[]) => void logs.push(args) }));

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
  files.read.mockResolvedValue([]);
  files.listen.mockResolvedValue(() => {});
  Object.defineProperty(document, "elementFromPoint", { configurable: true, value: (x: number, y: number) => x < 100 && y < 100 ? document.querySelector('[data-testid="terminal"]') : document.body });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  native.enabled = false;
  cleanup();
  xterms.length = 0;
  logs.length = 0;
  vi.unstubAllGlobals();
});

/* `write`: how the app's clipboard takes a copy (settled, or failed where it can't be written). */
function mount(paste?: () => Promise<string>, write: (text: string) => Promise<void> = () => Promise.resolve(), attachments?: import("./PtyTerminal").PtyTerminalProps["attachments"]) {
  const written: string[] = [];
  const copied: string[] = [];
  const opened: { at: unknown; handlers?: PtyViewHandlers; closed: boolean; focused: number } = { at: null, closed: false, focused: 0 };
  const open: PtyViewOpener = (at, _size, handlers) => {
    opened.at = at;
    opened.handlers = handlers;
    return { write: (d) => written.push(d), resize() {}, focus: () => void opened.focused++, close: () => void (opened.closed = true) };
  };
  const clipboard = { text: "" };
  const view = render(<PtyTerminal at={{ server: "brindle", target: "s:@1" }} open={open} copy={(text) => (copied.push(text), write(text))} paste={paste ?? (() => Promise.resolve(clipboard.text))} attachments={attachments} />);
  return { written, copied, clipboard, opened, view, term: () => xterms.at(-1)! };
}

const parsed = (term: import("@xterm/xterm").Terminal) => act(() => new Promise<void>((r) => term.write("", r)));

it("converts drag position to CSS coordinates per platform", async () => {
  const { listenForDrops } = await vi.importActual<typeof import("../api/attachments")>("../api/attachments");
  native.enabled = true;
  vi.stubGlobal("devicePixelRatio", 2);
  const platform = vi.spyOn(navigator, "platform", "get");
  for (const [os, x, y] of [["MacIntel", 160, 240], ["Linux x86_64", 160, 240], ["Win32", 80, 120]] as const) {
    platform.mockReturnValue(os);
    const received: unknown[] = [];
    const stop = await listenForDrops(drop => received.push(drop));
    webviewDrop.handle({ payload: { type: "drop", position: { x: 160, y: 240 }, paths: ["/photo.png"] } });
    expect(received).toEqual([{ type: "drop", x, y, paths: ["/photo.png"] }]);
    stop();
  }
  platform.mockRestore();
});

it("attaches native drops only under the cursor and keeps typing behind the returned paste, showing the refusal reason", async () => {
  let finish: (value: { text: string }) => void = () => {};
  const attach = vi.fn(() => new Promise<{ text: string }>(ok => { finish = ok; }));
  const { view, term, opened, written } = mount(undefined, undefined, { acceptsFiles: true, attach });
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
  await parsed(term());
  act(() => files.drop({ type: "over", x: 30, y: 40 }));
  expect(view.queryByTestId("terminal-drop")).not.toBeNull();
  act(() => files.drop({ type: "leave" }));
  expect(view.queryByTestId("terminal-drop")).toBeNull();
  act(() => files.drop({ type: "drop", x: 300, y: 40, paths: ["/outside"] }));
  await settled();
  expect(attach).not.toHaveBeenCalled();
  expect(view.queryByRole("alert")).toBeNull();
  // A drag over the view with no file behind it is told, not silently lost.
  act(() => files.drop({ type: "drop", x: 30, y: 40, paths: [] }));
  expect(view.queryByRole("alert")).not.toBeNull();
  expect(attach).not.toHaveBeenCalled();
  act(() => files.drop({ type: "drop", x: 30, y: 40, paths: ["/photo.png", "/notes.txt"] }));
  act(() => term().input("x", true));
  await settled();
  expect(written).toEqual([]);
  await act(async () => finish({ text: "@first" }));
  await settled();
  await act(async () => finish({ text: "@second" }));
  await settled();
  expect(written).toEqual(["\x1b[200~@first @second\x1b[201~", "x"]);
  const reason = "refused.png is a credential file and cannot be attached.";
  const otherReason = "missing.txt could not be found.";
  attach.mockResolvedValueOnce({ text: "@before" });
  attach.mockRejectedValueOnce(new Error(reason));
  attach.mockResolvedValueOnce({ text: "@after" });
  attach.mockRejectedValueOnce(otherReason);
  act(() => files.drop({ type: "drop", x: 30, y: 40, paths: ["/before", "/refused.png", "/after", "/missing.txt"] }));
  await settled();
  expect(view.getByRole("alert").textContent).toBe(`${reason} ${otherReason}`);
  expect(written.at(-1)).toBe("\x1b[200~@before @after\x1b[201~");
});

it.each(["MacIntel", "Win32"])("attaches a copied file before its filename text and preserves text pastes (%s)", async (os) => {
  const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue(os);
  const attach = vi.fn(async (file: { path?: string; name?: string }) => ({ text: file.path ?? file.name! }));
  const { view, term, opened, written, clipboard } = mount(undefined, undefined, { acceptsFiles: true, attach });
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
  await parsed(term());
  files.read.mockResolvedValue([{ path: "/finder/photo.png" }, { name: "clipboard.png", data_base64: "aQ==" }]);
  clipboard.text = "photo.png";
  const press = () => {
    act(() => void term().textarea!.dispatchEvent(new KeyboardEvent("keydown", { key: "v", ctrlKey: os !== "MacIntel", metaKey: os === "MacIntel", bubbles: true, cancelable: true })));
    if (os === "MacIntel") webviewPaste(term().textarea!, clipboard.text);
  };
  press();
  act(() => term().input("x", true));
  await settled();
  expect(written).toEqual(["\x1b[200~/finder/photo.png clipboard.png\x1b[201~", "x"]);
  expect(attach.mock.calls).toHaveLength(2);
  clipboard.text = "ordinary text";
  files.read.mockResolvedValue([]);
  press();
  await settled();
  expect(written.at(-1)).toBe("\x1b[200~ordinary text\x1b[201~");
  expect(attach.mock.calls).toHaveLength(2);
  attach.mockRejectedValueOnce(new Error("attachment exceeds the file size limit"));
  files.read.mockResolvedValue([{ name: "clipboard.png", data_base64: "aQ==" }]);
  clipboard.text = "";
  press();
  await settled();
  expect(view.queryByRole("alert")).not.toBeNull();
  files.read.mockRejectedValueOnce(new Error("clipboard image exceeds the file size limit"));
  press();
  await settled();
  expect(view.queryByRole("alert")).not.toBeNull();
  if (os === "MacIntel") {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let answer: (files: { path: string }[]) => void = () => {};
      files.read.mockClear();
      files.read.mockImplementation(() => new Promise(ok => { answer = ok; }));
      clipboard.text = "late-file";
      act(() => void term().textarea!.dispatchEvent(new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true })));
      await act(() => vi.advanceTimersByTimeAsync(PASTE_WAIT));
      // The filename text arrives while the native file read is already in flight.
      webviewPaste(term().textarea!, "late-file");
      await settled();
      expect(files.read).toHaveBeenCalledTimes(1);
      await act(async () => answer([{ path: "/late-file" }]));
      await settled();
      expect(written.at(-1)).toBe("\x1b[200~/late-file\x1b[201~");
      clipboard.text = "completed";
      files.read.mockResolvedValue([{ path: "/completed" }]);
      act(() => void term().textarea!.dispatchEvent(new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true })));
      await act(() => vi.advanceTimersByTimeAsync(PASTE_WAIT));
      await settled();
      const uploads = attach.mock.calls.length;
      const writes = written.length;
      webviewPaste(term().textarea!, "completed");
      await settled();
      expect(attach.mock.calls).toHaveLength(uploads);
      expect(written).toHaveLength(writes);
      // The late answer is consumed once; another menu paste is a new attachment.
      webviewPaste(term().textarea!, "completed");
      await settled();
      expect(attach.mock.calls).toHaveLength(uploads + 1);
    } finally {
      vi.useRealTimers();
      platform.mockRestore();
    }
  }
  platform.mockRestore();
});

it("attaches Windows drop and clipboard paths through the native engine path conversion", async () => {
  native.enabled = true;
  const client = new DaemonClient({ baseUrl: "http://localhost", token: "test", fetch: async (_url, init) => {
    const file = JSON.parse(init!.body as string);
    if (!file.path.startsWith("/windows/c/")) return new Response(JSON.stringify({ error: { code: "invalid", message: "Linux path required" } }), { status: 400 });
    return new Response(JSON.stringify({ paths: [file.path], text: `@${file.path}` }));
  } });
  const { term, opened, written } = mount(undefined, undefined, { acceptsFiles: true, attach: file => client.attach("agent", file) });
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
  await parsed(term());
  act(() => files.drop({ type: "drop", x: 20, y: 20, paths: ["C:\\Users\\name\\photo.png"] }));
  await settled();
  expect(written).toEqual(["\x1b[200~@/windows/c/Users/name/photo.png\x1b[201~"]);
  files.read.mockResolvedValue([{ path: "C:\\Users\\name\\notes.txt" }]);
  webviewPaste(term().textarea!, "");
  await settled();
  expect(written.at(-1)).toBe("\x1b[200~@/windows/c/Users/name/notes.txt\x1b[201~");
});

it("refuses attachments for unsupported agents and discards an attachment after unmount", async () => {
  const attach = vi.fn(async () => ({ text: "@file" }));
  const first = mount(undefined, undefined, { acceptsFiles: false, attach });
  act(() => files.drop({ type: "drop", x: 20, y: 20, paths: ["/file"] }));
  await settled();
  expect(first.view.queryByRole("status")).not.toBeNull();
  expect(attach).not.toHaveBeenCalled();
  cleanup();
  let finish: (value: { text: string }) => void = () => {};
  files.read.mockResolvedValue([{ path: "/file" }]);
  const next = mount(undefined, undefined, { acceptsFiles: true, attach: () => new Promise(ok => { finish = ok; }) });
  webviewPaste(next.term().textarea!, "");
  await settled();
  next.view.unmount();
  await act(async () => finish({ text: "@stale" }));
  expect(next.written).toEqual([]);
  files.read.mockClear();
  files.listen.mockClear();
  const signIn = mount();
  webviewPaste(signIn.term().textarea!, "");
  await settled();
  expect(files.listen).not.toHaveBeenCalled();
  expect(files.read).not.toHaveBeenCalled();
});

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
  const { written, opened, view, term } = mount();
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
  view.unmount();
  expect(opened.closed).toBe(true);
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
  vi.mocked(startupStep).mockClear();
  const { written, copied, clipboard, opened, term } = mount();
  expect(startupStep).not.toHaveBeenCalled();
  const textarea = term().textarea!;
  const press = (key: string) => {
    const e = new KeyboardEvent("keydown", { key, metaKey: true, bubbles: true, cancelable: true });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  // tmux has the view's terminal.
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049hhello pty")));
  await parsed(term());
  expect(startupStep).toHaveBeenCalledWith("terminal_attached");
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
  // Half an emoji can't be sent: the shell's JSON refuses a lone surrogate, and the whole paste with it.
  clipboard.text = "half \ud83d of 😀 and \ude00";
  press("v");
  await settled();
  expect(written.at(-1)).toBe("\x1b[200~half � of 😀 and �\x1b[201~");
  written.length = 1;
  // An empty clipboard pastes nothing.
  clipboard.text = "";
  press("v");
  await settled();
  expect(written).toEqual(["\x1b[200~from the clipboard ✓\x1b[201~"]);
  webviewPaste(textarea, "menu paste");
  await settled();
  expect(written).toEqual(["\x1b[200~from the clipboard ✓\x1b[201~", "\x1b[200~menu paste\x1b[201~"]);
});

it("keeps what's typed after ⌘V behind the paste while the clipboard is read, pastes in the order pressed, and drops a paste that can't be read", async () => {
  const reads: { ok(text: string): void; fail(): void }[] = [];
  const { written, opened, view, term } = mount(() => new Promise<string>((ok, fail) => reads.push({ ok, fail: () => fail(new Error("no clipboard")) })));
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
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

const commandV = (target: EventTarget) => {
  const e = new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true, cancelable: true });
  act(() => void target.dispatchEvent(e));
  return e;
};

it("holds a paste until tmux has the view's terminal, however its bytes split, and sends it bracketed: a two-liner's Enter never runs its first line", async () => {
  const { written, clipboard, opened, term } = mount();
  clipboard.text = "!echo one\necho two";
  commandV(term().textarea!);
  act(() => term().input("x", true));
  await settled();
  expect(written).toEqual([]);
  // Bytes alone don't say (a key's echo on a pty still cooked, a read ending inside a sequence).
  act(() => opened.handlers!.onData(new TextEncoder().encode("x\x1b[?104")));
  await parsed(term());
  await settled();
  expect(written).toEqual([]);
  act(() => opened.handlers!.onData(new TextEncoder().encode("9h")));
  await parsed(term());
  await settled();
  // Bracketed though xterm.js was never told to: tmux reads the markers, and keeps them from a pane that didn't ask.
  expect(term().modes.bracketedPasteMode).toBe(false);
  expect(written).toEqual(["\x1b[200~!echo one\recho two\x1b[201~", "x"]);
});

it("drops a paste, said, when tmux stalls partway through taking the terminal past the wait, rather than guess; the next one goes once it has", async (ctx) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  ctx.onTestFinished(() => void vi.useRealTimers());
  const { written, clipboard, opened, term } = mount();
  clipboard.text = "a\nb";
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?104")));
  await parsed(term());
  commandV(term().textarea!);
  act(() => term().input("x", true));
  await act(() => vi.advanceTimersByTimeAsync(ATTACH_WAIT));
  expect(written).toEqual(["x"]);
  expect(logs.at(-1)?.[2]).toBe(`paste dropped: tmux hadn't taken the view's terminal within ${ATTACH_WAIT} ms`);
  act(() => opened.handlers!.onData(new TextEncoder().encode("9h")));
  await parsed(term());
  commandV(term().textarea!);
  await settled();
  expect(written).toEqual(["x", "\x1b[200~a\rb\x1b[201~"]);
});

it("gives the new view the keyboard when a resume or reconnect replaces the one that had it, and takes nothing from a field in use", () => {
  const open: PtyViewOpener = () => ({ write() {}, resize() {}, focus() {}, close() {} });
  const at = { server: "brindle", target: "s:@1" };
  const view = render(<PtyTerminal key="attempt-1" at={at} open={open} />);
  xterms.at(-1)!.textarea!.focus();
  view.rerender(<PtyTerminal key="attempt-2" at={at} open={open} />);
  expect(xterms).toHaveLength(2);
  expect(document.activeElement).toBe(xterms.at(-1)!.textarea);

  const field = document.body.appendChild(document.createElement("input"));
  field.focus();
  view.rerender(<PtyTerminal key="attempt-3" at={at} open={open} />);
  expect(document.activeElement).toBe(field);
  field.remove();
});

it("takes a ⌘V pressed with the keyboard on nothing for the view last used, once; never one meant for a text field or a dialog", async (ctx) => {
  const rects = vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
  ctx.onTestFinished(() => rects.mockRestore());
  const first = mount();
  const second = mount();
  for (const { opened, term, clipboard } of [first, second]) {
    clipboard.text = "pasted";
    act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
    await parsed(term());
  }
  // A click on the app's own chrome leaves the keyboard on nothing; xterm.js hears no ⌘V.
  act(() => second.term().textarea!.focus());
  act(() => second.term().textarea!.blur());
  expect(document.activeElement).toBe(document.body);
  expect(commandV(document.body).defaultPrevented, "no webview paste follows to double it").toBe(true);
  await settled();
  expect(second.written).toEqual(["\x1b[200~pasted\x1b[201~"]);
  expect(first.written).toEqual([]);
  expect(document.activeElement).toBe(second.term().textarea);

  const field = document.body.appendChild(document.createElement("input"));
  field.focus();
  expect(commandV(field).defaultPrevented).toBe(false);
  field.remove();
  const dialog = document.body.appendChild(document.createElement("div"));
  dialog.setAttribute("role", "dialog");
  act(() => second.term().textarea!.blur());
  expect(commandV(document.body).defaultPrevented).toBe(false);
  dialog.remove();
  await settled();
  expect(second.written).toHaveLength(1);
  // The view last used, still mounted but off screen: the one on screen takes it.
  const hidden = second.view.container.querySelector('[data-testid="terminal"]');
  rects.mockImplementation(function (this: HTMLElement) {
    return (this === hidden ? [] : [{}]) as unknown as DOMRectList;
  });
  expect(commandV(document.body).defaultPrevented).toBe(true);
  await settled();
  expect(first.written).toEqual(["\x1b[200~pasted\x1b[201~"]);
  expect(second.written).toHaveLength(1);
});

it("logs every paste it drops, why and its size, never its text, and lets what's typed behind it go on", async (ctx) => {
  const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  vi.useFakeTimers({ shouldAdvanceTime: true });
  ctx.onTestFinished(() => {
    vi.useRealTimers();
    platform.mockRestore();
  });
  const said = () => logs.filter((l) => l[1] !== "info").map((l) => [l[2], (l[3] as { bytes?: number } | undefined)?.bytes]);
  const waited = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
  const SECRET = "secret text";

  // The view never draws: not sent blind, each Enter in it a line run.
  const silent = mount();
  commandV(silent.term().textarea!);
  webviewPaste(silent.term().textarea!, SECRET);
  act(() => silent.term().input("x", true));
  await waited(ATTACH_WAIT);
  expect(silent.written).toEqual(["x"]);
  expect(said().at(-1)).toEqual([`paste dropped: tmux hadn't taken the view's terminal within ${ATTACH_WAIT} ms`, SECRET.length]);
  silent.view.unmount();

  // The client has gone.
  const ended = mount();
  act(() => ended.opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
  await parsed(ended.term());
  act(() => ended.opened.handlers!.onExit(0));
  webviewPaste(ended.term().textarea!, SECRET);
  await waited(0);
  expect(ended.written).toEqual([]);
  expect(said().at(-1)).toEqual(["paste dropped: the view's client has exited", SECRET.length]);
  // The view closes with a paste held for it.
  const closing = mount();
  webviewPaste(closing.term().textarea!, SECRET);
  closing.view.unmount();
  await waited(0);
  expect(said().at(-1)).toEqual(["paste dropped: the view closed first", SECRET.length]);
  ended.view.unmount();

  // A ⌘V the webview never answers; a webview paste with no text, and an app's clipboard that has none or can't be read.
  let read: () => Promise<string> = () => Promise.resolve("");
  const live = mount(() => read());
  act(() => live.opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
  await parsed(live.term());
  commandV(live.term().textarea!);
  await waited(PASTE_WAIT);
  expect(said()).toContainEqual([`⌘V: the webview's paste didn't come within ${PASTE_WAIT} ms: reading the app's clipboard`, undefined]);
  commandV(live.term().textarea!);
  webviewPaste(live.term().textarea!, null);
  await waited(0);
  expect(said().at(-1)).toEqual(["paste dropped: the clipboard holds no text", 0]);
  read = () => Promise.reject(new Error("no clipboard"));
  commandV(live.term().textarea!);
  webviewPaste(live.term().textarea!, null);
  await waited(0);
  expect(said().at(-1)).toEqual(["paste dropped: couldn't read the app's clipboard: Error: no clipboard", 0]);
  expect(live.written).toEqual([]);
  expect(JSON.stringify(logs)).not.toContain(SECRET);
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

it("on a Mac, ⌘V pastes what the webview's own paste holds, in the ⌘V's place, once; past the wait the app's clipboard in that place, and its late paste isn't doubled", async (ctx) => {
  const platform = vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  vi.useFakeTimers({ shouldAdvanceTime: true });
  ctx.onTestFinished(() => {
    vi.useRealTimers();
    platform.mockRestore();
  });
  const waited = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
  let reads = 0;
  let reading: Promise<string> = Promise.resolve("the app's clipboard");
  const { written, opened, view, term } = mount(() => {
    reads++;
    return reading;
  });
  act(() => opened.handlers!.onData(new TextEncoder().encode("\x1b[?1049h")));
  await parsed(term());
  const textarea = term().textarea!;
  const press = () => {
    const e = new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true, cancelable: true });
    act(() => void textarea.dispatchEvent(e));
    return e;
  };
  // A webview slowed by load: its paste a second and a half behind the ⌘V still goes ahead of what's typed after.
  press();
  act(() => term().input("k", true));
  await waited(1500);
  expect(written).toEqual([]);
  webviewPaste(textarea, "slow");
  await settled();
  expect(written).toEqual(["\x1b[200~slow\x1b[201~", "k"]);
  written.length = 0;

  // ⌘V's default goes on: the webview's paste (its Edit menu's), taken from xterm.js, typing behind it.
  expect(press().defaultPrevented).toBe(false);
  act(() => term().input("x", true));
  expect(written).toEqual([]);
  const e = webviewPaste(textarea, "pasted ✓\nline two");
  expect(e.defaultPrevented, "nothing goes into the textarea for xterm.js to paste again").toBe(true);
  await settled();
  expect(written).toEqual(["\x1b[200~pasted ✓\rline two\x1b[201~", "x"]);
  await waited(PASTE_WAIT + 50);
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
  // A ⌘V whose paste hasn't come keeps its place past the wait: the app's clipboard goes there, typing still behind.
  // Its paste, coming later with the same text, isn't pasted again.
  written.length = 0;
  press();
  act(() => term().input("y", true));
  await waited(PASTE_WAIT + 50);
  expect(written).toEqual(["\x1b[200~the app's clipboard\x1b[201~", "y"]);
  expect(reads).toBe(2);
  webviewPaste(textarea, "the app's clipboard");
  await settled();
  expect(written).toHaveLength(2);
  // That was the one late paste it was owed: the same text pasted again is a paste of its own.
  webviewPaste(textarea, "the app's clipboard");
  await settled();
  expect(written).toHaveLength(3);
  // What the person copied since is theirs, and so is the same text long after.
  written.length = 0;
  press();
  await waited(PASTE_WAIT + 50);
  expect(written).toEqual(["\x1b[200~the app's clipboard\x1b[201~"]);
  webviewPaste(textarea, "copied since");
  await settled();
  expect(written.at(-1)).toBe("\x1b[200~copied since\x1b[201~");
  await waited(LATE_PASTE_WINDOW);
  webviewPaste(textarea, "the app's clipboard");
  await settled();
  expect(written).toHaveLength(3);
  // Its paste coming while the app's clipboard is being read: that goes, the read's text doesn't, nor is it owed.
  written.length = 0;
  let answer: (text: string) => void = () => {};
  reading = new Promise((ok) => (answer = ok));
  press();
  await waited(PASTE_WAIT + 50);
  webviewPaste(textarea, "just in time");
  answer("just in time");
  await settled();
  expect(written).toEqual(["\x1b[200~just in time\x1b[201~"]);
  webviewPaste(textarea, "just in time");
  await settled();
  expect(written).toHaveLength(2);
  // Unmounted while a ⌘V waits: nothing read for it.
  const before = reads;
  press();
  view.unmount();
  await waited(PASTE_WAIT + 50);
  expect(reads).toBe(before);
});

it("opens a link Ctrl-clicked (⌘-clicked on a Mac) in the system browser, a URL the pane prints or a program's OSC 8 hyperlink; a plain click opens nothing; a session's brindle://visual link opens the visual", async () => {
  const urls: string[] = [];
  const visuals: string[] = [];
  let handlers: PtyViewHandlers | undefined;
  const open: PtyViewOpener = (_at, _size, h) => ((handlers = h), { write() {}, resize() {}, focus() {}, close() {} });
  render(
    <VisualLinkContext.Provider value={(id) => visuals.push(id)}>
      <PtyTerminal at={{ server: "brindle", target: "s:@1" }} open={open} openUrl={(url) => (urls.push(url), Promise.resolve())} />
    </VisualLinkContext.Provider>,
  );
  const term = xterms.at(-1)!;
  act(() => handlers!.onData(new TextEncoder().encode("see https://example.com/docs?a=1.\r\n\x1b]8;;https://example.com/osc\x07a link\x1b]8;;\x07\r\nopen brindle://visual/v_8f2c to pick")));
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
  const [visual] = await linksOn(3);
  click(visual, { ctrlKey: true });
  expect(visuals).toEqual(["v_8f2c"]);
  expect(urls).toHaveLength(2);
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
