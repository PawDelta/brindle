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

/* How the engine names each model (brindle.model_names): the app shows these, never its own reading of an id. */
const DISPLAY: Record<string, string> = {
  "haiku-5.5": "Haiku 5.5", "sonnet-5.5": "Sonnet 5.5", "opus-5.5": "Opus 5.5", "fable-5.1": "Fable 5.1",
  "gpt-5.5": "GPT-5.5", "gpt-5.5-mini": "GPT-5.5 Mini", "gemini-3-pro": "Gemini 3 Pro",
};
const MAKER: Record<string, [string, string]> = { claude: ["Anthropic", "Claude Code"], codex: ["OpenAI", "Codex"], antigravity: ["Google", "Antigravity"], opencode: ["OpenCode", "OpenCode"] };

/* The role the engine gives each profile here (profiles.base_role); any other is a developer. */
const ROLE: Record<string, string> = { reviewer: "reviewer", "reviewer-codex": "reviewer", supervisor: "supervisor" };

/* A built-in profile, unless `builtin: false` says it's the person's own. */
const rp = (name: string, provider: string, model: string | null, cost_tier: string, fields: Partial<RoutingProfile> = {}): RoutingProfile => ({
  name,
  provider,
  model,
  display: model ? DISPLAY[model] : MAKER[provider][1],
  maker: MAKER[provider][0],
  tool: MAKER[provider][1],
  builtin: true,
  role: ROLE[name] ?? "developer",
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
  rp("quick-fix", "codex", "gpt-5.5-mini", "low", { builtin: false, kind: "pinned" }),
  // Agents: how to work, no model of their own (profiles.kind_of "agent").
  rp("qa", "claude", null, "medium", { kind: "agent", description: "Writes and fixes tests, never the code under test", rules: ["tests-first"] }),
  rp("backend-auditor", "claude", null, "medium", { kind: "agent", role: "reviewer", description: "Reviews a branch as a backend security audit" }),
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

const NAMES: Record<string, string> = { light: "Light", medium: "Medium", heavy: "Heavy", advanced: "Advanced" };
const row = (k: string) => screen.getByTestId(`kind-${k}`);
const runsOn = (k: string) => within(row(k)).getByRole("button", { name: `${NAMES[k]} runs on` });
/* What takes over at the size's limit, in order, by name. */
const chain = (k: string) => [...within(row(k)).getByTestId("chain").querySelectorAll("[data-chain-label]")].map((el) => el.textContent).join(", then ");
const skipped = (k: string, id: string) => within(row(k)).getByTestId("chain").querySelector<HTMLElement>(`[data-chain="${id}"]`)?.dataset.skipped;
/* The lock against learning is in the size's Edit. */
const lock = (k: string) => {
  const edit = within(row(k)).getByRole("button", { name: `Edit ${NAMES[k]}` });
  if (edit.getAttribute("aria-expanded") !== "true") fireEvent.click(edit);
  return within(row(k)).getByTestId("lock");
};
const need = (key: string) => screen.getByTestId("needs-you").querySelector<HTMLElement>(`[data-need="${key}"]`);
const agent = (name: string) => screen.getByTestId(`agent-${name}`);
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
  it("agents: lists the agents apart from the job sizes, then picks a size's model from the connected providers only, with cost and speed", async () => {
    const { daemon } = setup();
    await screen.findByTestId("kind-medium");
    expect(screen.getAllByTestId(/^kind-/).map((el) => el.dataset.testid)).toEqual(["kind-light", "kind-medium", "kind-heavy", "kind-advanced"]);
    // Who does the work: the agents, built-ins first, the person's own after; a custom profile that names a model runs its own.
    expect(screen.getAllByTestId(/^agent-/).map((el) => [el.dataset.testid, el.dataset.modelMode])).toEqual([
      ["agent-qa", "follows"],
      ["agent-backend-auditor", "follows"],
      ["agent-quick-fix", "own"],
    ]);

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

  it("agents: a built-in profile the app has never heard of is named by the engine's display name, not its id, and isn't a custom agent", async () => {
    const sol = rp("developer-codex-sol", "codex", "gpt-6.1-sol", "medium", { display: "GPT‑6.1 Sol", effort: "high" });
    const config = data().routingConfig!;
    setup({ routingProfiles: [...profiles, sol], routingConfig: { ...config, user: { ...config.user, routing: { ...config.user.routing, medium: ["developer-codex-sol", "developer"] } } } });
    await screen.findByTestId("kind-medium");

    const name = runsOn("medium").querySelector<HTMLElement>("[data-model-name]");
    expect(name?.dataset.modelName).toBe("GPT‑6.1 Sol");
    expect(runsOn("medium").textContent).not.toContain("developer-codex-sol");
    expect(runsOn("medium").querySelector('[data-effort="high"]')).toBeTruthy();
    // The quick-fix custom agent is shown by its own name, with no model name standing in for it.
    expect(within(row("light")).getByTestId("chain").textContent).toContain("quick-fix");
    fireEvent.click(runsOn("light"));
    const light = within(row("light")).getByRole("group", { name: "Choices for Light" });
    expect(option(light, "quick-fix")!.querySelector("[data-model-name]")).toBeNull();
    expect(option(light, "developer-codex-sol")!.querySelector("[data-model-name]")).toBeTruthy();
  });

  it("agents: renamed built-in profiles are offered by the role the engine gives them, and a reviewing agent keeps its own model for a size", async () => {
    const opus = rp("dev.claude-opus", "claude", "opus-5.5", "high", { role: "developer" });
    const sonnetHigh = rp("review.claude-sonnet.high", "claude", "sonnet-5.5", "medium", { role: "reviewer", effort: "high" });
    const { daemon } = setup({ routingProfiles: [...profiles, opus, sonnetHigh] });
    await screen.findByTestId("kind-medium");

    fireEvent.click(runsOn("medium"));
    const medium = within(row("medium")).getByRole("group", { name: "Choices for Medium" });
    expect(option(medium, "dev.claude-opus")).toBeTruthy();
    expect(option(medium, "review.claude-sonnet.high")).toBeNull();
    // An agent is never a model to pick.
    expect(option(medium, "backend-auditor")).toBeNull();

    // A built-in agent that reviews: its own model for Light comes from the reviewers, never the developers.
    fireEvent.click(within(agent("backend-auditor")).getByRole("button", { name: "Edit Backend auditor" }));
    fireEvent.click(within(agent("backend-auditor")).getByRole("button", { name: "Backend auditor Light model" }));
    const own = within(agent("backend-auditor")).getByRole("group", { name: "Choices for Backend auditor Light" });
    expect(option(own, "dev.claude-opus")).toBeNull();
    fireEvent.click(option(own, "review.claude-sonnet.high")!);
    await waitFor(() => expect(agent("backend-auditor").dataset.modelMode).toBe("except"));
    // Its order for Light: the one picked, then the shared Light order behind it.
    expect(puts(daemon).at(-1)).toEqual({
      repo: REPO,
      scope: "user",
      agents: { "backend-auditor": { models: { light: { model: "review.claude-sonnet.high", fallbacks: ["developer-light", "quick-fix", "developer"] } } } },
    });

    // Back to following the job size: the size's override is cleared.
    fireEvent.click(within(agent("backend-auditor")).getByRole("button", { name: "Backend auditor Light model" }));
    fireEvent.click(within(agent("backend-auditor")).getByRole("button", { name: "Follow the job size" }));
    await waitFor(() => expect(agent("backend-auditor").dataset.modelMode).toBe("follows"));
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", agents: { "backend-auditor": { models: { light: null } } } });
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

  it("agents: creating an agent from the form sends no provider or model, and its row follows the job size", async () => {
    const { daemon, onNavigate } = setup();
    await screen.findByTestId("kind-medium");
    fireEvent.click(screen.getByRole("button", { name: "New agent" }));
    const form = screen.getByRole("region", { name: "New agent" });
    const create = within(form).getByRole("button", { name: "Create agent" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);

    // Name, what it's for, instructions and rules: no model to pick anywhere.
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "careful-dev" } });
    fireEvent.change(within(form).getByLabelText("What it's for"), { target: { value: "Feature work, tests first, small commits" } });
    fireEvent.change(within(form).getByLabelText("Instructions"), { target: { value: "Write the failing test first." } });
    const rules = within(form).getByRole("group", { name: "Rules" });
    fireEvent.click(within(rules.querySelector<HTMLElement>('[data-rule="tests-first"]')!).getByRole("checkbox"));
    fireEvent.click(within(rules).getByRole("button", { name: "See all rules" }));
    expect(onNavigate).toHaveBeenLastCalledWith("knowledge");
    expect(within(form).getByTestId("defaults").dataset).toMatchObject({ worksLike: "developer", modelMode: "follows" });
    noPopups();
    fireEvent.click(create);

    await waitFor(() => expect(agent("careful-dev").dataset.modelMode).toBe("follows"));
    expect(daemon.requests.find((r) => r.method === "POST" && r.path === "/profiles")?.body).toEqual({
      repo: REPO,
      name: "careful-dev",
      description: "Feature work, tests first, small commits",
      works_like: "developer",
      instructions: "Write the failing test first.",
      rules: ["tests-first"],
    });
    // It follows the shared table: no per-size order written, and it's never offered as a size's model.
    expect(puts(daemon)).toEqual([]);
    expect(agent("careful-dev").dataset.custom).toBe("true");
    expect(screen.queryByRole("region", { name: "New agent" })).toBeNull();
    fireEvent.click(runsOn("medium"));
    expect(option(within(row("medium")).getByRole("group", { name: "Choices for Medium" }), "careful-dev")).toBeNull();
  });

  it("agents: a new agent's own model leaves out refused fallbacks, and when that write fails the agent stays made and Save retries only the models", async () => {
    // The org refuses Codex's small model (quick-fix, in the shared Light order); the first models write fails anyway.
    let failures = 1;
    const flaky = (d: FakeDaemon): typeof globalThis.fetch => (input, init) =>
      init?.method === "PUT" && String(input).includes("/routing") && failures-- > 0
        ? Promise.resolve(new Response(JSON.stringify({ error: { code: "unavailable", message: "the daemon is busy" } }), { status: 503, headers: { "content-type": "application/json" } }))
        : d.fetch(input, init);
    const { daemon } = setup({ routingPolicy: { org_id: "acme", allowed_models: ["haiku-5.5", "sonnet-5.5", "opus-5.5", "fable-5.1", "gpt-5.5"] } }, flaky);
    await screen.findByTestId("kind-medium");
    fireEvent.click(screen.getByRole("button", { name: "New agent" }));
    const form = screen.getByRole("region", { name: "New agent" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "careful-dev" } });
    fireEvent.change(within(form).getByLabelText("What it's for"), { target: { value: "Careful features" } });
    fireEvent.click(within(form).getByRole("button", { name: "Change" }));
    fireEvent.click(within(within(form).getByRole("group", { name: "Model" })).getByRole("button", { name: "Its own for a size…" }));
    fireEvent.click(within(form).getByRole("button", { name: "careful-dev Light model" }));
    fireEvent.click(option(within(form).getByRole("group", { name: "Choices for careful-dev Light" }), "developer-heavy")!);
    fireEvent.click(within(form).getByRole("button", { name: "Create agent" }));

    // Made, but its models weren't saved: it's listed, and the form stays to save only those again.
    await waitFor(() => expect(agent("careful-dev").dataset.modelMode).toBe("follows"));
    expect(within(form).getByTestId("form-error")).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: "Save its models" }));
    await waitFor(() => expect(agent("careful-dev").dataset.modelMode).toBe("except"));
    expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/profiles")).toHaveLength(1);
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", agents: { "careful-dev": { models: { light: { model: "developer-heavy", fallbacks: ["developer-light", "developer"] } } } } });
    expect(screen.queryByRole("region", { name: "New agent" })).toBeNull();
  });

  it("agents: editing one of your agents shows its saved instructions and role, clears them on purpose, and a new role re-checks its own models", async () => {
    const tidy = rp("tidy-dev", "claude", null, "medium", { kind: "agent", builtin: false, description: "Tidies code" });
    const { daemon } = setup({
      routingProfiles: [...profiles, tidy],
      profiles: [...described, { name: "tidy-dev", provider: "claude", model: null, description: "Tidies code" }],
      // GET /profiles/{name}: the resolved prompt carries what it extends; `instructions` is its own text.
      profileDetails: { "tidy-dev": { extends: ["qa", "developer"], prompt: "Implement the task you are given.\n\nKeep diffs small.", instructions: "Keep diffs small." } },
    });
    await screen.findByTestId("agent-tidy-dev");
    fireEvent.click(within(agent("tidy-dev")).getByRole("button", { name: "Edit tidy-dev" }));
    const form = within(agent("tidy-dev")).getByRole("region", { name: "Edit tidy-dev" });
    const box = within(form).getByLabelText("Instructions") as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe("Keep diffs small."));
    expect(within(form).getByTestId("defaults").dataset.worksLike).toBe("qa");

    // Its own Light model, as a developer would run…
    fireEvent.click(within(form).getByRole("button", { name: "Change" }));
    fireEvent.click(within(within(form).getByRole("group", { name: "Model" })).getByRole("button", { name: "Its own for a size…" }));
    fireEvent.click(within(form).getByRole("button", { name: "tidy-dev Light model" }));
    fireEvent.click(option(within(form).getByRole("group", { name: "Choices for tidy-dev Light" }), "developer-heavy")!);
    expect(within(form).getByTestId("defaults").dataset.modelMode).toBe("except");
    // …goes when it works like a Reviewer, whose models are the reviewers'.
    fireEvent.click(within(within(form).getByRole("group", { name: "Works like" })).getByRole("button", { name: "Reviewer" }));
    expect(within(form).getByTestId("defaults").dataset.modelMode).toBe("follows");
    fireEvent.click(within(form).getByRole("button", { name: "tidy-dev Light model" }));
    const light = within(form).getByRole("group", { name: "Choices for tidy-dev Light" });
    expect(option(light, "reviewer-codex")).toBeTruthy();
    expect(option(light, "developer-heavy")).toBeNull();

    // Emptied on purpose: sent as "", so the saved instructions are cleared.
    fireEvent.change(box, { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(agent("tidy-dev")).queryByRole("region", { name: "Edit tidy-dev" })).toBeNull());
    expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/profiles/tidy-dev")?.body).toEqual({
      repo: REPO, description: "Tidies code", rules: [], instructions: "", works_like: "reviewer",
    });
    expect(puts(daemon)).toEqual([]);
  });

  it("agents: clearing a size's Edit instructions sends null, the daemon refuses blank text, and a reload shows them empty", async () => {
    const { daemon, client, stop } = setup();
    const more = () => within(row("medium")).getByRole("button", { name: "Edit Medium" });
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
    await waitFor(() => expect(within(row("medium")).getByRole("button", { name: "Edit Medium" })).toBeTruthy());
    fireEvent.click(within(row("medium")).getByRole("button", { name: "Edit Medium" }));
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

    // Medium's GPT-5.5 can't run: Sonnet 5.5 stands in for it; Light's quick-fix fallback is skipped.
    expect(runsOn("medium").dataset).toMatchObject({ choice: "developer", standingIn: "true" });
    expect(skipped("light", "quick-fix")).toBe("signed_out");
    openFallbacks("light");
    // Its models aren't offered.
    fireEvent.click(runsOn("advanced"));
    const list = within(row("advanced")).getByRole("group", { name: "Choices for Advanced" });
    expect(option(list, "developer-codex")).toBeNull();
    expect(option(list, "quick-fix")).toBeNull();
    expect(list.querySelector('[data-missing="codex"]')).toBeTruthy();

    // Needs you: Codex is signed out, with the one way to fix it.
    fireEvent.click(within(need("provider-codex")!).getByRole("button", { name: "Sign in" }));
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
    // Codex is signed in for its developer, but a second Codex profile runs under another identity that's signed out; Antigravity is excluded.
    const work = rp("developer-codex-work", "codex", "gpt-5.5", "medium", { display: "GPT-5.5 (work)", signed_in: false });
    const mixed = [...profiles, work, rp("developer-gemini", "antigravity", "gemini-3-pro", "high")];
    const config = data().routingConfig!;
    const { onNavigate } = setup({
      routingProfiles: mixed,
      routingConfig: {
        ...config,
        user: { ...config.user, exclude_providers: ["antigravity"], routing: { ...config.user.routing, medium: ["developer-codex-work", "developer"], heavy: ["developer-heavy", "developer-gemini", "developer-codex"] } },
      },
    });
    await screen.findByTestId("kind-medium");

    // Its own Codex sign-in is out: it's skipped, with why, and the next one stands in.
    expect(runsOn("medium").dataset).toMatchObject({ choice: "developer", standingIn: "true" });
    expect(within(row("medium")).getByTestId("first-pick").textContent).toContain("Codex is signed out for it");
    fireEvent.click(within(need("provider-codex")!).getByRole("button", { name: "Sign in" }));
    expect(onNavigate).toHaveBeenLastCalledWith("providers");
    // Codex's signed-in developer is still offered; the signed-out one is listed, dimmed, with its reason.
    fireEvent.click(runsOn("medium"));
    const medium = within(row("medium")).getByRole("group", { name: "Choices for Medium" });
    expect(option(medium, "developer-codex")).toBeTruthy();
    expect(option(medium, "developer-codex-work")).toBeNull();
    expect(medium.querySelector('[data-choice-off="developer-codex-work"]')!.textContent).toContain("Codex is signed out for it");
    expect(medium.querySelector('[data-missing="codex"]')).toBeNull();

    // An excluded provider's agent is skipped where it's a fallback and can't be picked, with why; there's nothing to connect.
    expect(skipped("heavy", "developer-gemini")).toBe("excluded");
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
    const { daemon } = setup({ routingProfiles: broken, routingConfig: { ...config, user: { ...config.user, routing: { ...config.user.routing, advanced: ["quick-fix", "developer-fable"] } } } });
    await screen.findByTestId("kind-light");

    // Where it runs first: the next one stands in, and Needs you offers to put that one first, with nothing to connect.
    expect(runsOn("advanced").dataset).toMatchObject({ choice: "developer-fable", standingIn: "true" });
    const fix = need("size-advanced")!;
    expect(within(fix).queryByRole("button", { name: /Connect|Sign in/ })).toBeNull();
    // Where it's a fallback: skipped, with the problems behind its count.
    expect(skipped("light", "quick-fix")).toBe("problems");
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

    // Putting the stand-in first moves the broken one behind it, and the need goes.
    fireEvent.click(within(need("size-advanced")!).getByRole("button", { name: "Put Fable 5.1 first" }));
    await waitFor(() => expect(runsOn("advanced").dataset.standingIn).toBeUndefined());
    expect(puts(daemon).at(-1)).toEqual({ repo: REPO, scope: "user", kinds: { advanced: { model: "developer-fable", fallbacks: ["quick-fix"] } } });
    expect(document.querySelector('[data-need="size-advanced"]')).toBeNull();
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
