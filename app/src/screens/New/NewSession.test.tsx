import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonContext } from "../../api/context";
import type { SessionDTO } from "../../api/schema";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { FakeDaemon, freshProviders, TOKEN } from "../../test/fakeDaemon";
import { REPO, root, worker } from "../../test/fixtures";
import { NewSession } from ".";

afterEach(cleanup);

const OTHER = "/home/u/other";
const live: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

function setup(daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], sessions: [live], workspaces: [root, worker] }), client = daemon.client()) {
  const store = createAppStore();
  const sync = startSync({ client, store, connect: daemon.connect });
  const onOpen = vi.fn();
  const onSignIn = vi.fn();
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <NewSession client={client} onOpen={onOpen} onSignIn={onSignIn} />
        </SessionsProvider>
      </StoreContext.Provider>
    </DaemonContext.Provider>,
  );
  return { daemon, onOpen, onSignIn, stop: () => sync.stop() };
}

const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST");

// Start stays disabled until the repo's usable CLIs are known; a click before then does nothing.
const startButton = () => screen.findByRole("button", { name: "Start" }).then((b) => waitFor(() => expect(b).toHaveProperty("disabled", false)).then(() => b));

describe("NewSession", () => {
  it("asks for a git identity when the repo has none", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], workspaces: [root], sessions: [] });
    daemon.gitIdentityMissing.add(REPO);
    const fetch = daemon.fetch;
    let busy = true;
    daemon.fetch = async (input, init) => {
      if (new URL(String(input)).pathname.endsWith("/sessions") && init?.method === "POST" && !daemon.gitIdentityMissing.has(REPO) && busy) {
        busy = false;
        return Response.json({ error: { code: "busy", message: "No room to start yet." } }, { status: 503 });
      }
      return fetch(input, init);
    };
    const { onOpen, stop } = setup(daemon);
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix login" } });
    fireEvent.click(await startButton());
    let form = await screen.findByRole("region", { name: "Git identity" });
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(onOpen).not.toHaveBeenCalled();
    expect(daemon.data.sessions).toHaveLength(0);
    fireEvent.click(await startButton());
    form = await screen.findByRole("region", { name: "Git identity" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "A Person" } });
    fireEvent.change(within(form).getByLabelText("Email"), { target: { value: "person@example.com" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save and start" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("region", { name: "Git identity" })).toBeNull();
    fireEvent.click(await startButton());
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    expect(daemon.gitIdentityMissing.has(REPO)).toBe(false);
    stop();
  });

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
    // Start stays disabled until the usable CLIs are known.
    await waitFor(() => expect(screen.getByRole("button", { name: "Start" })).toHaveProperty("disabled", false));
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
    if (!agreed) fireEvent.click(within(card).getByRole("checkbox", { name: /Consent/ }));
    await waitFor(() => expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", false));
    fireEvent.click(within(card).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    stop();
  });

  it.each([
    ["installs Claude Code, checks again", undefined],
    ["starts on Codex instead", "codex"],
    // Codex's own preflight answers before the earlier check of Claude does: Codex's card stays.
    ["checks again, starts on Codex before that check answers", "race"],
    // The start on Codex fails outright: Claude Code's card stays, and its checks still count.
    ["starts on Codex, which fails, then installs Claude Code", "fail"],
    // Another project is picked and its usable-CLI lookup fails: the earlier project's alternatives don't carry over.
    ["switches project, whose usable lookup fails, then starts on Codex once a later one answers", "switch"],
  ] as const)("gets past a CLI that isn't installed: %s", async (_, instead) => {
    // Claude Code neither installed nor signed in, Codex installed: the engine's own 409 for a start on Claude.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], workspaces: [root], sessions: [], providers: freshProviders({ codex: { path: "/opt/homebrew/bin/codex", version: "0.48.0" } }) });
    let installed = false;
    let answerCheck: (() => void) | null = null;
    let usableLookup: "ok" | "hold" | "fail" = "ok";
    let failLookup: (() => void) | null = null;
    const started: (string | undefined)[] = [];
    const missing = [{ kind: "setup", id: "claude.install", provider: "claude", title: "Install Claude Code", detail: "Claude Code (`claude`) isn't on PATH.", action: null, link: null }];
    const codexSignIn = [{ kind: "signin", id: "codex.signin", provider: "codex", title: "Sign in to Codex", detail: "", action: { name: "check", method: "POST", path: "/v1/providers/codex/preflight" } }];
    const fetch = daemon.fetch;
    daemon.fetch = async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/providers/usable") && usableLookup !== "ok") {
        if (usableLookup === "hold") await new Promise<void>((r) => (failLookup = r));
        return Response.json({ error: { code: "internal", message: "down" } }, { status: 500 });
      }
      if (path.endsWith("/providers/claude/preflight")) {
        if (instead === "race") await new Promise<void>((r) => (answerCheck = r));
        return Response.json(installed ? [] : missing);
      }
      if (path.endsWith("/sessions") && init?.method === "POST") {
        const provider = JSON.parse(String(init.body)).provider as string | undefined;
        started.push(provider);
        if (!provider && !installed) return Response.json({ preflight: missing }, { status: 409 });
        if (instead === "race") return Response.json({ preflight: codexSignIn }, { status: 409 });
        if (instead === "fail" && provider) return Response.json({ error: { code: "internal", message: "Codex didn't start" } }, { status: 500 });
      }
      return fetch(input, init);
    };
    const { onOpen, stop } = setup(daemon);
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix login" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Start" })).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    const card = await screen.findByTestId("preflight");
    expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", true);
    if (instead === "race") {
      installed = true;
      fireEvent.click(within(card).getByRole("button", { name: "Check again" }));
      await waitFor(() => expect(answerCheck).not.toBeNull());
      fireEvent.click(await within(card).findByRole("button", { name: "Start on Codex instead" }));
      await screen.findByRole("region", { name: "Before Codex can start" });
      await act(async () => answerCheck?.());
      // The late answer about Claude neither replaces Codex's card nor readies a start on Claude.
      const now = screen.getByTestId("preflight");
      expect(now.getAttribute("aria-label")).toBe("Before Codex can start");
      expect(within(now).getByRole("button", { name: "Start" })).toHaveProperty("disabled", true);
      expect(started).toEqual([undefined, "codex"]);
      stop();
      return;
    }
    if (instead === "switch") {
      usableLookup = "hold";
      fireEvent.focus(screen.getByRole("combobox", { name: "Repo" }));
      fireEvent.mouseDown(screen.getByRole("option", { name: "other" }));
      await waitFor(() => expect(failLookup).not.toBeNull());
      expect(screen.queryByRole("button", { name: "Start on Codex instead" })).toBeNull();
      usableLookup = "fail";
      await act(async () => failLookup?.());
      await screen.findByText("Couldn't check which CLIs can run here.");
      expect(screen.queryByRole("button", { name: "Start on Codex instead" })).toBeNull();
      expect(started).toEqual([undefined]);
      // A later lookup answers: Codex is offered again, and starts explicitly.
      usableLookup = "ok";
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      fireEvent.click(await screen.findByRole("button", { name: "Start on Codex instead" }));
    } else if (instead === "fail") {
      fireEvent.click(await within(card).findByRole("button", { name: "Start on Codex instead" }));
      expect((await screen.findByRole("alert")).textContent).toContain("Codex didn't start");
    }
    if (instead === "codex") {
      // Without Claude Code: the same start on the CLI this Mac has.
      fireEvent.click(await within(card).findByRole("button", { name: "Start on Codex instead" }));
    } else if (instead !== "switch") {
      // Installed meanwhile: asked again, the card lets the start go.
      installed = true;
      fireEvent.click(within(card).getByRole("button", { name: "Check again" }));
      await waitFor(() => expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", false));
      fireEvent.click(within(card).getByRole("button", { name: "Start" }));
    }
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", instead === "switch" ? OTHER : REPO));
    expect(started).toEqual(instead === "fail" ? [undefined, "codex", undefined] : [undefined, instead === "switch" ? "codex" : instead]);
    stop();
  });

  it("starts a session from the composer past a running one, and opens it", async () => {
    const { daemon, onOpen, stop } = setup();
    const prompt = await screen.findByLabelText("What should brindle work on?");
    fireEvent.change(prompt, { target: { value: "Add a cache" } });
    // Branch, CLI and session name wait behind Options.
    fireEvent.click(screen.getByRole("button", { name: /Options/ }));
    expect(screen.queryByLabelText("Autopilot")).toBeNull();
    fireEvent.click(await startButton());
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
    fireEvent.click(await startButton());
    const ask = await screen.findByRole("group", { name: "Session already running" });
    expect(ask.textContent).toContain("Login fix");
    fireEvent.click(within(ask).getByRole("button", { name: "New session in its own worktree" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    // The chosen start carries the name too, and the engine lists the new session by it.
    expect(posts(daemon).at(-1)!.body).toMatchObject({ choice: "n", name: "Cache" });
    expect(daemon.data.sessions.find((s) => s.id === "new1")?.name).toBe("Cache");
    stop();
  });

  it("hides the demo repo from the project options", async () => {
    const demo = { repo_root: "/home/u/.brindle/demo/textkit-1", name: "textkit-1", demo: true };
    const { stop } = setup(new FakeDaemon({ repos: [demo, { repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], sessions: [live], workspaces: [root, worker] }));
    const repo = await screen.findByRole("combobox", { name: "Repo" });
    expect((repo as HTMLInputElement).value).toBe("proj");
    fireEvent.focus(repo);
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["proj", "other"]);
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

  it("says a start is under way the moment Start is pressed, and until its session opens", async () => {
    // A start the engine is slow to answer (a busy machine) left the screen looking frozen.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [{ ...root, agents: [] }] });
    const fetch = daemon.fetch;
    let answer!: () => void;
    const held = new Promise<void>((resolve) => (answer = resolve));
    daemon.fetch = async (input, init) => {
      if (new URL(String(input)).pathname.endsWith("/sessions") && init?.method === "POST") await held;
      return fetch(input, init);
    };
    const { onOpen, stop } = setup(daemon);
    fireEvent.click(await startButton());
    const form = screen.getByRole("form", { name: "Start a session" });
    expect(within(form).getByRole("status")).toBeTruthy();
    answer();
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    expect(within(form).queryByRole("status")).toBeNull();
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
    fireEvent.click(await startButton());
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
    fireEvent.click(await startButton());
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
    // The consent is a checkbox you click; the fake settles the key with the stored key.
    daemon.data.preflight.claude[0].done = true;
    fireEvent.click(within(card).getByRole("checkbox", { name: /Agree to Claude Code's terms/ }));
    await waitFor(() => expect(within(card).getByRole("button", { name: "Start" })).toHaveProperty("disabled", false));
    fireEvent.click(within(card).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", REPO));
    expect(posts(daemon).map((r) => r.path)).toEqual(["/sessions", "/providers/claude/preflight/consent", "/sessions"]);
    stop();
  });

  it("starts the demo again from New session, past the preflight its launch asks for, and opens its session", async () => {
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      workspaces: [root],
      preflight: { claude: [{ id: "consent", kind: "consent", label: "Agree to Claude Code's terms", done: false }] },
    });
    const { onOpen, stop } = setup(daemon);
    fireEvent.click(await screen.findByRole("button", { name: "Watch brindle work on a practice repo" }));
    const card = await screen.findByTestId("preflight");
    expect(daemon.demos).toEqual([]);
    fireEvent.click(within(card).getByRole("checkbox", { name: /Agree to Claude Code's terms/ }));
    await waitFor(() => expect(within(card).getByRole("button", { name: "Start the demo" })).toHaveProperty("disabled", false));
    // While that start is under way, every way to start the demo waits: a double click sends one request.
    daemon.holdLaunch = true;
    const launch = within(card).getByRole("button", { name: "Start the demo" });
    fireEvent.click(launch);
    fireEvent.click(launch);
    await waitFor(() => expect(launch).toHaveProperty("disabled", true));
    expect(screen.getByRole("button", { name: "Starting the demo…" })).toHaveProperty("disabled", true);
    await waitFor(() => expect(posts(daemon).filter((r) => r.path === "/demo")).toHaveLength(2));
    daemon.finishLaunch();
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("new1", "/home/u/.brindle/demo/textkit-1"));
    expect(posts(daemon).map((r) => r.path)).toEqual(["/demo", "/providers/claude/preflight/consent", "/demo"]);
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
    // Switch away, then reopen the same project: each pick is a fresh selection.
    const repo = screen.getByRole("combobox", { name: "Repo" });
    fireEvent.focus(repo);
    fireEvent.mouseDown(screen.getByRole("option", { name: "proj" }));
    expect((repo as HTMLInputElement).value).toBe("proj");
    pickFolder.mockResolvedValue("~/Projects/site");
    fireEvent.click(screen.getByRole("button", { name: "Add a project…" }));
    fireEvent.click(screen.getByRole("button", { name: "Open a folder…" }));
    await waitFor(() => expect((repo as HTMLInputElement).value).toBe("site"));
    // Start waits for the new project's usable CLIs.
    await waitFor(() => expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(posts(daemon).at(-1)?.body).toMatchObject({ repo: "~/Projects/site" }));
    sync.stop();
  });

  it("lists only usable CLIs, makes a first session pick one, remembers it and links to sign in to more", async () => {
    localStorage.clear();
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [root], providers: freshProviders({ claude: { path: "/bin/claude", version: "2" }, codex: { path: "/bin/codex", version: "1" }, antigravity: { path: "/bin/agy", version: "1", signed_in: false } }) });
    const { onSignIn, stop } = setup(daemon);
    const select = (await screen.findByRole("combobox", { name: "Supervisor CLI" })) as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual(["Pick a CLI", "Claude Code", "Codex"]);
    expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(true);
    // ⌘↵ in the prompt doesn't get past the pick either.
    fireEvent.keyDown(screen.getByRole("textbox", { name: "What should brindle work on?" }), { key: "Enter", metaKey: true });
    expect(posts(daemon)).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Sign in to more…" }));
    expect(onSignIn).toHaveBeenCalled();
    fireEvent.click(within(screen.getByText("Which CLI should run your sessions?").parentElement!).getByRole("button", { name: "Codex" }));
    expect(select.value).toBe("codex");
    expect(screen.queryByText("Which CLI should run your sessions?")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(posts(daemon).at(-1)?.body).toMatchObject({ provider: "codex" }));
    expect(localStorage.getItem("brindle.supervisorCli")).toBe("codex");
    stop();
  });

  it("won't start on an earlier answer: a repo's usable CLIs are asked again each time it is selected", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], sessions: [], workspaces: [root] });
    const fetch = daemon.fetch;
    let hold = false;
    const held: (() => void)[] = [];
    daemon.fetch = async (input, init) => {
      if (hold && new URL(String(input)).pathname.endsWith("/providers/usable")) await new Promise<void>((r) => held.push(r));
      return fetch(input, init);
    };
    const { stop } = setup(daemon);
    const start = () => screen.getByRole("button", { name: "Start" }) as HTMLButtonElement;
    await waitFor(() => expect(start().disabled).toBe(false));
    hold = true;
    const repo = screen.getByRole("combobox", { name: "Repo" });
    for (const name of ["other", "proj"]) {
      fireEvent.focus(repo);
      fireEvent.mouseDown(screen.getByRole("option", { name }));
    }
    // Back on the first repo, whose earlier answer is gone: nothing starts until it is asked again.
    await waitFor(() => expect(held).toHaveLength(2));
    expect(start().disabled).toBe(true);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "What should brindle work on?" }), { key: "Enter", metaKey: true });
    expect(posts(daemon)).toHaveLength(0);
    act(() => held.forEach((r) => r()));
    await waitFor(() => expect(start().disabled).toBe(false));
    stop();
  });

  it("starts nothing until the usable CLIs are known, and offers to look again when they couldn't be read", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [root] });
    const fetch = daemon.fetch;
    let down = true;
    daemon.fetch = async (input, init) => (down && new URL(String(input)).pathname.endsWith("/providers/usable") ? Response.json({ error: { code: "internal", message: "down" } }, { status: 500 }) : fetch(input, init));
    const { stop } = setup(daemon);
    await screen.findByText("Couldn't check which CLIs can run here.");
    fireEvent.keyDown(screen.getByRole("textbox", { name: "What should brindle work on?" }), { key: "Enter", metaKey: true });
    expect(posts(daemon)).toHaveLength(0);
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Start" }) as HTMLButtonElement).disabled).toBe(false));
    stop();
  });

  it("with no usable CLI, shows Finish setting up in place of the composer", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [root], providers: freshProviders() });
    const { onSignIn, stop } = setup(daemon);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in to a CLI" }));
    expect(onSignIn).toHaveBeenCalled();
    expect(screen.queryByRole("form", { name: "Start a session" })).toBeNull();
    stop();
  });
});
