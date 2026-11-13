import { connectDaemon, inTestContext } from "../../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createAppStore } from "../../store/store";
import { DaemonClient } from "../../api/client";
import { type PtyStatus, type PtyViewHandlers, type PtyViews, type PtyViewTarget } from "../../api/ptyView";
import type { GoalDTO, InboxItemDTO, SessionDTO } from "../../api/schema";
import type { SheetRef } from "../../routes";
import { BASE_URL, PANE_DEAD_LINE, TOKEN, flush, FakeDaemon, type DaemonData, type Pane } from "../../test/fakeDaemon";
import { REPO, permission, permissionInTerminal, root, windowOf, windowed, worker } from "../../test/fixtures";
import { NO_SHELL, NO_WINDOW } from "./ProviderSurface";
import { SessionScreen } from "./SessionScreen";
import { agentIds, sessionTree, sessionWork } from "./tree";
import { liveWorkers } from "../../store/overview";
import { SessionContext, sidebarChanges } from "../../components/SessionContext";

describe("sessionWork", () => {
  it.each(["tree", "worker list", "worker count"])("excludes past workers from the %s while keeping their running children", (place) => {
    const past = { ...worker.agents![0], status: "rewound" };
    const dismissed = { ...past, id: "closed", status: "processing", dismissed_at: 1 };
    const child = { ...worker.agents![0], id: "child", parent_id: past.id };
    const workspaces = [root, { ...worker, agents: [past, dismissed, child] }];
    if (place === "tree") expect([...agentIds(sessionTree(workspaces, "sup1"))]).toEqual(["sup1", "child"]);
    if (place === "worker list") expect(sessionWork(workspaces, "sup1", root.id).agents.map((a) => a.id)).toEqual(["sup1", "child"]);
    if (place === "worker count") expect(liveWorkers([past, dismissed, child], {})).toBe(1);
  });

  const agent = (id: string, parent_id: string | null = null) => ({ id, profile: "claude", provider: "claude", status: "processing", mode: "worker", parent_id, subagents: [] });
  const ws = (id: string, ...agents: ReturnType<typeof agent>[]) => ({ id, name: id, branch: id, path: `/r/${id}`, agents });

  it("finds a session's agents and workspaces with its root out of the snapshot, across left-out ancestors, each workspace once", () => {
    const snapshot = [
      ws("proj"), // the paused root's own checkout: no agent left in it
      ws("proj/a", agent("w1", "sup0"), agent("w2", "w1")),
      ws("proj/b", agent("w4", "w3")), // w3 was closed: its parent is known only through `hidden`
      ws("proj/c", agent("x1", "sup9")), // another session's
    ];
    const got = sessionWork(snapshot, "sup0", "proj", { w3: "w1", sup0: null });
    expect(got.agents.map((a) => a.id)).toEqual(["w1", "w2", "w4"]);
    expect(got.workspaces.map((w) => w.id)).toEqual(["proj", "proj/a", "proj/b"]);
    // An agent whose chain stops short of the root (an unknown left-out parent) isn't counted.
    expect(sessionWork(snapshot, "sup0", null, {}).agents.map((a) => a.id)).toEqual(["w1", "w2"]);
  });
});

/* jsdom has neither WebGL nor a canvas: xterm.js is replaced by a recorder,
   and the WebGL addon fails as it does where WebGL2 is missing. */
const fake = vi.hoisted(() => {
  type Listener<T> = (value: T) => void;
  class Xterm {
    static all: Xterm[] = [];
    cols = 80;
    rows = 24;
    written = "";
    disposed = false;
    private typed: Listener<string>[] = [];
    constructor() {
      Xterm.all.push(this);
    }
    open() {}
    loadAddon(addon: { activate(term: unknown): void }) {
      addon.activate(this);
    }
    write(data: string | Uint8Array) {
      this.written += typeof data === "string" ? data : new TextDecoder().decode(data);
    }
    /* The recorder parses nothing: hooks into its parser (the clipboard addon's OSC 52) never fire. */
    parser = {
      registerCsiHandler: () => ({ dispose() {} }),
      registerEscHandler: () => ({ dispose() {} }),
      registerOscHandler: () => ({ dispose() {} }),
    };
    onBinary(_fn: Listener<string>) {
      return { dispose() {} };
    }
    onKey(_fn: Listener<{ key: string; domEvent: KeyboardEvent }>) {
      return { dispose() {} };
    }
    onData(fn: Listener<string>) {
      this.typed.push(fn);
      return { dispose: () => (this.typed = this.typed.filter((f) => f !== fn)) };
    }
    onResize(_fn: Listener<{ cols: number; rows: number }>) {
      return { dispose() {} };
    }
    attachCustomKeyEventHandler() {}
    /* What xterm.js says the person typed: a key, a paste, a mouse report. */
    type(data: string) {
      for (const fn of this.typed) fn(data);
    }
    dispose() {
      this.disposed = true;
    }
  }
  /* FitAddon: the recorder keeps its size. */
  class Addon {
    activate() {}
    dispose() {}
    fit() {}
    proposeDimensions() {
      return undefined;
    }
  }
  /* jsdom has no ResizeObserver. */
  class Observer {
    observe() {}
    disconnect() {}
  }
  return { Xterm, Addon, Observer };
});

vi.mock("@xterm/xterm", () => ({ Terminal: fake.Xterm }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: fake.Addon }));
vi.mock("@xterm/addon-web-links", () => ({ WebLinksAddon: fake.Addon }));
vi.mock("@xterm/addon-webgl", () => ({ WebglAddon: class { constructor() { throw new Error("WebGL2 not supported"); } } }));

beforeEach(() => {
  fake.Xterm.all = [];
  vi.stubGlobal("ResizeObserver", fake.Observer);
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  sidebarChanges.shown = false;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const live: Pane = { live: true, screen: "$ claude\nhello\n", cursor: [2, 1] };
/* A pane as the app's tmux client draws it on attaching (the fake daemon's). */
const drawnLive = "\x1b[?1049h\x1b[H\x1b[2J$ claude\r\nhello\x1b[2;3H";
const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

/* Under the app the sidebar's tree picks the agent and the route carries it;
   here a row of buttons stands in for the tree, and one for the sidebar's Changes row. */
function Harness({ client, root, repo, refresh, store, agents, onOpen }: {
  client: DaemonClient;
  root: string;
  repo: string;
  refresh: () => Promise<void>;
  store: ReturnType<typeof createAppStore>;
  agents: string[];
  onOpen(root: string, repo: string): void;
}) {
  const [picked, setPicked] = useState<{ agent: string; sheet: SheetRef | null }>({ agent: root, sheet: null });
  return (
    <>
      {sidebarChanges.shown && <SessionContext session={{ root, repo }} route={{ screen: "session", root, repo }} navigate={() => {}} refresh={refresh} />}
      <div data-testid="tree">
        {agents.map((id) => (
          <button key={id} type="button" data-agent={id} onClick={() => setPicked({ agent: id, sheet: null })}>
            {id}
          </button>
        ))}
        <button type="button" data-testid="open-changes" onClick={() => setPicked((p) => ({ ...p, sheet: p.sheet?.kind === "changes" ? null : { kind: "changes" } }))}>
          Changes
        </button>
      </div>
      <SessionScreen client={client} root={root} repo={repo} refresh={refresh} store={store} agent={picked.agent} sheet={picked.sheet} onSelect={(agent, sheet) => setPicked({ agent, sheet })} onOpen={onOpen} />
    </>
  );
}

/* The session over the fake daemon, its panes shown through its PtyViews
   (`views` in their place; null: none, a browser tab's). */
function mount(terminals: Record<string, Pane>, data: Partial<DaemonData> = {}, fetch?: (daemon: FakeDaemon) => typeof globalThis.fetch, sessionRoot = "sup1", agents = ["sup1", "w1"], repo = REPO, views?: PtyViews | null) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], terminals, ...data, workspaces: windowed(data.workspaces ?? [root, worker]) });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: fetch?.(daemon) ?? daemon.fetch });
  const { store, sync } = connectDaemon(daemon, client);
  // The sessions the view asked the app to open (a handover's).
  const opened: [string, string][] = [];
  render(
    inTestContext(<><Harness client={client} root={sessionRoot} repo={repo} refresh={sync.refresh} store={store} agents={agents} onOpen={(r, repo) => opened.push([r, repo])} /></>, { client, store }, { daemon: true, sessions: true, views: views === undefined ? daemon.ptyViews() : views }),
  );
  const term = () => fake.Xterm.all.at(-1)!;
  return { daemon, sync, term, opened };
}

const pick = (id: string) => fireEvent.click(within(screen.getByTestId("tree")).getByText(id));
const shownAgent = () => screen.getByTestId("agent-id").textContent;
const surface = () => screen.getByTestId("provider-surface");
const title = () => screen.getByTestId("surface-title").textContent;

describe("SessionScreen", () => {
  it.each(["rewound", "dismissed"])("drops a %s selection from the surface and agent menu without a stopped badge or worker tab", async (reason) => {
    const past = { ...worker.agents![0], status: reason === "rewound" ? "rewound" : "exited", dismissed_at: reason === "dismissed" ? 1 : null };
    mount({ sup1: live }, { workspaces: [root, { ...worker, agents: [past] }] });
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    pick("w1");
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByText("stopped", { exact: true })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.queryByRole("menuitem", { name: "Rewind…" })).toBeNull();
  });

  it("shows live session changes before PR readiness in the chat's place, goes back to the chat with ← Chat or Esc keeping drafts, and edits hunks in either layout", async () => {
    sidebarChanges.shown = true;
    let holdDiff: Promise<void> | null = null;
    let diffReads = 0;
    const { daemon, term } = mount({ sup1: live, w1: live }, {
      workspaces: [{ ...root, branch: "brindle/session-18" }, worker],
      sessionReview: { sup1: { ready: false, reasons: ["work in progress"], branch: "brindle/session-18", base: "main", diff: "" } },
      sessionBase: { sup1: { "src/a.ts": "old\ncontext\n" } },
      sessionFiles: { sup1: { "src/a.ts": "new\ncontext\n", "docs/readme.md": "hello\n" } },
    }, (daemon) => async (input, init) => {
      if (String(input).endsWith("/sessions/sup1/diff")) {
        diffReads++;
        if (holdDiff) await holdDiff;
      }
      return daemon.fetch(input, init);
    });
    await waitFor(() => expect(term().written).toBe(drawnLive));
    // The chat alone: no tabs, no changes, no way back to show.
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByTestId("session-changes")).toBeNull();
    expect(screen.queryByTestId("back-to-chat")).toBeNull();
    const openChanges = () => fireEvent.click(screen.getByTestId("open-changes"));
    // The screen and sidebar share one initial read and one per change event.
    await waitFor(() => expect(screen.getByTestId("changes-count").textContent).toBe("2 files +2 −1"));
    expect(diffReads).toBe(1);
    pick("w1");
    const terminal = term();
    const readsBefore = diffReads;
    daemon.data.sessionFiles.sup1["docs/readme.md"] = "hello\nworld\n";
    daemon.publish("session.changed", { root_id: "sup1" });
    await waitFor(() => expect(screen.getByTestId("changes-count").textContent).toBe("2 files +3 −1"));
    expect(diffReads).toBe(readsBefore + 1);
    openChanges();
    const changes = () => within(screen.getByTestId("session-changes"));
    const summary = () => changes().getByTestId("changes-summary").textContent;
    await waitFor(() => expect(summary()).toBe("2 files changed against main · +3 −1"));
    // In the chat's place: the chat is hidden, not gone, and the bar offers the way back.
    expect(screen.getByTestId("back-to-chat").textContent).toBe("← Chat");
    expect(shownAgent()).toBe("w1");
    const files = await changes().findByRole("navigation", { name: "Changed files" });
    fireEvent.click(within(files).getByRole("button", { name: "src/" }));
    expect(within(files).queryByTitle("src/a.ts")).toBeNull();
    fireEvent.click(within(files).getByRole("button", { name: "src/" }));
    fireEvent.click(within(files).getByTitle("src/a.ts"));
    await waitFor(() => expect(changes().getByRole("button", { name: /@@/ })).toHaveProperty("disabled", false));
    fireEvent.click(changes().getByRole("button", { name: "Split" }));
    const split = changes().getByTestId("split-diff");
    expect(within(split).getByLabelText("Old version").textContent).toContain("old");
    expect(within(split).getByLabelText("New version").textContent).toContain("new");
    daemon.publish("agent.status", { root_id: "sup1", agent_id: "sup1", payload: { status: "processing" } });
    await waitFor(() => expect(changes().getByRole("button", { name: /@@/ })).toHaveProperty("disabled", true));
    expect(changes().getByTestId("split-diff")).toBeTruthy();
    daemon.publish("agent.status", { root_id: "sup1", agent_id: "sup1", payload: { status: "idle" } });
    await waitFor(() => expect(changes().getByRole("button", { name: /@@/ })).toHaveProperty("disabled", false));
    fireEvent.click(changes().getByRole("button", { name: /@@/ }));
    fireEvent.change(changes().getByRole("textbox"), { target: { value: "edited\ncontext\nextra" } });
    fireEvent.click(changes().getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(daemon.sessionEdits).toHaveLength(1));
    expect(daemon.data.sessionFiles.sup1["src/a.ts"]).toBe("edited\ncontext\nextra\n");
    await waitFor(() => expect(summary()).toContain("+4 −1"));
    fireEvent.click(changes().getByRole("button", { name: "Unified" }));
    await waitFor(() => expect(changes().queryByTestId("hunk-editor")).toBeNull());
    expect(changes().getByTestId("hunk").textContent).toContain("+edited");
    expect(changes().queryByTestId("split-diff")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();

    // Esc taken by an edit box closes only the box.
    const shown = () => !screen.getByTestId("session-changes").hidden;
    await waitFor(() => expect(changes().getByRole("button", { name: /@@/ })).toHaveProperty("disabled", false));
    fireEvent.click(changes().getByRole("button", { name: /@@/ }));
    fireEvent.keyDown(changes().getByRole("textbox"), { key: "Escape" });
    expect(changes().queryByTestId("hunk-editor")).toBeNull();
    expect(shown()).toBe(true);

    // An unsaved draft; ← Chat: the changes are hidden (kept, with the draft), the same chat is back (never torn
    // down), and the way back goes with them.
    fireEvent.click(changes().getByRole("button", { name: /@@/ }));
    fireEvent.change(changes().getByRole("textbox"), { target: { value: "keep this draft" } });
    fireEvent.click(screen.getByTestId("back-to-chat"));
    expect(shown()).toBe(false);
    expect(screen.getByTestId("session-changes").className).toContain("hidden");
    expect(screen.queryByTestId("back-to-chat")).toBeNull();
    expect(term()).toBe(terminal);
    expect(terminal.disposed).toBe(false);
    expect(shownAgent()).toBe("w1");
    // Reopened, the draft is as it was left.
    openChanges();
    expect(shown()).toBe(true);
    expect(changes().getByRole("textbox")).toHaveProperty("value", "keep this draft");
    // Esc (not in a box) goes back to the chat the same way; the draft stays through it too.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(shown()).toBe(false);
    expect(term()).toBe(terminal);
    expect(terminal.disposed).toBe(false);
    openChanges();
    expect(changes().getByRole("textbox")).toHaveProperty("value", "keep this draft");
    fireEvent.click(screen.getByTestId("back-to-chat"));
    // An external amend can change content without changing any git-status field.
    vi.useFakeTimers();
    // Opening reads afresh; an amend arrives through the stream.
    await act(async () => {
      openChanges();
      await vi.advanceTimersByTimeAsync(0);
    });
    daemon.data.sessionFiles.sup1["docs/readme.md"] = "amended\nsecond\nthird\n";
    await act(async () => { daemon.publish("session.changed", { root_id: "sup1" }); await vi.advanceTimersByTimeAsync(0); });
    expect(summary()).toContain("+5 −1");
    fireEvent.click(changes().getByTitle("docs/readme.md"));
    expect(changes().getByTestId("hunk").textContent).toContain("+amended");
    fireEvent.click(changes().getByTitle("src/a.ts"));
    expect(changes().getByRole("textbox")).toHaveProperty("value", "keep this draft");

    // Events and opening refreshes share one in-flight request and one queued refresh.
    let release!: () => void;
    holdDiff = new Promise<void>((resolve) => { release = resolve; });
    await act(async () => { daemon.publish("session.changed", { root_id: "sup1" }); await vi.advanceTimersByTimeAsync(0); });
    const pendingReads = diffReads;
    fireEvent.click(screen.getByTestId("back-to-chat"));
    await act(async () => {
      openChanges();
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(diffReads).toBe(pendingReads);
    holdDiff = null;
    await act(async () => { release(); await vi.advanceTimersByTimeAsync(0); });
    expect(diffReads).toBe(pendingReads + 1);

    // Opening Changes reads immediately, without advancing the periodic clock.
    fireEvent.click(screen.getByTestId("back-to-chat"));
    daemon.data.sessionFiles.sup1["docs/readme.md"] = "on activation\n";
    await act(async () => {
      openChanges();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(summary()).toContain("+3 −1");
    expect(changes().getByRole("textbox")).toHaveProperty("value", "keep this draft");
    fireEvent.click(changes().getByTitle("docs/readme.md"));
    expect(changes().getByTestId("hunk").textContent).toContain("+on activation");
    cleanup();
    const stoppedReads = diffReads;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(diffReads).toBe(stoppedReads);
  });

  it("shows the selected agent's pane, bare, named by provider and agent, through a tmux client on its window, and types into it as is", async () => {
    const child = { ...worker.agents![0], id: "c1", parent_id: "w1", provider: "codex", window: windowOf("c1") };
    const panes = { sup1: live, w1: { ...live, screen: "worker" }, c1: { ...live, screen: "reviewer" } };
    const { daemon, term } = mount(panes, { workspaces: [{ ...root, agents: [{ ...root.agents![0], status: "starting" }] }, { ...worker, agents: [...worker.agents!, child] }], inbox: [permissionInTerminal, permission] }, undefined, "sup1", ["sup1", "w1", "c1"]);
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    expect(title()).toBe("Claude Code · supervisor");
    // The provider session alone fills the view: no panel beside it.
    expect(screen.queryByRole("complementary")).toBeNull();

    await waitFor(() => expect(term().written).toBe(drawnLive));
    const [view] = daemon.viewers("sup1");
    expect(view.at).toEqual({ server: "brindle", target: windowOf("sup1") });
    expect(screen.getByTestId("terminal").dataset.renderer).toBe("dom");
    expect(screen.getByTestId("terminal").dataset.pty).toBe("1");
    // The provider's screen alone: no header of the app's naming the pane, no state card over it.
    await waitFor(() => expect(surface().dataset.card).toBe("live"));
    for (const status of ["waiting", "processing", "idle", "unknown"]) {
      act(() => daemon.publish("agent.status", { root_id: "sup1", agent_id: "sup1", payload: { status } }));
      await flush();
      expect(surface().dataset.card).toBe("live");
    }
    expect(screen.queryByTestId("state-card")).toBeNull();
    expect(surface().textContent).not.toContain("sup1");

    // What xterm.js says the person typed goes to the client as is: keys, a paste, a wheel report.
    term().type("\r");
    term().type("ls -la");
    term().type("\x1b[200~one\ntwo\x1b[201~");
    term().type("\x1b[<64;10;5M");
    expect(view.written).toEqual(["\r", "ls -la", "\x1b[200~one\ntwo\x1b[201~", "\x1b[<64;10;5M"]);
    daemon.print("sup1", "\x1b[1mdone\x1b[0m");
    await waitFor(() => expect(term().written).toContain("\x1b[1mdone\x1b[0m"));

    // Another agent picked (by the sidebar's tree, in the app): its pane, named by its branch; the view before it closed.
    pick("w1");
    await waitFor(() => expect(term().written).toBe("\x1b[?1049h\x1b[H\x1b[2Jworker\x1b[2;3H"));
    expect(shownAgent()).toBe("w1");
    expect(title()).toBe("Claude Code · ls");
    expect(view.closed).toBe(true);
    expect(daemon.viewers("w1")).toHaveLength(1);
    for (const id of ["c1", "sup1", "w1"] as const) {
      pick(id);
      await waitFor(() => expect(shownAgent()).toBe(id));
      await waitFor(() => expect(term().written).toContain(panes[id].screen.trimEnd().replaceAll("\n", "\r\n")));
      const open = daemon.views.filter((v) => !v.closed);
      expect(open).toHaveLength(1);
      expect(open[0]).toMatchObject({ pane: id, at: { server: "brindle", target: windowOf(id) } });
      if (id === "c1") expect(title()).toBe("Codex · claude");
    }
  });

  it.each([true, false])("observes Codex input only with visuals available (%s), excluding output and permission replies", async (enabled) => {
    const submitted: string[] = [];
    let bytesAtRecording: string | null = null;
    let release: () => void = () => {};
    const codex = { ...root, agents: root.agents!.map(a => ({ ...a, provider: "codex" })) };
    const { daemon, term } = mount({ sup1: live }, { workspaces: [codex] }, daemon => async (input, init) => {
      if (String(input).endsWith("/meta")) return Response.json({ ...(await (await daemon.fetch(input, init)).json()), visuals: enabled });
      if (String(input).endsWith("/visual-input")) {
        if (JSON.parse(String(init?.body)).begin || JSON.parse(String(init?.body)).refresh) return Response.json({ generation: 1 });
        bytesAtRecording = daemon.viewers("sup1")[0].written.join("");
        submitted.push(JSON.parse(String(init?.body)).text);
        await new Promise<void>(ok => { release = ok; });
        return Response.json({ observed: true });
      }
      return daemon.fetch(input, init);
    });
    await waitFor(() => expect(term().written).toBe(drawnLive));
    const [view] = daemon.viewers("sup1");
    term().type("Make a visual");
    term().type("\r");
    if (enabled) {
      await waitFor(() => expect(submitted).toHaveLength(1));
      expect(bytesAtRecording).toBe("Make a visual");
      await act(async () => release());
    }
    await waitFor(() => expect(view.written.join("")).toBe("Make a visual\r"));
    term().type("\r"); term().type("y\r");
    await waitFor(() => expect(view.written.join("")).toBe("Make a visual\r\ry\r"));
    daemon.print("sup1", "Make a visual\r");
    await waitFor(() => expect(term().written).toContain("Make a visual"));
    expect(submitted).toHaveLength(enabled ? 2 : 0);
  });

  it("shows a native card, never a shell, for an agent whose process isn't running, with what it left behind a disclosure", async () => {
    // The supervisor's process is gone (liveness says so, the pane is dead): an ended card with Continue.
    const { daemon, term } = mount({ sup1: { live: false, text: "$ \n" } });
    await waitFor(() => expect(daemon.open.length).toBeGreaterThan(0));
    daemon.send({ type: "liveness", agents: { sup1: false } });
    const card = await screen.findByTestId("state-card");
    expect(card.dataset.kind).toBe("ended");
    expect(card.textContent).toContain("Claude Code ended");
    expect(screen.queryByTestId("terminal")).toBeNull(); // nothing of the dead pane (its shell prompt) is on screen
    expect(daemon.viewers("sup1")).toEqual([]);
    expect(within(card).getByRole("button", { name: "Continue" })).toBeTruthy();
    // What it left: the dead pane tmux kept, through a tmux client on its window.
    fireEvent.click(within(card).getByRole("button", { name: "Show what it left" }));
    await waitFor(() => expect(term().written).toBe(`\x1b[?1049h\x1b[H\x1b[2J$ \r\n\r\n${PANE_DEAD_LINE}`));
    expect(daemon.viewers("sup1")).toHaveLength(1);
    expect(screen.getByTestId("session-status").textContent).toContain("stopped");

    // Continue: the pane is live again (the chat came back); the card goes, and a fresh view shows it.
    daemon.data.terminals.sup1 = live;
    fireEvent.click(within(screen.getByTestId("state-card")).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST").map((r) => r.path)).toEqual(["/sessions/sup1/continue"]));
    daemon.send({ type: "liveness", agents: { sup1: true } });
    await waitFor(() => expect(screen.queryByTestId("state-card")).toBeNull());
    await waitFor(() => expect(term().written).toBe(drawnLive));
    expect(daemon.viewers("sup1")).toHaveLength(1);
  });

  it("reopens a view whose client exited on request: a fresh view, the pane drawn again", async () => {
    const { daemon, term } = mount({ sup1: live });
    await waitFor(() => expect(term().written).toBe(drawnLive));
    const [first] = daemon.viewers("sup1");
    // The client exits (its view session went): a card over the surface, not a bare pane, with Reconnect.
    act(() => first.handlers.onExit(1));
    const card = await screen.findByTestId("state-card");
    expect(card.dataset.kind).toBe("disconnected");
    expect(card.textContent).toContain("The app's view of its pane closed.");
    fireEvent.click(within(card).getByRole("button", { name: "Reconnect" }));
    await waitFor(() => expect(screen.queryByTestId("state-card")).toBeNull());
    await waitFor(() => expect(daemon.viewers("sup1")).toHaveLength(1));
    expect(daemon.viewers("sup1")[0]).not.toBe(first);
    expect(first.closed).toBe(true);
    await waitFor(() => expect(term().written).toBe(drawnLive));
    daemon.print("sup1", "more");
    await waitFor(() => expect(term().written).toContain("more"));
  });

  it("shows a locked agent as a card, and a failed one saying why, with nothing popping up over it", async () => {
    const credentials: InboxItemDTO = { kind: "credentials", reason: "credentials need attention", agent_id: "w1", workspace_id: "proj/feat-ls", detail: "claude auth login" };
    const failed = { ...worker, agents: [{ ...worker.agents![0], status: "failed" }] };
    const locked = { ...root, agents: [{ ...root.agents![0], locked: "sam@example.com" }] };
    const { daemon } = mount({ sup1: live, w1: live }, { workspaces: [locked, failed], inbox: [credentials] });
    const card = await screen.findByTestId("state-card");
    expect(card.textContent).toContain("Locked by sam@example.com");
    expect(within(card).queryByRole("button", { name: "Continue" })).toBeNull();
    // Nothing of its conversation is opened.
    expect(daemon.views).toEqual([]);

    // The failed worker: its card says why; the sign-in itself is the Inbox card's, nothing opens over the session.
    pick("w1");
    const why = await screen.findByTestId("state-card");
    expect(why.dataset.kind).toBe("failed");
    expect(why.textContent).toContain("claude auth login");
    expect(screen.getByTestId("session-status").textContent).toContain("credentials need attention");
    expect(screen.queryByTestId("terminal")).toBeNull(); // its pane, a shell prompt by now, is never the surface
    expect(within(why).queryByRole("button", { name: /Sign in/ })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each([
    ["Close", "Confirm close", "/agents/w1/close"],
    ["Kill", "Confirm kill", "/agents/w1/kill"],
  ])("%s falls back to the supervisor", async (label, confirm, path) => {
    const { daemon } = mount({ sup1: live, w1: live });
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    pick("w1");
    await waitFor(() => expect(shownAgent()).toBe("w1"));

    // Close and kill wait behind the agent card's ⋯ menu, then ask once more.
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: label }));
    fireEvent.click(screen.getByRole("button", { name: confirm }));
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    expect(daemon.requests.filter((r) => r.method === "POST")).toEqual([{ method: "POST", path, query: {}, body: {} }]);
  });

  it("rewinds a worker from its ⋯ to a turn, on a profile, with a note, saying first what goes; a turn it has no snapshot of is said, and the fresh worker is shown", async () => {
    const profiles = [
      { name: "claude", description: "Claude Code", provider: "claude" },
      { name: "codex", description: "Codex", provider: "codex" },
    ];
    const { daemon, term } = mount({ sup1: live, w1: { ...live, screen: "worker pane" } }, { turns: { w1: 3 }, profiles });
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    pick("w1");
    await waitFor(() => expect(shownAgent()).toBe("w1"));

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.queryByRole("menuitem", { name: "Hand over…" })).toBeNull(); // the supervisor's alone
    fireEvent.click(screen.getByRole("menuitem", { name: "Rewind…" }));
    const dialog = screen.getByRole("dialog", { name: "Confirm rewind" });
    const go = () => within(dialog).getByRole("button", { name: /^Rewind to turn/ });
    expect((go() as HTMLButtonElement).disabled).toBe(true); // no turn yet
    await waitFor(() => expect(within(dialog).getByRole("option", { name: "codex" })).toBeTruthy());
    expect(within(dialog).queryByRole("option", { name: "claude" })).toBeNull(); // "Same (claude)" stands for it

    fireEvent.change(within(dialog).getByLabelText("Turn"), { target: { value: "5" } });
    fireEvent.change(within(dialog).getByLabelText("Profile"), { target: { value: "codex" } });
    fireEvent.change(within(dialog).getByLabelText("Note"), { target: { value: "keep the old flag" } });
    expect(screen.getByTestId("rewind-consequence").textContent).toContain("w1 is stopped and its worktree goes back to how it was after turn 5");
    fireEvent.click(go());
    // The job fails (rewind.rewind's own refusal): said in the dialog, nothing else changes.
    await waitFor(() => expect(within(dialog).getByText("w1 has no turn 5; its snapshots: 1, 2, 3")).toBeTruthy());
    expect(shownAgent()).toBe("w1");

    fireEvent.change(within(dialog).getByLabelText("Turn"), { target: { value: "2" } });
    fireEvent.click(go());
    expect(daemon.requests.filter((r) => r.method === "POST").map((r) => [r.path, r.body])).toEqual([
      ["/agents/w1/rewind", { to: 5, profile: "codex", note: "keep the old flag" }],
      ["/agents/w1/rewind", { to: 2, profile: "codex", note: "keep the old flag" }],
    ]);
    // The fresh worker takes w1's place, on codex, and is the one shown, streaming its own pane.
    await waitFor(() => expect(shownAgent()).toMatch(/^w1-r\d+$/));
    const fresh = shownAgent()!;
    expect(daemon.data.workspaces.flatMap((w) => w.agents ?? []).find((a) => a.id === fresh)?.profile).toBe("codex");
    await waitFor(() => expect(daemon.viewers(fresh)).toHaveLength(1));
    await waitFor(() => expect(term().written).toContain("worker pane"));
  });

  it("keeps a rewind's card through its worker leaving the tree, saying how it ended either way, with the profiles of the worker's own repo", async () => {
    // The worker works in an attached repo with its own profiles, not the session's.
    const LIB = "/home/u/lib";
    const away = { ...worker, id: "lib/feat-ls", path: `${LIB}/.brindle/worktrees/feat/ls` };
    const { daemon, sync } = mount(
      { sup1: live, w1: live },
      {
        repos: [{ repo_root: REPO, name: "proj" }, { repo_root: LIB, name: "lib" }],
        workspaces: [root, away],
        turns: { w1: 3 },
        profilesByRepo: { [REPO]: [{ name: "proj-local", description: "", provider: "claude" }], [LIB]: [{ name: "lib-local", description: "", provider: "claude" }] },
      },
    );
    daemon.holdJobs = true;
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    pick("w1");
    await waitFor(() => expect(shownAgent()).toBe("w1"));
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Rewind…" }));
    const dialog = () => screen.getByRole("dialog", { name: "Confirm rewind" });
    await waitFor(() => expect(within(dialog()).getByRole("option", { name: "lib-local" })).toBeTruthy());
    expect(within(dialog()).queryByRole("option", { name: "proj-local" })).toBeNull();
    expect(daemon.requests.filter((r) => r.path === "/profiles").map((r) => r.query.repo)).toEqual([LIB]);

    // Dismissed before its fresh worker exists: the view falls back to the supervisor, the card stays.
    fireEvent.change(within(dialog()).getByLabelText("Turn"), { target: { value: "2" } });
    fireEvent.change(within(dialog()).getByLabelText("Profile"), { target: { value: "lib-local" } });
    fireEvent.click(within(dialog()).getByRole("button", { name: "Rewind to turn 2" }));
    const dismissed = (id: string) => !daemon.data.workspaces.some((w) => w.agents?.some((a) => a.id === id));
    await waitFor(() => expect(dismissed("w1")).toBe(true));
    await act(() => sync.refresh()); // a snapshot taken in between
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    expect(within(dialog()).getByRole("button", { name: "Rewinding…" })).toBeTruthy();
    daemon.releaseJobs();
    await waitFor(() => expect(shownAgent()).toBe("w1-r2"));
    expect(screen.getByRole("dialog", { name: "Result" }).textContent).toContain("Rewound to turn 2: w1-r2 starts fresh");

    // Again, the spawn failing after the worker was dismissed: the failure is said in the card, over the supervisor.
    daemon.data.rewindFails = "claude isn't signed in";
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Rewind…" }));
    fireEvent.change(within(dialog()).getByLabelText("Turn"), { target: { value: "1" } });
    fireEvent.click(within(dialog()).getByRole("button", { name: "Rewind to turn 1" }));
    await waitFor(() => expect(dismissed("w1-r2")).toBe(true));
    await act(() => sync.refresh());
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    daemon.releaseJobs();
    await waitFor(() => expect(within(dialog()).getByText("claude isn't signed in")).toBeTruthy());
    expect(shownAgent()).toBe("sup1");
  });

  it("transfer: moves a scratch session's work into a repo from the supervisor's ⋯, saying first what happens; a folder outside a repo is refused there", async () => {
    const SCRATCH = "/home/u/.brindle/scratch/notes-20261020";
    const scratch = { ...root, id: "notes-20261020", name: "notes-20261020", path: SCRATCH, agents: [{ ...root.agents![0], id: "s1" }] };
    const { daemon, sync } = mount(
      { s1: live },
      {
        repos: [{ repo_root: REPO, name: "proj" }, { repo_root: SCRATCH, name: "notes-20261020" }],
        sessions: [{ ...session, id: "s1", workspace_id: "notes-20261020" }],
        workspaces: [scratch],
      },
      undefined,
      "s1",
      ["s1"],
      SCRATCH,
    );
    await waitFor(() => expect(shownAgent()).toBe("s1"));
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Move to a repo…" }));
    const dialog = screen.getByRole("dialog", { name: "Confirm transfer" });
    // The repos to move into: the real ones only, and any other folder.
    expect(within(dialog).getAllByRole("option").map((o) => o.textContent)).toEqual(["proj", "Another folder…"]);
    expect(screen.getByTestId("transfer-consequence").textContent).toContain("replayed onto a new branch brindle/from-notes-20261020 of proj");

    fireEvent.change(within(dialog).getByLabelText("Repository"), { target: { value: "" } });
    fireEvent.change(within(dialog).getByLabelText("Repository path"), { target: { value: "/home/u/Downloads" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Move the work" }));
    await waitFor(() => expect(within(dialog).getByText(/\/home\/u\/Downloads isn't inside a git repository/)).toBeTruthy());

    fireEvent.change(within(dialog).getByLabelText("Repository"), { target: { value: REPO } });
    fireEvent.change(within(dialog).getByLabelText("Branch"), { target: { value: "feat/notes" } });
    // The scratch root is paused and leaves the snapshot before the job ends: the card stays and says how it went.
    daemon.holdJobs = true;
    fireEvent.click(within(dialog).getByRole("button", { name: "Move the work" }));
    await waitFor(() => expect(daemon.data.workspaces.flatMap((w) => w.agents ?? []).some((a) => a.id === "s1")).toBe(false));
    await act(() => sync.refresh());
    await waitFor(() => expect(screen.queryByTestId("agent-id")).toBeNull());
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull(); // no agent left to act on
    expect(within(screen.getByRole("dialog", { name: "Confirm transfer" })).getByRole("button", { name: "Moving…" })).toBeTruthy();
    daemon.holdJobs = false;
    daemon.releaseJobs();
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Result" }).textContent).toContain("Moved 2 commits into proj on feat/notes"));
    expect(daemon.requests.filter((r) => r.method === "POST").map((r) => [r.path, r.body])).toEqual([
      ["/sessions/s1/transfer", { target_path: "/home/u/Downloads" }],
      ["/sessions/s1/transfer", { target_path: REPO, branch: "feat/notes" }],
    ]);
    expect(daemon.data.workspaces.some((w) => w.id === "proj/feat-notes")).toBe(true);
    expect(daemon.data.sessions.find((s) => s.id === "s1")?.status).toBe("paused");

    // As the engine answers while one runs: the same target again is that job, another a 409.
    const direct = daemon.client();
    const [first, again, elsewhere] = await Promise.allSettled([
      direct.transfer("s1", { target_path: REPO, branch: "feat/more" }),
      direct.transfer("s1", { target_path: REPO, branch: "feat/more" }),
      direct.transfer("s1", { target_path: REPO, branch: "feat/other" }),
    ]);
    expect(first.status === "fulfilled" && again.status === "fulfilled" && again.value.job === first.value.job).toBe(true);
    expect(elsewhere.status === "rejected" && (elsewhere.reason as { status: number }).status).toBe(409);
  });

  it("hands the session over from the supervisor's ⋯: a destination is required, the request carries it and the note, and the new session opens", async () => {
    const { daemon, opened } = mount({ sup1: live, w1: live });
    await waitFor(() => expect(shownAgent()).toBe("sup1"));
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Hand over…" }));
    const dialog = screen.getByRole("dialog", { name: "Confirm handover" });
    const confirm = within(dialog).getByRole("button", { name: "Confirm handover" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText("Branch or worktree"), { target: { value: "dev/next" } });
    fireEvent.change(within(dialog).getByLabelText("Handoff note"), { target: { value: "merge w1 first" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(opened).toEqual([["new1", REPO]]));
    expect(daemon.requests.filter((r) => r.method === "POST").map((r) => [r.path, r.body])).toEqual([["/sessions/sup1/handover", { to: "dev/next", note: "merge w1 first" }]]);
  });

  it("shows a paused session as a card that names it, and continues it from the bar: the engine's preflight first when it asks", async () => {
    const paused: SessionDTO = { ...session, id: "sup0", status: "paused", goal: "Refactor the cache" };
    const goal: GoalDTO = { root_id: "sup0", enabled: false, goal: "Refactor the cache", state: "paused", progress: 0, milestones: [{ id: 1, position: 1, title: "Read it", status: "passed" }, { id: 2, position: 2, title: "Write it", status: "pending" }] };
    const { daemon } = mount({}, { sessions: [session, paused], goal, preflight: { claude: [{ id: "trust", kind: "trust", label: "Trust the folder /home/u/proj", detail: "Claude Code asks once per folder.", done: false }] } }, undefined, "sup0");
    const card = await screen.findByTestId("state-card");
    expect(card.dataset.kind).toBe("ended");
    expect(card.textContent).toContain("is paused");
    expect(fake.Xterm.all).toHaveLength(0);
    expect(screen.getByTestId("session-status").textContent).toContain("paused");
    expect(screen.getByTestId("session-bar").textContent).not.toContain("Refactor the cache"); // the goal is the sidebar's
    // A paused root is in no snapshot, so with nothing of its own here every paused session would look the same.
    expect(screen.getByTestId("session-id").textContent).toBe("sup0");
    expect(screen.getByTestId("state-card-session").textContent).toBe("Refactor the cache");
    // A paused session's changes open too, read-only; ← Chat brings the card back.
    fireEvent.click(screen.getByTestId("open-changes"));
    await within(screen.getByTestId("session-changes")).findByRole("navigation", { name: "Changed files" });
    expect(screen.getByTestId("new-file")).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByTestId("back-to-chat"));
    expect(screen.getByTestId("session-changes").hidden).toBe(true);
    expect(screen.getByTestId("state-card")).toBeTruthy();

    // Continue, from the bar this time, is refused until Claude Code's preflight is settled: the engine's items
    // as native UI on the surface, no launch.
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "Continue" }));
    const preflight = await screen.findByTestId("preflight");
    expect(preflight.textContent).toContain("Before Claude Code can start");
    expect(preflight.textContent).toContain("Trust the folder /home/u/proj");
    expect(within(preflight).getByRole("button", { name: "Continue" })).toHaveProperty("disabled", true);
    fireEvent.click(within(preflight).getByRole("button", { name: "Trust this folder" }));
    await waitFor(() => expect(within(preflight).getByRole("button", { name: "Continue" })).toHaveProperty("disabled", false));
    expect(preflight.textContent).toContain("Claude Code is ready");
    fireEvent.click(within(preflight).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST").map((r) => r.path)).toEqual(["/sessions/sup0/continue", "/providers/claude/preflight/trust", "/sessions/sup0/continue"]));
    await waitFor(() => expect(screen.getByTestId("session-status").textContent).toContain("idle"));
    expect(screen.queryByTestId("preflight")).toBeNull();
  });
});

/* The app's pane view: a real tmux client on a pty, opened through PtyViews (the shell's in the app). */
describe("SessionScreen, the pty view", () => {
  it("replaces a missing pane at launch and follows recovery without applying an old pane check", async () => {
    const v = views({ kind: "ready", tmux: "tmux 3.8" });
    let pane = "%96";
    let status = "idle";
    let release: (() => void) | undefined;
    let hold = false;
    const { daemon, sync } = mount({}, { workspaces: [{ ...root, agents: [{ ...root.agents![0], window: pane }] }] }, (daemon) => async (input, init) => {
      if (String(input).endsWith("/agents/sup1")) {
        const response = new Response(JSON.stringify({ ...root.agents![0], window: pane, status, tmux_server: "brindle" }));
        if (hold) {
          hold = false;
          await new Promise<void>((resolve) => { release = resolve; });
        }
        return response;
      }
      return daemon.fetch(input, init);
    }, "sup1", ["sup1"], REPO, v.value);
    await waitFor(() => expect(screen.getByTestId("terminal").dataset.target).toBe("%96"));
    pane = "%102";
    act(() => v.opened[0].handlers.onError("tmux couldn't make the view: can't find pane: %96"));
    await waitFor(() => expect(screen.getByTestId("terminal").dataset.target).toBe("%102"));
    expect(screen.queryByTestId("state-card")).toBeNull();
    status = "paused";
    act(() => v.opened.at(-1)!.handlers.onError("tmux couldn't make the view: can't find pane: %102"));
    await waitFor(() => expect(screen.getByTestId("state-card").dataset.kind).toBe("ended"));
    expect(screen.queryByTestId("terminal")).toBeNull();
    expect(within(screen.getByTestId("state-card")).getByRole("button", { name: "Continue" })).toBeTruthy();
    hold = true;
    daemon.data.workspaces[0].agents![0].status = "waiting";
    act(() => daemon.publish("agent.status", { agent_id: "sup1", payload: { status: "waiting" } }));
    await waitFor(() => expect(release).toBeTruthy());
    await act(() => sync.idle());
    const oldView = v.opened.at(-1)!;
    status = "idle";
    pane = "%103";
    daemon.data.workspaces[0].agents![0].status = "idle";
    act(() => daemon.publish("agent.status", { agent_id: "sup1", payload: { status: "idle" } }));
    await act(() => sync.refresh());
    await waitFor(() => expect(screen.getByTestId("terminal").dataset.target).toBe("%103"));
    await act(async () => release?.());
    act(() => oldView.handlers.onError("old view closed"));
    expect(screen.queryByTestId("state-card")).toBeNull();
    status = "done";
    daemon.data.workspaces[0].agents![0].status = "done";
    act(() => daemon.publish("agent.status", { agent_id: "sup1", payload: { status: "done" } }));
    await act(() => sync.refresh());
    act(() => v.opened.at(-1)!.handlers.onError("can't find pane: %103"));
    expect(screen.queryByTestId("terminal")).toBeNull();
  });

  /* PtyViews recording each view the surface opens. */
  function views(status: PtyStatus) {
    const opened: { at: PtyViewTarget; size: { cols: number; rows: number }; handlers: PtyViewHandlers; written: string[]; closed: boolean }[] = [];
    const copied: string[] = [];
    const rechecks = { n: 0 };
    const value: PtyViews = {
      recheck: () => void rechecks.n++,
      status,
      open(at, size, handlers) {
        const view = { at, size, handlers, written: [] as string[], closed: false };
        opened.push(view);
        return { write: (d) => view.written.push(d), resize() {}, focus() {}, close: () => void (view.closed = true) };
      },
      copy: (text) => (copied.push(text), Promise.resolve()),
      paste: () => Promise.resolve(""),
    };
    return { value, opened, copied, rechecks };
  }

  it("says a browser tab, with no shell, can't show a pane, and neither can a window the engine names none for, offering to look again", async () => {
    const { sync } = mount({ sup1: live, w1: live }, {}, undefined, "sup1", ["sup1", "w1"], REPO, null);
    let card = await screen.findByTestId("state-card");
    expect(card.dataset.kind).toBe("failed");
    expect(card.textContent).toContain("Claude Code can't be shown");
    expect(card.textContent).toContain(NO_SHELL);
    expect(within(card).queryByRole("button", { name: "Restart" })).toBeNull();
    expect(screen.queryByTestId("terminal")).toBeNull();
    sync.stop();
    cleanup();

    const v = views({ kind: "ready", tmux: "tmux 3.8" });
    const unnamed = { ...worker, agents: worker.agents!.map((a) => ({ ...a, window: null })) };
    const again = mount({ sup1: live, w1: live }, { workspaces: [root, unnamed] }, undefined, "sup1", ["sup1", "w1"], REPO, v.value);
    await waitFor(() => expect(v.opened).toHaveLength(1));
    pick("w1");
    card = await screen.findByTestId("state-card");
    expect(card.textContent).toContain(NO_WINDOW);
    expect(within(card).getByRole("button", { name: "Check again" })).toBeTruthy();
    expect(v.opened).toHaveLength(1);
    again.sync.stop();
  });

  it("says why a view couldn't open (the agents' tmux too old), and offers to look again, which opens it afresh, not to restart the agent", async () => {
    const v = views({ kind: "ready", tmux: "tmux 3.8" });
    mount({ sup1: live, w1: live }, {}, undefined, "sup1", ["sup1", "w1"], REPO, v.value);
    await waitFor(() => expect(v.opened).toHaveLength(1));
    const message = "The agents' tmux server is tmux 3.0a, and brindle needs tmux 3.2 or newer to show a session. Update tmux, then reopen the session.";
    act(() => v.opened[0].handlers.onError(message));
    await waitFor(() => expect(screen.getByTestId("state-card").dataset.kind).toBe("failed"));
    const card = screen.getByTestId("state-card");
    expect(card.textContent).toContain("Claude Code can't be shown");
    expect(card.textContent).toContain(message);
    expect(within(card).queryByRole("button", { name: "Restart" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Check again" }));
    expect(v.rechecks.n).toBe(1);
    // A fresh view, the pane shown again once it opens.
    await waitFor(() => expect(v.opened).toHaveLength(2));
    expect(v.opened[1].at).toEqual(v.opened[0].at);
    expect(screen.queryByTestId("state-card")).toBeNull();
    expect(screen.getByTestId("terminal").dataset.pty).toBe("1");
  });

});
