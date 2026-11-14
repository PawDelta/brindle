import { inTestContext, trackedSync as startSync } from "../../test/harness";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { SettingsRail } from "../../components/SettingsRail";
import type { SettingsTab } from "../../routes";
import { createAppStore } from "../../store/store";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FAKE_PRESETS, FakeDaemon, TOKEN, flush, freshProviders, type DaemonData } from "../../test/fakeDaemon";
import { PtyViewsProvider } from "../../api/ptyView";
import { REPO, root } from "../../test/fixtures";
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

/* `props`: the screen's own (the tab to open on, the shell's hooks); the client is the fake daemon's. */
function setup(data: Partial<DaemonData> = {}, props: Omit<SettingsScreenProps, "client"> = {}) {
  function Settings() {
    const [tab, setTab] = useState<SettingsTab>(props.tab ?? props.section ?? (props.repo ? "project" : "general"));
    return <><SettingsRail tab={tab} onTab={setTab} onBack={() => undefined} /><SettingsScreen client={client} {...props} tab={tab} onTab={setTab} /></>;
  }
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
    inTestContext(<>{/* A sign-in's CLI read through the fake daemon's pty views, as the shell's show it. */}
            <PtyViewsProvider value={daemon.ptyViews()}>
              <Settings />
            </PtyViewsProvider></>, { client, store }, { daemon: true, sessions: true, providers: true }),
  );
  return { daemon, failing, stop: () => sync.stop() };
}

const patches = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "PATCH" && r.path === "/config");
const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST");
const doctorCalls = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.path === "/doctor");

describe("settings screen", () => {
  it("is one place in tabs: each tab shows its own sections and only those", async () => {
    setup();
    // Each tab and what it alone shows: the account's plan and usage live here too, as does the repo's own Project.
    const shows: [string, string][] = [
      ["Account", "features"],
      ["Usage", "usage"],
      ["General", "your-settings"],
      ["Workers & providers", "providers"],
      ["Permissions", "permissions-settings"],
      ["Doc format", "doc-format"],
      ["Storage", "storage"],
      ["Project", "project-settings"],
      ["About", "engine"],
    ];
    // A tab's section is shown when it is there and no panel around it is hidden (a visited tab's stays, hidden).
    const shown = (testId: string) => {
      const el = screen.queryByTestId(testId);
      return !!el && !el.closest("[hidden]");
    };
    for (const [tab, testId] of shows) {
      fireEvent.click(screen.getByRole("tab", { name: tab }));
      expect(screen.getByRole("tab", { name: tab }).getAttribute("aria-selected")).toBe("true");
      await waitFor(() => expect(shown(testId)).toBe(true));
      for (const [, other] of shows.filter(([t]) => t !== tab)) expect(shown(other)).toBe(false);
    }
    // Project's panel stays mounted, hidden, with its own Plan first: General's label still works its own switch.
    fireEvent.click(screen.getByRole("tab", { name: "General" }));
    const mine = screen.getByTestId("your-settings");
    const theirs = within(screen.getByTestId("project-settings")).getByRole("switch", { name: "Plan first", hidden: true });
    const before = theirs.getAttribute("aria-checked");
    fireEvent.click(within(mine).getByText("Plan first", { selector: "label" }));
    expect(within(mine).getByRole("switch", { name: "Plan first" }).getAttribute("aria-checked")).toBe("true");
    expect(theirs.getAttribute("aria-checked")).toBe(before);
    expect(within(screen.getByTestId("user-save")).getByText(/1 change/)).toBeTruthy();
  });

  it("github: Connect uses the gh sign-in, the row reads connected as @login, and Disconnect puts it back", async () => {
    const { daemon } = setup({}, { tab: "providers" });
    const row = await screen.findByTestId("github-row");
    fireEvent.click(await within(row).findByRole("button", { name: "Connect" }));
    fireEvent.click(await screen.findByRole("button", { name: "Use my GitHub CLI sign-in" }));
    await waitFor(() => expect(row.getAttribute("data-connected")).toBe("true"));
    expect(row.textContent).toContain("@sam");
    expect(daemon.githubRoutes).toEqual(["gh"]);
    fireEvent.click(within(row).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(row.getAttribute("data-connected")).toBe("false"));
    expect(daemon.requests.some((r) => r.method === "DELETE" && r.path === "/github/connection")).toBe(true);
  });

  it("edits your settings and saves them to the user scope, and leaves the repo's checks and rules to Project settings", async () => {
    const { daemon } = setup();
    const mine = await screen.findByTestId("your-settings");
    expect(daemon.requests.find((r) => r.path === "/config")?.query).toEqual({ repo: REPO });
    // Your settings lead the General tab; the handful people change most show, the rest behind More. Checks is not among them.
    expect(screen.getAllByRole("region").map((s) => s.id).filter(Boolean)[0]).toBe("your-settings");
    // The repo's own checks and rules are on the Project tab, not here; no Autopilot row anywhere.
    expect(screen.queryByTestId("repo-settings")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Checks" })).toBeNull();
    expect(screen.getByRole("tab", { name: "General" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByRole("switch", { name: "Autopilot" })).toBeNull();
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
    const coordinator = screen.getByRole("combobox", { name: "Coordinator" });
    for (const [option, stored] of [["false", false], ["true", true], ["", null]] as const) {
      fireEvent.change(coordinator, { target: { value: option } });
      fireEvent.click(screen.getByRole("button", { name: "Save your settings" }));
      await waitFor(() => expect(daemon.data.config.user.coordinator).toBe(stored));
      await waitFor(() => expect(screen.queryByRole("button", { name: "Save your settings" })).toBeNull());
    }
    fireEvent.click(screen.getByRole("button", { name: /Other settings you have/ }));
    expect(screen.getByTestId("user-rest").textContent).toContain("supervisor");
  });

  it("permissions: groups are the default view with the preset and ask rate; a group baseline and a subcategory override save through the groups API and show their marker", async () => {
    const { daemon } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: FAKE_PRESETS.autonomous.rules(), saved: [], prompts: { "d-push-default": 7 } } }, { tab: "permissions" });
    const perms = await screen.findByTestId("permissions-settings");
    await waitFor(() => expect(within(perms).getByTestId("preset-name").textContent).toBe("Autonomous"));
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
  });

  it("permissions: with the rules off nothing claims a preset is in force, one button turns them on, and a repo's or the person's own off isn't overridden by a preset", async () => {
    const policy = { preset: "autonomous", unmatched: "allow", rules: FAKE_PRESETS.autonomous.rules(), saved: [], prompts: {}, policy_on: false };
    const { daemon, stop } = setup({ policy: { ...policy, policy_source: "default" } }, { tab: "permissions" });
    const banner = await screen.findByTestId("policy-off");
    const perms = screen.getByTestId("permissions-settings");
    expect(within(perms).getByTestId("preset-name").textContent).toBe("Not in force");
    expect(within(perms).queryByRole("button", { name: "Current" })).toBeNull();
    fireEvent.click(within(banner).getByRole("button", { name: "Turn on with Autonomous" }));
    await waitFor(() => expect(within(perms).getByTestId("preset-name").textContent).toBe("Autonomous"));
    expect(within(perms).queryByTestId("policy-off")).toBeNull();
    expect(daemon.requests.find((r) => r.method === "PATCH" && r.path === "/config")?.body).toEqual({ scope: "user", values: { permission_policy: "on" } });
    expect(within(perms).getByRole("button", { name: "Current" })).toBeTruthy();
    stop();
    cleanup();

    // A repo's off: nothing here can change it, so no button, and a preset says it isn't in force.
    const repoOff = setup({ policy: { ...policy, policy_source: "repo" } }, { tab: "permissions" });
    const held = await screen.findByTestId("policy-off");
    expect(within(held).queryByRole("button")).toBeNull();
    fireEvent.click(within(screen.getByTestId("permissions-settings")).getByRole("button", { name: "Use Standard" }));
    await waitFor(() => expect(screen.getByTestId("permissions-feedback").textContent).toContain("isn't in force"));
    expect(screen.getByTestId("preset-name").textContent).toBe("Not in force");
    repoOff.stop();
  });

  it("permissions: unchecked commands opt-in is off, turns on only after an in-page confirmation, turns off in one click, and is locked when an org forces it", async () => {
    const { daemon } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: FAKE_PRESETS.autonomous.rules(), saved: [], prompts: {} } }, { tab: "permissions" });
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
  });

  it("permissions: unchecked commands opt-in is locked when org policy denies it", async () => {
    setup({ policy: { preset: "autonomous", unmatched: "allow", groups: { shell: { baseline: "ask", subcategories: { unchecked: "deny" } } }, rules: FAKE_PRESETS.autonomous.rules(), saved: [], prompts: {} } }, { tab: "permissions" });
    const row = await screen.findByTestId("dangerous");
    expect(within(row).getByText("Set by your organization")).toBeTruthy();
    expect((within(row).getByRole("switch") as HTMLButtonElement).disabled).toBe(true);
  });

  it("permissions: changes the preset, searches and filters the Advanced rules, removes a former default with undo, and sets unmatched actions", async () => {
    const gitCommit = { id: "u-git-commit", kind: "bash", match: "git commit", match_type: "prefix", decision: "allow", source: "user", created: 1, describe: "bash prefix 'git commit'", exclusions: [] };
    const { daemon } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: [...FAKE_PRESETS.autonomous.rules(), gitCommit], saved: [], prompts: {} } }, { tab: "permissions" });
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
  });

  it("permissions: a rule's limits save, and a deny that conflicts with an allow is auto-resolved without broadening the allow", async () => {
    const npm = { id: "u-npm", kind: "bash", match: "npm ", match_type: "prefix", decision: "allow", source: "user", created: 1, describe: "bash prefix 'npm '", exclusions: [] };
    const { daemon } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: [...FAKE_PRESETS.autonomous.rules(), npm], saved: [], prompts: {} } }, { tab: "permissions", rule: "d-push-default" });
    const editor = await screen.findByTestId("rule-editor");
    fireEvent.change(within(editor).getByRole("combobox", { name: "Remember approvals for" }), { target: { value: "time" } });
    fireEvent.change(within(editor).getByRole("spinbutton", { name: "Longest approval" }), { target: { value: "15" } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(daemon.data.policy.rules.find((r) => r.id === "d-push-default")?.limits).toEqual({ life: "time", minutes: 15, max_scope: "all" }));
    fireEvent.click(screen.getByRole("button", { name: "+ Add rule" }));
    const add = screen.getByTestId("rule-editor");
    fireEvent.change(within(add).getByRole("textbox", { name: "Target" }), { target: { value: "npm publish" } });
    fireEvent.change(within(add).getByRole("combobox", { name: "Decision" }), { target: { value: "deny" } });
    fireEvent.click(within(add).getByRole("button", { name: "Save rule" }));
    const review = within(add).getByTestId("conflict-review");
    // Stopped at the conflict: nothing saved until it is resolved.
    expect(daemon.data.policy.rules.some((r) => r.match === "npm publish")).toBe(false);
    fireEvent.click(within(review).getByRole("button", { name: "Auto-resolve" }));
    await waitFor(() => expect(daemon.data.policy.rules.find((r) => r.id === "u-npm")?.exclusions).toEqual([{ kind: "bash", match: "npm publish", match_type: "exact", describe: "bash exact 'npm publish'" }]));
    expect(daemon.data.policy.rules.find((r) => r.match === "npm publish")?.decision).toBe("deny");
  });

  it("permissions: a saved action's scope and lifetime are edited, and deleting it can be undone", async () => {
    const saved = { id: "s9", kind: "write", pattern: "src/auth/login.ts", decision: "allow", scope: "exact", life: "forever", created: 1_700_000_000, note: "This file only · src/auth/login.ts" };
    const { daemon } = setup({ policy: { preset: "autonomous", unmatched: "allow", rules: FAKE_PRESETS.autonomous.rules(), saved: [saved], prompts: {} } }, { tab: "permissions" });
    fireEvent.click(await screen.findByRole("button", { name: "Manage saved actions" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit Write files saved action" }));
    const editor = screen.getByTestId("saved-editor");
    fireEvent.change(within(editor).getByRole("combobox", { name: "Scope" }), { target: { value: "general" } });
    fireEvent.change(within(editor).getByRole("combobox", { name: "Lasts" }), { target: { value: "time" } });
    fireEvent.change(within(editor).getByRole("combobox", { name: "Saved action duration unit" }), { target: { value: "hours" } });
    fireEvent.change(within(editor).getByRole("spinbutton", { name: "Saved action duration" }), { target: { value: "2" } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(daemon.data.policy.saved[0]).toMatchObject({ pattern: "src/**", scope: "general", life: "time", minutes: 120 }));
    // The daemon changes before the policy read commits the updated row and its Delete handler.
    await waitFor(() => expect(within(screen.getByTestId("saved")).getByText(/src\/\*\*/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Delete Write files saved action" }));
    await waitFor(() => expect(daemon.data.policy.saved).toHaveLength(0));
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(daemon.data.policy.saved).toMatchObject([{ kind: "write", pattern: "src/**", life: "time", minutes: 120 }]));
  });

  it("providers: each row says how it connects, an API key with its health (Test, Replace, Remove, rejected naming only the agents its credentials stopped), a cloud with its login (Renew login), Change method saving Sign in over auto, and an Import for a key in the shell", async () => {
    const now = Date.now() / 1000;
    const failed = (id: string) => ({ ...root.agents![0], id, provider: "claude", status: "failed" });
    const { daemon } = setup({
      providers: freshProviders({ claude: { path: "/opt/homebrew/bin/claude", version: "2.1.0" }, opencode: { path: "/home/u/.opencode/bin/opencode", version: "1.2.8" } }),
      connections: {
        // Two Claude agents failed, one of them on its credentials: only that one is the key's.
        claude: { method: "api_key", stopped_agents: [{ id: "a1", name: "fix-login", profile: "developer" }] },
        opencode: { method: "cloud", cloud: "vertex", settings: { GOOGLE_CLOUD_PROJECT: "my-project", VERTEX_LOCATION: "us-central1" }, login: { valid: true, expired: false, expires_at: now + 40 * 60 + 30 } },
      },
      config: { user: {}, repo: {}, local: {}, keys: [{ name: "ANTHROPIC_API_KEY", set: true, last4: "WXYZ", status: "verified", checked_at: now - 2 * 3600 - 60 }] },
      workspaces: [{ ...root, agents: [failed("a1"), failed("a2")] }],
    }, { tab: "providers" });
    daemon.shellKeys.add("GEMINI_API_KEY");
    const health = (p: string) => within(screen.getByTestId(`provider-${p}`)).getByTestId("provider-health");
    const claude = await screen.findByTestId("provider-claude");
    expect(screen.queryByRole("button", { name: "API keys (optional, for advanced setups)" })).toBeNull();

    const puts = () => daemon.requests.filter((r) => r.method === "PUT" && r.path === "/providers/claude/connection").map((r) => (r.body as { method: string }).method);

    // API key: its last four, verified.
    await waitFor(() => expect(within(claude).getByTestId("provider-method").textContent).toContain("WXYZ"));
    await waitFor(() => expect(health("claude").getAttribute("data-tone")).toBe("pass"));
    // Test: Anthropic refuses it now: the fail tone, naming the one agent its credentials stopped.
    daemon.rejectedKeys.add("ANTHROPIC_API_KEY");
    fireEvent.click(within(claude).getByRole("button", { name: "Test" }));
    await waitFor(() => expect(health("claude").getAttribute("data-tone")).toBe("fail"));
    expect(health("claude").getAttribute("data-stopped")).toBe("1");
    expect(health("claude").textContent).toContain("fix-login");
    // Replace: one editor at a time, so opening it closes Change method.
    daemon.rejectedKeys.clear();
    fireEvent.click(within(claude).getByRole("button", { name: "Change method ▾" }));
    fireEvent.click(within(claude).getByRole("button", { name: "Replace" }));
    expect(within(claude).queryByTestId("connect-method")).toBeNull();
    // A fresh key pasted in place; while its check is pending Change method can't open (a Sign in there would be
    // overwritten by this save's method write).
    const key = within(claude).getByLabelText("Paste your key") as HTMLInputElement;
    expect(key.type).toBe("password");
    fireEvent.change(key, { target: { value: "sk-ant-api03-new-k7Qa" } });
    daemon.holdKeys = true;
    fireEvent.click(within(claude).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(claude).getByRole("button", { name: "Change method ▾" })).toHaveProperty("disabled", true));
    fireEvent.click(within(claude).getByRole("button", { name: "Change method ▾" }));
    expect(within(claude).queryByTestId("connect-method")).toBeNull();
    daemon.holdKeys = false;
    daemon.releaseKeys();
    await waitFor(() => expect(within(screen.getByTestId("provider-claude")).getByTestId("provider-method").textContent).toContain("k7Qa"));
    expect(health("claude").getAttribute("data-tone")).toBe("pass");
    expect(screen.queryByDisplayValue("sk-ant-api03-new-k7Qa")).toBeNull();
    // Remove: the key and the method go; the row is on auto.
    fireEvent.click(within(claude).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.getByTestId("provider-claude").getAttribute("data-method")).toBe("auto"));
    expect(daemon.requests.filter((r) => r.method === "DELETE").map((r) => r.path)).toEqual(["/keys/ANTHROPIC_API_KEY"]);
    expect(puts()).toEqual(["api_key", "auto"]);
    // Change method on auto: nothing is claimed; Sign in is saved at once.
    fireEvent.click(within(claude).getByRole("button", { name: "Change method ▾" }));
    expect(within(claude).getByTestId("connect-method").getAttribute("data-method")).toBe("");
    fireEvent.click(within(claude).getByRole("radio", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByTestId("provider-claude").getAttribute("data-method")).toBe("sign_in"));
    expect(puts()).toEqual(["api_key", "auto", "sign_in"]);
    // And back to an API key, chosen and pasted in the row; while its check is pending, nothing else can be chosen or closed.
    fireEvent.click(within(claude).getByRole("button", { name: "Change method ▾" }));
    fireEvent.click(within(claude).getByRole("radio", { name: "API key" }));
    fireEvent.change(within(claude).getByLabelText("Paste your key"), { target: { value: "sk-ant-api03-again-Zz99" } });
    daemon.holdKeys = true;
    fireEvent.click(within(claude).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(claude).getByRole("radio", { name: "Sign in" })).toHaveProperty("disabled", true));
    expect(within(claude).getByRole("button", { name: "Change method ▴" })).toHaveProperty("disabled", true);
    daemon.holdKeys = false;
    daemon.releaseKeys();
    await waitFor(() => expect(screen.getByTestId("provider-claude").getAttribute("data-method")).toBe("api_key"));
    expect(within(screen.getByTestId("provider-claude")).getByTestId("provider-method").textContent).toContain("Zz99");
    expect(within(claude).queryByTestId("connect-method")).toBeNull();

    // Your cloud: the route and its settings, the login near its end, and Renew login runs it again.
    const opencode = screen.getByTestId("provider-opencode");
    expect(within(opencode).getByTestId("provider-method").textContent).toContain("my-project");
    expect(health("opencode").getAttribute("data-tone")).toBe("sap");
    fireEvent.click(within(opencode).getByRole("button", { name: "Renew login" }));
    await waitFor(() => expect(daemon.cloudSignIns).toEqual(["opencode"]));
    daemon.completeCloudSignIn();
    await waitFor(() => expect(health("opencode").getAttribute("data-tone")).toBe("pass"));

    // A key the shell has and brindle doesn't: offered, and imported on request.
    fireEvent.click(within(await screen.findByTestId("key-import-GEMINI_API_KEY")).getByRole("button", { name: "Import" }));
    await waitFor(() => expect(screen.queryByTestId("key-import-GEMINI_API_KEY")).toBeNull());
    expect(posts(daemon).map((r) => r.path)).toContain("/keys/GEMINI_API_KEY/import");
    expect(screen.queryByTestId("terminal")).toBeNull();
  });

  it("providers: a key on an auto row stays manageable, an imported one too, OpenCode's controls follow its model provider's own key, and keys no provider reads keep a row", async () => {
    const stored = (name: string, last4: string) => ({ name, set: true, last4, status: "verified" as const, checked_at: Date.now() / 1000 });
    const { daemon } = setup({
      providers: freshProviders({ codex: { path: "/opt/homebrew/bin/codex", version: "0.48.0" }, opencode: { path: "/home/u/.opencode/bin/opencode", version: "1.2.8" }, antigravity: { path: "/home/u/.local/bin/agy", version: "1.3.0" } }),
      connections: { opencode: { method: "api_key" } },
      config: { user: {}, repo: {}, local: {}, keys: [stored("OPENAI_API_KEY", "1111"), stored("OPENROUTER_API_KEY", "2222"), stored("ANTHROPIC_API_KEY", "3333"), stored("REVIEW_KEY", "4444")] },
    }, { tab: "providers" });
    daemon.shellKeys.add("GEMINI_API_KEY");
    const deletes = () => daemon.requests.filter((r) => r.method === "DELETE").map((r) => r.path);

    // Codex left on auto with a key stored: the row says auto, and the key keeps Test and Remove; removing it leaves the method alone.
    const codex = await screen.findByTestId("provider-codex");
    await waitFor(() => expect(within(codex).getByTestId("provider-auto")).toBeTruthy());
    await waitFor(() => expect(within(codex).getByTestId("provider-state").textContent).toContain("1111"));
    fireEvent.click(within(codex).getByRole("button", { name: "Test" }));
    await waitFor(() => expect(posts(daemon).map((r) => r.path)).toContain("/keys/OPENAI_API_KEY/test"));
    fireEvent.click(within(codex).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(deletes()).toEqual(["/keys/OPENAI_API_KEY"]));
    expect(daemon.requests.some((r) => r.method === "PUT" && r.path === "/providers/codex/connection")).toBe(false);

    // OpenCode runs two model providers' keys: an Anthropic key saved through Change method moves the row's
    // controls onto it, so Test and Remove reach that key, never OpenRouter's.
    const opencode = screen.getByTestId("provider-opencode");
    await waitFor(() => expect(within(opencode).getByTestId("provider-method").textContent).toContain("2222"));
    fireEvent.click(within(opencode).getByRole("button", { name: "Change method ▾" }));
    fireEvent.change(within(opencode).getByLabelText("Model provider"), { target: { value: "anthropic" } });
    fireEvent.change(within(opencode).getByLabelText("Paste your key"), { target: { value: "sk-ant-api03-new-5555" } });
    fireEvent.click(within(opencode).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(opencode).getByTestId("provider-method").textContent).toContain("5555"));
    fireEvent.click(within(opencode).getByRole("button", { name: "Test" }));
    await waitFor(() => expect(posts(daemon).map((r) => r.path)).toContain("/keys/ANTHROPIC_API_KEY/test"));
    fireEvent.click(within(opencode).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(deletes()).toEqual(["/keys/OPENAI_API_KEY", "/keys/ANTHROPIC_API_KEY"]));

    // A key imported from the shell lands on its provider's auto row, with its controls.
    fireEvent.click(within(await screen.findByTestId("key-import-GEMINI_API_KEY")).getByRole("button", { name: "Import" }));
    const agy = screen.getByTestId("provider-antigravity");
    await waitFor(() => expect(within(agy).getByRole("button", { name: "Test" })).toBeTruthy());

    // A profile's own key, read by no provider: listed, and removable.
    const other = within(screen.getByTestId("other-keys")).getByTestId("other-key-REVIEW_KEY");
    expect(other.getAttribute("data-set")).toBe("true");
    fireEvent.click(within(other).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(deletes()).toContain("/keys/REVIEW_KEY"));
  });

  it("providers: one row per provider, found at its path with its version or not installed, signed in or not, with the settings note and no update controls", async () => {
    setup({}, { tab: "providers" });
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
    expect(within(claude).queryByRole("button", { name: /Sign in/ })).toBeNull();
    const codex = screen.getByTestId("provider-codex");
    expect(codex.getAttribute("data-state")).toBe("signed_out");
    expect(within(codex).getByRole("button", { name: "Sign in to Codex" })).toBeTruthy();
    fireEvent.click(within(codex).getByRole("button", { name: "Connection details" }));
    const opencode = screen.getByTestId("provider-opencode");
    expect(opencode.getAttribute("data-state")).toBe("not_installed");
    fireEvent.click(within(opencode).getByRole("button", { name: "Connection details" }));
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
    // No typed commands: no command-line text for a person to run.
  });

  it("providers: Connect runs the maker's official installer with its lines streaming into the row, then detects the CLI again; a failed install stays with Retry", async () => {
    const { daemon } = setup({}, { tab: "providers" });
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
    expect(screen.queryByTestId("provider-install-opencode")).toBeNull();
    expect(within(screen.getByTestId("provider-opencode")).getByRole("button", { name: "Sign in to OpenCode" })).toBeTruthy();

    // A failed install: the installer's words on the row, Retry runs it again, Dismiss lets it go.
    daemon.deferInstall = false;
    daemon.installFails.antigravity = 1;
    const agy = screen.getByTestId("provider-antigravity");
    fireEvent.click(within(agy).getByRole("button", { name: "Install Antigravity" }));
    const failed = await within(agy).findByTestId("provider-install-antigravity-failed");
    expect(agy.getAttribute("data-state")).toBe("not_installed");
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("provider-antigravity").getAttribute("data-state")).toBe("signed_out"));
    expect(daemon.installs).toEqual(["opencode", "antigravity", "antigravity"]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("providers: Sign in runs the CLI's own login in the browser-first card, and the row reads signed in after", async () => {
    const { daemon } = setup({}, { tab: "providers" });
    const codex = await screen.findByTestId("provider-codex");
    await waitFor(() => expect(codex.getAttribute("data-state")).toBe("signed_out"));
    // The engine reads it not ready, wanting a sign-in.
    expect((await within(codex).findByTestId("provider-ready")).getAttribute("data-action")).toBe("sign_in");
    fireEvent.click(within(codex).getByRole("button", { name: "Sign in to Codex" }));
    const flow = await within(codex).findByTestId("sign-in-flow");
    expect(posts(daemon).at(-1)).toEqual({ method: "POST", path: "/providers/codex/login", query: {}, body: {} });
    expect(daemon.actions.at(-1)?.command).toEqual(["codex", "login"]);
    await waitFor(() => expect(flow.getAttribute("data-phase")).toBe("waiting"));
    expect(within(flow).queryByRole("button", { name: "Show details" })).toBeNull();
    expect(screen.queryByTestId("terminal")).toBeNull();
    // The browser's sign-in finishes; the CLI exits and the rows read again say so.
    daemon.signInProvider("codex");
    await waitFor(() => expect(flow.getAttribute("data-phase")).toBe("done"));
    expect(screen.getByTestId("provider-codex").getAttribute("data-state")).toBe("signed_in");
    // Its readiness is read again with the rows, without reopening the page: nothing left to fix.
    await waitFor(() => expect(within(screen.getByTestId("provider-codex")).queryByTestId("provider-ready")).toBeNull());
  });

  it("lists the engine's health with a fix where the app has one, updates the engine as a job followed on the bus (done, or failed with uv's tail), and keeps the command-line tool's button off without the shell's installer", async () => {
    const { daemon } = setup({}, { tab: "about" });
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

    // The command-line tool comes with the bundled engine: without its installer the button is off, and says so.
    const cli = screen.getByTestId("install-cli");
    expect(within(cli).getByRole("button", { name: "Install command-line tool" })).toHaveProperty("disabled", true);
  });

  it("shows a doctor that didn't answer as a failure, never as a clean bill or an up-to-date engine", async () => {
    const { failing } = setup({ doctor: [] }, { tab: "about" });
    failing.add("/doctor");
    await screen.findByTestId("doctor-error");
    expect(screen.queryByRole("button", { name: "Update brindle" })).toBeNull();
    failing.clear();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByTestId("doctor-error")).toBeNull());
    // The provider rows come from the engine's detection, not the doctor: they stay.
    fireEvent.click(screen.getByRole("tab", { name: "Workers & providers" }));
    await waitFor(() => expect(screen.getByTestId("provider-claude").getAttribute("data-state")).toBe("signed_in"));
  });

  it("saves a doc format through the daemon and shows what it kept", async () => {
    const { daemon } = setup();
    fireEvent.click(await screen.findByRole("tab", { name: "Doc format" }));
    fireEvent.click(await screen.findByRole("button", { name: "+ New" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Rollout is required" }));
    fireEvent.change(screen.getByLabelText("Reference"), { target: { value: "DES-{number}" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved.");
    const put = daemon.requests.find((r) => r.method === "PUT" && r.path === "/doc-format");
    expect(put?.body).toMatchObject({ level: "org", format: { naming: { reference: "DES-{number}" }, blocks: ["sign_off"] } });
    // The daemon kept what was sent, and the screen now shows the daemon's answer: nothing left to save.
    expect(daemon.docFormat.templates?.[0].sections.find((s) => s.title === "Rollout")?.required).toBe(false);
    expect(screen.getByTestId("next-reference").textContent).toContain("DES-0001");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("checkbox", { name: "Rollout is required" }) as HTMLInputElement).checked).toBe(false);
  });

  it("saves the OpenCode permission mode picked, beside Claude's", async () => {
    const { daemon } = setup();
    fireEvent.click(await screen.findByRole("tab", { name: "Permissions" }));
    const group = await screen.findByRole("radiogroup", { name: "OpenCode permission mode" });
    await waitFor(() => expect(within(group).getByRole("radio", { name: "Ask" }).getAttribute("aria-checked")).toBe("true"));
    fireEvent.click(within(group).getByRole("radio", { name: "Autonomous" }));
    await waitFor(() => expect(within(group).getByRole("radio", { name: "Autonomous" }).getAttribute("aria-checked")).toBe("true"));
    expect(daemon.requests.find((r) => r.method === "PATCH" && r.path === "/user-settings")?.body).toEqual({ opencode_permission_mode: "autonomous" });
    expect(within(screen.getByRole("radiogroup", { name: "Claude permission mode" })).getByRole("radio", { name: "Normal" }).getAttribute("aria-checked")).toBe("true");
  });

  it("hides debug logging from anyone the daemon doesn't call staff", async () => {
    const { daemon } = setup({ debug: { staff: false, enabled: true } }, { tab: "about" });
    await screen.findByTestId("engine");
    await waitFor(() => expect(daemon.requests.some((r) => r.method === "GET" && r.path === "/debug")).toBe(true));
    await flush();
    expect(screen.queryByTestId("debug-logging")).toBeNull();
    expect(screen.queryByRole("switch", { name: "Debug logging" })).toBeNull();
    expect(screen.queryByTestId("staff-only")).toBeNull();
  });

  it("gives staff one Staff only section holding debug logging, app update and the release-branch rows, which turns debug logging on with PUT /debug", async () => {
    const updater = { install: vi.fn(async () => "0.1.0-rc.8"), restart: vi.fn(async () => {}) };
    const { daemon } = setup({ debug: { staff: true, enabled: false } }, { tab: "about", appUpdater: updater, releaseBuildUpdater, readAbout: async () => ({ version: "0.2.4+release.42.0123456789ab", files: [] }) });
    // The staff section holds the staff controls, on the About tab, last before About.
    const staffOnly = await screen.findByTestId("staff-only");
    await within(staffOnly).findByTestId("return-to-stable-row");
    expect(within(staffOnly).getByTestId("app-update-row")).toBeTruthy();
    expect(within(staffOnly).getByTestId("release-build-row")).toBeTruthy();
    expect(staffOnly.nextElementSibling?.id).toBe("about");
    const section = within(staffOnly).getByTestId("debug-logging");
    const toggle = within(section).getByRole("switch", { name: "Debug logging" });
    expect(within(staffOnly).getAllByRole("switch")).toHaveLength(1);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    expect(daemon.requests.filter((r) => r.method === "PUT" && r.path === "/debug").map((r) => r.body)).toEqual([{ enabled: true }]);
    expect(daemon.data.debug.enabled).toBe(true);
  });

  it("staff update: gives staff an Update button that installs the new build and offers a restart, across a look at another tab", async () => {
    // The install is held until the test lets it finish.
    let finish: (version: string) => void = () => {};
    const updater = { install: vi.fn(() => new Promise<string>((resolve) => (finish = resolve))), restart: vi.fn(async () => {}) };
    setup({ debug: { staff: true, enabled: false } }, { tab: "about", appUpdater: updater });
    const section = await screen.findByTestId("app-update");
    expect(updater.install).not.toHaveBeenCalled();
    fireEvent.click(within(section).getByRole("button", { name: "Update" }));
    // Another tab and back while it installs: the same row, still updating, with no second Update to start.
    fireEvent.click(screen.getByRole("tab", { name: "General" }));
    fireEvent.click(screen.getByRole("tab", { name: "About" }));
    expect(screen.getByTestId("app-update")).toBe(section);
    expect(within(section).getByRole("button", { name: "Updating…" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("tab", { name: "General" }));
    finish("0.1.0-rc.8");
    fireEvent.click(screen.getByRole("tab", { name: "About" }));
    const restart = await within(section).findByRole("button", { name: "Restart" });
    expect(updater.install).toHaveBeenCalledTimes(1);
    fireEvent.click(restart);
    await waitFor(() => expect(updater.restart).toHaveBeenCalledTimes(1));
  });

});
