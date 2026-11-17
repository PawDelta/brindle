import { connectDaemon, renderApp, trackedSync as startSync } from "../../test/harness";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../../App";
import { createAppStore } from "../../store/store";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN, flush, freshProviders } from "../../test/fakeDaemon";
import { ONBOARDED_KEY } from "../../api/providers";
import { DaemonContext } from "../../api/context";
import { SessionsProvider } from "../../store/sessions";
import { StoreContext } from "../../store/useStore";
import { PracticeRow } from "./Demo";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

const PROJECT = "/home/u/Projects/proj";
const SIGNED_IN = { signed_in: true, name: "sam@example.com", plan: "pro" };
const SIGNED_OUT = { signed_in: false };

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
});

function launch(daemon: FakeDaemon, extra: { pickFolder?: (title: string) => Promise<string | null> } = {}) {
  const { client, store, sync } = connectDaemon(daemon);
  const openUrl = vi.fn(() => Promise.resolve());
  const pickFolder = extra.pickFolder ?? vi.fn(() => Promise.resolve(PROJECT));
  // The panes (a sign-in's CLI) through the fake daemon's pty views, as the shell's show them.
  renderApp(<App store={store} client={client} refresh={sync.refresh} openUrl={openUrl} pickFolder={pickFolder} />, daemon.ptyViews());
  return { openUrl, pickFolder };
}

const step = async (name: string) => waitFor(() => expect(screen.getByTestId("onboarding").getAttribute("data-step")).toBe(name), { timeout: 3000 });
const sidebar = () => screen.queryByRole("complementary", { name: "Sidebar" });
const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST").map((r) => r.path);

describe("Onboarding", () => {
  it("onboarding: a first launch signs in, installs Claude Code with its own installer, signs in to it and picks a project, all without a sidebar", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_OUT, providers: freshProviders() });
    daemon.deferInstall = true;
    daemon.deferLogin = true;
    const { openUrl } = launch(daemon);

    // Starting brindle, then sign in to brindle (the app's sign-in gate, the flow's step 1): one button, the browser does it.
    await step("signin");
    expect(screen.getByTestId("signin-gate")).toBeTruthy();
    expect(screen.getByText("Step 1 of 3")).toBeTruthy();
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
    // Nothing picked: the one button below the cards is off.
    expect(screen.getAllByRole("button", { name: "Connect" }).filter((b) => (b as HTMLButtonElement).disabled)).toHaveLength(1);
    fireEvent.click(within(claude).getByRole("radio", { name: "Sign in" }));
    expect((await within(claude).findByTestId("onb-chosen-claude")).textContent).toContain("Runs Anthropic's installer");
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

    // No demo offer: the app opens on New session, in the project picked.
    await waitFor(() => expect(sidebar()).toBeTruthy());
    expect(daemon.demos).toEqual([]);
    expect(daemon.opened).toEqual([PROJECT]);
    expect(screen.queryByTestId("onboarding")).toBeNull();
    // The composer, in the project just opened (the app's repo list has it, though no event said so).
    const composer = await screen.findByRole("form", { name: "Start a session" });
    expect(within(composer).getByLabelText("What should brindle work on?")).toBeTruthy();
    expect(within(composer).getByRole("button", { name: "Project: proj" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Welcome" })).toBeNull();
    expect(daemon.installs).toEqual(["claude"]);
    // The one thing over it: the first-time walkthrough, offered once now that setup is done.
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe("Walkthrough");
  });

  it("onboarding uses a CLI found on this Mac as-is: it says where it was found, installs nothing, and asks only for its sign-in", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ codex: { path: "/opt/homebrew/bin/codex", version: "0.46.0", signed_in: false } }) });
    launch(daemon);

    await step("connect");
    const codex = screen.getByTestId("onb-card-codex");
    expect(within(codex).getByTestId("onb-found-codex").textContent).toContain("Found at /opt/homebrew/bin/codex · 0.46.0");
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

  const puts = (daemon: FakeDaemon, path: string) => daemon.requests.filter((r) => r.method === "PUT" && r.path === path).map((r) => r.body);
  const methodOn = (card: HTMLElement) => within(card).getByTestId("connect-method").getAttribute("data-method");

  it("onboarding's connect method choice: a cloud signed in before its CLI is installed, an API key checked and imported, connected only once the engine reads it ready", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders({ codex: { path: "/opt/homebrew/bin/codex", version: "0.46.0", signed_in: false } }) });
    daemon.rejectedKeys.add("OPENAI_API_KEY");
    daemon.shellKeys.add("OPENAI_API_KEY");
    daemon.codexChatGPT = true;
    const { openUrl } = launch(daemon);
    await step("connect");

    // The first not connected is open; the others are a line with Choose.
    const claude = screen.getByTestId("onb-card-claude");
    const codex = screen.getByTestId("onb-card-codex");
    expect(claude.getAttribute("data-open")).toBe("true");
    expect(codex.getAttribute("data-open")).toBe("false");

    // Your cloud on a CLI still to install: Bedrock seeded from the SSO profile found; Sign in saves the route, then runs its login.
    fireEvent.click(await within(claude).findByRole("radio", { name: "Your cloud" }));
    const setup = await within(claude).findByTestId("cloud-setup");
    await waitFor(() => expect(within(setup).getByLabelText("AWS profile")).toHaveProperty("value", "work-sso"));
    expect(within(setup).getByLabelText("Region")).toHaveProperty("value", "us-east-1");
    // While the route is being saved, the route, its fields and the method are held: nothing can write over it.
    daemon.holdConnections = true;
    fireEvent.click(within(within(setup).getByTestId("cloud-sign-in")).getByRole("button"));
    await waitFor(() => expect(within(setup).getByRole("radio", { name: "Google Vertex AI" })).toHaveProperty("disabled", true));
    fireEvent.click(within(setup).getByRole("radio", { name: "Google Vertex AI" }));
    expect(within(setup).getByLabelText("Region")).toHaveProperty("disabled", true);
    expect(within(claude).getByRole("radio", { name: "API key" })).toHaveProperty("disabled", true);
    expect(setup.getAttribute("data-route")).toBe("bedrock");
    daemon.holdConnections = false;
    daemon.releaseKeys();
    await waitFor(() => expect(daemon.cloudSignIns).toEqual(["claude"]));
    expect(puts(daemon, "/providers/claude/connection")).toEqual([{ method: "cloud", cloud: "bedrock", settings: { AWS_PROFILE: "work-sso", AWS_REGION: "us-east-1" } }]);
    await waitFor(() => expect(openUrl).toHaveBeenCalledWith("https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH"));
    daemon.completeCloudSignIn();
    await waitFor(() => expect(within(setup).getByTestId("cloud-sign-in").getAttribute("data-state")).toBe("signed in"));
    // Claude Code isn't installed yet: the first launch isn't done.
    expect(localStorage.getItem(ONBOARDED_KEY)).toBeNull();

    // Codex has no cloud route: Your cloud isn't offered.
    fireEvent.click(within(codex).getByRole("button", { name: "Choose how Codex connects" }));
    await waitFor(() => expect(codex.getAttribute("data-open")).toBe("true"));
    expect(within(codex).queryByRole("radio", { name: "Your cloud" })).toBeNull();
    // API key: another maker's key can't be saved here; OpenAI refuses this one, and nothing is stored.
    fireEvent.click(within(codex).getByRole("radio", { name: "API key" }));
    const key = await within(codex).findByLabelText("Paste your key");
    fireEvent.change(key, { target: { value: "sk-ant-api03-zzzz" } });
    expect(within(codex).getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    fireEvent.change(key, { target: { value: "sk-proj-abcd-x9Qa" } });
    fireEvent.click(within(codex).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(codex).getByTestId("key-state").getAttribute("data-status")).toBe("rejected"));
    expect(daemon.keysStored).toEqual([]);
    // The shell's key is good: imported, verified, the method saved; but Codex's ChatGPT login still wins, so it isn't connected yet.
    daemon.rejectedKeys.clear();
    fireEvent.click(within(codex).getByRole("button", { name: "Use OPENAI_API_KEY from my shell" }));
    await waitFor(() => expect(within(codex).getByTestId("key-state").getAttribute("data-status")).toBe("verified"));
    expect(puts(daemon, "/providers/codex/connection")).toEqual([{ method: "api_key" }]);
    const blocked = await within(codex).findByTestId("provider-ready");
    expect(blocked.getAttribute("data-action")).toBe("sign_out");
    expect(localStorage.getItem(ONBOARDED_KEY)).toBeNull();
    // Its fix, signing ChatGPT out: now Codex is ready, and the first launch done.
    fireEvent.click(within(blocked).getByRole("button"));
    await waitFor(() => expect(within(codex).queryByTestId("provider-ready")).toBeNull());
    expect(daemon.codexSignOuts).toBe(1);
    await waitFor(() => expect(localStorage.getItem(ONBOARDED_KEY)).toBe("1"));

    // Neither waits for a browser sign-in: Claude Code installs, then You're ready with both connected.
    fireEvent.click(screen.getByRole("button", { name: "Connect Claude Code" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("ready");
    await waitFor(() => expect(screen.getByTestId("onb-ready-claude").getAttribute("data-connected")).toBe("true"));
    expect(screen.getByTestId("onb-ready-codex").getAttribute("data-connected")).toBe("true");
    expect(posts(daemon).some((p) => p.endsWith("/login"))).toBe(false);
  });

  it("onboarding saves an explicit Sign in even over auto with a key stored, shows a refused one, and a key saved before an install stops counting once the method changes", async () => {
    const daemon = new FakeDaemon({
      account: SIGNED_IN,
      providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47", signed_in: false } }),
      config: { user: {}, repo: {}, local: {}, keys: [{ name: "ANTHROPIC_API_KEY", set: true, last4: "WXYZ", status: "verified" }] },
      connections: { codex: { managed: "org acme decides how Codex connects" } },
    });
    launch(daemon);
    await step("connect");

    // Claude Code is on auto with a key stored: that key would bill, so no method is claimed until one is picked; Sign in is saved.
    const claude = screen.getByTestId("onb-card-claude");
    await waitFor(() => expect(methodOn(claude)).toBe(""));
    // While that Sign in is being saved, no other choice can land before it and be undone by its answer.
    daemon.holdConnections = true;
    fireEvent.click(within(claude).getByRole("radio", { name: "Sign in" }));
    await waitFor(() => expect(within(claude).getByRole("radio", { name: "API key" })).toHaveProperty("disabled", true));
    fireEvent.click(within(claude).getByRole("radio", { name: "API key" }));
    expect(within(claude).queryByLabelText("Paste your key")).toBeNull();
    daemon.holdConnections = false;
    daemon.releaseKeys();
    await waitFor(() => expect(methodOn(claude)).toBe("sign_in"));
    expect(puts(daemon, "/providers/claude/connection")).toEqual([{ method: "sign_in" }]);

    // A key save still waiting on its check holds the choices: a Sign in can't race it and be overwritten after.
    const agy = screen.getByTestId("onb-card-antigravity");
    fireEvent.click(within(agy).getByRole("button", { name: "Choose how Antigravity connects" }));
    fireEvent.click(await within(agy).findByRole("radio", { name: "API key" }));
    daemon.holdKeys = true;
    fireEvent.change(await within(agy).findByLabelText("Paste your key"), { target: { value: "AIzaSyHeld0001" } });
    fireEvent.click(within(agy).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(agy).getByRole("radio", { name: "Sign in" })).toHaveProperty("disabled", true));
    fireEvent.click(within(agy).getByRole("radio", { name: "Sign in" }));
    expect(within(agy).getByRole("button", { name: "Undo" })).toHaveProperty("disabled", true);
    daemon.holdKeys = false;
    daemon.releaseKeys();
    await waitFor(() => expect(puts(daemon, "/providers/antigravity/connection")).toEqual([{ method: "api_key" }]));
    // Picked once the save answered, Sign in is saved last and is the method that stands.
    await waitFor(() => expect(within(agy).getByRole("radio", { name: "Sign in" })).toHaveProperty("disabled", false));
    fireEvent.click(within(agy).getByRole("radio", { name: "Sign in" }));
    await waitFor(() => expect(methodOn(agy)).toBe("sign_in"));
    expect(puts(daemon, "/providers/antigravity/connection")).toEqual([{ method: "api_key" }, { method: "sign_in" }]);
    fireEvent.click(within(agy).getByRole("button", { name: "Undo" }));

    // The org decides Codex's: Sign in is refused, said in the section, and nothing is picked.
    const codex = screen.getByTestId("onb-card-codex");
    fireEvent.click(within(codex).getByRole("button", { name: "Choose how Codex connects" }));
    fireEvent.click(await within(codex).findByRole("radio", { name: "Sign in" }));
    expect((await within(codex).findByRole("alert")).textContent).toContain("org acme decides how Codex connects");
    expect(codex.getAttribute("data-picked")).toBe("false");

    // OpenCode, not installed: a key saved before the install doesn't finish the first launch...
    const opencode = screen.getByTestId("onb-card-opencode");
    fireEvent.click(within(opencode).getByRole("button", { name: "Choose how OpenCode connects" }));
    fireEvent.click(await within(opencode).findByRole("radio", { name: "API key" }));
    fireEvent.change(await within(opencode).findByLabelText("Paste your key"), { target: { value: "sk-or-v1-abcd" } });
    fireEvent.click(within(opencode).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(opencode).getByTestId("key-state").getAttribute("data-status")).toBe("verified"));
    expect(daemon.keysStored).toEqual(["GEMINI_API_KEY", "OPENROUTER_API_KEY"]);
    expect(localStorage.getItem(ONBOARDED_KEY)).toBeNull();
    // ...and switching it to Sign in (saved, once the key's own write is done) drops what the key set up: installed, it still waits for its sign-in.
    await waitFor(() => expect(within(opencode).getByRole("radio", { name: "Sign in" })).toHaveProperty("disabled", false));
    fireEvent.click(within(opencode).getByRole("radio", { name: "Sign in" }));
    await waitFor(() => expect(methodOn(opencode)).toBe("sign_in"));
    expect(puts(daemon, "/providers/opencode/connection")).toEqual([{ method: "api_key" }, { method: "sign_in" }]);
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCode" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    await step("provider-signin");
    expect(localStorage.getItem(ONBOARDED_KEY)).toBeNull();
  });

  it("onboarding: an install that fails says what the installer said on its card, and Retry runs it again inline", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_IN, providers: freshProviders() });
    daemon.installFails.claude = 1;
    launch(daemon);

    await step("connect");
    fireEvent.click(within(screen.getByTestId("onb-card-claude")).getByRole("radio", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: "Connect Claude Code" }));

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
    renderApp(<App store={store} client={client} refresh={sync.refresh} />, daemon.ptyViews());
  }
  const refusal = (status: number, code: string, message: string) => new Response(JSON.stringify({ error: { code, message } }), { status, headers: { "Content-Type": "application/json" } });

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
    // The gate asked nothing; provider status stays out of the sidebar.
    expect(screen.queryByTestId("provider-problem")).toBeNull();
    // The feed's own ask comes from an effect, which may run after the sidebar has painted.
    await waitFor(() => expect(daemon.requests.filter((r) => r.path === "/providers/installed").length).toBe(asked + 1));
  });

  /* The way out of a sign-in step: Try again, and on a provider's, Skip for now. */
  const escape = (scope: HTMLElement) => ({ retry: within(scope).getByTestId("onb-signin-retry"), skip: within(scope).getByTestId("onb-signin-skip") });

  it("onboarding's brindle sign-in never hangs and can't be skipped: a failed or abandoned browser login offers Try again, never Skip for now", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_OUT, providers: freshProviders() });
    daemon.deferLogin = true;
    let refuse = true;
    const store = createAppStore();
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => (refuse && String(input).includes("/v1/account/login") ? Promise.resolve(refusal(502, "upstream", "pawdelta.com didn't answer")) : daemon.fetch(input, init)),
    });
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} openUrl={vi.fn(() => Promise.resolve())} />, daemon.ptyViews());
    await step("signin");
    const logins = () => posts(daemon).filter((p) => p === "/account/login").length;
    // An account is required: no way past the step but signing in, and the screen says it's free.
    const noSkip = () => {
      expect(screen.queryByTestId("onb-signin-skip")).toBeNull();
      expect(screen.queryByRole("button", { name: "Skip for now" })).toBeNull();
      expect(screen.queryByText(/sign in later/)).toBeNull();
    };
    noSkip();
    expect(screen.getByTestId("signin-free").textContent).toBe("brindle needs a free account to run. No payment details are asked for.");

    // The login fails: Try again starts it afresh.
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await screen.findByRole("alert");
    noSkip();
    refuse = false;
    fireEvent.click(screen.getByTestId("onb-signin-retry"));
    await waitFor(() => expect(logins()).toBe(1));

    // The browser never comes back: Try again ends that sign-in and starts a fresh one (the old one's end isn't a failure here).
    fireEvent.click(screen.getByTestId("onb-signin-retry"));
    await waitFor(() => expect(logins()).toBe(2));
    expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/account/login").at(-1)?.body).toEqual({ device: false, restart: true });
    await flush();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toBeTruthy();
    noSkip();
    // Only finishing it goes on, to the cards.
    expect(sidebar()).toBeNull();
    daemon.completeLogin();
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
    renderApp(<App store={store} client={client} refresh={sync.refresh} openUrl={vi.fn(() => Promise.resolve())} />, daemon.ptyViews());
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

  it("consent: the demo's consent is a checkbox you tick; a failed recording leaves it open to tick again, then the demo starts", async () => {
    const daemon = new FakeDaemon({ preflight: { claude: [{ id: "claude.onboarding", kind: "consent", label: "Claude Code's security notes", done: false }] } });
    let failNext = true;
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => {
        if (failNext && String(input).includes("/preflight/")) {
          failNext = false;
          return Promise.resolve(Response.json({ error: { code: "internal", message: "could not write the file" } }, { status: 500 }));
        }
        return daemon.fetch(input, init);
      },
    });
    const onStarted = vi.fn();
    render(
      <DaemonContext.Provider value={client}>
        <StoreContext.Provider value={createAppStore()}>
          <SessionsProvider client={client}>
            <PracticeRow client={client} onStarted={onStarted} />
          </SessionsProvider>
        </StoreContext.Provider>
      </DaemonContext.Provider>,
    );
    fireEvent.click(within(screen.getByRole("region", { name: "Practice run" })).getByRole("button"));
    const card = await screen.findByTestId("preflight");
    const box = within(card).getByRole("checkbox", { name: /Claude Code's security notes/ }) as HTMLInputElement;
    fireEvent.click(box);
    await within(card).findByText(/could not write the file/);
    expect(box.checked).toBe(false);
    expect(within(card).getByRole("button", { name: "Start the demo" })).toHaveProperty("disabled", true);
    fireEvent.click(box);
    await waitFor(() => expect(within(card).getByRole("button", { name: "Start the demo" })).toHaveProperty("disabled", false));
    fireEvent.click(within(card).getByRole("button", { name: "Start the demo" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
  });

});
