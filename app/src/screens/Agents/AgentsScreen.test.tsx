import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../../App";
import { DaemonClient } from "../../api/client";
import { PtyViewsProvider } from "../../api/ptyView";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { BASE_URL, FakeDaemon, TOKEN, flush, type DaemonData, type RoutingProfile } from "../../test/fakeDaemon";
import { REPO, root, worker } from "../../test/fixtures";
import { standInViews } from "../../test/panes";
import { AgentsScreen } from ".";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

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
    <StoreContext.Provider value={store}>
      <SessionsProvider client={client}>
        <AgentsScreen client={client} onNavigate={onNavigate} />
      </SessionsProvider>
    </StoreContext.Provider>,
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
    const { daemon, stop } = setup();
    await screen.findByTestId("kind-medium");
    const flow = screen.getByRole("list", { name: "How a task is run" });
    expect(within(flow).getAllByRole("listitem").map((li) => li.querySelector("b")!.textContent)).toEqual(["A task comes in", "It's sized", "That size's agent runs it", "At a limit, the next one"]);
    expect(screen.getAllByTestId(/^kind-/).map((el) => el.dataset.testid)).toEqual(["kind-light", "kind-medium", "kind-heavy", "kind-advanced", "kind-reviewer", "kind-supervisor"]);
    expect(document.body.textContent).not.toMatch(/profile/i);

    expect(runsOn("medium").dataset.choice).toBe("developer");
    expect(runsOn("medium").textContent).toMatch(/Sonnet 5\.5\$\$steady/);
    expect(chain("medium")).toBe("GPT-5.5, then Opus 5.5");

    fireEvent.click(runsOn("medium"));
    const list = within(row("medium")).getByRole("group", { name: "Choices for Medium" });
    // Your custom agents, then each connected provider's models with cost, speed and what they're good at.
    expect(list.textContent).toContain("Your custom agents");
    expect(option(list, "quick-fix")!.textContent).toContain("GPT-5.5-mini");
    expect(option(list, "developer-heavy")!.textContent).toMatch(/Opus 5\.5.*\$\$\$slower.*Architecture, hard bugs/);
    expect(option(list, "developer-codex")!.textContent).toContain("GPT-5.5");
    // OpenCode isn't connected: none of its models, one quiet line to Settings › Providers.
    expect(option(list, "developer-opencode")).toBeNull();
    expect(list.querySelector('[data-missing="opencode"]')!.textContent).toContain("OpenCode isn't connected, so its models aren't offered.");
    noPopups();

    fireEvent.click(option(list, "developer-heavy")!);
    await waitFor(() => expect(runsOn("medium").dataset.choice).toBe("developer-heavy"));
    // What ran it before is its first fallback; choosing locks the row against learning.
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { medium: { model: "developer-heavy", fallbacks: ["developer", "developer-codex"] } } });
    expect(chain("medium")).toBe("Sonnet 5.5, then GPT-5.5");
    expect(lock("medium").getAttribute("aria-pressed")).toBe("true");
    stop();
  });

  it("agents: reorders, removes and adds fallbacks inline, under the row", async () => {
    const { daemon, stop } = setup();
    await screen.findByTestId("kind-light");
    expect(chain("light")).toBe("quick-fix, then Sonnet 5.5");
    const list = openFallbacks("light");
    expect([...list.querySelectorAll("li[data-choice]")].map((li) => li.getAttribute("data-choice"))).toEqual(["quick-fix", "developer"]);
    // The first fallback can't move ahead of the pick.
    expect((within(list).getByRole("button", { name: "Move quick-fix earlier" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(list).getByRole("button", { name: "Move Sonnet 5.5 earlier" }));
    await waitFor(() => expect(chain("light")).toBe("Sonnet 5.5, then quick-fix"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { light: { fallbacks: ["developer", "quick-fix"] } } });

    fireEvent.click(within(openFallbacks("light")).getByRole("button", { name: "Move Sonnet 5.5 later" }));
    await waitFor(() => expect(chain("light")).toBe("quick-fix, then Sonnet 5.5"));

    fireEvent.click(within(openFallbacks("light")).getByRole("button", { name: "Remove quick-fix" }));
    await waitFor(() => expect(chain("light")).toBe("Sonnet 5.5"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { light: { fallbacks: ["developer"] } } });

    // "+ Add a fallback" opens the same list inline, without what's already listed.
    fireEvent.click(within(openFallbacks("light")).getByRole("button", { name: /Add a fallback/ }));
    const add = within(row("light")).getByRole("group", { name: "Choices for Light fallbacks" });
    expect(option(add, "developer")).toBeNull();
    expect(option(add, "developer-light")).toBeNull();
    fireEvent.click(option(add, "developer-codex")!);
    await waitFor(() => expect(chain("light")).toBe("Sonnet 5.5, then GPT-5.5"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { light: { fallbacks: ["developer", "developer-codex"] } } });
    noPopups();
    stop();
  });

  it("agents: lock keeps a row's choice from learning, and unlocks", async () => {
    const { daemon, stop } = setup();
    await screen.findByTestId("kind-advanced");
    expect(lock("supervisor").getAttribute("aria-pressed")).toBe("true");
    expect(lock("supervisor").textContent).toContain("Locked");
    expect(lock("advanced").getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(lock("advanced"));
    await waitFor(() => expect(lock("advanced").getAttribute("aria-pressed")).toBe("true"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { advanced: { locked: true } } });
    expect(lock("advanced").textContent).toContain("Locked");

    fireEvent.click(lock("advanced"));
    await waitFor(() => expect(lock("advanced").getAttribute("aria-pressed")).toBe("false"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { advanced: { locked: false } } });
    stop();
  });

  it("agents: a row learning changed runs the learned model, says only that, and 'use mine instead' locks it back to mine", async () => {
    const { daemon, stop } = setup();
    await screen.findByTestId("kind-heavy");
    expect(runsOn("heavy").dataset.choice).toBe("developer-fable");
    expect(runsOn("heavy").textContent).toContain("learned");
    const note = within(row("heavy")).getByTestId("learned");
    expect(note.textContent).toBe("brindle learned Fable 5.1 does better here · use mine instead" + "Lock this row to keep your choice.");
    // Never why: no scores, rates or reasons.
    expect(note.textContent).not.toMatch(/%|because|score|rate|\d+ task/i);
    // The person's pick waits behind it, marked, and can't be moved from there.
    expect(chain("heavy")).toBe("Opus 5.5, then GPT-5.5");
    const list = openFallbacks("heavy");
    expect(list.querySelector('li[data-choice="developer-heavy"]')!.textContent).toContain("your pick");

    fireEvent.click(within(note).getByRole("button", { name: "use mine instead" }));
    await waitFor(() => expect(runsOn("heavy").dataset.choice).toBe("developer-heavy"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { heavy: { locked: true } } });
    expect(within(row("heavy")).queryByTestId("learned")).toBeNull();
    expect(runsOn("heavy").textContent).not.toContain("learned");
    expect(lock("heavy").getAttribute("aria-pressed")).toBe("true");
    stop();
  });

  it("agents: makes a custom agent end to end from a row's list, and it runs that row and is in every list", async () => {
    const { daemon, onNavigate, stop } = setup();
    await screen.findByTestId("kind-medium");
    fireEvent.click(runsOn("medium"));
    fireEvent.click(within(row("medium")).getByRole("button", { name: /Make a custom agent/ }));
    const b = within(row("medium")).getByRole("region", { name: "New custom agent" });
    const create = within(b).getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    expect(within(b).getByTestId("check").textContent).toBe("Give it a name.");

    // 1. What it's for.
    fireEvent.change(within(b).getByLabelText("Name"), { target: { value: "careful-dev" } });
    fireEvent.change(within(b).getByLabelText("What it's for"), { target: { value: "Feature work, tests first, small commits" } });
    expect(within(b).getByTestId("check").textContent).toBe("Pick a model.");
    // 2. Provider (connected ones only; OpenCode named, not offered) and model, each with cost and speed.
    const providers = within(b).getByRole("group", { name: "Provider" });
    expect(within(providers).queryByRole("button", { name: /OpenCode/ })).toBeNull();
    expect(providers.querySelector('[data-provider-off="opencode"]')!.textContent).toContain("not connected");
    fireEvent.click(within(providers).getByRole("button", { name: /Claude Code/ }));
    const models = within(b).getByRole("group", { name: "Model" });
    expect(within(models).getAllByRole("button").map((m) => m.querySelector("span")!.textContent)).toEqual(["Haiku 5.5", "Sonnet 5.5", "Opus 5.5", "Fable 5.1"]);
    const sonnet = within(models).getByRole("button", { name: /Sonnet 5\.5/ });
    expect(sonnet.textContent).toContain("medium cost · steady");
    fireEvent.click(sonnet);
    // 3. How it works: effort, instructions, rules with their one-line descriptions, and where they all are.
    fireEvent.click(within(within(b).getByRole("group", { name: "Effort" })).getByRole("button", { name: "high" }));
    fireEvent.change(within(b).getByLabelText("Instructions"), { target: { value: "Write the failing test first." } });
    const rules = within(b).getByRole("group", { name: "Rules" });
    expect(rules.textContent).toContain("Rules are short instructions brindle adds to this agent's brief.");
    expect(rules.querySelector('[data-rule="tests-first"]')!.textContent).toContain("writes a failing test before the fix");
    fireEvent.click(within(rules.querySelector<HTMLElement>('[data-rule="tests-first"]')!).getByRole("checkbox"));
    fireEvent.click(within(rules).getByRole("button", { name: "See all rules" }));
    expect(onNavigate).toHaveBeenLastCalledWith("knowledge");
    // 4. Use it for: one choice, the row it came from first.
    const use = within(b).getByRole("group", { name: "Use it for" });
    expect(within(use).getAllByRole("button").map((x) => x.textContent)).toEqual(["Any kind of work", "Light", "Medium", "Heavy", "Advanced", "Reviewer", "Supervisor"]);
    expect(within(use).getByRole("button", { name: "Medium" }).getAttribute("aria-pressed")).toBe("true");
    // 5. Checked, summed up, created.
    expect(within(b).getByTestId("summary").textContent).toMatch(/careful-dev runs on Sonnet 5\.5 \(Claude Code\).*high effort · rules tests-first, and runs Medium work, ahead of Sonnet 5\.5\./);
    expect(within(b).getByTestId("check").textContent).toContain("brindle checked it");
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
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { medium: { model: "careful-dev", fallbacks: ["developer", "developer-codex", "developer-heavy"] } } });
    expect(chain("medium")).toBe("Sonnet 5.5, then GPT-5.5, then Opus 5.5");
    // The builder closed; the row's list is open with the new agent in it, marked new.
    expect(within(row("medium")).queryByRole("region", { name: "New custom agent" })).toBeNull();
    const list = within(row("medium")).getByRole("group", { name: "Choices for Medium" });
    expect(option(list, "careful-dev")!.textContent).toContain("new");
    // Every other row's list offers it too.
    fireEvent.click(runsOn("reviewer"));
    expect(option(within(row("reviewer")).getByRole("group", { name: "Choices for Reviewer" }), "careful-dev")).toBeTruthy();
    expect(screen.getByTestId("custom-agents").textContent).toContain("careful-dev");
    stop();
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
    await waitFor(() => expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { medium: { instructions: "Keep each commit to one step." } } }));
    await waitFor(() => expect(reloads()).toBeGreaterThan(before));
    await flush();

    // Blank text is a clear: it goes out as null, never "".
    fireEvent.change(box, { target: { value: "  " } });
    fireEvent.blur(box);
    await waitFor(() => expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { medium: { instructions: null } } }));
    // The daemon refuses blank text sent directly; only null clears.
    for (const instructions of ["", "   "]) {
      await expect(client.putRouting({ repo: REPO, scope: "local", kinds: { medium: { instructions } } })).rejects.toThrow();
    }
    stop();
    cleanup();

    const again = mount(daemon);
    await waitFor(() => expect(within(row("medium")).getByRole("button", { name: /^More/ })).toBeTruthy());
    fireEvent.click(within(row("medium")).getByRole("button", { name: /^More/ }));
    expect((within(row("medium")).getByLabelText("Instructions") as HTMLTextAreaElement).value).toBe("");
    again.stop();
  });

  it("agents: a provider that isn't connected is skipped, and its Connect opens Settings › Providers; the footer is Knowledge · Agents · Settings · Account", async () => {
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
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    render(
      <PtyViewsProvider value={standInViews}>
        <App store={store} client={client} refresh={sync.refresh} />
      </PtyViewsProvider>,
    );
    const pages = await screen.findByRole("navigation", { name: "Pages" });
    expect(within(pages).getAllByRole("button").map((x) => x.textContent)).toEqual(["Knowledge", "Agents", "Settings", "Account"]);
    expect(pages.className).toContain("flex-nowrap");
    fireEvent.click(within(pages).getByRole("button", { name: "Agents" }));
    await screen.findByTestId("kind-medium");

    // Medium's GPT-5.5 can't run: Sonnet 5.5 runs it for now; Light's quick-fix fallback is skipped.
    expect(within(row("medium")).getByTestId("cant-run").textContent).toContain("GPT-5.5 can't run: Codex is signed out. Sonnet 5.5 runs it until then.");
    openFallbacks("light");
    expect(within(row("light")).getByRole("list", { name: "Light fallbacks" }).querySelector('li[data-choice="quick-fix"]')!.textContent).toContain("skipped: Codex is signed out");
    // Its models aren't offered.
    fireEvent.click(runsOn("advanced"));
    const list = within(row("advanced")).getByRole("group", { name: "Choices for Advanced" });
    expect(option(list, "developer-codex")).toBeNull();
    expect(option(list, "quick-fix")).toBeNull();
    expect(list.querySelector('[data-missing="codex"]')).toBeTruthy();

    fireEvent.click(within(within(row("medium")).getByTestId("cant-run")).getByRole("button", { name: "Connect Codex" }));
    expect(await screen.findByRole("region", { name: "Settings" })).toBeTruthy();
    expect(within(pages).getByRole("button", { name: "Settings" }).getAttribute("aria-current")).toBe("page");
    sync.stop();
  });

  it("agents: a save that ends after the page moved to another repo doesn't reload the old repo into it", async () => {
    const OTHER = "/home/u/other";
    const held: Array<() => void> = [];
    const hold = (daemon: FakeDaemon): typeof globalThis.fetch => (input, init) =>
      init?.method === "PUT" ? new Promise((resolve) => held.push(() => resolve(daemon.fetch(input, init)))) : daemon.fetch(input, init);
    const { daemon, stop } = setup(
      {
        repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }],
        // The other repo runs Medium on Codex.
        routingByRepo: { [OTHER]: { local: { routing: { medium: ["developer-codex", "developer"] } } } },
      },
      hold,
    );
    await screen.findByTestId("kind-medium");
    expect(runsOn("medium").dataset.choice).toBe("developer");

    // A save for proj, still in flight when the page moves to other.
    fireEvent.click(lock("advanced"));
    await waitFor(() => expect(held.length).toBe(1));
    fireEvent.click(within(screen.getByRole("group", { name: "Repo" })).getByRole("button", { name: "other" }));
    await waitFor(() => expect(runsOn("medium").dataset.choice).toBe("developer-codex"));
    const before = daemon.requests.length;

    held.splice(0).forEach((go) => go());
    await flush();
    await flush();
    // proj isn't read again, and other stays on screen, editable.
    expect(daemon.requests.slice(before).filter((r) => r.path === "/routing" && r.method === "GET" && r.query.repo === REPO)).toEqual([]);
    expect(runsOn("medium").dataset.choice).toBe("developer-codex");
    expect((lock("advanced") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
    stop();
  });

  it("agents: the reviewer and supervisor rows have the same picker, ordered fallbacks and custom agents as the sizes", async () => {
    const { daemon, stop } = setup();
    await screen.findByTestId("kind-supervisor");
    expect(runsOn("reviewer").dataset.choice).toBe("reviewer-codex");
    expect(chain("reviewer")).toBe("Opus 5.5");
    expect(runsOn("supervisor").dataset.choice).toBe("developer-heavy");
    expect(chain("supervisor")).toBe("Sonnet 5.5");

    // The reviewer's fallbacks: added from the same inline list, reordered.
    fireEvent.click(within(openFallbacks("reviewer")).getByRole("button", { name: /Add a fallback/ }));
    fireEvent.click(option(within(row("reviewer")).getByRole("group", { name: "Choices for Reviewer fallbacks" }), "quick-fix")!);
    await waitFor(() => expect(chain("reviewer")).toBe("Opus 5.5, then quick-fix"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { reviewer: { fallbacks: ["reviewer", "quick-fix"] } } });
    fireEvent.click(within(openFallbacks("reviewer")).getByRole("button", { name: "Move quick-fix earlier" }));
    await waitFor(() => expect(chain("reviewer")).toBe("quick-fix, then Opus 5.5"));

    // The supervisor picks a model like any row; what ran it becomes its first fallback.
    fireEvent.click(runsOn("supervisor"));
    const list = within(row("supervisor")).getByRole("group", { name: "Choices for Supervisor" });
    expect(option(list, "developer-fable")!.textContent).toMatch(/Fable 5\.1.*\$\$\$slowest/);
    fireEvent.click(option(list, "developer-fable")!);
    await waitFor(() => expect(runsOn("supervisor").dataset.choice).toBe("developer-fable"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "local", kinds: { supervisor: { model: "developer-fable", fallbacks: ["developer-heavy", "developer"] } } });
    expect(chain("supervisor")).toBe("Opus 5.5, then Sonnet 5.5");

    // A custom agent made from the supervisor's list is for the supervisor.
    fireEvent.click(runsOn("supervisor"));
    fireEvent.click(within(row("supervisor")).getByRole("button", { name: /Make a custom agent/ }));
    const use = within(within(row("supervisor")).getByRole("region", { name: "New custom agent" })).getByRole("group", { name: "Use it for" });
    expect(within(use).getByRole("button", { name: "Supervisor" }).getAttribute("aria-pressed")).toBe("true");
    stop();
  });

  it("agents: whether each agent can run comes from its own sign-in and exclusion, with the reason", async () => {
    // Codex is signed in for its developer, but the reviewer runs under another Codex identity that's signed out; Antigravity is excluded.
    const mixed = [...profiles.map((p) => (p.name === "reviewer-codex" ? { ...p, signed_in: false } : p)), rp("developer-gemini", "antigravity", "gemini-3-pro", "high")];
    const config = data().routingConfig!;
    const { onNavigate, stop } = setup({
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
    const gemini = heavy.querySelector<HTMLElement>('li[data-choice="developer-gemini"]')!;
    expect(gemini.textContent).toContain("skipped: Antigravity is excluded from routing");
    fireEvent.click(runsOn("advanced"));
    const advanced = within(row("advanced")).getByRole("group", { name: "Choices for Advanced" });
    expect(option(advanced, "developer-gemini")).toBeNull();
    expect(advanced.querySelector('[data-choice-off="developer-gemini"]')!.textContent).toContain("Antigravity is excluded from routing");
    stop();
  });

  it("agents: two broken agents in one row each open their own problems", async () => {
    const broken = [
      ...profiles.map((p) => (p.name === "quick-fix" ? { ...p, lint_errors: ["gpt-4.1 isn't one of Codex's models any more."] } : p)),
      rp("slow-fix", "claude", "opus-5.5", "high", { lint_errors: ["Rule pack python-styl isn't installed.", "The prompt is empty."] }),
    ];
    const config = data().routingConfig!;
    const { stop } = setup({ routingProfiles: broken, routingConfig: { ...config, user: { ...config.user, routing: { ...config.user.routing, light: ["developer-light", "quick-fix", "slow-fix"] } } } });
    await screen.findByTestId("kind-light");
    const list = openFallbacks("light");
    const toggle = (id: string) => within(list.querySelector<HTMLElement>(`li[data-choice="${id}"]`)!).getByTestId("problems");
    expect(toggle("quick-fix").textContent).toContain("1 problem");
    expect(toggle("slow-fix").textContent).toContain("2 problems");
    const shown = () => [...row("light").querySelectorAll<HTMLElement>("[data-problems]")].map((el) => el.dataset.problems);

    fireEvent.click(toggle("slow-fix"));
    expect(shown()).toEqual(["slow-fix"]);
    const slow = row("light").querySelector('[data-problems="slow-fix"]')!.textContent;
    expect(slow).toContain("Rule pack python-styl isn't installed.");
    expect(slow).not.toContain("gpt-4.1");
    expect(toggle("slow-fix").getAttribute("aria-expanded")).toBe("true");
    expect(toggle("quick-fix").getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle("quick-fix"));
    expect(shown()).toEqual(["quick-fix"]);
    expect(row("light").querySelector('[data-problems="quick-fix"]')!.textContent).toContain("gpt-4.1 isn't one of Codex's models any more.");

    fireEvent.click(toggle("quick-fix"));
    expect(shown()).toEqual([]);
    stop();
  });

  it("agents: a broken custom agent shows its automatic check's problems where it's used, with no lint button", async () => {
    const broken = profiles.map((p) => (p.name === "quick-fix" ? { ...p, model: "gpt-4.1", lint_errors: ["gpt-4.1 isn't one of Codex's models any more.", "Rule pack python-styl isn't installed."] } : p));
    const config = data().routingConfig!;
    const { stop } = setup({ routingProfiles: broken, routingConfig: { ...config, user: { ...config.user, routing: { ...config.user.routing, advanced: ["quick-fix", "developer-fable"] } } } });
    await screen.findByTestId("kind-light");

    // Where it runs first: "2 problems", and the next one runs instead.
    expect(within(row("advanced")).getByTestId("problems").textContent).toContain("2 problems");
    expect(within(row("advanced")).getByTestId("cant-run").textContent).toContain("quick-fix can't run: 2 problems. Fable 5.1 runs it until then.");
    expect(within(within(row("advanced")).getByTestId("cant-run")).queryByRole("button", { name: /Connect/ })).toBeNull();
    // Where it's a fallback: skipped, struck, with the problems behind its count.
    expect(within(row("light")).getByTestId("chain").querySelector(".line-through")!.textContent).toBe("quick-fix");
    const list = openFallbacks("light");
    const entry = list.querySelector<HTMLElement>('li[data-choice="quick-fix"]')!;
    expect(entry.textContent).toContain("skipped: 2 problems");
    fireEvent.click(within(entry).getByTestId("problems"));
    const found = row("light").querySelector<HTMLElement>('[data-problems="quick-fix"]')!;
    expect(found.textContent).toContain("gpt-4.1 isn't one of Codex's models any more.");
    expect(found.textContent).toContain("Rule pack python-styl isn't installed.");
    expect(found.textContent).toContain("routing skips it and uses the next fallback");
    // It isn't offered in a list until it's fixed, and the custom agents line counts its problems.
    fireEvent.click(runsOn("medium"));
    expect(option(within(row("medium")).getByRole("group", { name: "Choices for Medium" }), "quick-fix")).toBeNull();
    expect(screen.getByTestId("custom-agents").textContent).toContain("quick-fix (2 problems)");
    // Checks are automatic: there's no lint or check button anywhere.
    expect(screen.queryByRole("button", { name: /lint|check every|check agents/i })).toBeNull();
    stop();
  });
});
