import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonContext } from "../../api/context";
import type { SessionDTO } from "../../api/schema";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { REPO, root, worker } from "../../test/fixtures";
import { NewSession } from ".";

afterEach(cleanup);

const OTHER = "/home/u/other";
const live: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

function setup(daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], sessions: [live], workspaces: [root, worker] }), client = daemon.client()) {
  const store = createAppStore();
  const sync = startSync({ client, store, connect: daemon.connect });
  const onOpen = vi.fn();
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <NewSession client={client} onOpen={onOpen} />
        </SessionsProvider>
      </StoreContext.Provider>
    </DaemonContext.Provider>,
  );
  return { daemon, onOpen, stop: () => sync.stop() };
}

const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST");

describe("NewSession", () => {
  it("starts a session from the composer past a running one, and opens it", async () => {
    const { daemon, onOpen, stop } = setup();
    const prompt = await screen.findByLabelText("What should brindle work on?");
    fireEvent.change(prompt, { target: { value: "Add a cache" } });
    fireEvent.change(screen.getByLabelText("Repo"), { target: { value: REPO } });
    // Branch, CLI and autopilot wait behind Options.
    expect(screen.queryByLabelText("Autopilot")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Options/ }));
    fireEvent.change(screen.getByLabelText("Autopilot"), { target: { value: "on" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    // sup1 is running here: the engine asks what to do with it first.
    const ask = await screen.findByRole("group", { name: "Session already running" });
    expect(ask.textContent).toContain("sup1");
    fireEvent.click(within(ask).getByRole("button", { name: "Pause it and start fresh" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    expect(posts(daemon).map((r) => r.body)).toEqual([
      { repo: REPO, autopilot: true, prompt: "Add a cache" },
      { repo: REPO, autopilot: true, prompt: "Add a cache", choice: "p", session_id: "sup1" },
    ]);
    stop();
  });

  it("opens a session it started once the sessions list has it, so it arrives not paused (#216)", async () => {
    // A first launch: nothing has ever run in the project. Opened before the list is read again, the
    // session view finds the root in neither the list nor the snapshot, and takes it for paused.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [{ ...root, agents: [] }] });
    const { onOpen, stop } = setup(daemon);
    const listed: boolean[] = [];
    onOpen.mockImplementation(() => {
      const start = daemon.requests.findIndex((r) => r.method === "POST" && r.path === "/sessions");
      listed.push(daemon.requests.some((r, i) => i > start && r.method === "GET" && r.path === "/sessions"));
    });
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix the flaky test" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    expect(listed).toEqual([true]);
    stop();
  });

  it("shows the engine's preflight as native UI when the CLI needs setting up, and starts once it is settled", async () => {
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      workspaces: [root],
      config: { user: {}, repo: {}, local: {}, keys: [] },
      preflight: {
        claude: [
          { id: "key", kind: "key", label: "An Anthropic API key", detail: "Claude Code runs on the console's billing here.", done: false, key_name: "ANTHROPIC_API_KEY" },
          { id: "consent", kind: "consent", label: "Agree to Claude Code's terms", done: false },
        ],
      },
    });
    const { onOpen, stop } = setup(daemon);
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Add a cache" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    // Refused with 409 {preflight}: no launch, the items as a card, Start disabled until they are done.
    const card = await screen.findByTestId("preflight");
    expect(card.textContent).toContain("Claude Code asks for 2 things first");
    expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", true);
    expect(onOpen).not.toHaveBeenCalled();
    // The key goes in a password field, to PUT /keys/{name}; the value never shows again.
    const key = within(card).getByLabelText("ANTHROPIC_API_KEY value");
    expect(key.getAttribute("type")).toBe("password");
    fireEvent.change(key, { target: { value: "sk-ant-test-0000" } });
    fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(daemon.keysStored).toEqual(["ANTHROPIC_API_KEY"]));
    expect(screen.queryByDisplayValue("sk-ant-test-0000")).toBeNull();
    // The consent is a button; the fake settles the key with the stored key.
    daemon.data.preflight.claude[0].done = true;
    fireEvent.click(within(card).getByRole("button", { name: "Agree" }));
    await waitFor(() => expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", false));
    fireEvent.click(within(card).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    expect(posts(daemon).map((r) => r.path)).toEqual(["/sessions", "/providers/claude/preflight/consent", "/sessions"]);
    stop();
  });

  it("shows a failed load with a retry, and welcomes a first-time user with no repo yet", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [live] });
    const client = daemon.client();
    daemon.token = "rotated";
    const { stop } = setup(daemon, client);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("missing or wrong token");
    daemon.token = TOKEN;
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByLabelText("What should brindle work on?")).toBeTruthy();
    stop();
    cleanup();

    const { stop: stop2 } = setup(new FakeDaemon());
    expect((await screen.findByRole("region", { name: "Welcome" })).textContent).toContain("No repo is set up yet");
    expect(screen.queryByLabelText("What should brindle work on?")).toBeNull();
    stop2();
  });

  it("adds a project as onboarding does: with none yet the welcome opens a folder, after it Add a project… clones one, and the composer starts in it", async () => {
    const daemon = new FakeDaemon();
    const client = daemon.client();
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect });
    const pickFolder = vi.fn(() => Promise.resolve(REPO));
    render(
      <DaemonContext.Provider value={client}>
        <StoreContext.Provider value={store}>
          <SessionsProvider client={client}>
            <NewSession client={client} onOpen={vi.fn()} pickFolder={pickFolder} />
          </SessionsProvider>
        </StoreContext.Provider>
      </DaemonContext.Provider>,
    );
    const welcome = await screen.findByRole("region", { name: "Welcome" });
    expect(welcome.textContent).not.toContain("In a terminal");
    fireEvent.click(within(welcome).getByRole("button", { name: "Open a folder…" }));
    expect(await screen.findByLabelText("What should brindle work on?")).toBeTruthy();
    expect(daemon.opened).toEqual([REPO]);

    // Another, cloned: it is listed and selected.
    fireEvent.click(screen.getByRole("button", { name: "Add a project…" }));
    const adding = screen.getByRole("region", { name: "Add a project" });
    fireEvent.change(within(adding).getByRole("textbox", { name: "Repository URL" }), { target: { value: "git@github.com:sam/site.git" } });
    fireEvent.click(within(adding).getByRole("button", { name: "Clone" }));
    await waitFor(() => expect((screen.getByRole("combobox", { name: "Repo" }) as HTMLSelectElement).value).toBe("~/Projects/site"));
    expect(screen.queryByRole("region", { name: "Add a project" })).toBeNull();
    sync.stop();
  });
});
