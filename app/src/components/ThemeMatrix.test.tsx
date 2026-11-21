/* The theme matrix: every surface that draws in the app's colours, fresh in each theme and switched while live, both ways.
   A change to any theme path must keep all 16 cells (4 surfaces x 4 columns) passing. Codex reads its colours only at its
   start, so its pane's xterm.js follows the app's palette, not Codex's own colours (an accepted exception). */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import type { Terminal } from "@xterm/xterm";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Diff, DaemonClient } from "../api/client";
import { DaemonContext } from "../api/context";
import { PtyViewsProvider, type PtyViewHandlers, type PtyViews } from "../api/ptyView";
import type { AgentDTO } from "../api/schema";
import { DiffView, type DiffState } from "../screens/Branch/DiffView";
import { parseDiff } from "../screens/Branch/parseDiff";
import { ProviderSurface } from "../screens/Session/ProviderSurface";
import { appThemeSheet, asGround, contrast, drawnColours, token } from "../test/themes";
import { parseCss } from "./textContrast";
import { type ShellTab, ShellView } from "./TerminalPanel";

const xterms = vi.hoisted(() => [] as import("@xterm/xterm").Terminal[]);
vi.mock("@xterm/xterm", async (original) => {
  const real = await original<typeof import("@xterm/xterm")>();
  return { ...real, Terminal: class extends real.Terminal { constructor(...a: ConstructorParameters<typeof real.Terminal>) { super(...a); xterms.push(this); } } };
});

type Theme = "light" | "dark";

/* tmux 3.8's queries on attach (the colour asked, and the terminal's state), as the pane sees them. */
const ATTACH = "\x1b[?1049h\x1b[?996n\x1b]11;?\x1b\\";
const ASK = "\x1b]11;?\x1b\\";
/* Mode 2031's report of a theme switch, as the pane is told it. */
const REPORT: Record<Theme, string> = { dark: "\x1b[?997;1n", light: "\x1b[?997;2n" };
/* What a pty surface draws in per app theme: its ground and ink (the app's shell tokens) and xterm.js's contrast floor. */
const PTY: Record<Theme, { ground: string; ink: string; floor: number }> = {
  light: { ground: "#ffffff", ink: "#0e1716", floor: 1 },
  dark: { ground: "#070b09", ink: "#e8f0eb", floor: 4.5 },
};
/* The Changes diff's add and remove tokens per app theme (styles.css --pass and --sap). */
const DIFF: Record<Theme, { add: string; remove: string }> = {
  light: { add: "#066f4c", remove: "#9b4724" },
  dark: { add: "#34d399", remove: "#e8875a" },
};
const PATCH = [
  "diff --git a/app.ts b/app.ts",
  "index 1111111..2222222 100644",
  "--- a/app.ts",
  "+++ b/app.ts",
  "@@ -1 +1 @@",
  "-const a = 1;",
  "+const a = 2;",
].join("\n");

/* One pty surface, as its xterm.js and what it sent to tmux. */
interface Pty { term: Terminal; written: string[]; fromTmux(text: string): Promise<void> }
type Row = { kind: "pty"; mount(): Promise<Pty> } | { kind: "diff" };

/* The four columns: fresh in a theme, or started in one and switched to the other while live. */
const COLUMNS: [string, Theme, Theme | null][] = [
  ["fresh in light", "light", null],
  ["fresh in dark", "dark", null],
  ["switch light to dark", "light", "dark"],
  ["switch dark to light", "dark", "light"],
];

/* Captures a pty view's bytes and its handlers, to feed tmux's side from the test. */
function capturePty(): { views: PtyViews; ready(): Promise<Pty> } {
  const written: string[] = [];
  let handlers: PtyViewHandlers | undefined;
  const views = { status: { kind: "ready", tmux: "tmux 3.8" }, recheck() {}, copy: async () => {}, paste: async () => "",
    open: (_at, _size, given) => { handlers = given; return { write: (d: string) => written.push(d), resize() {}, focus() {}, close() {} }; } } as PtyViews;
  return {
    views,
    ready: async () => {
      await waitFor(() => expect(handlers).toBeTruthy());
      const term = xterms.at(-1)!;
      const fromTmux = (text: string) => act(() => new Promise<void>((done) => { handlers!.onData(new TextEncoder().encode(text)); term.write("", done); }));
      return { term, written, fromTmux };
    },
  };
}

async function providerPane(provider: "claude" | "codex"): Promise<Pty> {
  const agent = { id: "sup1", provider, status: "idle", window: "s:@1", tmux_server: "brindle" } as AgentDTO;
  const client = { meta: async () => ({ visuals: true }), agent: async () => agent, transcript: async () => ({ cursor: "", reset: true, messages: [] }) } as unknown as DaemonClient;
  const cap = capturePty();
  const recovery = { resume: async () => {}, busy: false, notice: null, preflight: null, setPreflight() {}, attempt: 0 };
  render(<DaemonContext.Provider value={client}><PtyViewsProvider value={cap.views}><ProviderSurface repo="/repo" root="sup1" agent={agent} alive sessionStatus="idle" recovery={recovery} /></PtyViewsProvider></DaemonContext.Provider>);
  return cap.ready();
}

async function shellPane(): Promise<Pty> {
  const tab = { repo: "/repo", name: "repo", cwd: "/repo", why: "", terminal: "t1", at: { server: null, target: "%1" }, shell: "zsh" } as ShellTab;
  const cap = capturePty();
  render(<PtyViewsProvider value={cap.views}><ShellView tab={tab} onEnded={() => {}} /></PtyViewsProvider>);
  return cap.ready();
}

const ROWS: Record<string, Row> = {
  "Claude pane": { kind: "pty", mount: () => providerPane("claude") },
  "Codex pane": { kind: "pty", mount: () => providerPane("codex") },
  "Terminal panel": { kind: "pty", mount: shellPane },
  "Changes diff view": { kind: "diff" },
};

/* A pty cell: attached as tmux does, then (in a switch column) the app's theme changed while it is live. The pane's
   xterm.js takes the new colours and floor, reports the switch to tmux, and answers tmux's colour query with the new ground. */
async function ptyCell(live: Pty, start: Theme, to: Theme | null) {
  const { term, written, fromTmux } = live;
  await fromTmux(ATTACH);
  // Text already on screen in the start theme, drawn in a grey (slot 246).
  await fromTmux("\x1b[2J\x1b[H\x1b[38;5;246mfaint");
  const mark = written.length;
  if (to) {
    document.documentElement.dataset.theme = to;
    await waitFor(() => expect(written.slice(mark)).toEqual([REPORT[to]]));
  }
  const shown = to ?? start;
  const ground = PTY[shown];
  await fromTmux(ASK);
  expect(term.options.theme).toMatchObject({ background: ground.ground, foreground: ground.ink });
  expect(term.options.minimumContrastRatio).toBe(ground.floor);
  expect(written.slice(mark)).toEqual([...(to ? [REPORT[to]] : []), asGround(ground.ground)]);
  // The text on screen before the switch, and text continued after it with no SGR of its own, both reach 4.5:1 on the
  // ground the pane ends up with (lifted in light by the stream, by xterm.js's floor in dark).
  await fromTmux(" more");
  const drawn = drawnColours(term, 0);
  expect(drawn.map(d => d.text).join("")).toBe("faint more");
  for (const d of drawn) expect(contrast(d.css, d.ground), d.text).toBeGreaterThanOrEqual(4.5);
}

/* The colour a tint (an rgba) lies over a ground to, as the page shows it. */
function over(tint: string, ground: string): string {
  const [r, g, b, a] = tint.match(/[\d.]+/g)!.map(Number);
  const base = parseCss(ground)!;
  return `rgb(${[r, g, b].map((c, i) => Math.round(a * c + (1 - a) * base[i])).join(", ")})`;
}

/* A diff cell: the add and remove lines in their token colours, each readable on its tint over the page and the
   surface, in the theme the cell ends up in (the tokens are CSS, so a switch applies to the drawn lines at once). */
function diffCell(start: Theme, to: Theme | null) {
  const parsed = parseDiff(PATCH);
  const state: DiffState = { diff: { diff: PATCH, base: "main" } as unknown as Diff, error: null, files: parsed.files, untracked: parsed.untracked, base: "main", reload() {} };
  render(<DiffView state={state} />);
  const add = screen.getByText("+const a = 2;");
  const remove = screen.getByText("-const a = 1;");
  expect(add.className).toContain("text-pass");
  expect(remove.className).toContain("text-sap");
  if (to) document.documentElement.dataset.theme = to;
  const shown = to ?? start;
  for (const [ink, tint, expected] of [["--pass", "--pass-soft", DIFF[shown].add], ["--sap", "--sap-soft", DIFF[shown].remove]] as const) {
    expect(token(ink)).toBe(expected);
    for (const ground of [token("--canvas"), token("--surface")]) expect(contrast(token(ink), over(token(tint), ground))).toBeGreaterThanOrEqual(4.5);
  }
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
});

describe.each(Object.entries(ROWS))("%s", (_name, row) => {
  let sheet: HTMLStyleElement;
  beforeEach(() => { sheet = appThemeSheet(); });
  afterEach(() => sheet.remove());

  it.each(COLUMNS)("%s", async (_column, start, to) => {
    document.documentElement.dataset.theme = start;
    if (row.kind === "diff") return diffCell(start, to);
    return ptyCell(await row.mount(), start, to);
  });
});
