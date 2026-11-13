import { connectDaemon, inTestContext } from "../../test/harness";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Repo } from "../../api/client";
import { FakeDaemon, freshProviders } from "../../test/fakeDaemon";
import { REPO, root } from "../../test/fixtures";
import { NewSession } from ".";

afterEach(cleanup);

const OTHER = "/home/u/other";

function setup(daemon: FakeDaemon) {
  const { client, store } = connectDaemon(daemon);
  const onSignIn = vi.fn();
  render(inTestContext(<><NewSession client={client} onOpen={vi.fn()} onSignIn={onSignIn} /></>, { client, store }, { daemon: true, sessions: true }));
  return { onSignIn };
}

describe("New session's first page", () => {
  it.each<[string, Repo[], boolean]>([
    ["no project yet", [], true],
    ["a project", [{ repo_root: REPO, name: "proj" }], false],
  ])("with %s, the practice hero shows only before any project", async (_, repos, hero) => {
    setup(new FakeDaemon({ repos, sessions: [], workspaces: [root] }));
    // The hero (the first run), else the composer with the practice run's small row under it.
    await screen.findByRole("region", { name: hero ? "Welcome" : "Practice run" });
    expect(screen.queryByRole("region", { name: "Welcome" }) !== null).toBe(hero);
    expect(screen.queryByRole("form", { name: "Start a session" }) !== null).toBe(!hero);
  });

  it("arrived from a finished practice run's Open a folder…, picks the folder once: the composer the new project brings doesn't ask again", async () => {
    const daemon = new FakeDaemon({ repos: [], sessions: [], workspaces: [root] });
    const { client, store } = connectDaemon(daemon);
    const pickFolder = vi.fn(() => Promise.resolve(REPO));
    render(inTestContext(<><NewSession client={client} onOpen={vi.fn()} pickFolder={pickFolder} add="open" /></>, { client, store }, { daemon: true, sessions: true }));
    await screen.findByRole("form", { name: "Start a session" });
    await waitFor(() => expect(daemon.opened).toEqual([REPO]));
    expect(pickFolder).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("region", { name: "Add a project" })).toBeNull();
  });

  it("opens the project chip's menu (projects, then adding one) and the CLI chip's (signed-in CLIs, then sign in to more)", async () => {
    localStorage.clear();
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }],
      sessions: [],
      workspaces: [root],
      providers: freshProviders({ claude: { path: "/bin/claude", version: "2" }, codex: { path: "/bin/codex", version: "1" } }),
    });
    const { onSignIn } = setup(daemon);

    fireEvent.click(await screen.findByRole("button", { name: "Project: proj" }));
    let projects = screen.getByRole("menu", { name: "Project" });
    expect(within(projects).getAllByRole("menuitemradio").map((item) => item.getAttribute("aria-checked"))).toEqual(["true", "false"]);
    fireEvent.click(within(projects).getAllByRole("menuitemradio")[1]);
    expect(screen.queryByRole("menu")).toBeNull();
    // The second project is now the one a start goes to.
    fireEvent.click(screen.getByRole("button", { name: "Project: other" }));
    projects = screen.getByRole("menu", { name: "Project" });
    // Its actions: the second adds a project by cloning, in place under the composer.
    fireEvent.click(within(projects).getAllByRole("menuitem")[1]);
    expect(within(screen.getByRole("region", { name: "Add a project" })).getByRole("textbox", { name: "Repository URL" })).toBeTruthy();

    const cli = screen.getByRole("button", { name: /^Supervisor CLI:/ });
    await waitFor(() => expect(cli).toHaveProperty("disabled", false));
    fireEvent.click(cli);
    const clis = screen.getByRole("menu", { name: "Supervisor CLI" });
    expect(within(clis).getAllByRole("menuitemradio")).toHaveLength(2);
    fireEvent.click(within(clis).getByRole("menuitem"));
    expect(onSignIn).toHaveBeenCalled();
  });
});
