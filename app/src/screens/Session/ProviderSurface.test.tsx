import { act, render, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { DaemonClient } from "../../api/client";
import { DaemonContext } from "../../api/context";
import { PtyViewsProvider, type PtyViewHandlers, type PtyViews } from "../../api/ptyView";
import type { AgentDTO } from "../../api/schema";
import { appThemeSheet, asGround } from "../../test/themes";
import { ProviderSurface, surfaceCard } from "./ProviderSurface";
const xterms = vi.hoisted(() => [] as import("@xterm/xterm").Terminal[]);
vi.mock("@xterm/xterm", async (original) => {
  const real = await original<typeof import("@xterm/xterm")>();
  return { ...real, Terminal: class extends real.Terminal { constructor(...a: ConstructorParameters<typeof real.Terminal>) { super(...a); xterms.push(this); } } };
});
// The real xterm.js: its answers to tmux's queries reach the same observer as the person's keys.
it("typed_words_reach_the_engine", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  const agent = { id: "sup1", provider: "claude", status: "idle", window: "s:@1", tmux_server: "brindle" } as AgentDTO;
  const vouched: [string | null, boolean][] = [], written: string[] = [];
  const client = { meta: async () => ({ visuals: true }), agent: async () => agent,
    transcript: async () => ({ cursor: "history:3", reset: true, messages: [
      { role: "user", text: "Keep my earlier request", history: true },
      { role: "restart", text: "", history: true },
      { role: "assistant", text: "Only in the live terminal" },
    ] }),
    visualTypedWords: async (_root: string, text: string | null, _expiry: number, _submitted: number, _signal: AbortSignal, typed = true) => {
      vouched.push([text, typed]); return { observed: true }; } } as unknown as DaemonClient;
  let handlers: PtyViewHandlers | undefined;
  const views = { status: { kind: "ready", tmux: "tmux 3.8" }, recheck() {}, copy: async () => {}, paste: async () => "",
    open: (_at, _size, given) => { handlers = given; return { write: (d: string) => written.push(d), resize() {}, focus() {}, close() {} }; } } as PtyViews;
  const recovery = { resume: async () => {}, busy: false, notice: null, preflight: null, setPreflight() {}, attempt: 0 };
  const shown = render(<DaemonContext.Provider value={client}><PtyViewsProvider value={views}><ProviderSurface repo="/repo" root="sup1" agent={agent} alive sessionStatus="idle" recovery={recovery} /></PtyViewsProvider></DaemonContext.Provider>);
  await waitFor(() => expect(handlers).toBeTruthy());
  const history = await shown.findByTestId("restart-history");
  expect(within(history).getByText("Keep my earlier request")).toBeTruthy();
  expect(within(history).getByRole("separator")).toBeTruthy();
  expect(within(history).queryByText("Only in the live terminal")).toBeNull();
  expect(within(history).queryByRole("textbox")).toBeNull();
  const term = xterms.at(-1)!;
  // What tmux 3.8 asks its client's terminal on every attach (captured from `tmux attach`), answered by xterm.js.
  const attach = "\x1b[?1049h\x1b[?2004h\x1b[?996n\x1b[c\x1b[>c\x1b[>q\x1b[?2026$p\x1b]10;?\x1b\\\x1b]11;?\x1b\\\x1b[18t\x1b[14t";
  await act(() => new Promise<void>((done) => { handlers!.onData(new TextEncoder().encode(attach)); term.write("", done); }));
  expect(written).toEqual(["\x1b[?1;2c", "\x1b[>0;276;0c", "\x1b[?2026;2$y", "\x1b]10;rgb:ffff/ffff/ffff\x1b\\", "\x1b]11;rgb:0000/0000/0000\x1b\\"]);
  for (const key of "test it now\r") act(() => term.input(key, true));
  await waitFor(() => expect(written.at(-1)).toBe("\r"));
  // The gate is no wider: what's pasted is still not the person's typing (reported as not typed, no words).
  act(() => { term.paste("render it now"); term.input("\r", true); });
  await waitFor(() => expect(vouched).toEqual([["test it now", true], [null, false]]));
  shown.unmount();
  vi.unstubAllGlobals();
});

// A pane follows a theme switch made while it is live, both ways: xterm.js draws in the new colours and contrast,
// then tells tmux the switch (mode 2031's report), and answers tmux's colour query with the new ground. tmux passes
// the report to the pane, where Claude Code's "auto" theme asks that ground and redraws to match. Codex reads its
// colours only at its start: its pane's xterm.js follows (its own colours from its next launch, an accepted exception).
it.each(["claude", "codex"])("a live %s pane follows the app theme light to dark and back", async (provider) => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  const style = appThemeSheet();
  document.documentElement.dataset.theme = "light";
  let shown: ReturnType<typeof render> | undefined;
  try {
    const agent = { id: "sup1", provider, status: "idle", window: "s:@1", tmux_server: "brindle" } as AgentDTO;
    const client = { meta: async () => ({ visuals: true }), agent: async () => agent, transcript: async () => ({ cursor: "", reset: true, messages: [] }) } as unknown as DaemonClient;
    const written: string[] = [];
    let handlers: PtyViewHandlers | undefined;
    const views = { status: { kind: "ready", tmux: "tmux 3.8" }, recheck() {}, copy: async () => {}, paste: async () => "",
      open: (_at, _size, given) => { handlers = given; return { write: (d: string) => written.push(d), resize() {}, focus() {}, close() {} }; } } as PtyViews;
    const recovery = { resume: async () => {}, busy: false, notice: null, preflight: null, setPreflight() {}, attempt: 0 };
    shown = render(<DaemonContext.Provider value={client}><PtyViewsProvider value={views}><ProviderSurface repo="/repo" root="sup1" agent={agent} alive sessionStatus="idle" recovery={recovery} /></PtyViewsProvider></DaemonContext.Provider>);
    await waitFor(() => expect(handlers).toBeTruthy());
    const term = xterms.at(-1)!;
    // tmux sends these, and reads xterm's answers to the colour query.
    const fromTmux = (text: string) => act(() => new Promise<void>((done) => { handlers!.onData(new TextEncoder().encode(text)); term.write("", done); }));
    const shellBg = () => getComputedStyle(document.documentElement).getPropertyValue("--shell-bg").trim();
    // Attached in light (tmux 3.8's attach queries, captured): the light pane, its ground told to tmux.
    await fromTmux("\x1b[?1049h\x1b[?996n\x1b]11;?\x1b\\");
    expect(term.options.theme?.background).toBe("#ffffff");
    // Light: xterm.js's floor off (the stream lifts, textContrast.ts); dark: xterm.js's at 4.5.
    expect([term.options.minimumContrastRatio, written.at(-1)]).toEqual([1, asGround("#ffffff")]);
    for (const [theme, report, contrast] of [["dark", "\x1b[?997;1n", 4.5], ["light", "\x1b[?997;2n", 1]] as const) {
      const before = written.length;
      document.documentElement.dataset.theme = theme;
      await waitFor(() => expect(written.slice(before)).toEqual([report]));
      // The colours were in place before the report: tmux's query on it is answered with the new ground.
      const ground = theme === "light" ? "#ffffff" : shellBg();
      expect([term.options.theme?.background ?? shellBg(), term.options.minimumContrastRatio]).toEqual([ground, contrast]);
      await fromTmux("\x1b]11;?\x1b\\");
      expect(written.slice(before)).toEqual([report, asGround(ground)]);
    }
    expect(shellBg()).toBe("#ffffff");
  } finally {
    // Unmounted before the globals go: no React work of this pane may outlive the test.
    shown?.unmount();
    style.remove();
    delete document.documentElement.dataset.theme;
    vi.unstubAllGlobals();
  }
});

it("says why a session's CLI couldn't start, not that its credentials need attention", () => {
  const agent = { id: "sup1", provider: "claude", status: "failed", failed_why: "launch: claude isn't installed" } as AgentDTO;
  const card = surfaceCard(agent, undefined, "failed", null, null);
  expect(card).toMatchObject({ kind: "failed", why: "claude isn't installed" });
  expect(surfaceCard({ ...agent, failed_why: null }, undefined, "failed", null, null)).toMatchObject({ kind: "failed", why: expect.stringContaining("credentials") });
});
