import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import { PtyViewsProvider } from "./api/ptyView";
import { Sheet } from "./components/Sheet";
import { createAppStore } from "./store/store";
import { startSync } from "./store/sync";
import { FakeDaemon } from "./test/fakeDaemon";
import { REPO, question, root, worker } from "./test/fixtures";
import { standInViews } from "./test/panes";
import type { Route } from "./routes";

const terminal = vi.hoisted(() => ({ mount: null as Promise<void> | null }));

// xterm adds its helper textarea after its pane mounts (and loads asynchronously).
vi.mock("./components/PtyTerminal", async () => {
  const { useEffect, useState } = await import("react");
  return {
    PtyTerminal: ({ at }: { at: { target: string } }) => {
      const [ready, setReady] = useState(!terminal.mount);
      useEffect(() => {
        let mounted = true;
        void terminal.mount?.then(() => { if (mounted) setReady(true); });
        return () => { mounted = false; };
      }, []);
      return <div data-testid="terminal" data-target={at.target}>{ready && <textarea className="xterm-helper-textarea" aria-label="Terminal input" />}</div>;
    },
  };
});

beforeEach(() => {
  localStorage.clear();
  terminal.mount = null;
});
afterEach(cleanup);

function mount(initialRoute: Route = { screen: "inbox" }, overlay = false) {
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 1, goal: "Ship ls --json" }],
    workspaces: [root, worker],
    inbox: [question, { ...question, agent_id: "w1" }],
  });
  const store = createAppStore();
  const client = daemon.client();
  const sync = startSync({ client, store, connect: daemon.connect });
  const view = render(
    <PtyViewsProvider value={standInViews}>
      <App store={store} client={client} refresh={sync.refresh} initialRoute={initialRoute} />
      {overlay && <Sheet label="Other dialog" title="Other dialog" onClose={() => {}}>Keep focus here</Sheet>}
    </PtyViewsProvider>,
  );
  return { daemon, sync, ...view };
}

const sidebar = () => screen.getByRole("complementary", { name: "Sidebar" });
const textarea = () => screen.getByRole("textbox", { name: "Terminal input" });

async function openInboxItem(agent: string) {
  const inbox = await screen.findByRole("region", { name: "Inbox" });
  const card = await waitFor(() => {
    const cards = inbox.querySelectorAll('[data-kind="question"]');
    expect(cards.length).toBe(2);
    return cards[agent === "sup1" ? 0 : 1] as HTMLElement;
  });
  fireEvent.click(within(card).getByRole("button", { name: "Go to session ›" }));
}

it.each(["sup1", "w1"])("focus from inbox: opens the item's agent %s with keyboard focus in its terminal", async (agent) => {
  const { sync } = mount();
  try {
    await openInboxItem(agent);
    await waitFor(() => expect(document.activeElement).toBe(textarea()));
    expect(screen.getByTestId("terminal").getAttribute("data-target")).toContain(`:${agent}`);
  } finally { sync.stop(); }
});

it("focus from inbox: the session heading link focuses a textarea that mounts later", async () => {
  let ready!: () => void;
  terminal.mount = new Promise<void>((resolve) => { ready = resolve; });
  const { sync } = mount();
  try {
    fireEvent.click(await screen.findByRole("button", { name: "Open the session ›" }));
    await screen.findByTestId("terminal");
    expect(screen.queryByRole("textbox", { name: "Terminal input" })).toBeNull();
    await act(async () => ready());
    await waitFor(() => expect(document.activeElement).toBe(textarea()));
  } finally { sync.stop(); }
});

it("focus from inbox: sidebar agent navigation focuses the new pane without stealing focus on refresh", async () => {
  const { sync } = mount();
  try {
    await openInboxItem("sup1");
    await waitFor(() => expect(document.activeElement).toBe(textarea()));
    fireEvent.click(sidebar().querySelector<HTMLButtonElement>('[data-tree-agent="w1"]')!);
    await waitFor(() => expect(document.activeElement).toBe(textarea()));
    expect(screen.getByTestId("terminal").getAttribute("data-target")).toContain(":w1");
    const inboxButton = within(sidebar()).getByRole("button", { name: /^Inbox/ });
    inboxButton.focus();
    await act(async () => { await sync.refresh(); });
    expect(document.activeElement).toBe(inboxButton);
  } finally { sync.stop(); }
});

it("focus from inbox: an open review sheet keeps focus until dismissed", async () => {
  const { sync } = mount({ screen: "session", root: "sup1", repo: REPO, sheet: { kind: "review" } });
  try {
    const dialog = await screen.findByRole("dialog");
    await screen.findByRole("textbox", { name: "Terminal input" });
    expect(document.activeElement).toBe(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Close sheet" }));
    await waitFor(() => expect(document.activeElement).toBe(textarea()));
  } finally { sync.stop(); }
});

it("focus from inbox: a dialog outside the session keeps focus when a delayed terminal mounts", async () => {
  let ready!: () => void;
  terminal.mount = new Promise<void>((resolve) => { ready = resolve; });
  const { sync } = mount({ screen: "inbox" }, true);
  try {
    const dialog = screen.getByRole("dialog", { name: "Other dialog" });
    await openInboxItem("sup1");
    await screen.findByTestId("terminal");
    await act(async () => ready());
    expect(textarea()).toBeTruthy();
    expect(document.activeElement).toBe(dialog);
  } finally { sync.stop(); }
});
