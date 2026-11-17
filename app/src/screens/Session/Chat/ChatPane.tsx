/** Pane owner's contract: approved framed live CLI above ChatBox, no new sidebar.
 * Own this file and terminal integration; send/queue/attach own ChatBox regions.
 * The frame is 10px rounded, hairline, padded, with the composer's field colour.
 * Use brindle's ANSI palette in both themes (chat-ui4 table); verify Claude's
 * ANSI-only setting/orange slot live before activation, using supported settings.
 * Clip only Claude's prompt + footer rows; permission/selection rows stay usable.
 * Never parse/rewrite output into messages or start a headless transport.
 */
import { useMemo, useState, type DragEvent, type ReactNode } from "react";
import type { ITheme } from "@xterm/xterm";
import { PtyTerminal, type PtyTerminalProps } from "../../../components/PtyTerminal";

export type ChatTheme = "dark" | "light";
export interface PromptRegion {
  kind: "prompt" | "permission" | "selection" | "unknown";
  /** Detected bottom prompt + footer height in terminal cells, not output. */
  rows: number;
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
      <div
        className="flex min-h-0 min-w-0 flex-1 flex-col border border-line-strong"
        style={{ borderRadius: "10px", padding: "18px 22px", background: palette.background }}
        data-testid="chat-terminal-frame"
        data-clip-rows={clip}
      >
        <PtyTerminal {...pane} theme={palette} cropRows={clip} typeElsewhere={clip > 0 ? onComposeText : undefined} />
      </div>
      {composer}
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
   the text's in light; white the reverse), so a program's default text in either
   ANSI-only theme lands on ink. */
const PALETTES: Record<ChatTheme, ITheme> = {
  dark: {
    background: "#070b09", foreground: "#e8f0eb", cursor: "#afbfb6", cursorAccent: "#070b09", selectionBackground: "#889a9040",
    black: "#121a16", red: "#f08c88", green: "#34d399", yellow: "#d7af87", blue: "#6ea8fe", magenta: "#a99be0", cyan: "#5ccfc4", white: "#afbfb6",
    brightBlack: "#889a90", brightRed: "#f08c88", brightGreen: "#34d399", brightYellow: "#e8875a", brightBlue: "#6ea8fe", brightMagenta: "#a99be0", brightCyan: "#5ccfc4", brightWhite: "#e8f0eb",
  },
  light: {
    background: "#ffffff", foreground: "#0e1716", cursor: "#3a4d4a", cursorAccent: "#ffffff", selectionBackground: "#4c615e33",
    black: "#0e1716", red: "#a63e3a", green: "#046e50", yellow: "#86572a", blue: "#2f6fbf", magenta: "#6350b3", cyan: "#1f8a80", white: "#e9eded",
    brightBlack: "#4c615e", brightRed: "#a63e3a", brightGreen: "#046e50", brightYellow: "#9b4724", brightBlue: "#2f6fbf", brightMagenta: "#6350b3", brightCyan: "#1f8a80", brightWhite: "#f4f6f6",
  },
};

export function chatTheme(theme: ChatTheme): ITheme {
  return { ...PALETTES[theme] };
}

// -- clipping: add rows to terminal geometry, clip the bottom, preserve output -
// Unknown state and non-Claude providers show all rows. Permission and selection
// restore the full terminal and keyboard access; working/output rows stay visible.
export function promptClipRows(provider: string, region: PromptRegion): number {
  if (provider !== "claude" || region.kind !== "prompt") return 0;
  return Math.max(0, Math.floor(region.rows));
}
