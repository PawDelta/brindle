/* The chat pane scenes (dev only, preview.tsx): Session/Chat/ChatPane.tsx over a
   pane that prints sample Claude Code output in the terminal's named colours, as
   one of its ANSI-only themes would, and redraws its bottom block at the client's
   foot on every resize, as Claude Code does.

     ?scene=chat-pane              idle at its prompt box: those rows cropped
     ?scene=chat-pane-permission   a permission question: every row shown
     ?scene=chat-pane-idle         a live Claude Code pane's captured rows (Chat/fixtures), idle,
     ?scene=chat-pane-working      at work and streaming its answer with a message queued: each read as
     ?scene=chat-pane-streaming    ProviderSurface reads it (claudePromptRegion), the box cropped in all three

   The composer is a stand-in for ChatBox (its own owner's), only for the layout. */

import { useCallback, useMemo, useState } from "react";
import type { PtyViewOpener } from "../src/api/ptyView";
import { ChatPane, claudePromptRegion, type ChatTheme, type PromptRegion } from "../src/screens/Session/Chat/ChatPane";
import idle from "../src/screens/Session/Chat/fixtures/claude-turn-1-idle.txt?raw";
import working from "../src/screens/Session/Chat/fixtures/claude-turn-2-working.txt?raw";
import streaming from "../src/screens/Session/Chat/fixtures/claude-turn-3-streaming.txt?raw";

const CAPTURED: Record<string, string> = { "chat-pane-idle": idle, "chat-pane-working": working, "chat-pane-streaming": streaming };

const ESC = "\x1b[";
const dim = (s: string) => `${ESC}90m${s}${ESC}0m`;
const green = (s: string) => `${ESC}32m${s}${ESC}0m`;
const red = (s: string) => `${ESC}31m${s}${ESC}0m`;
const blue = (s: string) => `${ESC}34m${s}${ESC}0m`;
const magenta = (s: string) => `${ESC}35m${s}${ESC}0m`;
const cyan = (s: string) => `${ESC}36m${s}${ESC}0m`;
const yellow = (s: string) => `${ESC}33m${s}${ESC}0m`;
const orange = (s: string) => `${ESC}93m${s}${ESC}0m`;
const bold = (s: string) => `${ESC}1m${s}${ESC}22m`;

const OUTPUT = [
  `● I'll set the goal first, then hand each milestone to a worker and check it when its branch merges.`,
  ``,
  `${green("●")} ${bold("brindle - set_goal")} ${dim("(MCP)")}(goal: "UX revamp of the brindle desktop app", milestones: 5)`,
  `  ${dim("⎿  Goal recorded · 5 milestones · checks run from ~/Projects/pawdelta")}`,
  ``,
  `${green("●")} ${bold("brindle - assign")} ${dim("(MCP)")}(task: "Layout skeleton: sidebar, session bar, main area", milestone: 1)`,
  `  ${dim("⎿  started 4f1c2d90 on desktop/layout (developer, sonnet)")}`,
  ``,
  `${green("●")} ${bold("brindle - merge_workspace")} ${dim("(MCP)")}(workspace: "desktop/layout")`,
  `  ${dim("⎿  Merged desktop/layout into")} ${magenta("dev/desktop-app")} ${dim("· checks passed (41 tests)")}`,
  ``,
  `${green("●")} ${bold("brindle - workspace_diff")} ${dim("(MCP)")}(workspace: "desktop/sessions", stat_only: true)`,
  `  ${dim("⎿  desktop/src/screens/Sessions/SessionsScreen.tsx  | 164")} ${green("+++++")}`,
  `     ${dim("desktop/src/store/overview.ts                     |  37")} ${green("+")}${red("-")}`,
  `     ${dim("3 files changed, 214 insertions(+), 9 deletions(-)")}`,
  ``,
  `${dim(">")} ${bold("Start milestone 3, and fix the sidebar's width while the workers run.")}`,
  ``,
  `${green("●")} ${bold("Update")}(desktop/src/components/Sidebar.tsx)`,
  `  ${dim("⎿  Updated desktop/src/components/Sidebar.tsx with 2 additions and 1 removal")}`,
  `       212    const [width, setWidth] = useState(${blue("SIDEBAR_WIDTH")});`,
  `       ${red("213 -  const min = 200;")}`,
  `       ${green("213 +  const min = 240;")}`,
  `       ${green("214 +  const max = 420;")}`,
  ``,
  `${green("●")} Milestone 3 of 5 is with the developer on ${cyan("desktop/ux-revamp")}. Two workers wait on you in the`,
  `  Inbox: a permission prompt and a ${yellow("lapsed sign-in")}.`,
  ``,
  `${orange("✻ Working… (12s · esc to interrupt)")}`,
];

/* Claude Code's bottom block as it draws it at the terminal's foot: its prompt box and footer, or a permission question. */
function bottom(kind: PromptRegion["kind"], cols: number): string[] {
  const w = Math.max(20, cols - 1);
  const line = (s: string) => `│${s.padEnd(w - 2)}│`;
  if (kind === "permission") {
    return [
      dim(`╭${"─".repeat(w - 2)}╮`),
      `${dim("│")}${bold(" Bash command".padEnd(w - 2))}${dim("│")}`,
      dim(line("")),
      dim(line("   npm --prefix desktop test -- --run")),
      dim(line("")),
      dim(line(" Do you want to proceed?")),
      `${dim("│")}${cyan(" ❯ 1. Yes".padEnd(w - 2))}${dim("│")}`,
      dim(line("   2. Yes, and don't ask again for npm commands")),
      dim(line("   3. No, and tell Claude what to do differently (esc)")),
      dim(`╰${"─".repeat(w - 2)}╯`),
    ];
  }
  return [dim(`╭${"─".repeat(w - 2)}╮`), `${dim("│")} > ${" ".repeat(w - 5)}${dim("│")}`, dim(`╰${"─".repeat(w - 2)}╯`), dim("  ? for shortcuts")];
}

/* The pane: drawn on open and redrawn at each size, its bottom block on the client's last rows. */
function opener(kind: PromptRegion["kind"]): PtyViewOpener {
  return (_at, size, handlers) => {
    const draw = (cols: number, rows: number) => {
      const block = bottom(kind, cols);
      const text = `${ESC}2J${ESC}H${OUTPUT.join("\r\n")}${ESC}${rows - block.length + 1};1H${block.join("\r\n")}`;
      queueMicrotask(() => handlers.onData(new TextEncoder().encode(text)));
    };
    draw(size.cols, size.rows);
    return { write() {}, resize: draw, focus() {}, close() {} };
  };
}

/* A captured pane (24 rows, tmux capture-pane -e): redrawn at each size with its last row on the client's last row,
   as Claude Code redraws its bottom block there. */
function capturedOpener(frame: string): PtyViewOpener {
  const lines = frame.replace(/\n$/, "").split("\n");
  return (_at, size, handlers) => {
    const draw = (_cols: number, rows: number) => {
      const shown = lines.slice(Math.max(0, lines.length - rows));
      const text = `${ESC}2J${ESC}H${ESC}${rows - shown.length + 1};1H${shown.join("\r\n")}`;
      queueMicrotask(() => handlers.onData(new TextEncoder().encode(text)));
    };
    draw(size.cols, size.rows);
    return { write() {}, resize: draw, focus() {}, close() {} };
  };
}

export function ChatPaneScene({ scene, theme }: { scene: string; theme: ChatTheme }) {
  const captured = CAPTURED[scene];
  const [read, setRead] = useState<PromptRegion>({ kind: "unknown", rows: 0 });
  const openCaptured = useMemo(() => captured ? capturedOpener(captured) : null, [captured]);
  const onRows = useCallback((rows: string[]) => {
    const region = claudePromptRegion(rows);
    setRead((was) => was.kind === region.kind && was.rows === region.rows && was.top === region.top ? was : region);
  }, []);
  const prompt: PromptRegion = captured ? read : scene === "chat-pane-permission" ? { kind: "permission", rows: 10 } : { kind: "prompt", rows: 4 };
  const composer = (
    <div className="flex flex-col gap-s1 border border-line-strong px-s2 py-s2" style={{ borderRadius: "10px", background: theme === "dark" ? "#070b09" : "#ffffff" }}>
      <textarea aria-label="Draft" rows={1} readOnly placeholder="Message the supervisor" className="resize-none bg-transparent text-sm text-ink outline-none placeholder:text-ink-3" />
      <div className="flex justify-end"><button type="button" disabled className="rounded-md border border-line-strong px-s2 py-0.5 text-sm text-ink-3">Send</button></div>
    </div>
  );
  return (
    <main className="flex h-screen min-h-0 flex-col bg-canvas text-ink">
      <ChatPane provider="claude" theme={theme} prompt={prompt}
        terminal={openCaptured
          ? { at: { server: null, target: "chat:1" }, open: openCaptured, onRows }
          : { at: { server: null, target: "chat:1" }, open: opener(prompt.kind) }}
        composer={composer} onDropFiles={() => {}} onComposeText={() => {}} />
    </main>
  );
}
