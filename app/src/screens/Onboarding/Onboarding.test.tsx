import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../../App";
import { createAppStore } from "../../store/store";
import { startSync, type Sync } from "../../store/sync";
import { DaemonClient } from "../../api/client";
import { PtyViewsProvider } from "../../api/ptyView";
import { BASE_URL, FakeDaemon, TOKEN, flush, freshProviders } from "../../test/fakeDaemon";
import { ONBOARDED_KEY } from "../../api/providers";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

const PROJECT = "/home/u/Projects/proj";
const SIGNED_IN = { signed_in: true, name: "sam@example.com", plan: "pro" };

let syncs: Sync[] = [];
beforeEach(() => localStorage.clear());
afterEach(() => {
  for (const s of syncs) s.stop();
  syncs = [];
  cleanup();
});

function launch(daemon: FakeDaemon, extra: { pickFolder?: (title: string) => Promise<string | null> } = {}) {
  const store = createAppStore();
  const client = daemon.client();
  const sync = startSync({ client, store, connect: daemon.connect });
  syncs.push(sync);
  const openUrl = vi.fn(() => Promise.resolve());
  const pickFolder = extra.pickFolder ?? vi.fn(() => Promise.resolve(PROJECT));
  // The panes (a sign-in's CLI) through the fake daemon's pty views, as the shell's show them.
  render(
    <PtyViewsProvider value={daemon.ptyViews()}>
      <App store={store} client={client} refresh={sync.refresh} openUrl={openUrl} pickFolder={pickFolder} />
    </PtyViewsProvider>,
  );
  return { openUrl, pickFolder };
}

const step = async (name: string) => waitFor(() => expect(screen.getByTestId("onboarding").getAttribute("data-step")).toBe(name), { timeout: 3000 });
const sidebar = () => screen.queryByRole("complementary", { name: "Sidebar" });
const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST").map((r) => r.path);

describe("Onboarding", () => {
  it("onboarding: a first launch signs in, installs Claude Code with its own installer, signs in to it and picks a project, all without a sidebar", async () => {
    const daemon = new FakeDaemon({ providers: freshProviders() });
    daemon.deferInstall = true;
    daemon.deferLogin = true;
    const { openUrl } = launch(daemon);

    // Starting brindle, then sign in to brindle: one button, the browser does it.
    await step("signin");
    expect(sidebar()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(openUrl).toHaveBeenCalledWith(daemon.data.loginUrl));
    expect(screen.getByRole("status").textContent).toBe("Waiting for the browser…");
    fireEvent.click(screen.getByRole("button", { name: "Open the tab again" }));
    expect(openUrl).toHaveBeenCalledTimes(2);
    daemon.completeLogin();

    // Connect your providers: a section each, choosing how a missing one connects picks it; the one button names the picks.
    await step("connect");
    const claude = screen.getByTestId("onb-card-claude");
    expect(within(claude).getByText("Anthropic")).toBeTruthy();
    // Nothing picked: the one button below the cards is off.
    expect(screen.getAllByRole("button", { name: "Connect" }).filter((b) => (b as HTMLButtonElement).disabled)).toHaveLength(1);
    fireEvent.click(within(claude).getByRole("radio", { name: "Sign in" }));
    expect(within(claude).getByTestId("onb-chosen-claude").textContent).toContain("Runs Anthropic's installer");
    fireEvent.click(screen.getByRole("button", { name: "Connect Claude Code" }));

    // The calm progress card: the installer's own lines as it prints them, no Continue until it's done.
    await step("install");
    const card = await screen.findByTestId("onb-install-claude");
    await waitFor(() => expect(within(card).getByTestId("onb-install-claude-lines").textContent).toContain("Downloading Claude Code 2.1.47"));
    expect(card.getAttribute("data-state")).toBe("running");
    expect(within(card).getByText("claude.ai/install.sh")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    daemon.completeInstall();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Claude Code is connected"));
    expect(screen.getByTestId("onb-install-claude").textContent).toContain("Installed by Anthropic's installer at /home/u/.local/bin/claude");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    // Claude Code's own sign-in, in the browser; the flow moves on when it reads signed in.
    await step("provider-signin");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Sign in to Claude");
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(posts(daemon)).toContain("/providers/claude/login"));
    await waitFor(() => expect(screen.getByTestId("onb-provider-signin").getAttribute("data-phase")).toBe("waiting"));
    daemon.signInProvider("claude");

    // You're ready, then a project: the folder picked is opened, and the app appears on New session in it.
    await step("ready");
    expect(screen.getByTestId("onb-ready-claude").textContent).toContain("signed in");
    expect(sidebar()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    // Permissions: Autonomous is preselected and recommended; continuing applies it.
    await step("permissions");
    expect(screen.getByTestId("onb-preset-autonomous").getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Continue with Autonomous" }));
    await step("project");
    expect(daemon.requests.filter((r) => r.method === "PUT" && r.path === "/permissions/preset").map((r) => r.body)).toEqual([{ preset: "autonomous" }]);
    fireEvent.click(screen.getByRole("button", { name: "Open a folder…" }));
    await waitFor(() => expect(sidebar()).toBeTruthy());
    expect(daemon.opened).toEqual([PROJECT]);
    expect(screen.queryByTestId("onboarding")).toBeNull();
    // The composer, in the project just opened (the app's repo list has it, though no event said so).
    const composer = await screen.findByRole("form", { name: "Start a session" });
    expect(within(composer).getByLabelText("What should brindle work on?")).toBeTruthy();
    expect(composer.textContent).toContain("in proj");
    expect(screen.queryByRole("region", { name: "Welcome" })).toBeNull();
    expect(daemon.installs).toEqual(["claude"]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("onboarding uses a CLI found on this Mac as-is: it says where it was found, installs nothing, and asks only for its sign-in", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ codex: { path: "/opt/homebrew/bin/codex", version: "0.46.0", signed_in: false } }) });
    launch(daemon);

    await step("connect");
    const codex = screen.getByTestId("onb-card-codex");
    expect(within(codex).getByTestId("onb-found-codex").textContent).toContain("Found at /opt/homebrew/bin/codex · 0.46.0");
    expect(within(codex).queryByRole("button")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Continue with Codex" }));

    await step("install");
    expect(screen.getByTestId("onb-install-codex").textContent).toContain("Found at /opt/homebrew/bin/codex; it updates itself, brindle doesn't touch it.");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await step("provider-signin");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Sign in to OpenAI");
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(posts(daemon)).toContain("/providers/codex/login"));
    await waitFor(() => expect(screen.getByTestId("onb-provider-signin").getAttribute("data-phase")).toBe("waiting"));
    daemon.signInProvider("codex");

    await step("ready");
    expect(screen.getByTestId("onb-ready-codex").textContent).toContain("signed in · your own copy");
    expect(daemon.installs).toEqual([]);
    expect(posts(daemon).some((p) => p.endsWith("/install"))).toBe(false);

    // A different choice is the one applied, and the cards are locked while the request is pending.
    daemon.holdPreset = true;
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await step("permissions");
    fireEvent.click(screen.getByTestId("onb-preset-secure"));
    fireEvent.click(screen.getByRole("button", { name: "Continue with Secure" }));
    await waitFor(() => expect(daemon.pendingPreset).not.toBeNull());
    fireEvent.click(screen.getByTestId("onb-preset-autonomous"));
    expect(screen.getByTestId("onb-preset-secure").getAttribute("aria-checked")).toBe("true");
    daemon.pendingPreset?.();
    await step("project");
    expect(daemon.requests.filter((r) => r.path === "/permissions/preset").map((r) => r.body)).toEqual([{ preset: "secure" }]);
    expect(daemon.data.policy.preset).toBe("secure");
  });

  it("onboarding's connect method choice: an API key pasted and checked, a cloud signed in, and neither waits for a browser sign-in", async () => {
    const daemon = new FakeDaemon({
      account: SIGNED_IN,
      providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47", signed_in: false }, codex: { path: "/opt/homebrew/bin/codex", version: "0.46.0", signed_in: false } }),
    });
    daemon.rejectedKeys.add("OPENAI_API_KEY");
    daemon.shellKeys.add("OPENAI_API_KEY");
    const { openUrl } = launch(daemon);
    await step("connect");
    const puts = (path: string) => daemon.requests.filter((r) => r.method === "PUT" && r.path === path).map((r) => r.body);

    // Three equal choices up front for each; a CLI found starts on Sign in.
    const codex = screen.getByTestId("onb-card-codex");
    expect(within(codex).getAllByRole("radio").map((r) => r.textContent)).toEqual([expect.stringMatching(/^Sign in/), expect.stringMatching(/^API key/), expect.stringMatching(/^Your cloud/)]);
    expect(within(codex).getByTestId("connect-method").getAttribute("data-method")).toBe("sign_in");

    // API key: the prefix says whose key it is; another maker's can't be saved here.
    fireEvent.click(within(codex).getByRole("radio", { name: "API key" }));
    const key = within(codex).getByLabelText("Paste your key");
    fireEvent.change(key, { target: { value: "sk-ant-api03-zzzz" } });
    expect(within(codex).getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    fireEvent.change(key, { target: { value: "sk-proj-abcd-x9Qa" } });
    expect(within(codex).getByRole("status").textContent).toContain("OpenAI key recognized.");
    // OpenAI refuses it: rejected, in its words, and nothing stored.
    fireEvent.click(within(codex).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(codex).getByTestId("key-state").getAttribute("data-status")).toBe("rejected"));
    expect(within(codex).getByTestId("key-state").textContent).toContain("the provider rejected this key");
    expect(daemon.keysStored).toEqual([]);
    // The one in the shell is good: imported, verified with its last four, and the method saved.
    daemon.rejectedKeys.clear();
    fireEvent.click(within(codex).getByRole("button", { name: "Use OPENAI_API_KEY from my shell" }));
    await waitFor(() => expect(within(codex).getByTestId("key-state").textContent).toMatch(/OpenAI key recognized · verified just now · ending OPEN/));
    expect(within(codex).getByRole("status").textContent).toContain("instead of a ChatGPT plan");
    expect(puts("/providers/codex/connection")).toEqual([{ method: "api_key" }]);
    fireEvent.click(within(codex).getByRole("button", { name: "Get a key in the OpenAI platform" }));
    expect(openUrl).toHaveBeenCalledWith("https://platform.openai.com/api-keys");

    // Your cloud: Bedrock, with the SSO profile and its region found on this Mac; Sign in saves the route and runs its login.
    const claude = screen.getByTestId("onb-card-claude");
    fireEvent.click(within(claude).getByRole("radio", { name: "Your cloud" }));
    const setup = await within(claude).findByTestId("cloud-setup");
    await waitFor(() => expect(within(setup).getByRole("radio", { name: "Amazon Bedrock" }).getAttribute("aria-checked")).toBe("true"));
    expect(within(setup).getByLabelText("AWS profile")).toHaveProperty("value", "work-sso");
    expect(within(setup).getByLabelText("Region")).toHaveProperty("value", "us-east-1");
    fireEvent.click(within(setup).getByRole("button", { name: "Sign in with AWS" }));
    await waitFor(() => expect(daemon.cloudSignIns).toEqual(["claude"]));
    expect(puts("/providers/claude/connection")).toEqual([{ method: "cloud", cloud: "bedrock", settings: { AWS_PROFILE: "work-sso", AWS_REGION: "us-east-1" } }]);
    await waitFor(() => expect(openUrl).toHaveBeenCalledWith("https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH"));
    expect(within(setup).getByRole("status").textContent).toBe("Waiting for the browser…");
    daemon.completeCloudSignIn();
    await waitFor(() => expect(within(setup).getByTestId("cloud-sign-in").textContent).toContain("Signed in through Amazon Bedrock"));

    // Neither waits for a browser sign-in: straight to You're ready, each saying how it connects.
    fireEvent.click(screen.getByRole("button", { name: "Continue with Claude Code and Codex" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("ready");
    expect(screen.getByTestId("onb-ready-claude").textContent).toContain("connected through Amazon Bedrock");
    expect(screen.getByTestId("onb-ready-codex").textContent).toContain("connected by API key ending OPEN");
    expect(posts(daemon).some((p) => p.endsWith("/login"))).toBe(false);
    expect(localStorage.getItem(ONBOARDED_KEY)).toBe("1");
  });

  it("onboarding: an install that fails says what the installer said on its card, and Retry runs it again inline", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders() });
    daemon.installFails.claude = 1;
    launch(daemon);

    await step("connect");
    fireEvent.click(within(screen.getByTestId("onb-card-claude")).getByRole("radio", { name: "Sign in" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect Claude Code" }));

    const failed = await screen.findByTestId("onb-install-claude-failed");
    expect(failed.textContent).toContain("Install failed.");
    expect(failed.textContent).toContain("Recv failure: Connection reset by peer");
    expect(screen.getByTestId("onb-install-claude").getAttribute("data-state")).toBe("failed");
    expect(screen.getByTestId("onb-install-claude-lines").textContent).toContain("curl: (56)");
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Claude Code is connected"));
    expect(screen.queryByTestId("onb-install-claude-failed")).toBeNull();
    expect(daemon.installs).toEqual(["claude", "claude"]);
  });

  it("onboarding: Skip for now on a failed install goes back to the cards when nothing else is connected", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders() });
    daemon.installFails.opencode = 1;
    launch(daemon);
    await step("connect");
    fireEvent.click(within(screen.getByTestId("onb-card-opencode")).getByRole("radio", { name: "Sign in" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCode" }));
    const failed = await screen.findByTestId("onb-install-opencode-failed");
    fireEvent.click(within(failed).getByRole("button", { name: "Skip for now" }));
    await step("connect");
    expect(screen.getByTestId("onb-card-opencode").getAttribute("data-picked")).toBe("false");
  });

  it("onboarding shows once: a second launch, with a provider connected and signed in, goes straight to the app", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47", signed_in: false } }) });
    launch(daemon);
    await step("connect");
    fireEvent.click(screen.getByRole("button", { name: "Continue with Claude Code" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("provider-signin");
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(screen.getByTestId("onb-provider-signin").getAttribute("data-phase")).toBe("waiting"));
    daemon.signInProvider("claude");
    await step("ready");
    cleanup();

    // The next launch: Starting brindle, then the app itself, sidebar and all.
    launch(daemon);
    await waitFor(() => expect(sidebar()).toBeTruthy(), { timeout: 3000 });
    expect(screen.queryByTestId("onboarding")).toBeNull();
    expect(daemon.requests.filter((r) => r.path === "/providers/installed").length).toBeGreaterThan(0);
  });

  it("onboarding stays until a provider is signed in: \"Do this later\" brings it back on the next launch", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47", signed_in: false } }) });
    launch(daemon);
    await step("connect");
    fireEvent.click(screen.getByRole("button", { name: "Continue with Claude Code" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("provider-signin");
    fireEvent.click(screen.getByRole("button", { name: "Do this later" }));
    await step("ready");
    expect(screen.getByTestId("onb-ready-claude").textContent).toContain("not signed in yet");
    cleanup();

    launch(daemon);
    await step("connect");
    expect(sidebar()).toBeNull();
  });

  /* The app over a daemon whose GET /providers/installed answers `answer()` while it returns a Response. */
  function launchWith(daemon: FakeDaemon, answer: () => Response | null) {
    const store = createAppStore();
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => {
        const got = String(input).includes("/v1/providers/installed") ? answer() : null;
        return got ? Promise.resolve(got) : daemon.fetch(input, init);
      },
    });
    const sync = startSync({ client, store, connect: daemon.connect });
    syncs.push(sync);
    render(
      <PtyViewsProvider value={daemon.ptyViews()}>
        <App store={store} client={client} refresh={sync.refresh} />
      </PtyViewsProvider>,
    );
  }
  const refusal = (status: number, code: string, message: string) => new Response(JSON.stringify({ error: { code, message } }), { status, headers: { "Content-Type": "application/json" } });

  it("onboarding's gate lets the app straight in on an older engine without the route (404)", async () => {
    launchWith(new FakeDaemon({ providers: freshProviders() }), () => refusal(404, "not_found", "no route /providers/installed"));
    await waitFor(() => expect(sidebar()).toBeTruthy(), { timeout: 3000 });
    expect(screen.queryByTestId("onboarding")).toBeNull();
  });

  it("onboarding isn't skipped when the first lookup fails: it says so with Retry, and the retry opens the first launch", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders() });
    let failing = true;
    launchWith(daemon, () => (failing ? refusal(500, "internal", "the engine hit an error") : null));
    const failed = await screen.findByTestId("starting-failed");
    expect(failed.textContent).toContain("the engine hit an error");
    expect(sidebar()).toBeNull();
    failing = false;
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await step("connect");
    expect(sidebar()).toBeNull();
  });

  it("onboarding stays done once a provider was confirmed signed in: a relaunch after it signed out goes straight to the app", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47", signed_in: false } }) });
    launch(daemon);
    await step("connect");
    fireEvent.click(screen.getByRole("button", { name: "Continue with Claude Code" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("provider-signin");
    expect(localStorage.getItem(ONBOARDED_KEY)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(screen.getByTestId("onb-provider-signin").getAttribute("data-phase")).toBe("waiting"));
    daemon.signInProvider("claude");
    await step("ready");
    expect(localStorage.getItem(ONBOARDED_KEY)).toBe("1");
    cleanup();

    // Claude Code signs out (its token lapsed) before the next launch: still the app, not the first launch.
    daemon.data.providers = daemon.data.providers.map((r) => (r.provider === "claude" ? { ...r, signed_in: false } : r));
    const asked = daemon.requests.filter((r) => r.path === "/providers/installed").length;
    launch(daemon);
    await waitFor(() => expect(sidebar()).toBeTruthy(), { timeout: 3000 });
    expect(screen.queryByTestId("onboarding")).toBeNull();
    // The gate asked nothing; the app's own look at the providers says it where it belongs: the status line.
    await waitFor(() => expect(screen.getByTestId("provider-problem").textContent).toBe("Claude Code signed out"));
    expect(daemon.requests.filter((r) => r.path === "/providers/installed").length).toBe(asked + 1);
  });

  it("onboarding never moves on from a failed providers refresh: an install succeeds, the read after it fails, Retry recovers", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders() });
    daemon.deferInstall = true;
    let down = false;
    launchWith(daemon, () => (down ? refusal(503, "unavailable", "the engine is restarting") : null));
    await step("connect");
    fireEvent.click(within(screen.getByTestId("onb-card-claude")).getByRole("radio", { name: "Sign in" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect Claude Code" }));
    await step("install");
    await screen.findByTestId("onb-install-claude-lines");

    // The installer finishes, but reading the providers again fails: no Continue, the error inline.
    down = true;
    daemon.completeInstall();
    const failed = await screen.findByTestId("onb-refresh-failed");
    expect(failed.textContent).toContain("the engine is restarting");
    expect(screen.queryByRole("button", { name: "Continue" })).toBeNull();
    expect(screen.getByTestId("onboarding").getAttribute("data-step")).toBe("install");

    // Retry while it's still down: still here.
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await flush();
    expect(screen.getByTestId("onboarding").getAttribute("data-step")).toBe("install");
    expect(screen.getByTestId("onb-refresh-failed")).toBeTruthy();

    // Back: Retry reads it, Continue appears and goes on to the sign-in.
    down = false;
    fireEvent.click(within(screen.getByTestId("onb-refresh-failed")).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByTestId("onb-refresh-failed")).toBeNull());
    expect(screen.getByTestId("onb-install-claude").textContent).toContain("Installed by Anthropic's installer at /home/u/.local/bin/claude");
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await step("provider-signin");
  });

  /* The way out of a sign-in step: Try again and Skip for now, both there. */
  const escape = (scope: HTMLElement) => ({ retry: within(scope).getByTestId("onb-signin-retry"), skip: within(scope).getByTestId("onb-signin-skip") });

  it("onboarding's brindle sign-in never hangs: a failed or abandoned browser login offers Try again and Skip for now", async () => {
    const daemon = new FakeDaemon({ providers: freshProviders() });
    daemon.deferLogin = true;
    let refuse = true;
    const store = createAppStore();
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => (refuse && String(input).includes("/v1/account/login") ? Promise.resolve(refusal(502, "upstream", "pawdelta.com didn't answer")) : daemon.fetch(input, init)),
    });
    const sync = startSync({ client, store, connect: daemon.connect });
    syncs.push(sync);
    render(
      <PtyViewsProvider value={daemon.ptyViews()}>
        <App store={store} client={client} refresh={sync.refresh} openUrl={vi.fn(() => Promise.resolve())} />
      </PtyViewsProvider>,
    );
    await step("signin");
    const logins = () => posts(daemon).filter((p) => p === "/account/login").length;

    // The login fails: Try again starts it afresh.
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await screen.findByRole("alert");
    refuse = false;
    fireEvent.click(escape(screen.getByTestId("onboarding")).retry);
    await waitFor(() => expect(logins()).toBe(1));

    // The browser never comes back: Try again ends that sign-in and starts a fresh one (the old one's end isn't a failure here),
    // or Skip for now on to the cards.
    fireEvent.click(escape(screen.getByTestId("onboarding")).retry);
    await waitFor(() => expect(logins()).toBe(2));
    expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/account/login").at(-1)?.body).toEqual({ device: false, restart: true });
    await flush();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toBeTruthy();
    fireEvent.click(escape(screen.getByTestId("onboarding")).skip);
    await step("connect");
  });

  it("onboarding's provider sign-in never hangs: a waiting or failed browser login offers Try again and Skip for now", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47", signed_in: false } }) });
    // Requests `holding` matches wait until release(): the daemon answers them only then.
    let holding: (path: string) => boolean = () => false;
    const held: Array<() => void> = [];
    const release = () => held.splice(0).forEach((go) => go());
    const store = createAppStore();
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => (holding(String(input)) ? new Promise((ok) => held.push(() => ok(daemon.fetch(input, init)))) : daemon.fetch(input, init)),
    });
    const sync = startSync({ client, store, connect: daemon.connect });
    syncs.push(sync);
    render(
      <PtyViewsProvider value={daemon.ptyViews()}>
        <App store={store} client={client} refresh={sync.refresh} openUrl={vi.fn(() => Promise.resolve())} />
      </PtyViewsProvider>,
    );
    await step("connect");
    fireEvent.click(screen.getByRole("button", { name: "Continue with Claude Code" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("provider-signin");
    const phase = () => screen.getByTestId("onb-provider-signin").getAttribute("data-phase");
    const logins = () => posts(daemon).filter((p) => p === "/providers/claude/login").length;
    const runs = () => daemon.actions.filter((a) => a.kind === "login claude");
    const retry = () => fireEvent.click(escape(screen.getByTestId("onboarding")).retry);

    // A fresh login: one more CLI than before, the only one running, and the step following it.
    const fresh = async (count: number) => {
      await waitFor(() => expect(runs()).toHaveLength(count));
      expect(runs().filter((a) => a.alive)).toEqual([runs()[count - 1]]);
      await waitFor(() => expect(daemon.viewers(runs()[count - 1].id).length).toBeGreaterThan(0));
      expect(phase()).toBe("waiting");
    };

    // Waiting on the browser, the login still running: Try again ends it and starts a fresh one.
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(phase()).toBe("waiting"));
    expect(runs()[0].alive).toBe(true);
    retry();
    await fresh(2);
    expect(logins()).toBe(2);

    // The CLI exits unsigned and its check is slow: Try again starts a new login, and the old check's answer changes nothing.
    holding = (path) => path.includes("/v1/providers/installed");
    daemon.exitAction(runs()[1].id, "Login cancelled.");
    await waitFor(() => expect(phase()).toBe("checking"));
    retry();
    await fresh(3);
    holding = () => false;
    release();
    await flush();
    expect(phase()).toBe("waiting");

    // That one gives up too: Try again and Skip for now, and Skip goes on.
    daemon.exitAction(runs()[2].id, "Login cancelled.");
    await waitFor(() => expect(phase()).toBe("failed"));
    fireEvent.click(escape(screen.getByTestId("onboarding")).skip);
    await step("ready");
  });

  it("onboarding treats a CLI whose sign-in the engine can't confirm as not signed in yet", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47" } }) });
    daemon.data.providers = daemon.data.providers.map((r) => (r.provider === "claude" ? { ...r, signed_in: null } : r));
    launch(daemon);
    // Not skipped: the first launch, and Claude Code's sign-in is asked for.
    await step("connect");
    fireEvent.click(screen.getByRole("button", { name: "Continue with Claude Code" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("provider-signin");
    fireEvent.click(screen.getByRole("button", { name: "Do this later" }));
    await step("ready");
    expect(screen.getByTestId("onb-ready-claude").textContent).toContain("sign-in not confirmed");
    expect(screen.getByTestId("onb-ready-claude").textContent).not.toContain("signed in ·");
  });
});
