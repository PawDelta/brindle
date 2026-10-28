import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  it.each([["claude", false, false], ["antigravity", false, false], ["antigravity", true, false], ["claude", false, true], ["claude", false, "manual"]] as const)("unblocks %s sign-in using unresolved preflight items (API key: %s, cloud: %s)", async (provider, key, cloud) => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], workspaces: [root], sessions: [] });
    let signed = false;
    let agreed = !!cloud || provider !== "claude";
    let checked = !cloud;
    const context = cloud ? { repo: REPO, profile: "company-developer" } : undefined;
    const fetch = daemon.fetch;
    daemon.fetch = async (input, init) => {
      const path = new URL(String(input)).pathname;
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      const selected = body.repo === REPO && body.profile === "company-developer";
      if (cloud && path.endsWith("/login")) {
        if (!selected) return Response.json({ error: { code: "invalid", message: "Missing identity" } }, { status: 400 });
        signed = true;
      }
      if (cloud && path.endsWith("/preflight") && init?.method === "POST" && selected) checked = signed;
      if (path.endsWith("/consent") && JSON.parse(String(init?.body)).accept) {
        agreed = true;
        return Response.json({ recorded: true });
      }
      const items = (signed && checked) || (key && daemon.keysStored.includes("GEMINI_API_KEY"))
        ? agreed ? [] : [{ id: "claude.onboarding", provider, kind: "consent", title: "Security notes", detail: "", action: { method: "POST", path: "/v1/providers/claude/consent", body: { id: "claude.onboarding" } } }]
        : key ? [{ id: "key.GEMINI_API_KEY", provider, kind: "key", title: "Add your Gemini API key", detail: "", action: { name: "set_key", method: "PUT", path: "/v1/keys/GEMINI_API_KEY" } }]
        : [{ id: `${provider}.signin`, provider, kind: "signin", title: "Sign in", detail: "", action: { name: cloud === "manual" ? "check" : "login", method: "POST", path: `/v1/providers/${provider}/${cloud === "manual" ? "preflight" : "login"}`, body: context } }];
      if (path.endsWith("/preflight")) return Response.json(items);
      if (path.endsWith("/sessions") && init?.method === "POST" && items.length) return Response.json({ error: { code: "preflight", message: "Sign in" }, preflight: items }, { status: 409 });
      return fetch(input, init);
    };
    const { onOpen, stop } = setup(daemon);
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix login" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    const card = await screen.findByTestId("preflight");
    expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", true);
    if (key) {
      fireEvent.change(within(card).getByLabelText(/ value$/), { target: { value: "fixture-key" } });
      fireEvent.click(within(card).getByRole("button", { name: "Save" }));
    } else if (cloud === "manual") {
      signed = true;
      fireEvent.click(within(card).getByRole("button", { name: "Check again" }));
    } else {
      vi.useFakeTimers();
      try {
        await act(async () => { fireEvent.click(within(card).getByRole("button", { name: /Sign in to/ })); });
        if (!cloud) signed = true;
        await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
        expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", !agreed);
      } finally {
        vi.useRealTimers();
      }
    }
    if (!agreed) fireEvent.click(within(card).getByRole("button", { name: "Agree" }));
    await waitFor(() => expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", false));
    fireEvent.click(within(card).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    stop();
  });

  it("starts a session from the composer past a running one, and opens it", async () => {
    const { daemon, onOpen, stop } = setup();
    const prompt = await screen.findByLabelText("What should brindle work on?");
    fireEvent.change(prompt, { target: { value: "Add a cache" } });
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

  it("lists a named session by its name in the running chooser, and a name given at creation goes in the start request and is kept", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [{ ...live, name: "Login fix" }], workspaces: [root, worker] });
    const { onOpen, stop } = setup(daemon);
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Add a cache" } });
    fireEvent.click(screen.getByRole("button", { name: /Options/ }));
    fireEvent.change(screen.getByLabelText("Session name"), { target: { value: "Cache" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    const ask = await screen.findByRole("group", { name: "Session already running" });
    expect(ask.textContent).toContain("Login fix");
    fireEvent.click(within(ask).getByRole("button", { name: "New session in its own worktree" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    // The chosen start carries the name too, and the engine lists the new session by it.
    expect(posts(daemon).at(-1)!.body).toMatchObject({ choice: "n", name: "Cache" });
    expect(daemon.data.sessions.find((s) => s.id === "new1")?.name).toBe("Cache");
    stop();
  });

  it("filters the project list as you type, picks with the keyboard, and closes on an outside click or Escape", async () => {
    const { stop } = setup();
    const repo = await screen.findByRole("combobox", { name: "Repo" });
    fireEvent.focus(repo);
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["proj", "other"]);
    fireEvent.change(repo, { target: { value: "oth" } });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["other"]);
    fireEvent.keyDown(repo, { key: "Enter" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect((repo as HTMLInputElement).value).toBe("other");

    fireEvent.focus(repo);
    fireEvent.keyDown(repo, { key: "ArrowUp" });
    fireEvent.keyDown(repo, { key: "ArrowDown" });
    fireEvent.keyDown(repo, { key: "ArrowDown" });
    fireEvent.keyDown(repo, { key: "Enter" });
    expect((repo as HTMLInputElement).value).toBe("other");

    fireEvent.focus(repo);
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.focus(repo);
    fireEvent.keyDown(repo, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();

    // Tabbing away closes it.
    fireEvent.focus(repo);
    fireEvent.blur(repo, { relatedTarget: screen.getByRole("button", { name: "Add a project…" }) });
    expect(screen.queryByRole("listbox")).toBeNull();
    stop();
  });

  it("scrolls the option an arrow key reaches into view, and Enter picks it", async () => {
    const names = ["p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7"];
    const repos = names.map((name) => ({ repo_root: `/home/u/${name}`, name }));
    const { stop } = setup(new FakeDaemon({ repos, sessions: [], workspaces: [] }));
    const original = Element.prototype.scrollIntoView;
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled(this.textContent);
    };
    try {
      const repo = await screen.findByRole("combobox", { name: "Repo" });
      fireEvent.focus(repo);
      scrolled.mockClear();
      for (let i = 0; i < 6; i++) fireEvent.keyDown(repo, { key: "ArrowDown" });
      expect(scrolled).toHaveBeenLastCalledWith("p6");
      fireEvent.keyDown(repo, { key: "Enter" });
      expect((repo as HTMLInputElement).value).toBe("p6");
    } finally {
      Element.prototype.scrollIntoView = original;
      stop();
    }
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
    await waitFor(() => expect((screen.getByRole("combobox", { name: "Repo" }) as HTMLInputElement).value).toBe("site"));
    expect(screen.queryByRole("region", { name: "Add a project" })).toBeNull();
    sync.stop();
  });
});
