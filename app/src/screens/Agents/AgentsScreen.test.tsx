import { connectDaemon, renderApp, inTestContext, trackedSync as startSync } from "../../test/harness";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../../App";
import { openExternal } from "../../api/open";
import { ONBOARDED_KEY } from "../../api/providers";
import { DaemonClient } from "../../api/client";
import { createAppStore } from "../../store/store";
import { BASE_URL, FakeDaemon, TOKEN, flush, type DaemonData, type RoutingProfile } from "../../test/fakeDaemon";
import { REPO, root, worker } from "../../test/fixtures";
import { AgentsScreen } from ".";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));
vi.mock("../../api/open", () => ({ openExternal: vi.fn(() => Promise.resolve()), pickFolder: undefined }));

afterEach(cleanup);

const rp = (name: string, provider: string, model: string | null, cost_tier: string, fields: Partial<RoutingProfile> = {}): RoutingProfile => ({
  name,
  provider,
  model,
  cost_tier,
  installed: true,
  signed_in: true,
  ...fields,
});

/* Claude Code and Codex connected, OpenCode not installed; the person's own quick-fix on Codex's small model. */
const profiles: RoutingProfile[] = [
  rp("developer-light", "claude", "haiku-5.5", "low"),
  rp("developer", "claude", "sonnet-5.5", "medium"),
  rp("developer-heavy", "claude", "opus-5.5", "high"),
  rp("developer-fable", "claude", "fable-5.1", "high"),
  rp("developer-codex", "codex", "gpt-5.5", "medium"),
  rp("developer-opencode", "opencode", null, "medium", { installed: false, signed_in: false }),
  rp("reviewer", "claude", "opus-5.5", "high"),
  rp("reviewer-codex", "codex", "gpt-5.5", "medium"),
  rp("supervisor", "claude", null, "medium"),
  rp("quick-fix", "codex", "gpt-5.5-mini", "low"),
];

const described: DaemonData["profiles"] = [
  { name: "developer-light", provider: "claude", model: "haiku-5.5", description: "Small, clear edits: docs, renames, simple tests" },
  { name: "developer", provider: "claude", model: "sonnet-5.5", description: "Everyday features and bugfixes" },
  { name: "developer-heavy", provider: "claude", model: "opus-5.5", description: "Architecture, hard bugs, careful review" },
  { name: "quick-fix", provider: "codex", model: "gpt-5.5-mini", description: "My quick fixes, on Codex's small model." },
];

const data = (more: Partial<DaemonData> = {}): Partial<DaemonData> => ({
  repos: [{ repo_root: REPO, name: "proj" }],
  routingProfiles: profiles,
  profiles: described,
  routingConfig: {
    user: {
      routing: {
        light: ["developer-light", "quick-fix", "developer"],
        medium: ["developer", "developer-codex", "developer-heavy"],
        heavy: ["developer-heavy", "developer-codex", "developer-fable"],
        advanced: ["developer-fable", "developer-heavy"],
      },
      reviewer: "reviewer-codex",
      supervisor: "developer-heavy",
      kind_fallbacks: { reviewer: ["reviewer"], supervisor: ["developer"] },
      routing_locked: ["supervisor"],
    },
    repo: {},
    local: {},
  },
  // Hosted learning runs Fable for Heavy work.
  learned: { heavy: "developer-fable" },
  rulePacks: [
    { name: "tests-first", description: "writes a failing test before the fix", source: "built-in rule pack tests-first" },
    { name: "small-commits", description: "makes one focused commit per step", source: "built-in rule pack small-commits" },
  ],
  ...more,
});

/* Renders the screen against a daemon; a second mount of the same daemon reloads what it saved. */
function mount(daemon: FakeDaemon, fetch?: (daemon: FakeDaemon) => typeof globalThis.fetch) {
  const store = createAppStore();
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: fetch?.(daemon) ?? daemon.fetch });
  const sync = startSync({ client, store, connect: daemon.connect });
  const onNavigate = vi.fn();
  render(
    inTestContext(<><AgentsScreen client={client} onNavigate={onNavigate} /></>, { client, store }, { sessions: true }),
  );
  return { client, onNavigate, stop: () => sync.stop() };
}

function setup(more: Partial<DaemonData> = {}, fetch?: (daemon: FakeDaemon) => typeof globalThis.fetch) {
  const daemon = new FakeDaemon(data(more));
  return { daemon, ...mount(daemon, fetch) };
}

const NAMES: Record<string, string> = { light: "Light", medium: "Medium", heavy: "Heavy", advanced: "Advanced", reviewer: "Reviewer", supervisor: "Supervisor" };
const row = (k: string) => screen.getByTestId(`kind-${k}`);
const runsOn = (k: string) => within(row(k)).getByRole("button", { name: `${NAMES[k]} runs on` });
const chain = (k: string) => within(row(k)).getByTestId("chain").textContent;
const lock = (k: string) => within(row(k)).getByTestId("lock");
const puts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "PUT" && r.path === "/routing").map((r) => r.body);
const openFallbacks = (k: string) => {
  const b = within(row(k)).getByTestId("fallbacks");
  if (b.getAttribute("aria-expanded") !== "true") fireEvent.click(b);
  return within(row(k)).getByRole("list", { name: `${NAMES[k]} fallbacks` });
};
const option = (list: HTMLElement, id: string) => list.querySelector<HTMLButtonElement>(`button[data-choice="${id}"]`);
/* Nothing opens over the page: no menus, dialogs, popovers or native selects. */
const noPopups = () => expect(document.querySelector("select, dialog, [role=dialog], [role=menu], [role=listbox], [popover]")).toBeNull();

describe("agents tab", () => {
  it("agents: shows how a task gets run, then picks a model for a kind from the connected providers only, with cost and speed", async () => {
    const { daemon } = setup();
    await screen.findByTestId("kind-medium");
    expect(screen.getAllByTestId(/^kind-/).map((el) => el.dataset.testid)).toEqual(["kind-light", "kind-medium", "kind-heavy", "kind-advanced", "kind-reviewer", "kind-supervisor"]);

    expect(runsOn("medium").dataset.choice).toBe("developer");
    expect(chain("medium")).toBe("GPT-5.5, then Opus 5.5");

    fireEvent.click(runsOn("medium"));
    const list = within(row("medium")).getByRole("group", { name: "Choices for Medium" });
    // Your custom agents, then each connected provider's models with cost, speed and what they're good at.
    // OpenCode isn't connected: none of its models, one quiet line to Settings › Providers.
    expect(option(list, "developer-opencode")).toBeNull();
    expect(list.querySelector('[data-missing="opencode"]')!.textContent).toContain("OpenCode isn't connected, so its models aren't offered.");
    noPopups();

    fireEvent.click(option(list, "developer-heavy")!);
    await waitFor(() => expect(runsOn("medium").dataset.choice).toBe("developer-heavy"));
    // What ran it before is its first fallback; choosing locks the row against learning.
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { medium: { model: "developer-heavy", fallbacks: ["developer", "developer-codex"] } } });
    expect(chain("medium")).toBe("Sonnet 5.5, then GPT-5.5");
    expect(lock("medium").getAttribute("aria-pressed")).toBe("true");
  });

  it("agents: reorders, removes and adds fallbacks inline, under the row", async () => {
    const { daemon } = setup();
    await screen.findByTestId("kind-light");
    expect(chain("light")).toBe("quick-fix, then Sonnet 5.5");
    const list = openFallbacks("light");
    expect([...list.querySelectorAll("li[data-choice]")].map((li) => li.getAttribute("data-choice"))).toEqual(["quick-fix", "developer"]);
    // The first fallback can't move ahead of the pick.
    expect((within(list).getByRole("button", { name: "Move quick-fix earlier" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(list).getByRole("button", { name: "Move Sonnet 5.5 earlier" }));
    await waitFor(() => expect(chain("light")).toBe("Sonnet 5.5, then quick-fix"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { light: { fallbacks: ["developer", "quick-fix"] } } });

    fireEvent.click(within(openFallbacks("light")).getByRole("button", { name: "Move Sonnet 5.5 later" }));
    await waitFor(() => expect(chain("light")).toBe("quick-fix, then Sonnet 5.5"));

    fireEvent.click(within(openFallbacks("light")).getByRole("button", { name: "Remove quick-fix" }));
    await waitFor(() => expect(chain("light")).toBe("Sonnet 5.5"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { light: { fallbacks: ["developer"] } } });

    // "+ Add a fallback" opens the same list inline, without what's already listed.
    fireEvent.click(within(openFallbacks("light")).getByRole("button", { name: /Add a fallback/ }));
    const add = within(row("light")).getByRole("group", { name: "Choices for Light fallbacks" });
    expect(option(add, "developer")).toBeNull();
    expect(option(add, "developer-light")).toBeNull();
    fireEvent.click(option(add, "developer-codex")!);
    await waitFor(() => expect(chain("light")).toBe("Sonnet 5.5, then GPT-5.5"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { light: { fallbacks: ["developer", "developer-codex"] } } });
    noPopups();
  });

  it("agents: a row learning changed runs the learned model, says only that, and 'use mine instead' locks it back to mine", async () => {
    const { daemon } = setup();
    await screen.findByTestId("kind-heavy");
    expect(runsOn("heavy").dataset.choice).toBe("developer-fable");
    const note = within(row("heavy")).getByTestId("learned");
    // Never why: no scores, rates or reasons.
    // The person's pick waits behind it, marked, and can't be moved from there.
    expect(chain("heavy")).toBe("Opus 5.5, then GPT-5.5");
    openFallbacks("heavy");

    fireEvent.click(within(note).getByRole("button", { name: "use mine instead" }));
    await waitFor(() => expect(runsOn("heavy").dataset.choice).toBe("developer-heavy"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { heavy: { locked: true } } });
    expect(within(row("heavy")).queryByTestId("learned")).toBeNull();
    expect(lock("heavy").getAttribute("aria-pressed")).toBe("true");
  });

  it("agents: makes a custom agent end to end from a row's list, and it runs that row and is in every list", async () => {
    const { daemon, onNavigate } = setup();
    await screen.findByTestId("kind-medium");
    fireEvent.click(runsOn("medium"));
    fireEvent.click(within(row("medium")).getByRole("button", { name: /Make a custom agent/ }));
    const b = within(row("medium")).getByRole("region", { name: "New custom agent" });
    const create = within(b).getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);

    // 1. What it's for.
    fireEvent.change(within(b).getByLabelText("Name"), { target: { value: "careful-dev" } });
    fireEvent.change(within(b).getByLabelText("What it's for"), { target: { value: "Feature work, tests first, small commits" } });
    // 2. Provider (connected ones only; OpenCode named, not offered) and model, each with cost and speed.
    const providers = within(b).getByRole("group", { name: "Provider" });
    expect(within(providers).queryByRole("button", { name: /OpenCode/ })).toBeNull();
    expect(providers.querySelector('[data-provider-off="opencode"]')!.textContent).toContain("not connected");
    fireEvent.click(within(providers).getByRole("button", { name: /Claude Code/ }));
    const models = within(b).getByRole("group", { name: "Model" });
    const sonnet = within(models).getByRole("button", { name: /Sonnet 5\.5/ });
    fireEvent.click(sonnet);
    // 3. How it works: effort, instructions, rules with their one-line descriptions, and where they all are.
    fireEvent.click(within(within(b).getByRole("group", { name: "Effort" })).getByRole("button", { name: "high" }));
    fireEvent.change(within(b).getByLabelText("Instructions"), { target: { value: "Write the failing test first." } });
    const rules = within(b).getByRole("group", { name: "Rules" });
    fireEvent.click(within(rules.querySelector<HTMLElement>('[data-rule="tests-first"]')!).getByRole("checkbox"));
    fireEvent.click(within(rules).getByRole("button", { name: "See all rules" }));
    expect(onNavigate).toHaveBeenLastCalledWith("knowledge");
    // 4. Use it for: one choice, the row it came from first.
    const use = within(b).getByRole("group", { name: "Use it for" });
    expect(within(use).getByRole("button", { name: "Medium" }).getAttribute("aria-pressed")).toBe("true");
    // 5. Checked, summed up, created.
    noPopups();
    fireEvent.click(create);

    await waitFor(() => expect(runsOn("medium").dataset.choice).toBe("careful-dev"));
    expect(daemon.requests.find((r) => r.method === "POST" && r.path === "/profiles")?.body).toEqual({
      repo: REPO,
      name: "careful-dev",
      description: "Feature work, tests first, small commits",
      provider: "claude",
      model: "sonnet-5.5",
      effort: "high",
      instructions: "Write the failing test first.",
      rules: ["tests-first"],
    });
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { medium: { model: "careful-dev", fallbacks: ["developer", "developer-codex", "developer-heavy"] } } });
    expect(chain("medium")).toBe("Sonnet 5.5, then GPT-5.5, then Opus 5.5");
    // The builder closed; the row's list is open with the new agent in it, marked new.
    expect(within(row("medium")).queryByRole("region", { name: "New custom agent" })).toBeNull();
    const list = within(row("medium")).getByRole("group", { name: "Choices for Medium" });
    expect(option(list, "careful-dev")).toBeTruthy();
    // Every other row's list offers it too.
    fireEvent.click(runsOn("reviewer"));
    expect(option(within(row("reviewer")).getByRole("group", { name: "Choices for Reviewer" }), "careful-dev")).toBeTruthy();
    expect(screen.getByTestId("custom-agents").textContent).toContain("careful-dev");
  });

  it("agents: clearing More's instructions sends null, the daemon refuses blank text, and a reload shows them empty", async () => {
    const { daemon, client, stop } = setup();
    const more = () => within(row("medium")).getByRole("button", { name: /^More/ });
    await waitFor(() => expect(more()).toBeTruthy());
    fireEvent.click(more());
    const box = within(row("medium")).getByLabelText("Instructions");
    // A save reloads the routing; wait for the reload, so the clear compares against what was saved.
    const reloads = () => daemon.requests.filter((r) => r.method === "GET" && r.path === "/routing").length;
    const before = reloads();
    fireEvent.change(box, { target: { value: "Keep each commit to one step." } });
    fireEvent.blur(box);
    await waitFor(() => expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { medium: { instructions: "Keep each commit to one step." } } }));
    await waitFor(() => expect(reloads()).toBeGreaterThan(before));
    await flush();

    // Blank text is a clear: it goes out as null, never "".
    fireEvent.change(box, { target: { value: "  " } });
    fireEvent.blur(box);
    await waitFor(() => expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { medium: { instructions: null } } }));
    // The daemon refuses blank text sent directly; only null clears.
    for (const instructions of ["", "   "]) {
      await expect(client.putRouting({ repo: REPO, scope: "user", kinds: { medium: { instructions } } })).rejects.toThrow();
    }
    stop();
    cleanup();

    const again = mount(daemon);
    await waitFor(() => expect(within(row("medium")).getByRole("button", { name: /^More/ })).toBeTruthy());
    fireEvent.click(within(row("medium")).getByRole("button", { name: /^More/ }));
    expect((within(row("medium")).getByLabelText("Instructions") as HTMLTextAreaElement).value).toBe("");
    again.stop();
  });

  it("agents: a provider that isn't connected is skipped, and its Connect opens Settings › Providers", async () => {
    const offline = profiles.map((p) => (p.provider === "codex" ? { ...p, signed_in: false } : p));
    const config = data().routingConfig!;
    const daemon = new FakeDaemon(
      data({
        sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: Date.now() / 1000, goal: "Ship it" }],
        workspaces: [root, worker],
        routingProfiles: offline,
        routingConfig: { ...config, user: { ...config.user, routing: { ...config.user.routing, medium: ["developer-codex", "developer"] } } },
      }),
    );
    const { client, store, sync } = connectDaemon(daemon);
    localStorage.setItem(ONBOARDED_KEY, "1");
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    const pages = await screen.findByRole("group", { name: "Sidebar foot" });
    expect(within(pages).getByRole("button", { name: "Open Account › Usage" })).toBeTruthy();
    expect(pages.className).toContain("flex-nowrap");
    fireEvent.click(within(pages).getByRole("button", { name: "Open Account › Usage" }));
    fireEvent.click(await screen.findByRole("button", { name: "Agents" }));
    await screen.findByTestId("kind-medium");

    // Medium's GPT-5.5 can't run: Sonnet 5.5 runs it for now; Light's quick-fix fallback is skipped.
    expect(within(row("medium")).getByTestId("cant-run").querySelector("b")?.textContent).toBe("Sonnet 5.5");
    openFallbacks("light");
    // Its models aren't offered.
    fireEvent.click(runsOn("advanced"));
    const list = within(row("advanced")).getByRole("group", { name: "Choices for Advanced" });
    expect(option(list, "developer-codex")).toBeNull();
    expect(option(list, "quick-fix")).toBeNull();
    expect(list.querySelector('[data-missing="codex"]')).toBeTruthy();

    fireEvent.click(within(within(row("medium")).getByTestId("cant-run")).getByRole("button", { name: "Connect Codex" }));
    expect(await screen.findByRole("region", { name: "Settings" })).toBeTruthy();
    expect(within(pages).getByRole("button", { name: "Open Account › Usage" }).getAttribute("aria-current")).toBe("page");
  });

  it("agents: one list for the person whatever the repos, up before the slow learned picks, which fill in after", async () => {
    const OTHER = "/home/u/other";
    // What learning runs instead resolves the dispatch for every candidate (seconds): held until released.
    const held: Array<() => void> = [];
    const hold = (daemon: FakeDaemon): typeof globalThis.fetch => (input, init) => {
      const url = new URL(String(input));
      return url.pathname.endsWith("/routing") && !init?.method && url.searchParams.get("learned") !== "0"
        ? new Promise((resolve) => held.push(() => resolve(daemon.fetch(input, init))))
        : daemon.fetch(input, init);
    };
    const { daemon } = setup(
      {
        repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }],
        // Each repo's own file runs Medium on Codex: the person's own list doesn't.
        routingByRepo: { [REPO]: { local: { routing: { medium: ["developer-codex"] } } }, [OTHER]: { local: { routing: { medium: ["developer-codex"] } } } },
      },
      hold,
    );
    await screen.findByTestId("kind-medium");
    await waitFor(() => expect(held.length).toBe(1));
    expect(runsOn("medium").dataset.choice).toBe("developer");
    expect(runsOn("heavy").dataset.choice).toBe("developer-heavy");
    expect(screen.queryByRole("group", { name: "Repo" })).toBeNull();

    held.splice(0).forEach((go) => go());
    await waitFor(() => expect(runsOn("heavy").dataset.choice).toBe("developer-fable"));
    // One read of the person's settings and one of their library, never one per repo.
    const gets = daemon.requests.filter((r) => r.method === "GET" && ["/routing", "/profiles"].includes(r.path));
    expect(gets.map((r) => [r.path, r.query.scope ?? null, r.query.learned ?? null, r.query.repo ?? null])).toEqual([
      ["/routing", "user", "0", REPO],
      ["/profiles", null, null, null],
      ["/routing", "user", null, REPO],
    ]);

    // A save's reload keeps what learning runs while that is read again.
    fireEvent.click(lock("advanced"));
    await waitFor(() => expect(held.length).toBe(1));
    expect(lock("advanced").getAttribute("aria-pressed")).toBe("true");
    expect(runsOn("heavy").dataset.choice).toBe("developer-fable");
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { advanced: { locked: true } } });
  });

  it("agents: whether each agent can run comes from its own sign-in and exclusion, with the reason", async () => {
    // Codex is signed in for its developer, but the reviewer runs under another Codex identity that's signed out; Antigravity is excluded.
    const mixed = [...profiles.map((p) => (p.name === "reviewer-codex" ? { ...p, signed_in: false } : p)), rp("developer-gemini", "antigravity", "gemini-3-pro", "high")];
    const config = data().routingConfig!;
    const { onNavigate } = setup({
      routingProfiles: mixed,
      routingConfig: { ...config, user: { ...config.user, exclude_providers: ["antigravity"], routing: { ...config.user.routing, heavy: ["developer-heavy", "developer-gemini", "developer-codex"] } } },
    });
    await screen.findByTestId("kind-reviewer");

    // The reviewer's own Codex sign-in is out: it's skipped, with why, and the next one runs.
    const note = within(row("reviewer")).getByTestId("cant-run");
    expect(note.textContent).toContain("GPT-5.5 can't run: Codex is signed out for it. Opus 5.5 runs it until then.");
    fireEvent.click(within(note).getByRole("button", { name: "Connect Codex" }));
    expect(onNavigate).toHaveBeenLastCalledWith("providers");
    // Codex's signed-in developer is still offered; the signed-out reviewer is listed, dimmed, with its reason.
    fireEvent.click(runsOn("medium"));
    expect(option(within(row("medium")).getByRole("group", { name: "Choices for Medium" }), "developer-codex")).toBeTruthy();
    fireEvent.click(runsOn("reviewer"));
    const reviewers = within(row("reviewer")).getByRole("group", { name: "Choices for Reviewer" });
    expect(option(reviewers, "reviewer-codex")).toBeNull();
    expect(reviewers.querySelector('[data-choice-off="reviewer-codex"]')!.textContent).toContain("Codex is signed out for it");
    expect(reviewers.querySelector('[data-missing="codex"]')).toBeNull();

    // An excluded provider's agent is skipped where it's a fallback and can't be picked, with why; there's nothing to connect.
    expect(within(row("heavy")).getByTestId("chain").querySelector(".line-through")!.textContent).toBe("Gemini 3 Pro");
    const heavy = openFallbacks("heavy");
    expect(heavy.querySelector('li[data-choice="developer-gemini"]')).toBeTruthy();
    fireEvent.click(runsOn("advanced"));
    const advanced = within(row("advanced")).getByRole("group", { name: "Choices for Advanced" });
    expect(option(advanced, "developer-gemini")).toBeNull();
    expect(advanced.querySelector('[data-choice-off="developer-gemini"]')!.textContent).toContain("Antigravity is excluded from routing");
  });

  it("agents: a broken custom agent shows its automatic check's problems where it's used, with no lint button", async () => {
    const broken = profiles.map((p) => (p.name === "quick-fix" ? { ...p, model: "gpt-4.1", lint_errors: ["gpt-4.1 isn't one of Codex's models any more.", "Rule pack python-styl isn't installed."] } : p));
    const config = data().routingConfig!;
    setup({ routingProfiles: broken, routingConfig: { ...config, user: { ...config.user, routing: { ...config.user.routing, advanced: ["quick-fix", "developer-fable"] } } } });
    await screen.findByTestId("kind-light");

    // Where it runs first: "2 problems", and the next one runs instead.
    expect(within(row("advanced")).getByTestId("cant-run").querySelector("b")?.textContent).toBe("Fable 5.1");
    expect(within(within(row("advanced")).getByTestId("cant-run")).queryByRole("button", { name: /Connect/ })).toBeNull();
    // Where it's a fallback: skipped, struck, with the problems behind its count.
    expect(within(row("light")).getByTestId("chain").querySelector(".line-through")!.textContent).toBe("quick-fix");
    const list = openFallbacks("light");
    const entry = list.querySelector<HTMLElement>('li[data-choice="quick-fix"]')!;
    fireEvent.click(within(entry).getByTestId("problems"));
    const found = row("light").querySelector<HTMLElement>('[data-problems="quick-fix"]')!;
    expect(found.textContent).toContain("gpt-4.1 isn't one of Codex's models any more.");
    expect(found.textContent).toContain("Rule pack python-styl isn't installed.");
    // It isn't offered in a list until it's fixed, and the custom agents line counts its problems.
    fireEvent.click(runsOn("medium"));
    expect(option(within(row("medium")).getByRole("group", { name: "Choices for Medium" }), "quick-fix")).toBeNull();
    // Checks are automatic: there's no lint or check button anywhere.
    expect(screen.queryByRole("button", { name: /lint|check every|check agents/i })).toBeNull();
  });

  it("org refusal link", async () => {
    const url = "https://brindle.example/org/models";
    const refuse = (d: FakeDaemon) => (input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "PUT" && String(input).endsWith("/routing")
        ? Promise.resolve(new Response(JSON.stringify({ error: { code: "org_refused", message: `Your org doesn't allow that model. See ${url}` } }), { status: 409, headers: { "content-type": "application/json" } }))
        : d.fetch(input, init);
    setup({}, refuse);
    await screen.findByTestId("kind-medium");
    fireEvent.click(runsOn("medium"));
    const list = within(row("medium")).getByRole("group", { name: "Choices for Medium" });
    fireEvent.click(option(list, "developer-heavy")!);

    // The refusal's https link is a real link that opens in the system browser; the text stays as it was.
    const link = await screen.findByRole("link", { name: url });
    expect(link.closest('[role="alert"]')!.textContent).toContain(`Could not save: Your org doesn't allow that model. See ${url}`);
    fireEvent.click(link);
    expect(openExternal).toHaveBeenCalledWith(url);
  });
});
