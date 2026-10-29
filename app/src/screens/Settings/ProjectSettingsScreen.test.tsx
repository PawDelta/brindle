import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../../api/client";
import { DaemonContext } from "../../api/context";
import { PtyViewsProvider } from "../../api/ptyView";
import { ProvidersProvider } from "../../store/providers";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { BASE_URL, FakeDaemon, TOKEN, type DaemonData } from "../../test/fakeDaemon";
import { REPO } from "../../test/fixtures";
import { SettingsScreen } from ".";
import { SETTINGS, scopeOf, statusLine } from "./settings";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

afterEach(cleanup);

function setup(data: Partial<DaemonData> = {}) {
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    config: {
      user: { supervisor: "claude" },
      repo: { checks: ["npm test"], max_agents: 5, default_agent: "developer", sidebar: "left", overlap: null },
      local: {},
      keys: [{ name: "ANTHROPIC_API_KEY", set: true, last4: "WXYZ" }],
    },
    ...data,
  });
  const store = createAppStore();
  const failing = new Set<string>();
  const fetch: typeof globalThis.fetch = (input, init) =>
    [...failing].some((p) => String(input).includes(p)) ? Promise.reject(new Error("socket hung up")) : daemon.fetch(input, init);
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch });
  const sync = startSync({ client, store, connect: daemon.connect });
  const views = daemon.ptyViews();
  const ui = (props: { section?: "providers"; ask?: number } = {}) => (
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <ProvidersProvider client={client}>
            <PtyViewsProvider value={views}>
              <SettingsScreen client={client} repo={REPO} {...props} />
            </PtyViewsProvider>
          </ProvidersProvider>
        </SessionsProvider>
      </StoreContext.Provider>
    </DaemonContext.Provider>
  );
  const { rerender } = render(ui());
  return { daemon, failing, stop: () => sync.stop(), goto: (props: { section?: "providers"; ask?: number }) => rerender(ui(props)) };
}

const patches = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "PATCH" && r.path === "/config");

describe("settings repo view", () => {
  it("opens on the repo side of the switch, draws at once, then fills in the checks (asked for without the stored keys) and saves them to the repo", async () => {
    const { daemon, stop, goto } = setup();
    // Before the config has answered: the repo view and its sections are there, each saying it is loading.
    expect(screen.getByTestId("project-settings")).toBeTruthy();
    expect(screen.getByTestId("checks-loading")).toBeTruthy();
    // Checks, typed a keystroke at a time (the space and the Enter stay on screen), saved to the repo.
    const checks = await screen.findByRole("textbox", { name: "Checks" });
    expect(screen.getByRole("radio", { name: "proj" }).getAttribute("aria-checked")).toBe("true");
    expect(daemon.requests.find((r) => r.path === "/config" && r.query?.keys === "0")?.query).toEqual({ repo: REPO, keys: "0" });
    expect(checks).toHaveProperty("value", "npm test");
    for (const typed of ["npm test ", "npm test \n", "npm test \nn", "npm test \nnpm run build"]) fireEvent.change(checks, { target: { value: typed } });
    expect(checks).toHaveProperty("value", "npm test \nnpm run build");
    const bar = screen.getByTestId("settings-save");
    expect(bar.textContent).toContain("1 change");
    // Setup, the PR branch and the project rules are repo-only rows too, saved with it.
    fireEvent.change(screen.getByRole("textbox", { name: "Setup" }), { target: { value: "npm ci" } });
    fireEvent.change(screen.getByRole("textbox", { name: "PR branch" }), { target: { value: "develop" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Project rules" }), { target: { value: "Keep PRs small" } });
    fireEvent.click(within(screen.getByTestId("settings-save")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches(daemon)).toHaveLength(1));
    expect(patches(daemon)[0].body).toEqual({
      scope: "repo",
      repo: REPO,
      values: { checks: ["npm test", "npm run build"], setup: ["npm ci"], base_branch: "develop", rules: ["Keep PRs small"] },
    });
    expect(daemon.data.config.local).toMatchObject({ checks: ["npm test", "npm run build"], setup: ["npm ci"], base_branch: "develop", rules: ["Keep PRs small"] });
    await waitFor(() => expect(screen.queryByTestId("settings-save")).toBeNull());
    fireEvent.click(within(screen.getByTestId("repo-settings")).getByRole("button", { name: /Everything else/ }));
    expect(screen.getByTestId("config-rest").textContent).toContain("default_agent");
    // The Autopilot row is gone, and so is the old overrides list.
    expect(screen.queryByRole("switch", { name: "Autopilot" })).toBeNull();
    expect(screen.queryByTestId("project-overrides")).toBeNull();
    // A section asked for (the sidebar's provider problem line) brings You's side back.
    goto({ section: "providers", ask: 1 });
    expect((await screen.findByRole("radio", { name: "You" })).getAttribute("aria-checked")).toBe("true");
    expect(await screen.findByTestId("providers")).toBeTruthy();
    stop();
  });

  it("'Change just for me' writes a team-set value to the local file, and Clear takes it back out", async () => {
    const shared = { max_agents: 2, base_branch: "main", rules: ["Require review"] };
    const { daemon, stop } = setup({ config: { user: { plan_first: false }, repo: shared, local: {}, keys: [] } });
    const status = await screen.findByTestId("status-max_agents");
    expect(status.textContent).toContain("Set by the team");
    fireEvent.click(within(status).getByRole("button", { name: "Change just for me" }));
    await waitFor(() => expect(screen.getByTestId("status-max_agents").textContent).toContain("Just for you"));
    expect(patches(daemon)[0].body).toEqual({ scope: "repo", repo: REPO, values: { max_agents: 2 } });
    expect(daemon.data.config.local).toEqual({ max_agents: 2 });
    // The committed file is untouched.
    expect(daemon.data.config.repo).toEqual(shared);
    // An unsaved edit to the cleared row is dropped (it would otherwise be saved into the team's file); another row's edit stays.
    fireEvent.change(screen.getByRole("spinbutton", { name: "Workers at once" }), { target: { value: "4" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Checks" }), { target: { value: "make" } });
    fireEvent.click(within(screen.getByTestId("status-max_agents")).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(daemon.data.config.local).toEqual({}));
    await waitFor(() => expect(screen.getByRole("spinbutton", { name: "Workers at once" })).toHaveProperty("value", "2"));
    expect(screen.getByTestId("settings-save").textContent).toContain("1 change");
    fireEvent.click(within(screen.getByTestId("settings-save")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(daemon.data.config.local).toEqual({ checks: ["make"] }));
    expect(daemon.data.config.repo).toEqual(shared);
    // Rules union across scopes: the box is the local file's own list, the team's rule is shown apart and stays in force.
    expect(screen.getByTestId("inherited-rules").textContent).toContain("Require review");
    expect(screen.getByRole("textbox", { name: "Project rules" })).toHaveProperty("value", "");
    fireEvent.change(screen.getByRole("textbox", { name: "Project rules" }), { target: { value: "Use tests" } });
    // A team-set PR branch is edited in the committed file; Change just for me and Clear work on it like any row.
    expect(screen.getByTestId("status-base_branch").textContent).toContain("Set by the team");
    fireEvent.change(screen.getByRole("textbox", { name: "PR branch" }), { target: { value: "develop" } });
    expect(screen.getByTestId("settings-save").textContent).toContain("base_branch would change .brindle/config.json");
    fireEvent.click(within(screen.getByTestId("settings-save")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(daemon.data.config.repo).toEqual({ ...shared, base_branch: "develop" }));
    await waitFor(() => expect(daemon.data.config.local).toEqual({ checks: ["make"], rules: ["Use tests"] }));
    expect(screen.getByTestId("inherited-rules").textContent).toContain("Require review");
    fireEvent.click(within(screen.getByTestId("status-base_branch")).getByRole("button", { name: "Change just for me" }));
    await waitFor(() => expect(daemon.data.config.local).toMatchObject({ base_branch: "develop" }));
    fireEvent.click(await within(await screen.findByTestId("status-base_branch")).findByRole("button", { name: "Clear" }));
    await waitFor(() => expect(daemon.data.config.local).not.toHaveProperty("base_branch"));
    stop();
  });

  it("never saves one project's checks into another while its config has not loaded", async () => {
    const OTHER = "/home/u/other";
    const { daemon, failing, stop } = setup({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }] });
    const checks = await screen.findByRole("textbox", { name: "Checks" });
    fireEvent.change(checks, { target: { value: "npm test\nmake all" } });
    expect(screen.getByTestId("settings-save")).toBeTruthy();
    // The other repo's config fails to load: no checks to edit and no save bar to write this repo's edit into it.
    failing.add("/config");
    fireEvent.click(screen.getByRole("radio", { name: /^proj/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "other" }));
    await screen.findByText(/Could not load project settings/);
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
});

describe("statusLine", () => {
  const setting = (key: string) => SETTINGS.find((s) => s.key === key)!;
  it.each([
    ["plan_first", { plan_first: "repo" as const }, false, "Set by the team"],
    ["plan_first", { plan_first: "local" as const }, false, "Just for you"],
    ["max_agents", {}, undefined, "Same as yours (5)"],
    ["stale_after", undefined, 45, "Same as yours (45 minutes)"],
  ])("%s resolves through the three layers to %s", (key, overrides, yours, line) => {
    expect(statusLine(setting(key), scopeOf(overrides, key), yours)).toBe(line);
  });
});
