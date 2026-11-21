/** Pane owner's contract: approved framed live CLI above ChatBox, no new sidebar.
 * Own this file and terminal integration; send/queue/attach own ChatBox regions.
 * The frame is 10px rounded, hairline, padded, with the composer's field colour.
 * Use brindle's ANSI palette in both themes (chat-ui4 table); verify Claude's
 * ANSI-only setting/orange slot live before activation, using supported settings.
 * Clip only Claude's prompt + footer rows; permission/selection rows stay usable.
 * Never parse/rewrite output into messages or start a headless transport.
 */
import { useMemo, useState, type DragEvent, type HTMLAttributes, type ReactNode } from "react";
import type { ITheme } from "@xterm/xterm";
import { PtyTerminal, type PtyTerminalProps } from "../../../components/PtyTerminal";

export type ChatTheme = "dark" | "light";
export interface PromptRegion {
  /** "prompt": Claude Code's box, idle or through a turn; cropped while brindle's message box stands in for it. */
  kind: "prompt" | "permission" | "selection" | "unknown";
  /** Detected bottom prompt + footer height in terminal cells, not output. */
  rows: number;
  /** The region's first row, counted from the terminal client's first row (detected regions only). */
  top?: number;
}

export interface ChatPaneProps {
  terminal: PtyTerminalProps;
  provider: string;
  theme: ChatTheme;
  prompt: PromptRegion;
  composer: ReactNode;
  onDropFiles(files: File[]): void;
  /** Redirect typing to the composer only while hiding the CLI's prompt. */
  onComposeText(text: string): void;
}

// -- frame/drop integration: whole session is a green drop target --------------
// The live CLI in a card (hairline frame, 10px corners, room around its text, on
// the field colour its palette's background is), the composer a second card under it.
// While the CLI's prompt is cropped, what's typed or pasted in its card goes to the
// composer (onComposeText); a permission question or picker has the keyboard back.
// Files dropped anywhere on the pane go to the composer's attachments (onDropFiles),
// as File objects, once: the terminal gets no attachment path of its own, so a drop
// is never also pasted into the CLI.
export function ChatPane({ terminal, provider, theme, prompt, composer, onDropFiles, onComposeText }: ChatPaneProps) {
  const palette = useMemo(() => chatTheme(theme), [theme]);
  const clip = promptClipRows(provider, prompt);
  const [dropping, setDropping] = useState(false);
  const { attachments: _direct, ...pane } = terminal;
  const withFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
  return (
    <div
      className={`flex min-h-0 min-w-0 flex-1 flex-col gap-3 p-s2 ${dropping ? "outline outline-2 -outline-offset-2 outline-pass" : ""}`}
      style={{ borderRadius: "10px" }}
      data-testid="chat-pane"
      data-theme-mode={theme}
      data-dropping={dropping ? "1" : undefined}
      onDragOver={(e) => {
        if (!withFiles(e)) return;
        e.preventDefault();
        setDropping(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropping(false);
      }}
      onDrop={(e) => {
        if (!withFiles(e)) return;
        e.preventDefault();
        e.stopPropagation();
        setDropping(false);
        const files = Array.from(e.dataTransfer.files);
        if (files.length) onDropFiles(files);
      }}
    >
      <TerminalFrame ground={palette.background} data-testid="chat-terminal-frame" data-clip-rows={clip}>
        <PtyTerminal {...pane} theme={palette} {...chatContrast(theme)} cropRows={clip} cropFrom={clip > 0 ? prompt.top : undefined} typeElsewhere={clip > 0 ? onComposeText : undefined} />
      </TerminalFrame>
      {composer}
    </div>
  );
}

/* The card a provider's live session sits in (chat-ui4): a hairline frame, 10px corners and room around the
   terminal's text, on the ground the terminal draws on (its theme's background; the shell block's unless given),
   so the room reads as the terminal's own. ChatPane's and the session screen's for every provider's session; on
   the canvas when what it holds is a state card (ProviderSurface's data-card: paused, ended, signed out). */
export function TerminalFrame({ ground, children, ...rest }: { ground?: string; children: ReactNode } & Omit<HTMLAttributes<HTMLDivElement>, "className" | "style">) {
  return (
    <div
      {...rest}
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden border border-line-strong bg-shell-bg [&:has(>[data-card]:not([data-card=live]))]:bg-canvas [&>[data-card]:not([data-card=live])]:bg-canvas"
      style={{ borderRadius: "10px", padding: "18px 22px", ...(ground ? { background: ground } : {}) }}
    >
      {children}
    </div>
  );
}

// -- ANSI palette: apply to the mounted terminal and update on theme changes --
/* brindle's tokens (styles.css) as Claude Code's named colours, per the approved
   table: background is the message box's field, text ink, dim text ink-3, green
   pass, orange sap (brightYellow: the slot the working line takes), red fail,
   magenta branch, blue group-1, yellow thread-in, cyan group-4. The slots the
   table leaves open follow the same tokens: their bright twins the same hue, and
   black/white the ground and text ends of each theme (black the ground's in dark,
   the text's in light; white the reverse). In light white is a grey that still reads on the field, since
   SGR 37/97 text is drawn with it, so a program's default text in either ANSI-only theme lands on ink. */
const PALETTES: Record<ChatTheme, ITheme> = {
  dark: {
    background: "#070b09", foreground: "#e8f0eb", cursor: "#afbfb6", cursorAccent: "#070b09", selectionBackground: "#889a9040",
    black: "#121a16", red: "#f08c88", green: "#34d399", yellow: "#d7af87", blue: "#6ea8fe", magenta: "#a99be0", cyan: "#5ccfc4", white: "#afbfb6",
    brightBlack: "#889a90", brightRed: "#f08c88", brightGreen: "#34d399", brightYellow: "#e8875a", brightBlue: "#6ea8fe", brightMagenta: "#a99be0", brightCyan: "#5ccfc4", brightWhite: "#e8f0eb",
  },
  light: {
    background: "#ffffff", foreground: "#0e1716", cursor: "#06744f", cursorAccent: "#ffffff", selectionBackground: "#dcebe5", selectionInactiveBackground: "#dcebe5",
    black: "#0e1716", red: "#a63e3a", green: "#06744f", yellow: "#86572a", blue: "#2d69b5", magenta: "#6350b3", cyan: "#1a756d", white: "#556b68",
    brightBlack: "#4c615e", brightRed: "#a63e3a", brightGreen: "#06744f", brightYellow: "#9b4724", brightBlue: "#2d69b5", brightMagenta: "#6350b3", brightCyan: "#1a756d", brightWhite: "#3a4d4a",
  },
};

export function chatTheme(theme: ChatTheme): ITheme {
  // Claude's captured sent prompts use extended ANSI 237 for the ground and
  // 231 for text, even when the terminal's default ground is light. Supply the
  // full standard cube/ramp in both modes: xterm retains extended slots omitted
  // from a later theme, so switching back to dark must restore them explicitly.
  const hex = (n: number) => n.toString(16).padStart(2, "0");
  const levels = [0, 95, 135, 175, 215, 255];
  const extendedAnsi = Array.from({ length: 240 }, (_, i) => {
    if (i >= 216) return `#${hex(8 + (i - 216) * 10).repeat(3)}`;
    return `#${hex(levels[Math.floor(i / 36)])}${hex(levels[Math.floor(i / 6) % 6])}${hex(levels[i % 6])}`;
  });
  if (theme === "light") {
    extendedAnsi[237 - 16] = "#e9eded";
    extendedAnsi[231 - 16] = PALETTES.light.foreground!;
  }
  return { ...PALETTES[theme], extendedAnsi };
}

/* How a pane lifts text to WCAG AA (4.5:1), per theme, as PtyTerminal's two contrast props.

   Dark: xterm.js lifts every cell against its own ground (minimumContrastRatio). Under brindle's tmux Claude Code
   sends its dark diff grounds as 256-colour slots (rgb(34,92,43) as 65, the changed words' rgb(56,166,96) as 72), and
   the default text measures 3.5:1 and 2.3:1 on them; text that already reads is drawn as sent.

   Light: text on the pane's own white field is lifted to 4.5:1 (groundContrastRatio, groundContrast.ts): there
   Claude Code's light accents and their 256-colour stand-ins (its orange as 174, its subtle grey as 248) measure 2.4
   to 4.4:1. Its diffs bring their own grounds and GitHub syntax colours, and those keep Claude's colours wherever
   they reach 3:1 (minimumContrastRatio): a 4.5 floor there redrew its keywords (3.9:1 on the line grounds) and, on
   the changed words' grounds (rgb(47,157,68), rgb(209,69,75), which nothing but black lifts to 4.5), turned every
   highlighted token black or white. Its own colours as sent go as low as 1.1:1 there (its blue numbers on the red
   word), so 3 still lifts those, darkened in their own hue. */
export function chatContrast(theme: ChatTheme): { minimumContrastRatio: number; groundContrastRatio: number } {
  return theme === "light" ? { minimumContrastRatio: 3, groundContrastRatio: 4.5 } : { minimumContrastRatio: 4.5, groundContrastRatio: 1 };
}

// -- clipping: add rows to terminal geometry, clip the bottom, preserve output -
// Unknown state and non-Claude providers show all rows. Permission and selection
// restore the full terminal and keyboard access; output and the working line stay visible.
export function promptClipRows(provider: string, region: PromptRegion): number {
  if (provider !== "claude" || region.kind !== "prompt") return 0;
  return Math.max(0, Math.floor(region.rows));
}

// -- detection: Claude Code's prompt box at the pane's foot, from its rows' text --
/* Claude Code's prompt box as it draws it (2.1, captured from a live pane):
     ────────────────────────          a rule, the pane's width
     ❯ the input, if any               its input, "❯ " then text; a long input wraps onto more rows
     ────────────────────────          a rule again
                                       its footer: blank rows, and the mode line
       ⏵⏵ bypass permissions on (shift+tab to cycle)
   Rules and text are read without their colours (the CLI's 256-colour greys, whatever the theme). Whatever is
   typed or pasted in the box ("❯ 1. Fix the bug" too) is still the prompt. A permission question or a picker
   (/model's) takes the box's place: its border is no closing rule over a footer, and its "❯" (the choice the
   cursor is on) sits under any rule there is, so it is never taken for the prompt and its rows stay shown. The
   region is the box's rows from its first rule through its footer's last line, and where that first rule is
   (`top`, from the client's first row): the crop hides from there, so blank rows under a box that isn't at the
   foot hide too, without being counted (cropping them would only grow the client by as many blank rows again).
   The box is the prompt through a turn too: Claude Code draws its working line over it ("✳ Finagling… (3s · ↓ 2
   tokens)", the row or two above the box's first rule), "esc to interrupt" in its mode line, and "Press up to edit
   queued messages" in the box while a message waits (brindle's own queue shows those). The crop starts at the box's
   first rule, so the working line, the queued messages over the box and the answer stay shown; the box itself never
   comes back mid-turn, and the client keeps its size from idle through the turn and back. */
const FOOTER_ROWS = 8;
const isRule = (row: string): boolean => {
  const line = row.trim();
  if (!line.startsWith("───")) return false;
  const drawn = [...line].filter((c) => c === "─").length;
  return drawn >= 10 && drawn * 2 >= [...line].length;
};

export function claudePromptRegion(rows: readonly string[]): PromptRegion {
  let last = rows.length - 1;
  while (last >= 0 && !rows[last].trim()) last--;
  // The closing rule: within the footer's rows of the last line, with no "❯" (a picker's cursor) under it.
  let close = last;
  while (close >= 0 && last - close <= FOOTER_ROWS && !isRule(rows[close])) {
    if (rows[close].trimStart().startsWith("❯")) return { kind: "unknown", rows: 0 };
    close--;
  }
  if (close < 0 || last - close > FOOTER_ROWS) return { kind: "unknown", rows: 0 };
  let open = close - 1;
  while (open >= 0 && !isRule(rows[open])) open--;
  if (open < 0 || open === close - 1) return { kind: "unknown", rows: 0 };
  if (!rows[open + 1].startsWith("❯")) return { kind: "unknown", rows: 0 };
  return { kind: "prompt", rows: last - open + 1, top: open };
}
