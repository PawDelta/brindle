import { act, render, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { DaemonClient } from "../../api/client";
import { DaemonContext } from "../../api/context";
import { PtyViewsProvider, type PtyViewHandlers, type PtyViews } from "../../api/ptyView";
import type { AgentDTO } from "../../api/schema";
import { ProviderSurface } from "./ProviderSurface";
const xterms = vi.hoisted(() => [] as import("@xterm/xterm").Terminal[]);
vi.mock("@xterm/xterm", async (original) => {
  const real = await original<typeof import("@xterm/xterm")>();
  return { ...real, Terminal: class extends real.Terminal { constructor(...a: ConstructorParameters<typeof real.Terminal>) { super(...a); xterms.push(this); } } };
});
// The real xterm.js: its answers to tmux's queries reach the same observer as the person's keys.
it("typed_words_reach_the_engine", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  const agent = { id: "sup1", provider: "claude", status: "idle", window: "s:@1", tmux_server: "brindle" } as AgentDTO;
  const vouched: string[] = [], written: string[] = [];
  const client = { meta: async () => ({ visuals: true }), agent: async () => agent,
    transcript: async () => ({ cursor: "history:3", reset: true, messages: [
      { role: "user", text: "Keep my earlier request", history: true },
      { role: "restart", text: "", history: true },
      { role: "assistant", text: "Only in the live terminal" },
    ] }),
    visualTypedWords: async (_root: string, text: string) => { vouched.push(text); return { observed: true }; } } as unknown as DaemonClient;
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
  // The gate is no wider: what's pasted is still not the person's typing.
  act(() => { term.paste("render it now"); term.input("\r", true); });
  expect(vouched).toEqual(["test it now"]);
});
