/** Pane owner's executable handoff: frame, both ANSI themes, prompt-only crop. */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PtyViewHandlers } from "../../../api/ptyView";
import { ChatPane, chatTheme, promptClipRows, type ChatTheme } from "./ChatPane";

// xterm.js watches the pixel ratio through matchMedia, which jsdom lacks.
beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ChatPane", () => {
  it("frames the live terminal separately above the composer", () => {
    render(<ChatPane provider="claude" theme="dark" prompt={{ kind: "unknown", rows: 0 }}
      terminal={{ at: { server: null, target: "%1" }, open: () => ({ write() {}, resize() {}, focus() {}, close() {} }) }}
      composer={<textarea aria-label="Draft" />} onDropFiles={() => {}} onComposeText={() => {}} />);
    const frame = screen.getByTestId("chat-terminal-frame");
    expect(getComputedStyle(frame).borderRadius).toBe("10px");
    // The composer's field; xterm.js's own input is a textbox too, inside the frame.
    const draft = screen.getByRole("textbox", { name: "Draft" });
    expect(frame.contains(draft)).toBe(false);
    expect(frame.compareDocumentPosition(draft) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it.each([
    ["dark", ["#070b09", "#e8f0eb", "#889a90", "#34d399", "#e8875a", "#f08c88", "#a99be0", "#6ea8fe", "#d7af87", "#5ccfc4"]],
    ["light", ["#ffffff", "#0e1716", "#4c615e", "#046e50", "#9b4724", "#a63e3a", "#6350b3", "#2f6fbf", "#86572a", "#1f8a80"]],
  ] as const)("maps the approved brindle ANSI palette in %s theme", (mode, colours) => {
    const theme = chatTheme(mode as ChatTheme);
    // Slot names follow xterm; brightYellow carries the working-line orange.
    expect([theme.background, theme.foreground, theme.brightBlack, theme.green,
      theme.brightYellow, theme.red, theme.magenta, theme.blue, theme.yellow, theme.cyan]).toEqual(colours);
  });

  it("sends typing to the composer while the prompt is cropped and gives the keyboard back for a permission question", () => {
    const written: string[] = [];
    const composed: string[] = [];
    const props = {
      provider: "claude", theme: "dark" as const, composer: <textarea aria-label="Draft" />, onDropFiles() {},
      onComposeText: (text: string) => void composed.push(text),
      terminal: { at: { server: null, target: "%1" }, open: () => ({ write: (d: string) => void written.push(d), resize() {}, focus() {}, close() {} }) },
    };
    const view = render(<ChatPane {...props} prompt={{ kind: "prompt", rows: 4 }} />);
    const input = () => screen.getByRole("textbox", { name: "Terminal input" });
    fireEvent.keyDown(input(), { key: "h", code: "KeyH", keyCode: 72 });
    fireEvent.keyDown(input(), { key: "Enter", code: "Enter", keyCode: 13 });
    // Text with no key behind it (an emoji picker's), which xterm.js sends past its key handler.
    fireEvent.input(input(), { data: "🙂", inputType: "insertText" });
    expect(composed).toEqual(["h", "🙂"]);
    expect(written).toEqual([]);
    view.rerender(<ChatPane {...props} prompt={{ kind: "permission", rows: 4 }} />);
    fireEvent.keyDown(input(), { key: "Enter", code: "Enter", keyCode: 13 });
    fireEvent.keyUp(input(), { key: "Enter", code: "Enter", keyCode: 13 });
    fireEvent.input(input(), { data: "🙂", inputType: "insertText" });
    expect(written).toEqual(["\r", "🙂"]);
    expect(composed).toEqual(["h", "🙂"]);
  });

  it("drops a paste made for the composer when a permission question comes while the clipboard is read", async () => {
    const written: string[] = [];
    const composed: string[] = [];
    let give: (text: string) => void = () => {};
    const props = {
      provider: "claude", theme: "dark" as const, composer: <textarea aria-label="Draft" />, onDropFiles() {},
      onComposeText: (text: string) => void composed.push(text),
      terminal: {
        at: { server: null, target: "%1" },
        // tmux takes the client's terminal (the alternate screen), so a paste meant for the pane would reach it.
        open: (_at: unknown, _size: unknown, handlers: PtyViewHandlers) => {
          queueMicrotask(() => handlers.onData(new TextEncoder().encode("\x1b[?1049h")));
          return { write: (d: string) => void written.push(d), resize() {}, focus() {}, close() {} };
        },
        paste: () => new Promise<string>((ok) => (give = ok)),
      },
    };
    const view = render(<ChatPane {...props} prompt={{ kind: "prompt", rows: 4 }} />);
    await act(async () => {});
    // Ctrl+V (not a Mac): the app's clipboard read, still pending as the question comes.
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Terminal input" }), { key: "v", code: "KeyV", keyCode: 86, ctrlKey: true });
    view.rerender(<ChatPane {...props} prompt={{ kind: "permission", rows: 4 }} />);
    await act(async () => give("draft text"));
    expect(composed).toEqual([]);
    expect(written).toEqual([]);
  });

  it("hands files dropped on the pane to the composer once, never to the terminal", () => {
    const dropped: File[][] = [];
    const attach = vi.fn();
    render(<ChatPane provider="claude" theme="dark" prompt={{ kind: "prompt", rows: 4 }} composer={<textarea aria-label="Draft" />}
      terminal={{ at: { server: null, target: "%1" }, open: () => ({ write() {}, resize() {}, focus() {}, close() {} }), attachments: { acceptsFiles: true, attach } }}
      onDropFiles={(files) => void dropped.push(files)} onComposeText={() => {}} />);
    const shot = new File(["png bytes"], "shot.png", { type: "image/png" });
    fireEvent.drop(screen.getByTestId("chat-terminal-frame"), { dataTransfer: { types: ["Files"], files: [shot] } });
    expect(dropped).toEqual([[shot]]);
    expect(attach).not.toHaveBeenCalled();
  });

  it("hides only Claude prompt rows and restores permission and choice rows", () => {
    expect(promptClipRows("claude", { kind: "prompt", rows: 4 })).toBe(4);
    expect(promptClipRows("claude", { kind: "permission", rows: 4 })).toBe(0);
    expect(promptClipRows("claude", { kind: "selection", rows: 4 })).toBe(0);
    expect(promptClipRows("claude", { kind: "unknown", rows: 4 })).toBe(0);
    expect(promptClipRows("codex", { kind: "prompt", rows: 4 })).toBe(0);
  });
});
