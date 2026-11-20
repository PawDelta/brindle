/** Pane owner's executable handoff: frame, both ANSI themes, prompt-only crop. */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PtyViewHandlers } from "../../../api/ptyView";
import { Terminal } from "@xterm/xterm";
import { useState } from "react";
import { ChatPane, chatTheme, claudePromptRegion, promptClipRows, type ChatTheme, type PromptRegion } from "./ChatPane";
import idle from "./fixtures/claude-turn-1-idle.txt?raw";
import working from "./fixtures/claude-turn-2-working.txt?raw";
import streaming from "./fixtures/claude-turn-3-streaming.txt?raw";
import done from "./fixtures/claude-turn-4-idle.txt?raw";
import idle34 from "./fixtures/claude-turn-34-1-idle.txt?raw";
import working34 from "./fixtures/claude-turn-34-2-working.txt?raw";
import streaming34 from "./fixtures/claude-turn-34-3-streaming.txt?raw";
import done34 from "./fixtures/claude-turn-34-4-idle.txt?raw";

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
    ["light", ["#ffffff", "#0e1716", "#4c615e", "#06744f", "#9b4724", "#a63e3a", "#6350b3", "#2d69b5", "#86572a", "#1a756d"]],
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

  // tmux capture-pane -e of live Claude Code 2.1.327 panes, their last rows.
  it.each([
    // At work (171 columns): the working line, the prompt box between its 256-colour grey rules, a blank row, the mode
    // line ("esc to interrupt"): the box is still the prompt, from its first rule down; the working line is over it.
    ["its prompt box", 171, [
      "\x1b[38;5;174m✶\x1b[39m \x1b[38;5;174mBillowing… \x1b[38;5;246m(1m 47s · ↓\x1b[39m \x1b[38;5;246m4.5k tokens · thought for 3s)\x1b[39m",
      "",
      `\x1b[38;5;244m${"─".repeat(171)}`,
      "\x1b[38;5;246m❯ \x1b[39m",
      `\x1b[38;5;244m${"─".repeat(171)}`,
      "",
      "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle) · esc to interrupt\x1b[39m",
    ], { kind: "prompt", rows: 5, top: 2 }],
    // A numbered message typed in the box (120 columns) is still the prompt.
    ["a numbered message in its box", 120, [
      `\x1b[38;5;244m${"─".repeat(120)}`,
      "\x1b[39m❯ 1. Fix the bug",
      `\x1b[38;5;244m${"─".repeat(120)}`,
      "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m",
    ], { kind: "prompt", rows: 4, top: 0 }],
    // /model's picker in the box's place (120 columns): shown, every row.
    ["/model's picker", 120, [
      `\x1b[38;5;153m${"▔".repeat(120)}`,
      "\x1b[39m  \x1b[1m\x1b[38;5;153mSelect model\x1b[0m",
      "  \x1b[38;5;246mYour pick is the default for new sessions. For other models, use --model.\x1b[39m",
      "",
      "    \x1b[38;5;246m1.  \x1b[39mDefault (recommended)  \x1b[38;5;246mOpus 5.5 · Best for everyday, complex tasks\x1b[39m",
      "  \x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m2.  \x1b[38;5;114mOpus 5.5\x1b[39m \x1b[38;5;114m✔\x1b[39m             \x1b[38;5;246mFor complex work and everyday tasks\x1b[39m",
      "    \x1b[38;5;246m3.  \x1b[38;5;174mFable 5.2\x1b[39m              \x1b[38;5;246mFor your toughest challenges\x1b[39m",
      "    \x1b[38;5;246m4.  \x1b[39mSonnet 5.5             \x1b[38;5;246mMost efficient for simpler tasks\x1b[39m",
      "    \x1b[38;5;246m5.  \x1b[39mHaiku 5.5              \x1b[38;5;246mFastest for quick answers\x1b[39m",
      "    \x1b[38;5;246m↓ 1 more\x1b[39m",
      "",
      "  \x1b[38;5;174m◐\x1b[38;5;246m Medium effort (default)\x1b[39m",
      "",
      "  \x1b[38;5;246mUse \x1b[1m/fast\x1b[0m\x1b[38;5;246m to turn on fast mode (Opus 5.5).\x1b[39m",
      "",
      "  \x1b[3m\x1b[38;5;246m←/→ to adjust · s to use this session only · Enter to save · Esc to cancel\x1b[0m",
    ], { kind: "unknown", rows: 0 }],
  ])("reads %s from a live pane's real rows", async (_name, cols, lines, region) => {
    // Read as the view reads the pane: xterm.js's rows' text.
    const term = new Terminal({ cols, rows: lines.length });
    await new Promise<void>((done) => term.write(lines.join("\r\n"), done));
    const buffer = term.buffer.active;
    const rows = Array.from({ length: term.rows }, (_, y) => buffer.getLine(y)?.translateToString(true) ?? "");
    term.dispose();
    expect(claudePromptRegion(rows)).toEqual(region);
  });

  // tmux capture-pane -e -p of a live Claude Code 2.1.327 (its fullscreen renderer, 24 rows) through one turn: idle at
  // its prompt box, at work (the working line over the box, "esc to interrupt" in its mode line), its answer streaming
  // in with a message queued, and idle again. Fed through ChatPane as the pane's client draws them, its rows read as
  // ProviderSurface reads them: the box (its rules, its input or queued-message hint, its mode line) is cropped in every
  // frame, idle or at work, from its first rule down; the working line and the queued message over it stay shown. At 34
  // columns the mode line is cut short ("esc to inte…").
  it.each([
    ["80", [idle, working, streaming, done]],
    ["34", [idle34, working34, streaming34, done34]],
  ])("crops Claude Code's prompt box through a turn and keeps its working line (%s columns)", async (_cols, frames) => {
    let feed: (bytes: string) => void = () => {};
    const open = (_at: unknown, _size: unknown, handlers: PtyViewHandlers) => {
      feed = (bytes) => handlers.onData(new TextEncoder().encode(bytes));
      return { write() {}, resize() {}, focus() {}, close() {} };
    };
    let read: PromptRegion = { kind: "unknown", rows: 0 };
    function Live() {
      const [prompt, setPrompt] = useState<PromptRegion>(read);
      read = prompt;
      return (
        <ChatPane provider="claude" theme="dark" prompt={prompt} composer={null} onDropFiles={() => {}} onComposeText={() => {}}
          terminal={{ at: { server: null, target: "%1" }, open, onRows: (rows) => setPrompt(claudePromptRegion(rows)) }} />
      );
    }
    render(<Live />);
    const clipped: string[] = [];
    for (const frame of frames) {
      // The client's screen as tmux redraws it (the alternate screen, taken once on attaching).
      const drawn = `${clipped.length ? "" : "\x1b[?1049h"}\x1b[H\x1b[2J${frame.replace(/\n$/, "").split("\n").join("\r\n")}`;
      await act(async () => {
        feed(drawn);
        await new Promise<void>((ok) => setTimeout(ok));
      });
      // The rows cropped, and the row the crop starts at (the region's top, which ChatPane hands the terminal).
      clipped.push(`${screen.getByTestId("chat-terminal-frame").dataset.clipRows} from ${read.top}`);
      // The working line ("✳ Finagling…") is row 18, above the crop's first row (20): shown.
      const spinner = frame.split("\n").findIndex((line) => /^(\x1b\[[0-9;]*m)*[✢✳✶✻✽]\s*(\x1b\[[0-9;]*m)*\s*\S+…/.test(line));
      if (frame === working || frame === working34) expect(spinner).toBe(18);
    }
    // From the box's first rule (row 20 of 24) down, in every frame.
    expect(clipped).toEqual(["4 from 20", "4 from 20", "4 from 20", "4 from 20"]);
  });

  it("hides only Claude prompt rows and restores permission and choice rows", () => {
    expect(promptClipRows("claude", { kind: "prompt", rows: 4 })).toBe(4);
    expect(promptClipRows("claude", { kind: "permission", rows: 4 })).toBe(0);
    expect(promptClipRows("claude", { kind: "selection", rows: 4 })).toBe(0);
    expect(promptClipRows("claude", { kind: "unknown", rows: 4 })).toBe(0);
    expect(promptClipRows("codex", { kind: "prompt", rows: 4 })).toBe(0);
  });
});
