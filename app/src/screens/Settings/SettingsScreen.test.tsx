import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonContext } from "../../api/context";
import { ProvidersProvider } from "../../store/providers";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN, flush, freshProviders, type DaemonData } from "../../test/fakeDaemon";
import { PtyViewsProvider } from "../../api/ptyView";
import { REPO } from "../../test/fixtures";
import { SettingsScreen } from ".";
import type { SettingsScreenProps } from "./SettingsScreen";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

afterEach(cleanup);

function setup(data: Partial<DaemonData> = {}, installCli?: SettingsScreenProps["installCli"], appUpdater?: SettingsScreenProps["appUpdater"]) {
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    config: {
      user: { supervisor: "claude" },
      repo: { checks: ["npm test"], max_agents: 5, default_agent: "developer", sidebar: "left", overlap: null },
      local: {},
      keys: [
        { name: "ANTHROPIC_API_KEY", set: true, last4: "WXYZ" },
        { name: "OPENAI_API_KEY", set: false, last4: "" },
      ],
    },
    doctor: [
      { level: "ok", name: "python", detail: "3.12" },
      { level: "fail", name: "tmux", detail: "not found: brindle runs every agent in a tmux window. Install: brew install tmux" },
      { level: "ok", name: "claude", detail: "/opt/homebrew/bin/claude (2.1.0)" },
      { level: "ok", name: "codex", detail: "/opt/homebrew/bin/codex (0.48.0)" },
      { level: "warn", name: "agy", detail: "not found: only needed for Google Antigravity agents" },
      { level: "ok", name: "Claude Code sign-in", detail: "its own login; quota: no limit in effect" },
      { level: "warn", name: "Codex sign-in", detail: "signed out: run `codex login`; quota: no limit in effect" },
      { level: "ok", name: "brindle update", detail: "brindle 0.5.1 is available (you have 0.5.0): uv tool install --upgrade brindle" },
    ],
    // Claude Code found and signed in, Codex found but signed out, OpenCode and Antigravity not installed.
    providers: freshProviders({ claude: { path: "/opt/homebrew/bin/claude", version: "2.1.0" }, codex: { path: "/opt/homebrew/bin/codex", version: "0.48.0", signed_in: false } }),
    ...data,
  });
  const store = createAppStore();
  /* Paths the daemon refuses with a transport error, for the moment. */
  const failing = new Set<string>();
  const fetch: typeof globalThis.fetch = (input, init) =>
    [...failing].some((p) => String(input).includes(p)) ? Promise.reject(new Error("socket hung up")) : daemon.fetch(input, init);
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch });
  const sync = startSync({ client, store, connect: daemon.connect });
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <ProvidersProvider client={client}>
            {/* A sign-in's CLI read through the fake daemon's pty views, as the shell's show it. */}
            <PtyViewsProvider value={daemon.ptyViews()}>
              <SettingsScreen client={client} installCli={installCli} appUpdater={appUpdater} />
            </PtyViewsProvider>
          </ProvidersProvider>
        </SessionsProvider>
      </StoreContext.Provider>
    </DaemonContext.Provider>,
  );
  return { daemon, failing, stop: () => sync.stop() };
}

const patches = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "PATCH" && r.path === "/config");
const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST");
const doctorCalls = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.path === "/doctor");

describe("settings screen", () => {
  it("shows the repo's common settings as rows with the rest behind More, and saves what changed where the person says", async () => {
    const { daemon, stop } = setup();
    const repoSettings = await screen.findByTestId("repo-settings");
    expect(daemon.requests.find((r) => r.path === "/config")?.query).toEqual({ repo: REPO });
    // The handful people change most, read as the engine merges the files; the rest behind More.
    const checks = screen.getByRole("textbox", { name: "Checks" });
    expect(checks).toHaveProperty("value", "npm test");
    expect(screen.getByRole("spinbutton", { name: "Workers at once" })).toHaveProperty("value", "5");
    expect(screen.getByRole("switch", { name: "Plan first" }).getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByRole("combobox", { name: "Overlapping tasks" })).toBeNull();
    expect(screen.queryByTestId("settings-save")).toBeNull();
    // Typed a keystroke at a time: the space and the Enter stay on screen while the draft reads the lines.
    for (const typed of ["npm test ", "npm test \n", "npm test \nn", "npm test \nnpm run build"]) fireEvent.change(checks, { target: { value: typed } });
    expect(checks).toHaveProperty("value", "npm test \nnpm run build");
    fireEvent.change(screen.getByRole("spinbutton", { name: "Workers at once" }), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("switch", { name: "Plan first" }));
    expect(screen.getByRole("switch", { name: "Plan first" }).getAttribute("aria-checked")).toBe("true");
    expect(repoSettings.querySelector('[data-setting="plan_first"]')!.textContent).toContain("edited");
    const bar = screen.getByTestId("settings-save");
    expect(bar.textContent).toContain("3 changes");
    // Checks live in the repo's files only: the save is pinned to this repo, whatever was picked before.
    expect(within(bar).getByRole("radio", { name: "This repo" })).toHaveProperty("checked", true);
    expect(within(bar).getByRole("radio", { name: "Every repo" })).toHaveProperty("disabled", true);
    expect(screen.getByTestId("repo-only").textContent).toContain("Checks only count from a repo's own files");
    fireEvent.click(within(bar).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Saved checks, max_agents, plan_first to /home/u/proj/.brindle/config.local.json"));
    expect(patches(daemon)[0].body).toEqual({ scope: "repo", repo: REPO, values: { checks: ["npm test", "npm run build"], max_agents: 3, plan_first: true } });
    // The local file took them; read back as the engine merges the files (a local list replaces the shared one): the
    // same lines, so the text stays as typed, and nothing is left to save.
    expect(daemon.data.config.local).toEqual({ checks: ["npm test", "npm run build"], max_agents: 3, plan_first: true });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Checks" })).toHaveProperty("value", "npm test \nnpm run build"));
    expect(screen.queryByTestId("settings-save")).toBeNull();
    // A number can't be left empty: the engine would store the null and choke on it later.
    fireEvent.change(screen.getByRole("spinbutton", { name: "Workers at once" }), { target: { value: "" } });
    expect(screen.getByTestId("settings-invalid").textContent).toContain("Workers at once needs a number");
    expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    // A change the person's file can carry goes there, without the repo.
    fireEvent.change(screen.getByRole("spinbutton", { name: "Workers at once" }), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("radio", { name: "Every repo" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches(daemon)).toHaveLength(2));
    expect(patches(daemon)[1].body).toEqual({ scope: "user", values: { max_agents: 2 } });
    expect(daemon.data.config.user.max_agents).toBe(2);
    // The local file still wins for this repo: the screen reads 3 again, as the engine would.
    await waitFor(() => expect(screen.getByRole("spinbutton", { name: "Workers at once" })).toHaveProperty("value", "3"));
    // More: the other settings; Everything else: the rest of the config to read; the person's own defaults likewise.
    fireEvent.click(within(repoSettings).getByRole("button", { name: /^More/ }));
    expect(screen.getByRole("combobox", { name: "Overlapping tasks" })).toBeTruthy();
    expect(screen.getByRole("switch", { name: "Coordinator" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Everything else/ }));
    expect(screen.getByTestId("config-rest").textContent).toContain("default_agent");
    fireEvent.click(screen.getByRole("button", { name: /Your defaults for every repo/ }));
    expect(screen.getByTestId("user-rest").textContent).toContain("supervisor");
    stop();
  });

  it("reads the person's Claude permission mode, saves a pick at once, and says when it could not", async () => {
    const { daemon, failing, stop } = setup();
    const row = await screen.findByTestId("permission-mode");
    await waitFor(() => expect(row.getAttribute("data-mode")).toBe("default"));
    expect(within(row).getByRole("radio", { name: "Normal" }).getAttribute("aria-checked")).toBe("true");
    expect(row.textContent).toContain("show up in the Inbox");
    fireEvent.click(within(row).getByRole("radio", { name: "Auto" }));
    await waitFor(() => expect(row.getAttribute("data-mode")).toBe("auto"));
    expect(daemon.requests.filter((r) => r.method === "PATCH" && r.path === "/user-settings").at(-1)?.body).toEqual({ claude_permission_mode: "auto" });
    expect(daemon.data.userSettings.claude_permission_mode).toBe("auto");
    expect(row.textContent).toContain("approves most commands itself");
    // The engine refusing (or not answering) is said; the mode stays what it was.
    failing.add("/user-settings");
    fireEvent.click(within(row).getByRole("radio", { name: "Normal" }));
    await waitFor(() => expect(within(row).getByRole("alert").textContent).toContain("Could not save: socket hung up"));
    expect(row.getAttribute("data-mode")).toBe("auto");
    expect(daemon.data.userSettings.claude_permission_mode).toBe("auto");
    // No popup anywhere: the choice and its words are in the row itself.
    expect(screen.queryByRole("dialog")).toBeNull();
    stop();
  });

  it("lists the live permission grants and revokes one through the engine", async () => {
    const grant = { id: "g1", agent_id: "wk", repo_root: REPO, kind: "fetch", match: "https://example.com/docs", expires_at: 2e9, uses_left: 2, created: 1e9 };
    const { daemon, stop } = setup({ grants: [grant, { ...grant, id: "g2", kind: "bash", match: "terraform plan" }] });
    const rows = await within(await screen.findByTestId("grants")).findAllByTestId("grant");
    expect(rows.map((r) => r.querySelector("code")?.textContent)).toEqual(["fetch https://example.com/docs", "bash terraform plan"]);
    fireEvent.click(within(rows[0]).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.getAllByTestId("grant")).toHaveLength(1));
    expect(daemon.requests.some((r) => r.method === "POST" && r.path === "/grants/g1/revoke")).toBe(true);
    expect(daemon.data.grants.map((g) => g.id)).toEqual(["g2"]);
    stop();
  });

  it("adds permission rules as the engine describes them back, and resets them only after asking", async () => {
    const { daemon, stop } = setup();
    const form = await screen.findByRole("form", { name: "Add a rule" });
    fireEvent.change(within(form).getByLabelText("Match"), { target: { value: "npm test" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add rule" }));
    await waitFor(() => expect(screen.getByTestId("permissions").textContent).toContain("Added: allow bash exact 'npm test'"));
    expect(daemon.requests.filter((r) => r.path === "/permissions").at(-1)?.body).toEqual({ op: "allow", kind: "bash", match: "npm test", match_type: "exact" });
    expect(daemon.permissionRules).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Reset your rules…" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset rules" }));
    await waitFor(() => expect(daemon.permissionRules).toHaveLength(0));
    stop();
  });

  it("keeps the keys in Providers: a key stored write-only from a password field, no terminal anywhere", async () => {
    const { daemon, stop } = setup();
    const keys = await screen.findByTestId("providers");
    await within(keys).findByTestId("key-ANTHROPIC_API_KEY");
    expect(keys.textContent).not.toContain("in a terminal");
    expect(within(keys).getByTestId("key-ANTHROPIC_API_KEY").textContent).toContain("••••WXYZ");
    expect(within(keys).getByTestId("key-OPENAI_API_KEY").textContent).toContain("Not set");

    // Set a key: a password field; Save sends the value to PUT /keys/{name} and keeps nothing of it.
    fireEvent.click(within(keys).getByRole("button", { name: "Set OPENAI_API_KEY" }));
    const form = within(keys).getByRole("form", { name: "Value for OPENAI_API_KEY" });
    const value = within(form).getByLabelText("OPENAI_API_KEY value") as HTMLInputElement;
    expect(value.type).toBe("password");
    fireEvent.change(value, { target: { value: "sk-test-0000-9xTz" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByTestId("key-OPENAI_API_KEY").textContent).toContain("••••9xTz"));
    expect(daemon.requests.filter((r) => r.method === "PUT")).toEqual([{ method: "PUT", path: "/keys/OPENAI_API_KEY", query: {}, body: { value: "sk-test-0000-9xTz", repo: REPO } }]);
    expect(daemon.keysStored).toEqual(["OPENAI_API_KEY"]);
    expect(screen.queryByDisplayValue("sk-test-0000-9xTz")).toBeNull();
    expect(screen.queryByRole("form", { name: "Value for OPENAI_API_KEY" })).toBeNull();
    expect(screen.getByTestId("key-OPENAI_API_KEY").textContent).toContain("OPENAI_API_KEY stored.");
    // A key brindle doesn't know yet, by name. A value typed for one new key never carries over to the field
    // for another: the second starts empty, with nothing to save.
    const add = screen.getByRole("form", { name: "Add a key" });
    fireEvent.change(within(add).getByLabelText("Key name"), { target: { value: "review_first" } });
    expect(within(add).getByLabelText("Key name")).toHaveProperty("value", "REVIEW_FIRST");
    fireEvent.click(within(add).getByRole("button", { name: "Add a key…" }));
    fireEvent.change(screen.getByLabelText("REVIEW_FIRST value"), { target: { value: "first-value-0001" } });
    fireEvent.change(within(add).getByLabelText("Key name"), { target: { value: "REVIEW_SECOND" } });
    fireEvent.click(within(add).getByRole("button", { name: "Add a key…" }));
    expect(screen.queryByRole("form", { name: "Value for REVIEW_FIRST" })).toBeNull();
    const second = screen.getByRole("form", { name: "Value for REVIEW_SECOND" });
    expect(within(second).getByLabelText("REVIEW_SECOND value")).toHaveProperty("value", "");
    expect(within(second).getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    fireEvent.click(within(second).getByRole("button", { name: "Cancel" }));
    // Brindle's own token is refused by the engine, in its words, in the field.
    fireEvent.change(within(add).getByLabelText("Key name"), { target: { value: "brindle_token" } });
    expect(within(add).getByLabelText("Key name")).toHaveProperty("value", "BRINDLE_TOKEN");
    fireEvent.click(within(add).getByRole("button", { name: "Add a key…" }));
    const token = screen.getByRole("form", { name: "Value for BRINDLE_TOKEN" });
    fireEvent.change(within(token).getByLabelText("BRINDLE_TOKEN value"), { target: { value: "nope" } });
    fireEvent.click(within(token).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(token).getByRole("alert").textContent).toContain("Could not store BRINDLE_TOKEN: BRINDLE_TOKEN isn't a key agents can be given"));
    expect(daemon.keysStored).toEqual(["OPENAI_API_KEY"]);
    fireEvent.click(within(token).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("form", { name: "Value for BRINDLE_TOKEN" })).toBeNull();
    expect(screen.queryByTestId("terminal")).toBeNull();
    stop();
  });

  it("providers: one row per provider, found at its path with its version or not installed, signed in or not, with the settings note and no update controls", async () => {
    const { stop } = setup();
    const section = await screen.findByTestId("providers");
    expect(within(section).getByRole("heading", { name: "Providers" })).toBeTruthy();
    const claude = await within(section).findByTestId("provider-claude");
    expect([...section.querySelectorAll("[data-provider]")].map((r) => r.getAttribute("data-provider"))).toEqual(["claude", "codex", "opencode", "antigravity"]);
    expect(claude.getAttribute("data-state")).toBe("signed_in");
    expect(claude.textContent).toContain("Found at /opt/homebrew/bin/claude · 2.1.0");
    expect(claude.textContent).toContain("brindle keeps its own copy of Claude Code's settings.");
    expect(within(claude).getByTestId("provider-state").textContent).toBe("Signed in");
    expect(within(claude).queryByRole("button", { name: /Sign in/ })).toBeNull();
    const codex = screen.getByTestId("provider-codex");
    expect(codex.getAttribute("data-state")).toBe("signed_out");
    expect(within(codex).getByTestId("provider-state").textContent).toBe("Not signed in");
    expect(within(codex).getByRole("button", { name: "Sign in to Codex" })).toBeTruthy();
    const opencode = screen.getByTestId("provider-opencode");
    expect(opencode.getAttribute("data-state")).toBe("not_installed");
    expect(opencode.textContent).toContain("Connect runs Anomaly's official installer (opencode.ai/install), as-is.");
    expect(within(opencode).getByRole("button", { name: "Connect OpenCode" })).toBeTruthy();
    expect(within(opencode).queryByRole("button", { name: /Remove/ })).toBeNull();
    // Each found CLI can be taken out of brindle; one not installed can't.
    expect(within(claude).getByRole("button", { name: "Remove Claude Code from brindle" })).toBeTruthy();
    expect(within(codex).getByRole("button", { name: "Remove Codex from brindle" })).toBeTruthy();
    // The CLI keeps itself up to date: nothing here offers or reports an update.
    expect(within(section).queryByRole("button", { name: /update/i })).toBeNull();
    expect(section.textContent).not.toMatch(/updated|update now|is ready/i);
    // No typed commands: no command-line text for a person to run.
    expect(section.textContent).not.toMatch(/curl|brew install|npm i|codex login/);
    stop();
  });

  it("providers: Antigravity's row says its settings are the shared ~/.gemini", async () => {
    const { stop } = setup({ providers: freshProviders({ antigravity: { path: "/home/u/.local/bin/agy", version: "1.3.0" } }) });
    const agy = await screen.findByTestId("provider-antigravity");
    await waitFor(() => expect(agy.getAttribute("data-state")).toBe("signed_in"));
    expect(agy.textContent).toContain("Antigravity shares its settings with yours, in ~/.gemini.");
    expect(agy.textContent).not.toContain("own copy");
    stop();
  });

  it("providers: Connect runs the maker's official installer with its lines streaming into the row, then detects the CLI again; a failed install stays with Retry", async () => {
    const { daemon, stop } = setup();
    const opencode = await screen.findByTestId("provider-opencode");
    await waitFor(() => expect(opencode.getAttribute("data-state")).toBe("not_installed"));
    daemon.deferInstall = true;
    fireEvent.click(within(opencode).getByRole("button", { name: "Connect OpenCode" }));
    await waitFor(() => expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/providers/opencode/install", query: {}, body: {} }));
    const card = await within(opencode).findByTestId("provider-install-opencode");
    await waitFor(() => expect(within(card).getByTestId("provider-install-opencode-lines").textContent).toContain("Downloading opencode-darwin-arm64.zip"));
    expect(card.getAttribute("data-state")).toBe("running");
    expect(within(opencode).queryByRole("button", { name: "Connect OpenCode" })).toBeNull();
    const asked = daemon.requests.filter((r) => r.path === "/providers/installed").length;
    daemon.completeInstall();
    // Done: the rows are read again, and the row says where the installer put it.
    await waitFor(() => expect(screen.getByTestId("provider-opencode").getAttribute("data-state")).toBe("signed_out"));
    expect(daemon.requests.filter((r) => r.path === "/providers/installed").length).toBeGreaterThan(asked);
    expect(screen.getByTestId("provider-opencode").textContent).toContain("Found at /home/u/.opencode/bin/opencode · 1.2.8");
    expect(screen.queryByTestId("provider-install-opencode")).toBeNull();
    expect(within(screen.getByTestId("provider-opencode")).getByRole("button", { name: "Sign in to OpenCode" })).toBeTruthy();

    // A failed install: the installer's words on the row, Retry runs it again, Dismiss lets it go.
    daemon.deferInstall = false;
    daemon.installFails.antigravity = 1;
    const agy = screen.getByTestId("provider-antigravity");
    fireEvent.click(within(agy).getByRole("button", { name: "Connect Antigravity" }));
    const failed = await within(agy).findByTestId("provider-install-antigravity-failed");
    expect(failed.textContent).toContain("Install failed.");
    expect(failed.textContent).toContain("Connection reset by peer");
    expect(agy.getAttribute("data-state")).toBe("not_installed");
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("provider-antigravity").getAttribute("data-state")).toBe("signed_out"));
    expect(daemon.installs).toEqual(["opencode", "antigravity", "antigravity"]);
    expect(screen.queryByRole("dialog")).toBeNull();
    stop();
  });

  it("providers: Sign in runs the CLI's own login in the browser-first card, and the row reads signed in after", async () => {
    const { daemon, stop } = setup();
    const codex = await screen.findByTestId("provider-codex");
    await waitFor(() => expect(codex.getAttribute("data-state")).toBe("signed_out"));
    fireEvent.click(within(codex).getByRole("button", { name: "Sign in to Codex" }));
    const flow = await within(codex).findByTestId("sign-in-flow");
    expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/providers/codex/login", query: {}, body: {} });
    expect(daemon.actions.at(-1)?.command).toEqual(["codex", "login"]);
    await waitFor(() => expect(flow.getAttribute("data-phase")).toBe("waiting"));
    expect(flow.textContent).toContain("Finish signing in to Codex in your browser");
    expect(within(flow).queryByRole("button", { name: "Show details" })).toBeNull();
    expect(screen.queryByTestId("terminal")).toBeNull();
    // The browser's sign-in finishes; the CLI exits and the rows read again say so.
    daemon.signInProvider("codex");
    await waitFor(() => expect(flow.getAttribute("data-phase")).toBe("done"));
    expect(flow.textContent).toContain("Signed in to Codex ✓");
    expect(screen.getByTestId("provider-codex").getAttribute("data-state")).toBe("signed_in");
    stop();
  });

  it("providers: Remove from brindle turns the CLI off without uninstalling it, and Connect turns the copy still found back on, no installer run", async () => {
    const { daemon, failing, stop } = setup();
    const codex = await screen.findByTestId("provider-codex");
    await waitFor(() => expect(codex.getAttribute("data-state")).toBe("signed_out"));
    fireEvent.click(within(codex).getByRole("button", { name: "Remove Codex from brindle" }));
    await waitFor(() => expect(screen.getByTestId("provider-codex").getAttribute("data-state")).toBe("off"));
    expect(daemon.requests.filter((r) => r.method === "DELETE")).toEqual([{ method: "DELETE", path: "/providers/codex", query: {}, body: {} }]);
    expect(daemon.removedProviders).toEqual(["codex"]);
    // The engine's answer, as GET /providers/installed shows it now: off, its path still found.
    const off = daemon.data.providers.find((r) => r.provider === "codex")!;
    expect(off).toMatchObject({ state: "off", path: "/opt/homebrew/bin/codex", version: "0.48.0" });
    const row = screen.getByTestId("provider-codex");
    expect(within(row).getByTestId("provider-state").textContent).toBe("Removed from brindle");
    expect(row.textContent).toContain("Still installed at /opt/homebrew/bin/codex; brindle doesn't use it.");
    expect(within(row).queryByRole("button", { name: /Sign in/ })).toBeNull();
    expect(within(row).queryByRole("button", { name: /Remove/ })).toBeNull();
    // A turn-on the engine can't answer says so on the row, and leaves it off.
    failing.add("/providers/codex/use-mine");
    fireEvent.click(within(row).getByRole("button", { name: "Connect Codex" }));
    await waitFor(() => expect(within(screen.getByTestId("provider-codex")).getByRole("alert").textContent).toContain("Couldn't connect Codex: socket hung up"));
    expect(screen.getByTestId("provider-codex").getAttribute("data-state")).toBe("off");
    failing.clear();
    // Connect: the copy still found runs again (POST .../use-mine), nothing installed or uninstalled.
    fireEvent.click(within(screen.getByTestId("provider-codex")).getByRole("button", { name: "Connect Codex" }));
    await waitFor(() => expect(screen.getByTestId("provider-codex").getAttribute("data-state")).toBe("signed_out"));
    expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/providers/codex/use-mine", query: {}, body: { path: "/opt/homebrew/bin/codex" } });
    expect(daemon.pickedInstalls).toEqual(["codex /opt/homebrew/bin/codex"]);
    expect(daemon.installs).toEqual([]);
    expect(within(screen.getByTestId("provider-codex")).getByRole("button", { name: "Remove Codex from brindle" })).toBeTruthy();
    stop();
  });

  it("providers: an install that finished while the look for the CLI failed keeps its card with Look again, which finds it without installing twice", async () => {
    const { daemon, stop } = setup();
    const opencode = await screen.findByTestId("provider-opencode");
    await waitFor(() => expect(opencode.getAttribute("data-state")).toBe("not_installed"));
    daemon.installDetectFails = 2;
    fireEvent.click(within(opencode).getByRole("button", { name: "Connect OpenCode" }));
    // The installer finished; the look after it failed. The stale row offers no second install.
    const detect = await screen.findByTestId("provider-install-opencode-detect");
    await waitFor(() => expect(detect.textContent).toContain("OpenCode is installed, but brindle couldn't look for it again"));
    expect(screen.getByTestId("provider-install-opencode").getAttribute("data-state")).toBe("done");
    expect(within(screen.getByTestId("provider-opencode")).queryByRole("button", { name: "Connect OpenCode" })).toBeNull();
    expect(screen.getByTestId("providers-refresh-error").textContent).toContain("Couldn't look for the providers again");
    // Looking again fails once more, then finds it: the row reads it, the card goes, one install all along.
    const looks = () => daemon.requests.filter((r) => r.path === "/providers/installed").length;
    const asked = looks();
    fireEvent.click(within(detect).getByRole("button", { name: "Look again" }));
    await waitFor(() => expect(looks()).toBe(asked + 1));
    await flush();
    expect(screen.getByTestId("provider-install-opencode-detect").textContent).toContain("couldn't look for it again");
    expect(screen.getByTestId("provider-opencode").getAttribute("data-state")).toBe("not_installed");
    fireEvent.click(within(screen.getByTestId("provider-install-opencode-detect")).getByRole("button", { name: "Look again" }));
    await waitFor(() => expect(screen.getByTestId("provider-opencode").getAttribute("data-state")).toBe("signed_out"));
    expect(screen.queryByTestId("provider-install-opencode")).toBeNull();
    expect(screen.queryByTestId("providers-refresh-error")).toBeNull();
    expect(screen.getByTestId("provider-opencode").textContent).toContain("Found at /home/u/.opencode/bin/opencode · 1.2.8");
    expect(posts(daemon).filter((r) => r.path === "/providers/opencode/install")).toHaveLength(1);
    expect(daemon.installs).toEqual(["opencode"]);
    stop();
  });

  it("lists the engine's health with a fix where the app has one, updates the engine as a job followed on the bus (done, or failed with uv's tail), and keeps the command-line tool's button off without the shell's installer", async () => {
    const { daemon, stop } = setup();
    const engine = await screen.findByTestId("engine");
    await waitFor(() => expect(engine.textContent).toContain("brindle 0.5.0"));
    // What is wrong, counted (the pending update counts; optional warnings and the sign-in rows don't).
    await waitFor(() => expect(engine.textContent).toContain("2 things need attention"));
    const doctor = screen.getByTestId("doctor");
    expect(doctor.querySelector('li[data-check="tmux"]')!.getAttribute("data-level")).toBe("fail");
    // The install pointer is the fix for a missing tool: kept as the doctor wrote it.
    expect(doctor.querySelector('li[data-check="tmux"]')!.textContent).toContain("brew install tmux");
    expect(doctor.querySelector('li[data-check="Codex sign-in"]')).toBeNull();
    expect(doctor.querySelector('li[data-check="agy"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Only some setups need these/ }));
    expect(screen.getByTestId("doctor-optional").querySelector('li[data-check="agy"]')).toBeTruthy();
    // The update line: a warning here, with its command dropped and a button instead.
    const update = doctor.querySelector<HTMLElement>('li[data-check="brindle update"]')!;
    expect(update.getAttribute("data-level")).toBe("warn");
    expect(update.textContent).not.toContain("uv tool install");
    expect(screen.getByTestId("update-notice").textContent).toBe("brindle 0.5.1 is available (you have 0.5.0).");
    expect(within(update).getByRole("button", { name: "Update" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(doctorCalls(daemon)).toHaveLength(2));

    // The engine's update: a job of the engine's, followed on the bus. A failure shows uv's tail, from the
    // job.failed event; no terminal is shown or read.
    daemon.deferUpgrade = true;
    daemon.upgradeFails = true;
    fireEvent.click(screen.getByRole("button", { name: "Update brindle" }));
    const card = await screen.findByTestId("upgrade-card");
    expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/engine/upgrade", query: {}, body: {} });
    await waitFor(() => expect(card.getAttribute("data-state")).toBe("running"));
    expect(screen.queryByRole("button", { name: "Update brindle" })).toBeNull();
    daemon.completeUpgrade();
    await waitFor(() => expect(card.getAttribute("data-state")).toBe("failed"));
    expect(card.textContent).toContain("The update didn't finish: uv tool upgrade brindle exited 1");
    expect(screen.getByTestId("upgrade-log").textContent).toContain("network unreachable");
    expect(screen.queryByTestId("terminal")).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("upgrade-card")).toBeNull();
    // Done, the engine and the doctor are read again: the new version, nothing left to update.
    daemon.upgradeFails = false;
    fireEvent.click(screen.getByRole("button", { name: "Update brindle" }));
    await waitFor(() => expect(screen.getByTestId("upgrade-card").getAttribute("data-state")).toBe("running"));
    daemon.data.doctor = daemon.data.doctor.filter((c) => c.name !== "brindle update");
    daemon.completeUpgrade();
    await waitFor(() => expect(screen.getByTestId("upgrade-card").getAttribute("data-state")).toBe("done"));
    await waitFor(() => expect(doctorCalls(daemon)).toHaveLength(3));
    await waitFor(() => expect(screen.getByTestId("engine").textContent).toContain("brindle 0.5.1"));
    await waitFor(() => expect(screen.getByTestId("engine").textContent).toContain("Up to date"));

    // The command-line tool comes with the bundled engine: without its installer the button is off, and says so.
    const cli = screen.getByTestId("install-cli");
    expect(within(cli).getByRole("button", { name: "Install command-line tool" })).toHaveProperty("disabled", true);
    expect(cli.textContent).toContain("Not in this build yet");
    stop();
  });

  it("installs the command-line tool through the shell when it offers to, and says where the link went on the row", async () => {
    const install = vi.fn(() => Promise.resolve({ path: "/home/sam/.local/bin/brindle", target: "/Applications/brindle.app/Contents/Resources/engine/bin/brindle" }));
    const { stop } = setup({}, install);
    const cli = await screen.findByTestId("install-cli");
    const btn = within(cli).getByRole("button", { name: "Install command-line tool" });
    expect(btn).toHaveProperty("disabled", false);
    fireEvent.click(btn);
    await waitFor(() => expect(cli.textContent).toContain("Installed as /home/sam/.local/bin/brindle: a terminal can run brindle now."));
    expect(install).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    stop();
  });

  it("shows a doctor that didn't answer as a failure, never as a clean bill or an up-to-date engine", async () => {
    const { failing, stop } = setup({ doctor: [] });
    failing.add("/doctor");
    await screen.findByTestId("doctor-error");
    const engine = screen.getByTestId("engine");
    expect(engine.textContent).toContain("The checks didn't run.");
    expect(engine.textContent).toContain("socket hung up");
    expect(engine.textContent).not.toContain("Everything brindle needs is here");
    expect(engine.textContent).not.toContain("Up to date");
    expect(screen.queryByRole("button", { name: "Update brindle" })).toBeNull();
    // The provider rows come from the engine's detection, not the doctor: they stay.
    await waitFor(() => expect(screen.getByTestId("provider-claude").getAttribute("data-state")).toBe("signed_in"));
    failing.clear();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByTestId("doctor-error")).toBeNull());
    stop();
  });

  it("hides debug logging from anyone the daemon doesn't call staff", async () => {
    const { daemon, stop } = setup({ debug: { staff: false, enabled: true } });
    await screen.findByTestId("engine");
    await waitFor(() => expect(daemon.requests.some((r) => r.method === "GET" && r.path === "/debug")).toBe(true));
    await flush();
    expect(screen.queryByTestId("debug-logging")).toBeNull();
    expect(screen.queryByRole("switch", { name: "Debug logging" })).toBeNull();
    stop();
  });

  it("gives staff one debug logging switch, which turns it on with PUT /debug", async () => {
    const { daemon, stop } = setup({ debug: { staff: true, enabled: false } });
    const section = await screen.findByTestId("debug-logging");
    expect(section.textContent).toContain("upload to brindle");
    const toggle = within(section).getByRole("switch", { name: "Debug logging" });
    expect(within(section).getAllByRole("switch")).toHaveLength(1);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    expect(daemon.requests.filter((r) => r.method === "PUT" && r.path === "/debug").map((r) => r.body)).toEqual([{ enabled: true }]);
    expect(daemon.data.debug.enabled).toBe(true);
    stop();
  });

  it("staff update: hides the app's Update from anyone the daemon doesn't call staff", async () => {
    const updater = { install: vi.fn(async () => "0.1.0-rc.8"), restart: vi.fn(async () => {}) };
    const { daemon, stop } = setup({ debug: { staff: false, enabled: false } }, undefined, updater);
    await screen.findByTestId("engine");
    await waitFor(() => expect(daemon.requests.some((r) => r.method === "GET" && r.path === "/debug")).toBe(true));
    await flush();
    expect(screen.queryByTestId("app-update")).toBeNull();
    expect(screen.queryByTestId("app-update-row")).toBeNull();
    expect(screen.queryByText("Update the app")).toBeNull();
    expect(updater.install).not.toHaveBeenCalled();
    stop();
  });

  it("staff update: gives staff an Update button that installs the new build and offers a restart", async () => {
    const updater = { install: vi.fn(async () => "0.1.0-rc.8"), restart: vi.fn(async () => {}) };
    const { stop } = setup({ debug: { staff: true, enabled: false } }, undefined, updater);
    const section = await screen.findByTestId("app-update");
    expect(updater.install).not.toHaveBeenCalled();
    fireEvent.click(within(section).getByRole("button", { name: "Update" }));
    const restart = await within(section).findByRole("button", { name: "Restart" });
    expect(updater.install).toHaveBeenCalledTimes(1);
    expect(section.textContent).toContain("brindle 0.1.0-rc.8 is installed");
    fireEvent.click(restart);
    await waitFor(() => expect(updater.restart).toHaveBeenCalledTimes(1));
    stop();
  });

  it("staff update: says when this is the latest build, and shows the shell's refusal", async () => {
    const latest = { install: vi.fn(async () => null), restart: vi.fn(async () => {}) };
    const first = setup({ debug: { staff: true, enabled: false } }, undefined, latest);
    let section = await screen.findByTestId("app-update");
    fireEvent.click(within(section).getByRole("button", { name: "Update" }));
    await waitFor(() => expect(section.textContent).toContain("This is the latest build."));
    expect(within(section).queryByRole("button", { name: "Restart" })).toBeNull();
    first.stop();
    cleanup();

    const refused = { install: vi.fn(async () => Promise.reject(new Error("updates are off on this machine"))), restart: vi.fn(async () => {}) };
    const second = setup({ debug: { staff: true, enabled: false } }, undefined, refused);
    section = await screen.findByTestId("app-update");
    fireEvent.click(within(section).getByRole("button", { name: "Update" }));
    expect((await within(section).findByRole("alert")).textContent).toContain("updates are off on this machine");
    second.stop();
  });

  it("staff update: shows no Update without the shell's updater (a browser tab), staff or not", async () => {
    const { daemon, stop } = setup({ debug: { staff: true, enabled: false } });
    await screen.findByTestId("debug-logging");
    await flush();
    expect(daemon.requests.some((r) => r.path === "/debug")).toBe(true);
    expect(screen.queryByTestId("app-update")).toBeNull();
    stop();
  });
});
