import { connectDaemon, inTestContext } from "../../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SessionDTO } from "../../api/schema";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO, root, worker } from "../../test/fixtures";
import { NewSession } from ".";

afterEach(cleanup);

const running: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };
const paused: SessionDTO = { ...running, id: "sup2", status: "paused", goal: "Tidy the docs" };

const OTHER = "/home/u/other";

function setup(sessions: SessionDTO[], refresh?: () => Promise<void>) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], sessions, workspaces: [root, worker] });
  const client = daemon.client();
  const { store } = connectDaemon(daemon, client);
  const onOpen = vi.fn();
  render(inTestContext(<NewSession client={client} onOpen={onOpen} refresh={refresh} />, { client, store }, { daemon: true, sessions: true }));
  return { daemon, onOpen };
}

const send = () => screen.findByRole("button", { name: "Send" }).then((b) => waitFor(() => expect(b).toHaveProperty("disabled", false)).then(() => b));
const chevron = () => screen.queryByRole("button", { name: "Other ways to start" });
// The menu's row for a session, by the state it shows.
const row = (state: string) => within(screen.getByRole("menu")).getAllByRole("menuitem").find((el) => el.querySelector(`[data-state="${state}"]`))!;

describe("Send's menu", () => {
  it("opens the running session it lists, and resumes the paused one, each the one picked, starting nothing", async () => {
    const { daemon, onOpen } = setup([running, paused]);
    await send();
    fireEvent.click(chevron()!);
    fireEvent.click(row("idle"));
    expect(onOpen).toHaveBeenCalledWith("sup1", REPO);
    expect(screen.queryByRole("menu")).toBeNull();

    fireEvent.click(chevron()!);
    fireEvent.click(row("paused"));
    await waitFor(() => expect(onOpen).toHaveBeenLastCalledWith("sup2", REPO));
    // Only the paused one is resumed, and no session is started.
    expect(daemon.requests.filter((r) => r.method === "POST").map((r) => r.path)).toEqual(["/sessions/sup2/continue"]);
  });

  it("sends beside a paused session on a new branch in its own worktree, not in its checkout", async () => {
    const { daemon, onOpen } = setup([paused]);
    fireEvent.click(await send());
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    expect(daemon.data.sessions.find((s) => s.id === "new1")?.workspace_id).toMatch(/^proj\/session-/);
  });

  it("stays put when a resume answers after another project was picked", async () => {
    let answer!: () => void;
    const held = new Promise<void>((resolve) => (answer = resolve));
    const refresh = vi.fn(() => held);
    const { onOpen } = setup([paused], refresh);
    await send();
    fireEvent.click(chevron()!);
    fireEvent.click(row("paused"));
    await waitFor(() => expect(refresh).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Project: proj" }));
    fireEvent.click(within(screen.getByRole("menu", { name: "Project" })).getAllByRole("menuitemradio")[1]);
    await act(async () => {
      answer();
      await held;
    });
    // The resume is over (Send is usable again) and opened nothing.
    await send();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("has no chevron when nothing runs or is paused in the project", async () => {
    setup([]);
    await send();
    expect(chevron()).toBeNull();
  });
});
