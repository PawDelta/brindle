import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { DaemonClient } from "../../api/client";
import { DaemonContext } from "../../api/context";
import { PtyViewsProvider, type PtyViewHandlers, type PtyViews } from "../../api/ptyView";
import type { AgentDTO } from "../../api/schema";
import { ProviderSurface } from "./ProviderSurface";

/* The real xterm.js: its answers to the client's queries are what reach the observer. */
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
vi.mock("@tauri-apps/api/webview", () => ({ getCurrentWebview: () => ({ onDragDropEvent: async () => () => {} }) }));
vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => ({ onDragDropEvent: async () => () => {} }) }));

beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup();
  xterms.length = 0;
  vi.unstubAllGlobals();
});

/* What tmux 3.8 writes to a client's terminal as it attaches (captured from `tmux attach`): it asks the
   terminal what it is (DA1, DA2, XTVERSION, DECRQM), its colours (OSC 10/11) and its size (14t, 18t). */
const TMUX_ATTACH = "\x1b[?1049h\x1b[?2004h\x1b[?996n\x1b[c\x1b[>c\x1b[>q\x1b[?2026$p\x1b]10;?\x1b\\\x1b]11;?\x1b\\\x1b[18t\x1b[14t";

it("typed_words_reach_the_engine", async () => {
  const agent = { id: "sup1", provider: "claude", status: "idle", window: "s:@1", tmux_server: "brindle" } as AgentDTO;
  const vouched: string[] = [];
  const client = {
    meta: async () => ({ visuals: true }),
    agent: async () => agent,
    attach: async () => ({ text: "" }),
    visualTypedWords: async (_root: string, text: string) => { vouched.push(text); return { observed: true }; },
  } as unknown as DaemonClient;
  let handlers: PtyViewHandlers | undefined;
  const written: string[] = [];
  const views: PtyViews = {
    status: { kind: "ready", tmux: "tmux 3.8" },
    recheck() {},
    open(_at, _size, given) { handlers = given; return { write: (d) => written.push(d), resize() {}, focus() {}, close() {} }; },
    copy: async () => {},
    paste: async () => "",
  };
  const recovery = { resume: async () => {}, busy: false, notice: null, preflight: null, setPreflight() {}, attempt: 0 };
  render(
    <DaemonContext.Provider value={client}>
      <PtyViewsProvider value={views}>
        <ProviderSurface repo="/repo" root="sup1" agent={agent} alive sessionStatus="idle" recovery={recovery} />
      </PtyViewsProvider>
    </DaemonContext.Provider>,
  );
  await waitFor(() => expect(handlers).toBeTruthy());
  const term = xterms.at(-1)!;
  // tmux attaches, and xterm.js answers it through the same input the person types into.
  await act(() => new Promise<void>((done) => { handlers!.onData(new TextEncoder().encode(TMUX_ATTACH)); term.write("", done); }));
  // What xterm.js 6 answers: DA1, DA2, the 2026 mode report and both colours (each its own write).
  expect(written).toEqual(["\x1b[?1;2c", "\x1b[>0;276;0c", "\x1b[?2026;2$y", "\x1b]10;rgb:ffff/ffff/ffff\x1b\\", "\x1b]11;rgb:0000/0000/0000\x1b\\"]);
  for (const key of "test it now\r") act(() => term.input(key, true));
  await waitFor(() => expect(vouched).toEqual(["test it now"]));
  await waitFor(() => expect(written.at(-1)).toBe("\r"));
  // The gate is no wider: what's pasted is still not the person's typing.
  act(() => term.paste("render it now"));
  act(() => term.input("\r", true));
  await waitFor(() => expect(written.at(-1)).toBe("\r"));
  expect(written.at(-2)).toBe("\x1b[200~render it now\x1b[201~");
  expect(vouched).toEqual(["test it now"]);
});
