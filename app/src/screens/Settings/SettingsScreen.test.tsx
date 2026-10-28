import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonContext } from "../../api/context";
import { ProvidersProvider } from "../../store/providers";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FAKE_PRESETS, FakeDaemon, TOKEN, flush, freshProviders, type DaemonData } from "../../test/fakeDaemon";
import { PtyViewsProvider } from "../../api/ptyView";
import { REPO } from "../../test/fixtures";
import { releaseBuildUpdater } from "../../api/tauri";
import { SettingsScreen } from ".";
import type { SettingsScreenProps } from "./SettingsScreen";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

/* The shell's IPC, for the real release-build updater (api/tauri.ts): each
   invoke recorded, the install's answer held until the test gives it. */
const ipc = vi.hoisted(() => {
  const calls: Array<{ cmd: string; args: Record<string, unknown> }> = [];
  const answers: Array<(value: unknown) => void> = [];
  class Channel<T> {
    onmessage: ((message: T) => void) | null = null;
  }
  const invoke = vi.fn((cmd: string, args: Record<string, unknown> = {}) => {
    calls.push({ cmd, args });
    return new Promise((resolve) => answers.push(resolve));
  });
  return { calls, answers, Channel, invoke };
});
vi.mock("@tauri-apps/api/core", () => ({ Channel: ipc.Channel, invoke: ipc.invoke, isTauri: () => true }));

afterEach(() => {
  cleanup();
  ipc.calls.length = 0;
  ipc.answers.length = 0;
});

function setup(data: Partial<DaemonData> = {}, installCli?: SettingsScreenProps["installCli"], appUpdater?: SettingsScreenProps["appUpdater"], releaseUpdater?: SettingsScreenProps["releaseBuildUpdater"], rule?: string, readAbout?: SettingsScreenProps["readAbout"]) {
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
              <SettingsScreen client={client} installCli={installCli} appUpdater={appUpdater} releaseBuildUpdater={releaseUpdater} readAbout={readAbout} rule={rule} />
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
  it("edits your settings and saves them to the user scope, and keeps the repo-only rules in Project rules, saved to the repo", async () => {
    const { daemon, stop } = setup();
    const mine = await screen.findByTestId("your-settings");
    expect(daemon.requests.find((r) => r.path === "/config")?.query).toEqual({ repo: REPO });
    // Your settings lead; the handful people change most show, the rest behind More. Checks is not among them.
    expect(screen.getAllByRole("region").map((s) => s.id).filter(Boolean).slice(0, 3)).toEqual(["your-settings", "you", "this-repo"]);
    expect(within(mine).queryByRole("textbox", { name: "Checks" })).toBeNull();
    expect(within(mine).queryByRole("combobox", { name: "Overlapping tasks" })).toBeNull();
    expect(screen.queryByTestId("user-save")).toBeNull();
    fireEvent.click(within(mine).getByRole("switch", { name: "Plan first" }));
    // A number can't be left empty: the engine would store the null and choke on it later.
    const workers = within(mine).getByRole("spinbutton", { name: "Workers at once" });
    fireEvent.change(workers, { target: { value: "" } });
    expect(screen.getByTestId("settings-invalid").textContent).toContain("Workers at once needs a number");
    expect(screen.getByRole("button", { name: "Save your settings" })).toHaveProperty("disabled", true);
    fireEvent.change(workers, { target: { value: "2" } });
    expect(mine.querySelector('[data-setting="plan_first"]')!.textContent).toContain("edited");
    fireEvent.click(screen.getByRole("button", { name: "Save your settings" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Saved max_agents, plan_first for every project to /home/u/.brindle/config.json"));
    expect(patches(daemon)[0].body).toEqual({ scope: "user", values: { max_agents: 2, plan_first: true } });
    expect(daemon.data.config.user).toMatchObject({ max_agents: 2, plan_first: true });
    expect(daemon.data.config.local).toEqual({});
    // More: the other settings; what the person has set beyond the rows is listed to read.
    fireEvent.click(within(mine).getByRole("button", { name: /^More/ }));
    expect(screen.getByRole("combobox", { name: "Overlapping tasks" })).toBeTruthy();
    expect(screen.getByRole("switch", { name: "Coordinator" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Other settings you have/ }));
    expect(screen.getByTestId("user-rest").textContent).toContain("supervisor");

    // Project rules: checks, typed a keystroke at a time (the space and the Enter stay on screen), saved to the repo.
    const rules = screen.getByTestId("repo-settings");
    const checks = within(rules).getByRole("textbox", { name: "Checks" });
    expect(checks).toHaveProperty("value", "npm test");
    for (const typed of ["npm test ", "npm test \n", "npm test \nn", "npm test \nnpm run build"]) fireEvent.change(checks, { target: { value: typed } });
    expect(checks).toHaveProperty("value", "npm test \nnpm run build");
    const bar = screen.getByTestId("settings-save");
    expect(bar.textContent).toContain("1 change");
    fireEvent.click(within(bar).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches(daemon)).toHaveLength(2));
    expect(patches(daemon)[1].body).toEqual({ scope: "repo", repo: REPO, values: { checks: ["npm test", "npm run build"] } });
    expect(daemon.data.config.local).toEqual({ checks: ["npm test", "npm run build"] });
    await waitFor(() => expect(screen.queryByTestId("settings-save")).toBeNull());
    fireEvent.click(within(rules).getByRole("button", { name: /Everything else/ }));
    expect(screen.getByTestId("config-rest").textContent).toContain("default_agent");
    stop();
  });

  it("marks a setting a project overrides, and clears an override in the local file but not the committed one", async () => {
    const { daemon, stop } = setup({
      config: {
        user: { plan_first: false },
        repo: { max_agents: 2 },
        local: { plan_first: true },
        keys: [],
      },
    });
    const mine = await screen.findByTestId("your-settings");
    // The row is the person's own value; the marker says what this project does instead.
    expect(within(mine).getByRole("switch", { name: "Plan first" }).getAttribute("aria-checked")).toBe("false");
    const marker = await within(mine).findByTestId("override-plan_first");
    expect(marker.textContent).toContain("On in this project");
    expect(within(mine).getByTestId("override-max_agents").textContent).toContain("2 in this project");
    expect(within(mine).queryByTestId("override-autopilot")).toBeNull();
    // Edits not saved yet, in both sections: a cleared override reloads the config but must not eat them.
    fireEvent.click(within(mine).getByRole("switch", { name: "Plan first" }));
    const checks = screen.getByRole("textbox", { name: "Checks" });
    fireEvent.change(checks, { target: { value: "npm test\nnpm run lint" } });
    fireEvent.click(within(marker).getByRole("button", { name: "Clear override" }));
    await waitFor(() => expect(within(mine).queryByTestId("override-plan_first")).toBeNull());
    expect(patches(daemon)[0].body).toEqual({ scope: "repo", repo: REPO, unset: ["plan_first"] });
    expect(daemon.data.config.local).toEqual({});
    expect(daemon.data.config.user).toEqual({ plan_first: false });
    expect(within(mine).getByRole("switch", { name: "Plan first" }).getAttribute("aria-checked")).toBe("true");
    expect(checks).toHaveProperty("value", "npm test\nnpm run lint");
    // The committed file's override has its own explicit clear, scoped to that file; saving your settings keeps the rules' edit.
    fireEvent.click(within(within(mine).getByTestId("override-max_agents")).getByRole("button", { name: "Clear in .brindle/config.json" }));
    await waitFor(() => expect(within(mine).queryByTestId("override-max_agents")).toBeNull());
    expect(patches(daemon)[1].body).toEqual({ scope: "shared", repo: REPO, unset: ["max_agents"] });
    expect(daemon.data.config.repo).toEqual({});
    fireEvent.click(screen.getByRole("button", { name: "Save your settings" }));
    await waitFor(() => expect(daemon.data.config.user).toEqual({ plan_first: true }));
    expect(screen.getByRole("textbox", { name: "Checks" })).toHaveProperty("value", "npm test\nnpm run lint");
    expect(screen.getByTestId("settings-save")).toBeTruthy();
    stop();
  });

  it("never saves one project's rules into another while its config has not loaded", async () => {
    const OTHER = "/home/u/other";
    const { daemon, failing, stop } = setup({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }] });
    const checks = await screen.findByRole("textbox", { name: "Checks" });
    fireEvent.change(checks, { target: { value: "npm test\nmake all" } });
    expect(screen.getByTestId("settings-save")).toBeTruthy();
    // The other repo's config fails to load: no rules to edit and no save bar to write this repo's edit into it.
    failing.add("/config");
    fireEvent.change(screen.getByRole("combobox", { name: "Repo" }), { target: { value: OTHER } });
    await screen.findByText(/Could not load settings/);
    expect(screen.queryByTestId("settings-save")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Checks" })).toBeNull();
    expect(patches(daemon)).toHaveLength(0);
    // Once it loads, the draft is that repo's own, not the edit made for the first.
    failing.clear();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("textbox", { name: "Checks" })).toHaveProperty("value", "npm test");
    expect(screen.queryByTestId("settings-save")).toBeNull();
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

  it("permissions: groups are the default view with the preset and ask rate; a group baseline and a subcategory override save through the groups API and show their marker", async () => {
    const { daemon, stop } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: FAKE_PRESETS.autonomous.rules(), saved: [], prompts: { "d-push-default": 7 } } });
    const perms = await screen.findByTestId("permissions-settings");
    await waitFor(() => expect(within(perms).getByTestId("preset-name").textContent).toBe("Autonomous"));
    expect(within(perms).getByTestId("presets").querySelector('[data-preset="autonomous"]')!.textContent).toContain("RECOMMENDED");
    await waitFor(() => expect(within(perms).getByTestId("ask-rate").textContent).toMatch(/\b7\b.*\b26%/));
    // A group's baseline: one click, sent as the group's baseline only.
    fireEvent.click(within(within(perms).getByTestId("groups").querySelector<HTMLElement>('[data-group="shell"]')!).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(daemon.data.policy.groups?.shell.baseline).toBe("deny"));
    expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/permissions/groups")?.body).toEqual({ groups: { shell: { baseline: "deny" } } });
    // Open it: a subcategory overrides the group, marked while it does.
    fireEvent.click(within(perms).getByRole("button", { name: "Show Shell items" }));
    const installs = () => within(perms).getByTestId("group-shell").querySelector<HTMLElement>('[data-sub="install"]')!;
    expect(within(installs()).getByRole("button", { name: "Inherit" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(installs()).queryByTestId("override-marker")).toBeNull();
    fireEvent.click(within(installs()).getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(within(installs()).getByTestId("override-marker")).toBeTruthy());
    expect(within(installs()).getByRole("button", { name: "Ask" }).getAttribute("aria-pressed")).toBe("true");
    expect(daemon.data.policy.groups?.shell.subcategories.install).toBe("ask");
    expect(daemon.requests.filter((r) => r.path === "/permissions/groups").at(-1)?.body).toEqual({ groups: { shell: { subcategories: { install: "ask" } } } });
    stop();
  });

  it("permissions: unchecked commands opt-in is off, turns on only after an in-page confirmation, turns off in one click, and is locked when an org forces it", async () => {
    const { daemon, stop } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: FAKE_PRESETS.autonomous.rules(), saved: [], prompts: {} } });
    const row = await screen.findByTestId("dangerous");
    const toggle = () => within(row).getByRole("switch", { name: "Run unchecked commands without asking" });
    expect(toggle().getAttribute("aria-checked")).toBe("false");
    // On needs the confirmation; Cancel sends nothing.
    fireEvent.click(toggle());
    fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));
    expect(daemon.requests.some((r) => r.method === "PUT" && r.path === "/permissions/groups")).toBe(false);
    fireEvent.click(toggle());
    fireEvent.click(within(row).getByRole("button", { name: "Run unchecked commands" }));
    await waitFor(() => expect(toggle().getAttribute("aria-checked")).toBe("true"));
    expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/permissions/groups")?.body).toEqual({ groups: { shell: { subcategories: { unchecked: "allow" } } } });
    fireEvent.click(toggle());
    await waitFor(() => expect(toggle().getAttribute("aria-checked")).toBe("false"));
    expect(daemon.data.policy.groups?.shell.subcategories.unchecked).toBe("ask");
    stop();
  });

  it("permissions: unchecked commands opt-in is locked when org policy denies it", async () => {
    const { stop } = setup({ policy: { preset: "autonomous", unmatched: "allow", groups: { shell: { baseline: "ask", subcategories: { unchecked: "deny" } } }, rules: FAKE_PRESETS.autonomous.rules(), saved: [], prompts: {} } });
    const row = await screen.findByTestId("dangerous");
    expect(within(row).getByText("Set by your organization")).toBeTruthy();
    expect((within(row).getByRole("switch") as HTMLButtonElement).disabled).toBe(true);
    stop();
  });

  it("permissions: changes the preset, searches and filters the Advanced rules, removes a former default with undo, and sets unmatched actions", async () => {
    const gitCommit = { id: "u-git-commit", kind: "bash", match: "git commit", match_type: "prefix", decision: "allow", source: "user", created: 1, describe: "bash prefix 'git commit'", exclusions: [] };
    const { daemon, stop } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: [...FAKE_PRESETS.autonomous.rules(), gitCommit], saved: [], prompts: {} } });
    const perms = await screen.findByTestId("permissions-settings");
    await waitFor(() => expect(within(perms).getByTestId("preset-name").textContent).toBe("Autonomous"));
    fireEvent.click(within(perms).getByRole("button", { name: "Use Standard" }));
    await waitFor(() => expect(within(perms).getByTestId("preset-name").textContent).toBe("Standard"));
    expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/permissions/preset")?.body).toEqual({ preset: "standard" });
    // The flat list lives in Advanced: searchable, and filterable by decision.
    fireEvent.click(within(perms).getByRole("button", { name: "Open advanced view" }));
    expect(within(perms).getByRole("combobox", { name: "Unmatched actions" })).toHaveProperty("value", "ask");
    const total = within(perms).getByTestId("rules").querySelectorAll("[data-rule]").length;
    fireEvent.change(within(perms).getByRole("searchbox", { name: "Search rules" }), { target: { value: "ssh" } });
    const found = within(perms).getByTestId("rules").querySelectorAll("[data-rule]");
    expect(found.length).toBeGreaterThan(0);
    expect(found.length).toBeLessThan(total);
    fireEvent.change(within(perms).getByRole("combobox", { name: "Decision filter" }), { target: { value: "allow" } });
    expect(within(perms).getByTestId("rules").querySelectorAll("[data-rule]").length).toBe(0);
    fireEvent.click(within(perms).getByRole("button", { name: "Clear" }));
    expect(within(perms).getByTestId("rules").querySelectorAll("[data-rule]").length).toBe(total);
    // Git & push holds the push rules and the force-flag matcher, not the file rules.
    fireEvent.change(within(perms).getByRole("combobox", { name: "Capability" }), { target: { value: "git" } });
    const ids = [...within(perms).getByTestId("rules").querySelectorAll("[data-rule]")].map((e) => e.getAttribute("data-rule"));
    expect(ids).toContain("d-git-force");
    expect(ids).toContain("d-push-default");
    expect(ids).toContain("u-git-commit");
    expect(ids).not.toContain("d-ssh");
    fireEvent.change(within(perms).getByRole("combobox", { name: "Capability" }), { target: { value: "shell" } });
    const shellIds = [...within(perms).getByTestId("rules").querySelectorAll("[data-rule]")].map((e) => e.getAttribute("data-rule"));
    expect(shellIds).not.toContain("u-git-commit");
    expect(shellIds).not.toContain("d-git-force");
    fireEvent.click(within(perms).getByRole("button", { name: "Clear" }));
    // A preset's deny is removable like any rule, and Undo adds it back.
    const row = () => within(perms).getByTestId("rules").querySelector<HTMLElement>('[data-rule="d-ssh"]');
    fireEvent.click(within(row()!).getByRole("button", { name: "Remove Read files rule" }));
    await waitFor(() => expect(row()).toBeNull());
    fireEvent.click(within(perms).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(daemon.data.policy.rules.some((r) => r.match === "~/.ssh*" && r.decision === "deny")).toBe(true));
    expect(posts(daemon).find((r) => r.path === "/permissions/rules")?.body).toEqual({ kind: "read", match: "~/.ssh*", match_type: "glob", decision: "deny" });
    fireEvent.change(within(perms).getByRole("combobox", { name: "Unmatched actions" }), { target: { value: "deny" } });
    await waitFor(() => expect(daemon.data.policy.unmatched).toBe("deny"));
    stop();
  });

  it("permissions: a rule opens in a three-step editor, saves its limits, and a new rule that disagrees with one is checked and auto-resolved", async () => {
    const npm = { id: "u-npm", kind: "bash", match: "npm ", match_type: "prefix", decision: "allow", source: "user", created: 1, describe: "bash prefix 'npm '", exclusions: [] };
    const { daemon, stop } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: [...FAKE_PRESETS.autonomous.rules(), npm], saved: [], prompts: {} } }, undefined, undefined, undefined, "d-push-default");
    // Opened for a rule (the Inbox's "Open the deny rule"): its editor, over the Advanced list.
    const editor = await screen.findByTestId("rule-editor");
    expect(within(editor).getByRole("combobox", { name: "Action" })).toHaveProperty("value", "push");
    fireEvent.change(within(editor).getByRole("combobox", { name: "Remember approvals for" }), { target: { value: "time" } });
    fireEvent.change(within(editor).getByRole("spinbutton", { name: "Longest approval" }), { target: { value: "15" } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(daemon.data.policy.rules.find((r) => r.id === "d-push-default")?.limits).toEqual({ life: "time", minutes: 15, max_scope: "all" }));
    expect(screen.queryByTestId("rule-editor")).toBeNull();
    // A new deny on a command an allow rule covers: the save stops at the conflict; auto-resolve keeps the deny and
    // excepts the command from the allow.
    fireEvent.click(screen.getByRole("button", { name: "+ Add rule" }));
    const add = screen.getByTestId("rule-editor");
    fireEvent.change(within(add).getByRole("textbox", { name: "Target" }), { target: { value: "npm publish" } });
    fireEvent.change(within(add).getByRole("combobox", { name: "Decision" }), { target: { value: "deny" } });
    fireEvent.click(within(add).getByRole("button", { name: "Save rule" }));
    const review = within(add).getByTestId("conflict-review");
    expect(review.textContent).toContain("Until resolved: Deny wins on the shared requests.");
    expect(daemon.data.policy.rules.some((r) => r.match === "npm publish")).toBe(false);
    fireEvent.click(within(review).getByRole("button", { name: "Auto-resolve" }));
    await waitFor(() => expect(daemon.data.policy.rules.find((r) => r.id === "u-npm")?.exclusions).toEqual([{ kind: "bash", match: "npm publish", match_type: "exact", describe: "bash exact 'npm publish'" }]));
    expect(daemon.data.policy.rules.find((r) => r.match === "npm publish")?.decision).toBe("deny");
    stop();
  });

  it("permissions: a saved action moves along its scope ladder with a duration, is deleted with undo, and shows in Activity", async () => {
    const saved = { id: "s9", kind: "write", pattern: "src/auth/login.ts", decision: "allow", scope: "exact", life: "forever", created: 1_700_000_000, note: "This file only · src/auth/login.ts" };
    const { daemon, stop } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: FAKE_PRESETS.autonomous.rules(), saved: [saved], prompts: {} } });
    fireEvent.click(await screen.findByRole("button", { name: "Manage saved actions" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit Write files saved action" }));
    const editor = screen.getByTestId("saved-editor");
    fireEvent.change(within(editor).getByRole("combobox", { name: "Scope" }), { target: { value: "general" } });
    fireEvent.change(within(editor).getByRole("combobox", { name: "Lasts" }), { target: { value: "time" } });
    fireEvent.change(within(editor).getByRole("combobox", { name: "Saved action duration unit" }), { target: { value: "hours" } });
    fireEvent.change(within(editor).getByRole("spinbutton", { name: "Saved action duration" }), { target: { value: "2" } });
    expect(within(editor).getByTestId("saved-summary").textContent).toBe("Allow without asking · Write files · Everything under src/** · 2 hours");
    fireEvent.click(within(editor).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(daemon.data.policy.saved[0]).toMatchObject({ pattern: "src/**", scope: "general", life: "time", minutes: 120 }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Write files saved action" }));
    await waitFor(() => expect(daemon.data.policy.saved).toHaveLength(0));
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(daemon.data.policy.saved).toMatchObject([{ kind: "write", pattern: "src/**", life: "time", minutes: 120 }]));
    fireEvent.click(screen.getByRole("button", { name: "← Back to permissions" }));
    fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    expect(screen.getByTestId("activity").textContent).toContain("Saved action deleted");
    stop();
  });

  it("keeps the keys in Providers: a key stored write-only from a password field, no terminal anywhere", async () => {
    const { daemon, stop } = setup();
    const keys = await screen.findByTestId("providers");
    expect(within(keys).queryByRole("form", { name: "Add a key" })).toBeNull();
    const advanced = await within(keys).findByRole("button", { name: "API keys (optional, for advanced setups)" });
    fireEvent.click(advanced);
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
    fireEvent.click(advanced);
    expect(within(keys).queryByRole("form", { name: "Add a key" })).toBeNull();
    stop();
  });

  it("providers: one row per provider, found at its path with its version or not installed, signed in or not, with the settings note and no update controls", async () => {
    const { stop } = setup();
    const section = await screen.findByTestId("providers");
    expect(within(section).getByRole("heading", { name: "Providers" })).toBeTruthy();
    const claude = await within(section).findByTestId("provider-claude");
    const details = within(claude).getByRole("button", { name: "Connection details" });
    expect(details.getAttribute("aria-expanded")).toBe("false");
    expect(within(claude).queryByRole("button", { name: "Remove Claude Code from brindle" })).toBeNull();
    fireEvent.click(details);
    expect(details.getAttribute("aria-expanded")).toBe("true");
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
    fireEvent.click(within(codex).getByRole("button", { name: "Connection details" }));
    const opencode = screen.getByTestId("provider-opencode");
    expect(opencode.getAttribute("data-state")).toBe("not_installed");
    fireEvent.click(within(opencode).getByRole("button", { name: "Connection details" }));
    expect(opencode.textContent).toContain("Connect runs Anomaly's official installer (opencode.ai/install), as-is.");
    expect(within(opencode).getByRole("button", { name: "Install OpenCode" })).toBeTruthy();
    expect(within(opencode).queryByRole("button", { name: /Remove/ })).toBeNull();
    // Each found CLI can be taken out of brindle; one not installed can't.
    expect(within(claude).getByRole("button", { name: "Remove Claude Code from brindle" })).toBeTruthy();
    expect(within(codex).getByRole("button", { name: "Remove Codex from brindle" })).toBeTruthy();
    fireEvent.click(details);
    expect(details.getAttribute("aria-expanded")).toBe("false");
    expect(within(claude).queryByRole("button", { name: "Remove Claude Code from brindle" })).toBeNull();
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
    fireEvent.click(within(agy).getByRole("button", { name: "Connection details" }));
    expect(agy.textContent).toContain("Antigravity shares its settings with yours, in ~/.gemini.");
    expect(agy.textContent).not.toContain("own copy");
    stop();
  });

  it("providers: Connect runs the maker's official installer with its lines streaming into the row, then detects the CLI again; a failed install stays with Retry", async () => {
    const { daemon, stop } = setup();
    const opencode = await screen.findByTestId("provider-opencode");
    await waitFor(() => expect(opencode.getAttribute("data-state")).toBe("not_installed"));
    daemon.deferInstall = true;
    fireEvent.click(within(opencode).getByRole("button", { name: "Install OpenCode" }));
    await waitFor(() => expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/providers/opencode/install", query: {}, body: {} }));
    const card = await within(opencode).findByTestId("provider-install-opencode");
    await waitFor(() => expect(within(card).getByTestId("provider-install-opencode-lines").textContent).toContain("Downloading opencode-darwin-arm64.zip"));
    expect(card.getAttribute("data-state")).toBe("running");
    expect(within(opencode).queryByRole("button", { name: "Install OpenCode" })).toBeNull();
    const asked = daemon.requests.filter((r) => r.path === "/providers/installed").length;
    daemon.completeInstall();
    // Done: the rows are read again, and the row says where the installer put it.
    await waitFor(() => expect(screen.getByTestId("provider-opencode").getAttribute("data-state")).toBe("signed_out"));
    expect(daemon.requests.filter((r) => r.path === "/providers/installed").length).toBeGreaterThan(asked);
    fireEvent.click(within(opencode).getByRole("button", { name: "Connection details" }));
    expect(screen.getByTestId("provider-opencode").textContent).toContain("Found at /home/u/.opencode/bin/opencode · 1.2.8");
    expect(screen.queryByTestId("provider-install-opencode")).toBeNull();
    expect(within(screen.getByTestId("provider-opencode")).getByRole("button", { name: "Sign in to OpenCode" })).toBeTruthy();

    // A failed install: the installer's words on the row, Retry runs it again, Dismiss lets it go.
    daemon.deferInstall = false;
    daemon.installFails.antigravity = 1;
    const agy = screen.getByTestId("provider-antigravity");
    fireEvent.click(within(agy).getByRole("button", { name: "Install Antigravity" }));
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

  it("providers: Sign out on a signed-in row signs brindle's copy out and the row reads Not signed in", async () => {
    const { daemon, stop } = setup();
    const codex = await screen.findByTestId("provider-codex");
    await waitFor(() => expect(codex.getAttribute("data-state")).toBe("signed_out"));
    expect(within(codex).queryByRole("button", { name: "Sign out of Codex" })).toBeNull();
    fireEvent.click(within(codex).getByRole("button", { name: "Sign in to Codex" }));
    const flow = await within(codex).findByTestId("sign-in-flow");
    await waitFor(() => expect(flow.getAttribute("data-phase")).toBe("waiting"));
    daemon.signInProvider("codex");
    await waitFor(() => expect(screen.getByTestId("provider-codex").getAttribute("data-state")).toBe("signed_in"));
    fireEvent.click(within(flow).getByRole("button", { name: "Close" }));
    fireEvent.click(await within(screen.getByTestId("provider-codex")).findByRole("button", { name: "Sign out of Codex" }));
    await waitFor(() => expect(screen.getByTestId("provider-codex").getAttribute("data-state")).toBe("signed_out"));
    expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/providers/codex/logout", query: {}, body: {} });
    expect(within(screen.getByTestId("provider-codex")).getByTestId("provider-state").textContent).toBe("Not signed in");
    stop();
  });

  it("providers: Remove from brindle turns the CLI off without uninstalling it, and Connect turns the copy still found back on, no installer run", async () => {
    const { daemon, failing, stop } = setup();
    const codex = await screen.findByTestId("provider-codex");
    fireEvent.click(within(codex).getByRole("button", { name: "Connection details" }));
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
    fireEvent.click(within(opencode).getByRole("button", { name: "Install OpenCode" }));
    // The installer finished; the look after it failed. The stale row offers no second install.
    const detect = await screen.findByTestId("provider-install-opencode-detect");
    await waitFor(() => expect(detect.textContent).toContain("OpenCode is installed, but brindle couldn't look for it again"));
    expect(screen.getByTestId("provider-install-opencode").getAttribute("data-state")).toBe("done");
    expect(within(screen.getByTestId("provider-opencode")).queryByRole("button", { name: "Install OpenCode" })).toBeNull();
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
    fireEvent.click(within(opencode).getByRole("button", { name: "Connection details" }));
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
    const toggleDetails = () => fireEvent.click(within(screen.getByTestId("engine-health-summary")).getByRole("button", { name: /Details|Less/ }));
    let card = await screen.findByTestId("upgrade-card");
    expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/engine/upgrade", query: {}, body: {} });
    await waitFor(() => expect(card.getAttribute("data-state")).toBe("running"));
    expect(screen.queryByRole("button", { name: "Update brindle" })).toBeNull();
    // Details closed mid-update: the summary alone carries the progress and then the failure.
    toggleDetails();
    expect(screen.queryByTestId("upgrade-card")).toBeNull();
    expect(screen.getByTestId("update-status").textContent).toBe("Updating…");
    daemon.completeUpgrade();
    await waitFor(() => expect(screen.getByTestId("update-status").textContent).toContain("Update failed"));
    expect(screen.queryByTestId("upgrade-card")).toBeNull();
    expect(screen.queryByTestId("upgrade-log")).toBeNull();
    // Reopened, the card still holds the failure and the log.
    toggleDetails();
    card = screen.getByTestId("upgrade-card");
    expect(card.getAttribute("data-state")).toBe("failed");
    expect(card.textContent).toContain("The update didn't finish: uv tool upgrade brindle exited 1");
    expect(screen.getByTestId("upgrade-log").textContent).toContain("network unreachable");
    expect(screen.queryByTestId("terminal")).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("upgrade-card")).toBeNull();
    expect(screen.queryByTestId("update-status")).toBeNull();
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

  it("keeps Engine & health to one summary line with Details closed when healthy, and opens them when a check fails", async () => {
    const healthy = setup({ doctor: [{ name: "git", level: "ok", detail: "git 2.45" }] });
    const summary = await screen.findByTestId("engine-health-summary");
    await waitFor(() => expect(summary.textContent).toContain("Healthy"));
    expect(summary.textContent).toContain("brindle 0.5.0");
    expect(summary.textContent).not.toContain("CLI");
    expect(screen.queryByTestId("engine-details")).toBeNull();
    fireEvent.click(within(summary).getByRole("button", { name: /Details/ }));
    expect(screen.getByTestId("engine-details")).toBeTruthy();
    healthy.stop();
    cleanup();
    const bad = setup();
    await screen.findByTestId("engine-details");
    expect(screen.getByTestId("health-status").getAttribute("data-bad")).toBe("true");
    bad.stop();
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
    expect(screen.queryByTestId("staff-only")).toBeNull();
    expect(screen.queryByRole("button", { name: "Staff only" })).toBeNull();
    stop();
  });

  it("gives staff one Staff only section holding debug logging, app update and the release-branch rows, which turns debug logging on with PUT /debug", async () => {
    const updater = { install: vi.fn(async () => "0.1.0-rc.8"), restart: vi.fn(async () => {}) };
    const { daemon, stop } = setup({ debug: { staff: true, enabled: false } }, undefined, updater, releaseBuildUpdater, undefined, async () => ({ version: "0.2.4+release.42.0123456789ab", files: [] }));
    // The staff section holds the staff controls, last before About, and is in the nav.
    const staffOnly = await screen.findByTestId("staff-only");
    await within(staffOnly).findByTestId("return-to-stable-row");
    expect(within(staffOnly).getByTestId("app-update-row")).toBeTruthy();
    expect(within(staffOnly).getByTestId("release-build-row")).toBeTruthy();
    expect(staffOnly.nextElementSibling?.id).toBe("about");
    expect(screen.getByRole("button", { name: "Staff only" })).toBeTruthy();
    const section = within(staffOnly).getByTestId("debug-logging");
    expect(section.textContent).toContain("upload to brindle");
    const toggle = within(section).getByRole("switch", { name: "Debug logging" });
    expect(within(staffOnly).getAllByRole("switch")).toHaveLength(1);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    expect(daemon.requests.filter((r) => r.method === "PUT" && r.path === "/debug").map((r) => r.body)).toEqual([{ enabled: true }]);
    expect(daemon.data.debug.enabled).toBe(true);
    stop();
  });

  it("staff update: hides the app's Update from anyone the daemon doesn't call staff", async () => {
    const updater = { install: vi.fn(async () => "0.1.0-rc.8"), restart: vi.fn(async () => {}) };
    // Even on a release-branch build (which makes Return to stable eligible by version), a non-staff account sees nothing.
    const { daemon, stop } = setup({ debug: { staff: false, enabled: false } }, undefined, updater, releaseBuildUpdater, undefined, async () => ({ version: "0.2.4+release.42.0123456789ab", files: [] }));
    await screen.findByTestId("engine");
    await waitFor(() => expect(daemon.requests.some((r) => r.method === "GET" && r.path === "/debug")).toBe(true));
    await flush();
    expect(screen.queryByTestId("app-update")).toBeNull();
    expect(screen.queryByTestId("app-update-row")).toBeNull();
    expect(screen.queryByTestId("release-build-row")).toBeNull();
    expect(screen.queryByTestId("return-to-stable-row")).toBeNull();
    expect(screen.queryByText("Update the app")).toBeNull();
    expect(updater.install).not.toHaveBeenCalled();
    expect(ipc.calls).toEqual([]);
    stop();
  });

  it("staff update: installs the release branch's latest build through the shell, following its download, then restarts into it", async () => {
    const build = "0.1.0-rc.9+release.42.fedcba987654";
    const { stop } = setup({ debug: { staff: true, enabled: false } }, undefined, undefined, releaseBuildUpdater);
    const row = await within(await screen.findByTestId("app-update")).findByTestId("release-build-row");
    fireEvent.click(within(row).getByRole("button", { name: "Install" }));
    // The shell's command, given only the channel it reports on: no feed, no version.
    const [{ cmd, args }] = ipc.calls;
    expect(cmd).toBe("app_release_build_install");
    expect(Object.keys(args)).toEqual(["onProgress"]);
    const progress = (args.onProgress as InstanceType<typeof ipc.Channel>).onmessage!;
    progress({ kind: "found", version: build });
    progress({ kind: "downloading", downloaded: 3_000_000, total: 12_000_000 });
    const bar = await within(row).findByRole("progressbar");
    expect([bar.getAttribute("value"), bar.getAttribute("max")]).toEqual(["3000000", "12000000"]);
    progress({ kind: "installing" });
    await waitFor(() => expect(within(row).queryByRole("progressbar")).toBeNull());
    ipc.answers[0](build);
    const restart = await within(row).findByRole("button", { name: "Restart" });
    expect(row.textContent).toContain(build);
    fireEvent.click(restart);
    await waitFor(() => expect(ipc.calls.map((c) => c.cmd)).toEqual(["app_release_build_install", "app_restart"]));
    stop();
  });

  it("staff update: Return to stable shows only on a release-branch build, and installs the stable feed after a confirmation", async () => {
    const about = (version: string) => async () => ({ version, files: [] });
    const plain = setup({ debug: { staff: true, enabled: false } }, undefined, undefined, releaseBuildUpdater, undefined, about("0.2.4"));
    await within(await screen.findByTestId("app-update")).findByTestId("release-build-row");
    await flush();
    expect(screen.queryByTestId("return-to-stable-row")).toBeNull();
    plain.stop();
    cleanup();

    const { stop } = setup({ debug: { staff: true, enabled: false } }, undefined, undefined, releaseBuildUpdater, undefined, about("0.2.4+release.42.0123456789ab"));
    const row = await screen.findByTestId("return-to-stable-row");
    fireEvent.click(within(row).getByRole("button", { name: "Return to stable" }));
    // The shell is asked which version first; nothing installs until it is confirmed, and Cancel installs nothing.
    expect(ipc.calls.map((c) => c.cmd)).toEqual(["app_stable_check"]);
    // Nothing to return to at first (the stable is older); a later check finds one, and that version is what is shown to confirm.
    ipc.answers[0](null);
    fireEvent.click(await within(row).findByRole("button", { name: "Return to stable" }));
    ipc.answers[1]("0.2.4");
    await within(row).findByRole("button", { name: "Confirm" });
    expect(row.textContent).toContain("0.2.4");
    fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));
    expect(ipc.calls.map((c) => c.cmd)).toEqual(["app_stable_check", "app_stable_check"]);
    fireEvent.click(within(row).getByRole("button", { name: "Return to stable" }));
    ipc.answers[2]("0.2.4");
    fireEvent.click(await within(row).findByRole("button", { name: "Confirm" }));
    // The install is pinned to the version the person confirmed.
    expect(ipc.calls.map((c) => c.cmd)).toEqual(["app_stable_check", "app_stable_check", "app_stable_check", "app_return_to_stable"]);
    expect(ipc.calls[3].args.version).toBe("0.2.4");
    ipc.answers[3]("0.2.4");
    fireEvent.click(await within(row).findByRole("button", { name: "Restart" }));
    await waitFor(() => expect(ipc.calls.at(-1)?.cmd).toBe("app_restart"));
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
