import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DaemonClient } from "../../api/client";
import { DaemonContext } from "../../api/context";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { BASE_URL, FakeDaemon, TOKEN, type DaemonData } from "../../test/fakeDaemon";
import { REPO } from "../../test/fixtures";
import { ProjectSettingsScreen } from ".";
import { SETTINGS, overrideLabel } from "./settings";

afterEach(cleanup);

function setup(data: Partial<DaemonData> = {}, repo?: string) {
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
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <ProjectSettingsScreen client={client} repo={repo} onSettings={() => {}} />
        </SessionsProvider>
      </StoreContext.Provider>
    </DaemonContext.Provider>,
  );
  return { daemon, failing, stop: () => sync.stop() };
}

const patches = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "PATCH" && r.path === "/config");

describe("project settings screen", () => {
  it("draws the page at once, then fills in the checks (asked for without the stored keys) and saves them to the repo", async () => {
    const { daemon, stop } = setup({}, REPO);
    // Before the config has answered: the page and its sections are there, each saying it is loading.
    expect(screen.getByTestId("project-settings")).toBeTruthy();
    expect(screen.getByTestId("checks-loading")).toBeTruthy();
    // Checks, typed a keystroke at a time (the space and the Enter stay on screen), saved to the repo.
    const checks = await screen.findByRole("textbox", { name: "Checks" });
    expect(daemon.requests.find((r) => r.path === "/config")?.query).toEqual({ repo: REPO, keys: "0" });
    expect(checks).toHaveProperty("value", "npm test");
    for (const typed of ["npm test ", "npm test \n", "npm test \nn", "npm test \nnpm run build"]) fireEvent.change(checks, { target: { value: typed } });
    expect(checks).toHaveProperty("value", "npm test \nnpm run build");
    const bar = screen.getByTestId("settings-save");
    expect(bar.textContent).toContain("1 change");
    fireEvent.click(within(bar).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches(daemon)).toHaveLength(1));
    expect(patches(daemon)[0].body).toEqual({ scope: "repo", repo: REPO, values: { checks: ["npm test", "npm run build"] } });
    expect(daemon.data.config.local).toEqual({ checks: ["npm test", "npm run build"] });
    await waitFor(() => expect(screen.queryByTestId("settings-save")).toBeNull());
    fireEvent.click(within(screen.getByTestId("repo-settings")).getByRole("button", { name: /Everything else/ }));
    expect(screen.getByTestId("config-rest").textContent).toContain("default_agent");
    stop();
  });

  it("lists what the project overrides with what it overrides, and clears an override in the local file but not the committed one", async () => {
    const { daemon, stop } = setup({
      config: {
        user: { coordinator: false },
        repo: { max_agents: 2 },
        local: { coordinator: true, autopilot: false },
        keys: [],
      },
    }, REPO);
    const list = await screen.findByTestId("project-overrides");
    const marker = await within(list).findByTestId("override-coordinator");
    expect(marker.textContent).toContain("On here · yours: Off");
    expect(within(list).getByTestId("override-max_agents").textContent).toContain("2 here · yours: 5");
    // The goal-driving key the project's file still sets is neither an override nor one of everything else.
    expect(within(list).queryByTestId("override-autopilot")).toBeNull();
    fireEvent.click(within(screen.getByTestId("repo-settings")).getByRole("button", { name: /Everything else/ }));
    expect(screen.getByTestId("project-settings").textContent).not.toMatch(/autopilot/i);
    // An unsaved edit to the checks survives a cleared override reloading the config.
    const checks = screen.getByRole("textbox", { name: "Checks" });
    fireEvent.change(checks, { target: { value: "npm test\nnpm run lint" } });
    fireEvent.click(within(marker).getByRole("button", { name: "Clear override" }));
    await waitFor(() => expect(within(list).queryByTestId("override-coordinator")).toBeNull());
    expect(patches(daemon)[0].body).toEqual({ scope: "repo", repo: REPO, unset: ["coordinator"] });
    expect(daemon.data.config.local).toEqual({ autopilot: false });
    expect(daemon.data.config.user).toEqual({ coordinator: false });
    expect(checks).toHaveProperty("value", "npm test\nnpm run lint");
    // The committed file's override has its own explicit clear, scoped to that file.
    fireEvent.click(within(within(list).getByTestId("override-max_agents")).getByRole("button", { name: "Clear in .brindle/config.json" }));
    await waitFor(() => expect(within(list).queryByTestId("override-max_agents")).toBeNull());
    expect(patches(daemon)[1].body).toEqual({ scope: "shared", repo: REPO, unset: ["max_agents"] });
    expect(daemon.data.config.repo).toEqual({});
    expect(list.textContent).toContain("overrides none of your settings");
    expect(screen.getByTestId("settings-save")).toBeTruthy();
    stop();
  });

  it.each([
    ["your default", undefined],
    // The project's committed config.json sets it: clearing the local override reads that, never yours.
    ["the project's shared config", { [REPO]: "auto" as const }],
  ])("plan approvals: a local override over %s, and clearing it", async (_, shared) => {
    const { daemon, stop } = setup({ plan: { user: "never", projects: {}, shared } }, REPO);
    const section = screen.getByTestId("project-plan-approvals");
    const plan = await within(section).findByRole("combobox", { name: "Plan approvals" });
    const unset = () => within(plan).getAllByRole("option")[0];
    expect(plan).toHaveProperty("value", "");
    expect(unset().textContent!.startsWith("Yours")).toBe(!shared);
    expect(!!within(section).queryByTestId("plan-shared")).toBe(!!shared);
    fireEvent.change(plan, { target: { value: "always" } });
    await waitFor(() => expect(daemon.data.plan.projects).toEqual({ [REPO]: "always" }));
    await waitFor(() => expect(plan).toHaveProperty("value", "always"));
    expect(within(section).queryByTestId("plan-shared")).toBeNull();
    expect(unset().textContent!.startsWith("Yours")).toBe(!shared);
    fireEvent.change(plan, { target: { value: "" } });
    await waitFor(() => expect(daemon.data.plan.projects).toEqual({}));
    await waitFor(() => expect(!!within(section).queryByTestId("plan-shared")).toBe(!!shared));
    const puts = daemon.requests.filter((r) => r.method === "PUT" && r.path === "/governance/plan").map((r) => r.body);
    expect(puts).toEqual([{ scope: "repo", repo: REPO, plan: "always" }, { scope: "repo", repo: REPO, plan: null }]);
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
    fireEvent.change(screen.getByRole("combobox", { name: "Repo" }), { target: { value: OTHER } });
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

describe("overrideLabel", () => {
  const setting = (key: string) => SETTINGS.find((s) => s.key === key)!;
  it.each([
    ["coordinator", true, false, "On here · yours: Off"],
    ["coordinator", false, undefined, "Off here · yours: Off"],
    ["max_agents", 2, undefined, "2 here · yours: 5"],
    ["stale_after", 0, 45, "0 minutes here · yours: 45 minutes"],
    ["sidebar", "bottom", "left", "Below the chat here · yours: Left of the chat"],
  ])("%s: project %j over %j reads %s", (key, project, yours, label) => {
    expect(overrideLabel(setting(key), project, yours)).toBe(label);
  });
});
