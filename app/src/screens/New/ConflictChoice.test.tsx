import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { useState } from "react";
import { DaemonContext } from "../../api/context";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO, root } from "../../test/fixtures";
import { NewSession } from "./NewSession";

afterEach(cleanup);

const session = { id: "private-session-id", workspace_id: root.id, profile: "supervisor", status: "processing", mode: "interactive", created_at: 0, name: "Login fix", goal: "Repair expired sign-ins" };
function setup(daemon: FakeDaemon) {
  const client = daemon.client();
  const store = createAppStore();
  const sync = startSync({ client, store, connect: daemon.connect });
  function Harness() {
    const [opened, setOpened] = useState<{ root: string; repo: string } | null>(null);
    return <DaemonContext.Provider value={client}><StoreContext.Provider value={store}><SessionsProvider client={client}>
      {opened ? <output aria-label="Opened session" data-root={opened.root} data-repo={opened.repo} /> : <NewSession client={client} onOpen={(root, repo) => setOpened({ root, repo })} />}
    </SessionsProvider></StoreContext.Provider></DaemonContext.Provider>;
  }
  render(<Harness />);
  return { stop: () => sync.stop() };
}

async function openConflict() {
  const start = await screen.findByRole("button", { name: "Start" });
  await waitFor(() => expect(start).toHaveProperty("disabled", false));
  fireEvent.click(start);
  return screen.findByRole("group", { name: "Session already running" });
}

it("shows a single running session's context without an identifier or selector, and opens the current candidate after replacement", async () => {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], goal: {
    root_id: session.id, enabled: true, state: "running", progress: 99,
    milestones: [{ id: 1, position: 1, title: "Repair", status: "passed" }, { id: 2, position: 2, title: "Verify", status: "running" }],
  } });
  const { stop } = setup(daemon);
  try {
    const prompt = await openConflict();
    expect(within(prompt).queryByRole("radio")).toBeNull();
    expect(prompt.textContent).toContain(session.name);
    expect(prompt.textContent).toContain(session.goal);
    expect(prompt.textContent).not.toContain(session.id);
    expect(within(prompt).getByRole("status").getAttribute("data-state")).toBe(session.status);
    const progress = await within(prompt).findByRole("progressbar");
    expect(progress.getAttribute("aria-valuenow")).toBe("1");
    expect(progress.getAttribute("aria-valuemax")).toBe("2");
    const replacement = { ...session, id: "replacement-session-id", name: "Cache fix", goal: "Repair stale cache entries" };
    daemon.data.sessions = [replacement];
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(prompt.textContent).toContain(replacement.name));
    expect(screen.getByRole("group", { name: "Session already running" })).toBe(prompt);
    expect(within(prompt).queryByRole("radio")).toBeNull();
    const primary = prompt.querySelector<HTMLButtonElement>(".btn-primary")!;
    expect(document.activeElement).toBe(primary);
    expect(primary.getAttribute("aria-describedby")).toBeTruthy();
    fireEvent.click(primary);
    const opened = await screen.findByLabelText("Opened session");
    expect(opened.getAttribute("data-root")).toBe(replacement.id);
    expect(opened.getAttribute("data-repo")).toBe(REPO);
    expect(daemon.data.sessions).toHaveLength(1);
  } finally {
    stop();
  }
});

it("cancels by keyboard or button, and applies a choice to the selected session among several", async () => {
  const other = { ...session, id: "other-private-id", name: null, goal: "Fix cache invalidation" };
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, other], workspaces: [root] });
  const { stop } = setup(daemon);
  try {
    let prompt = await openConflict();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("group", { name: "Session already running" })).toBeNull());
    expect(daemon.data.sessions.map((s) => s.status)).toEqual(["processing", "processing"]);
    prompt = await openConflict();
    fireEvent.click(within(prompt).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Session already running" })).toBeNull());
    prompt = await openConflict();
    const radios = within(prompt).getAllByRole("radio");
    expect(radios[0]).toHaveProperty("checked", true);
    expect(radios[1].getAttribute("value")).toBe(other.id);
    fireEvent.click(radios[1]);
    expect(radios[1]).toHaveProperty("checked", true);
    expect(prompt.textContent).toContain(other.goal);
    expect(prompt.textContent).not.toContain(other.id);
    const updatedGoal = "Verify cache repair";
    daemon.data.sessions = [session, { ...other, goal: updatedGoal }];
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(prompt.textContent).toContain(updatedGoal));
    expect(within(prompt).getAllByRole("radio")[1]).toHaveProperty("checked", true);
    const choices = within(prompt).getAllByRole("button").filter((b) => b.hasAttribute("aria-describedby"));
    expect(choices).toHaveLength(3);
    choices.forEach((choice) => expect(document.getElementById(choice.getAttribute("aria-describedby")!)?.textContent).toBeTruthy());
    fireEvent.click(choices[2]);
    await screen.findByLabelText("Opened session");
    expect(daemon.data.sessions.find((s) => s.id === session.id)?.status).toBe("processing");
    expect(daemon.data.sessions.find((s) => s.id === other.id)?.status).toBe("paused");
    expect(daemon.data.sessions).toHaveLength(3);
  } finally {
    stop();
  }
});
