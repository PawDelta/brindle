import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../../api/client";
import { DaemonContext } from "../../api/context";
import { scopesFor } from "../../api/permissions";
import type { AskDTO, AskDecisionDTO, AskGroupDTO, InboxItemDTO, SessionDTO } from "../../api/schema";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { BASE_URL, FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { REPO, permission, permissionInTerminal, question, root, waiting, worker } from "../../test/fixtures";
import { releaseNotesUrl, type AppUpdateStatus, type AppUpdates } from "../../api/tauri";
import { watchAppUpdates } from "../../store/appUpdates";
import { Permissions } from "../Settings/Permissions";
import { InboxScreen, type InboxScreenProps } from "./InboxScreen";
import { watchInbox } from "./notify";

afterEach(cleanup);

const notice: InboxItemDTO = { kind: "org_notice", reason: "Reviewer profiles now default to reviewer-codex.", detail: "admin", notice_id: "n-18" };
const prReady: InboxItemDTO = { kind: "pr_ready", reason: "PR ready for your approval", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "Ship ls --json" };
const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

function mount(daemon: FakeDaemon, client: DaemonClient = daemon.client(), props: Partial<InboxScreenProps> = {}) {
  const store = createAppStore();
  const sync = startSync({ client, store, connect: daemon.connect });
  const onOpen = vi.fn();
  const onOpenRule = vi.fn();
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <InboxScreen client={client} refresh={sync.refresh} onOpen={onOpen} onOpenRule={onOpenRule} {...props} />
        </SessionsProvider>
      </StoreContext.Provider>
    </DaemonContext.Provider>,
  );
  return { store, sync, onOpen, onOpenRule };
}

/* Held requests as GET /permissions/asks groups them: two pushes into main (w1's is the `permission` fixture's
   hold) under the push rule's 30-minute limit, and a read the credential deny blocks. */
const now = Date.now() / 1000;
const limits = { max_scope: "destination", life: "time", minutes: 30 };
const ask = (agent_id: string, request_id: string, kind: string, exact: string, since: number, rule: string, blocked = false): AskDTO => ({
  agent_id, root_id: "sup1", request_id, digest: `d-${request_id}`, repo: REPO, kind, tool: kind === "read" ? "Read" : "Bash", exact,
  summary: exact, waiting_since: now - since, worker: agent_id === "w1" ? "developer" : "supervisor", rule, blocked, limits: blocked ? null : limits,
});
const pushes: AskGroupDTO = {
  key: `${REPO}|push|* → main @ origin`, repo: REPO, kind: "push", title: "Allow pushes into main?", general: scopesFor("push", "fix/login → main @ origin")[1],
  scopes: scopesFor("push", "fix/login → main @ origin"), blocked: false, rule: "d-push-default", limits, modes: ["once", "deny", "always-deny", "timed"],
  items: [ask("w1", "req-1", "push", "fix/login → main @ origin", 600, "d-push-default"), ask("sup1", "req-2", "push", "chore/ci → main @ origin", 120, "d-push-default")],
};

const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST");
/* The one card an item gets, whatever its kind. */
const card = (kind: string) => document.querySelector<HTMLElement>(`[data-testid="inbox-card"][data-kind="${kind}"]`)!;

describe("inbox", () => {
  it("groups the queue by session as banner cards, each going to its session and agent; an org notice is a card to read", async () => {
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: [session],
      workspaces: [root, worker],
      inbox: [question, waiting, notice, prReady],
    });
    const { sync, onOpen } = mount(daemon);
    const group = await screen.findByRole("region", { name: "Ship ls --json" });
    expect(within(group).getAllByTestId("inbox-card")).toHaveLength(3);
    // The PR brindle prepared (its checks passed, nothing pushed): Review & open PR opens the session's sheet.
    expect(card("pr_ready").textContent).toContain("PR ready for your approval");
    fireEvent.click(within(card("pr_ready")).getByRole("button", { name: "Review & open PR" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1", { kind: "review" });
    expect(group.textContent).toContain("sup1 · proj");
    fireEvent.click(within(group).getByRole("button", { name: "Open the session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO);

    // A card: the kind, what is asked, who (the worker by its branch; the supervisor as such), the session, the age,
    // and Go to session: the agent in its session with the item's sheet over it, once; nothing is answered on
    // a card that only links.
    const w = card("waiting");
    expect(w.textContent).toContain("waiting");
    expect(w.textContent).toContain("Waiting on your input in its terminal");
    expect(w.textContent).toContain("ls · feat/ls");
    expect(w.textContent).toContain("answered in its session");
    expect(within(w).queryByRole("group")).toBeNull();
    fireEvent.click(within(w).getByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "w1");
    expect(card("question").textContent).toContain("supervisor");
    fireEvent.click(within(card("question")).getByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1");
    expect(screen.queryByRole("dialog")).toBeNull();

    // An org notice has no session: a card to read, with Mark read on it.
    expect(card("org_notice").textContent).toContain("from your org's admin");
    expect(within(card("org_notice")).queryByRole("button", { name: "Go to session ›" })).toBeNull();
    fireEvent.click(within(card("org_notice")).getByRole("button", { name: "Mark read" }));
    await waitFor(() => expect(posts(daemon)).toEqual([{ method: "POST", path: "/inbox/n-18/read", query: {}, body: {} }]));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Your org" })).toBeNull());
    sync.stop();
  });

  it("starts a sign-in from its card", async () => {
    // As the engine's permission-items branch emits a lapsed sign-in: no detail, the sign-in route named on the
    // item; the worker is on Codex (its record), so the sign-in must be Codex's, never Claude's.
    const codexWorker = { ...worker, agents: [{ ...worker.agents![0], provider: "codex", profile: "developer-codex" }] };
    const credentials: InboxItemDTO = { kind: "credentials", reason: "credentials need attention", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1", detail: null, action: { method: "POST", path: "/v1/providers/codex/login" } };
    const login: InboxItemDTO = { kind: "login", reason: "cloud login expired", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "brindle account login" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, codexWorker], inbox: [credentials, login, notice] });
    const { sync } = mount(daemon);
    await screen.findByRole("region", { name: "Ship ls --json" });

    // A lapsed sign-in: Sign in starts that provider CLI's own login on the engine (Codex, as the engine's route
    // and the worker's record say).
    const signIn = card("credentials");
    expect(signIn.textContent).toContain("Codex needs a sign-in");
    expect(within(signIn).queryByRole("button", { name: "Sign in to Claude" })).toBeNull();
    fireEvent.click(within(signIn).getByRole("button", { name: "Sign in to Codex" }));
    await waitFor(() => expect(daemon.actions.map((a) => a.kind)).toEqual(["login codex"]));
    expect(within(signIn).getByTestId("sign-in-flow")).toBeTruthy();

    // brindle's own cloud sign-in: the card starts it (a job on the daemon; its browser URL is followed) and says so.
    const cloud = card("login");
    fireEvent.click(within(cloud).getByRole("button", { name: "Sign in to brindle" }));
    await waitFor(() => expect(posts(daemon).map((r) => r.path)).toContain("/account/login"));
    await waitFor(() => expect(within(cloud).getByTestId("cloud-login").textContent).toContain("Signed in again"));
    sync.stop();
  });

  it("approves a plan, accepts or declines proposed checks, on the card, at once; a refusal comes back as one line on the card", async () => {
    const plan: InboxItemDTO = { kind: "plan", reason: "plan awaiting approval", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1", detail: "1. Keep the plan.\n2. Ship it." };
    const checks: InboxItemDTO = { kind: "proposed_checks", reason: "checks proposed", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "npm test\nnpm run build" };
    const review: InboxItemDTO = { kind: "review", reason: "to review", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [{ ...session, name: "Login fix" }], workspaces: [root, worker], inbox: [plan, checks, review] });
    const { sync, onOpen } = mount(daemon);
    // A named session is headed by its name, not its goal.
    const group = await screen.findByRole("region", { name: "Login fix" });
    // A plan: Approve alone (changes are asked for in its session), no reply field, no undo; the plan whole on the
    // card, never cut short.
    expect(within(card("plan")).queryByRole("textbox")).toBeNull();
    expect(within(card("plan")).queryByRole("button", { name: /Request changes/ })).toBeNull();
    expect(within(card("plan")).getByTestId("card-detail").textContent).toBe("1. Keep the plan.\n2. Ship it.");
    expect(within(card("plan")).getByTestId("card-detail").className).not.toContain("line-clamp");
    // The plan the engine lists changes: the card shows the new one whole before anything is approved
    // (useInboxActions.test covers an approval of a plan the card no longer shows).
    daemon.data.inbox = daemon.data.inbox.map((i) => (i.kind === "plan" ? { ...i, detail: "1. Keep the plan.\n2. Ship it.\n3. Also delete the database." } : i));
    daemon.publish("message.queued", { agent_id: "w1" });
    await waitFor(() => expect(within(card("plan")).getByTestId("card-detail").textContent).toContain("3. Also delete the database."));
    fireEvent.click(within(card("plan")).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts(daemon)).toEqual([{ method: "POST", path: "/inbox/w1/approve", query: {}, body: { feedback: "" } }]));
    await waitFor(() => expect(within(group).queryAllByTestId("inbox-card").some((c) => c.dataset.kind === "plan")).toBe(false));
    expect(screen.queryByRole("status")).toBeNull();
    // Proposed checks: Accept or Decline. The engine's refusal (nothing waiting any more) comes back on the card.
    daemon.data.inbox = daemon.data.inbox.filter((i) => i.kind !== "proposed_checks");
    fireEvent.click(within(card("proposed_checks")).getByRole("button", { name: "Decline" }));
    await waitFor(() => expect(within(card("proposed_checks")).getByRole("alert").textContent).toContain("no agent sup1"));
    // A branch to review only goes to the session.
    expect(within(card("review")).queryByRole("button", { name: /Request review|Merge/ })).toBeNull();
    expect(card("review").textContent).toContain("handled in its session");
    fireEvent.click(within(card("review")).getByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "w1");
    sync.stop();
  });

  it("self-repair: a setup problem the engine can fix is a card with one Fix, a job on the engine; the card clears when it is done, or shows the job's error", async () => {
    // As the engine's M10 inbox lists it (service/inbox._setup_items): no agent, a fix the app has a button for.
    const setupItem: InboxItemDTO = { kind: "setup", fix: "codex_hook_trust", reason: "Codex's trust of brindle's permission hook was removed from Codex's config", action: { method: "POST", path: "/v1/setup/codex-hook" } };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [setupItem, question] });
    daemon.deferFixes = true;
    daemon.fixFails = true;
    const { sync } = mount(daemon);
    const group = await screen.findByRole("region", { name: "Setup" });
    expect(group.textContent).toContain("brindle on this computer");
    const setup = card("setup");
    expect(setup.textContent).toContain("Codex's trust of brindle's permission hook was removed");
    expect(setup.textContent).toContain("Fix makes Codex trust brindle's permission hook again");
    // One button, inline: no confirmation, no sheet, no Go to session (it has none).
    expect(within(setup).getAllByRole("button").map((b) => b.textContent)).toEqual(["Fix"]);
    fireEvent.click(within(setup).getByRole("button", { name: "Fix" }));
    await waitFor(() => expect(posts(daemon)).toEqual([{ method: "POST", path: "/setup/codex-hook", query: {}, body: {} }]));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("upgrade-card")).toBeNull();
    // While the engine writes Codex's config the button waits, as any card's does while its request is out.
    await waitFor(() => expect(within(setup).getByRole("button", { name: "Fix" })).toHaveProperty("disabled", true));
    // The job fails: its error, in the engine's words, is the one line on the card, and Fix is there to press again.
    daemon.completeFixes();
    await waitFor(() => expect(within(card("setup")).getByRole("alert").textContent).toContain("couldn't trust brindle's Codex hook: codex isn't installed"));
    expect(within(card("setup")).getByRole("button", { name: "Fix" })).toHaveProperty("disabled", false);
    // Pressed again, it goes through: job.done, and the card is gone (the engine no longer lists the problem).
    daemon.fixFails = false;
    fireEvent.click(within(card("setup")).getByRole("button", { name: "Fix" }));
    await waitFor(() => expect(daemon.fixes).toHaveLength(2));
    daemon.completeFixes();
    await waitFor(() => expect(screen.queryByRole("region", { name: "Setup" })).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
    // The question's card is untouched by any of it.
    expect(card("question")).toBeTruthy();
    expect(posts(daemon).map((r) => r.path)).toEqual(["/setup/codex-hook", "/setup/codex-hook"]);
    sync.stop();
  });

  it("one permission card per source: a held request (Deny, Allow once, Allow ▾, details collapsed) and an agent-window prompt (a way to the window), under a one-line stall banner", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission, permissionInTerminal], asks: [pushes] });
    const { sync, onOpen } = mount(daemon);
    const held = await screen.findByRole("article", { name: "Allow pushes into main?" });
    const cards = screen.getAllByTestId("permission-card");
    expect(cards.map((c) => c.dataset.source)).toEqual(["hook", "window"]);

    // The banner is one line with no actions of its own but the way to the first card.
    const stall = screen.getByTestId("stall-notice");
    expect(within(stall).getAllByRole("button").map((b) => b.textContent)).toEqual(["Review all"]);
    // The held card: why it reached the person, the three actions, and the requests under collapsed details.
    expect(within(held).getAllByRole("button").filter((b) => ["Deny", "Allow once", "Allow"].includes(b.textContent ?? "")).map((b) => b.textContent)).toEqual(["Deny", "Allow once", "Allow"]);
    expect(held.querySelector("details")!.open).toBe(false);

    // The prompt only the agent's window can take: no decision, the way to that window instead.
    const prompt = cards[1];
    expect(within(prompt).queryByRole("group", { name: "Decision" })).toBeNull();
    fireEvent.click(within(prompt).getByRole("button", { name: /^Open / }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1");

    fireEvent.click(within(held).getByRole("button", { name: "Allow once" }));
    await waitFor(() => expect(posts(daemon).find((r) => r.path === "/permissions/asks/decide")?.body).toEqual({ key: pushes.key, scope: "general", mode: "once" }));
    sync.stop();
  });

  it.each([
    ["the groups entry's reason", null, "the shell/unchecked baseline is ask"],
    ["the engine's d-shell-syntax rule", "d-shell-syntax", "shell syntax cannot be checked safely"],
  ])("unchecked commands opt-in: the ask (%s) offers only Deny and Allow once, and a link to the Settings row", async (_, rule, reason) => {
    const unchecked: AskGroupDTO = { ...pushes, key: "k-unchecked", title: "Allow this unchecked command?", modes: ["once", "deny"], rule, items: pushes.items.map((a) => ({ ...a, rule, reason })) };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [unchecked] });
    const { sync, onOpenRule } = mount(daemon);
    const held = await screen.findByRole("article", { name: "Allow this unchecked command?" });
    expect(within(held).getAllByRole("button").filter((b) => ["Deny", "Allow once", "Allow"].includes(b.textContent ?? "")).map((b) => b.textContent)).toEqual(["Deny", "Allow once"]);
    fireEvent.click(within(held).getByRole("button", { name: "Stop asking for these…" }));
    expect(within(held).queryByRole("button", { name: /rule/ })).toBeNull();
    expect(onOpenRule).toHaveBeenCalledWith("unchecked-commands");
    sync.stop();
  });

  it("a hold with no group: the request is in the headline, Allow never saves a grant by itself, and a refusal shows on the card", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission, permissionInTerminal] });
    const { sync } = mount(daemon);
    const cards = await screen.findAllByTestId("permission-card");
    expect(cards.map((c) => c.dataset.source)).toEqual(["hook", "window"]);
    // Both requests are named with Details closed.
    expect(cards[0].querySelector("h3")!.textContent).toContain("npx playwright install chromium");
    expect(cards[1].querySelector("h3")!.textContent).toContain("uv run alembic upgrade head");
    // Allow only opens the menu: no choice is sent, least of all Allow always.
    fireEvent.click(within(cards[0]).getByRole("button", { name: "Allow" }));
    expect(within(cards[0]).getByRole("menuitem", { name: /Allow always/ })).toBeTruthy();
    expect(posts(daemon)).toEqual([]);
    // The engine refuses (the request is gone): its reason is on the card.
    daemon.data.inbox = [];
    fireEvent.click(within(cards[0]).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(within(cards[0]).getByRole("alert")).toBeTruthy());
    sync.stop();
  });

  it("a decision out blocks a second one from the menu's duration button", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [pushes] });
    const client = daemon.client();
    let settle!: (got: AskDecisionDTO) => void;
    const decide = vi.spyOn(client, "decidePermissionAsks").mockImplementation(() => new Promise<AskDecisionDTO>((resolve) => (settle = resolve)));
    const { sync } = mount(daemon, client);
    const item = await screen.findByRole("article", { name: "Allow pushes into main?" });
    fireEvent.click(within(item).getByRole("button", { name: "More ways to allow" }));
    fireEvent.click(within(item).getByRole("menuitem", { name: /^For 30 minutes/ }));
    const submit = within(item).getByRole("button", { name: /^Allow for 30 minutes/ });
    // Both clicks land before React commits anything: only the guard can stop the second.
    act(() => {
      fireEvent.click(submit);
      fireEvent.click(submit);
    });
    expect(decide).toHaveBeenCalledTimes(1);
    // Once the first is answered, the next decision goes out.
    await act(async () => settle({ mode: "timed", scope: pushes.general, resumed: [] }));
    await waitFor(() => expect(within(item).getByRole("button", { name: "Allow once" })).toHaveProperty("disabled", false));
    fireEvent.click(within(item).getByRole("button", { name: "Allow once" }));
    expect(decide).toHaveBeenCalledTimes(2);
    sync.stop();
  });

  it("Always for <group item> sets that subcategory's override through the group baselines, then resumes the request", async () => {
    const install: AskGroupDTO = {
      key: `${REPO}|bash|npm install`, repo: REPO, kind: "bash", title: "Allow npm install?", general: scopesFor("bash", "npm install left-pad")[0],
      scopes: scopesFor("bash", "npm install left-pad"), blocked: false, rule: null, limits: null, modes: ["once", "deny", "session"],
      items: [{ ...ask("w1", "req-1", "bash", "npm install left-pad", 60, "d-none"), rule: null, limits: null, reason: "the shell/install baseline is ask" }],
    };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [install] });
    const { sync } = mount(daemon);
    const item = await screen.findByRole("article", { name: "Allow npm install?" });
    expect(within(item).getByTestId("permission-chip")).toBeTruthy();
    fireEvent.click(within(item).getByRole("button", { name: "More ways to allow" }));
    fireEvent.click(within(item).getByRole("menuitem", { name: /^Always for / }));
    await waitFor(() => expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/permissions/groups")?.body).toEqual({ groups: { shell: { subcategories: { install: "allow" } } } }));
    await waitFor(() => expect(daemon.data.asks).toEqual([]));
    // An override, not a saved action.
    expect(daemon.data.policy.saved).toEqual([]);
    expect(daemon.data.policy.groups?.shell.subcategories).toEqual({ install: "allow" });
    sync.stop();
  });

  it("org policy shown to member: an org Ask is labelled with the org and its reason, saves only what the org allows; a Block is one calm notice; Settings shows locked rows", async () => {
    const org = { name: "Acme", what: "local git changes", decision: "ask", reason: "commits are audited" };
    const commits: AskGroupDTO = {
      key: `${REPO}|bash|family: git/local`, repo: REPO, kind: "bash", title: "Allow local git changes?", general: scopesFor("bash", "git add -A")[0],
      scopes: scopesFor("bash", "git add -A"), blocked: false, rule: "org-git-local", limits: { life: "time", minutes: 15 }, modes: ["once", "deny", "always-deny", "timed"], org,
      items: [{ ...ask("w1", "req-1", "bash", "git add -A", 60, "org-git-local"), limits: { life: "time", minutes: 15 } }],
    };
    const blocked: InboxItemDTO = { kind: "org_blocked", reason: "Blocked by Acme policy: installs go through platform review", detail: "3 requests stopped today. Agents were told to work around it." };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission, blocked], asks: [commits] });
    daemon.data.policy.org_controls = [org, { name: "Acme", what: "package installs", decision: "deny", reason: "installs go through platform review" }];
    const { sync } = mount(daemon);
    const item = await screen.findByRole("article", { name: "Allow local git changes?" });
    const why = within(item).getByTestId("org-why");
    expect([why.dataset.org, why.dataset.reason, why.dataset.setting]).toEqual(["Acme", "commits are audited", "ask"]);
    // Offered: what the org allows (no lasting save), and no way into the member's own rules.
    expect(within(item).queryByRole("button", { name: /rule/ })).toBeNull();
    fireEvent.click(within(item).getByRole("button", { name: "More ways to allow" }));
    expect(within(item).getAllByRole("menuitem")).toHaveLength(1);   // a timed approval, its only save
    expect(within(item).getByRole("button", { name: "Allow once" })).toBeTruthy();
    // A Block: one quiet notice in the org's group, however many requests it stopped.
    expect(document.querySelectorAll('[data-kind="org_blocked"]')).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Your org" }).contains(card("org_blocked"))).toBe(true);
    sync.stop();
    cleanup();

    render(<Permissions client={daemon.client()} />);
    const rows = [...(await screen.findByTestId("org-controls")).querySelectorAll<HTMLElement>("[data-locked]")];
    expect(rows.map((r) => [r.dataset.org, r.dataset.what, r.dataset.setting, r.dataset.reason])).toEqual([
      ["Acme", "local git changes", "ask", "commits are audited"], ["Acme", "package installs", "deny", "installs go through platform review"]]);
    expect(rows.flatMap((r) => [...r.querySelectorAll("button, input, select")])).toEqual([]);   // locked: nothing to change
  });

  it("groups a closed worker's running child under its session, as the sidebar does", async () => {
    // The snapshot leaves w1 out (closed) but keeps c1, which w1 started; w1's record says where c1 belongs.
    const child = { ...worker.agents![0], id: "c1", parent_id: "w1" };
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: [session],
      workspaces: [root, { ...worker, agents: [child] }],
      closed: [worker.agents![0]],
      inbox: [{ ...waiting, agent_id: "c1" }],
    });
    const { sync, onOpen } = mount(daemon);
    const group = await screen.findByRole("region", { name: "Ship ls --json" });
    expect(group.textContent).toContain("Waiting on your input");
    fireEvent.click(within(card("waiting")).getByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "c1");
    sync.stop();
  });

  it("shows a failed load with a retry that fetches again, and all clear when nothing waits", async () => {
    const daemon = new FakeDaemon({ inbox: [question] });
    let down = true;
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => (down ? Promise.reject(new TypeError("connection refused")) : daemon.fetch(input, init)),
    });
    const { sync } = mount(daemon, client);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("connection refused"));
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(card("question")).toBeTruthy());
    daemon.data.inbox = [];
    daemon.publish("message.queued", { agent_id: "sup1" });
    await waitFor(() => expect(screen.getByTestId("all-clear")).toBeTruthy());
    sync.stop();
  });

  it("shows the app's own update until restarted, then once until dismissed, counted on the badge too; a late first status never hides it; nothing for an rc or staff build", async () => {
    const idle: AppUpdateStatus = { version: "0.6.0", automatic: true, pending: null, updated: null, last_checked: null, checking: false };
    let announce: (s: AppUpdateStatus) => void = () => {};
    let answer: ((s: AppUpdateStatus) => void) | null = null;
    const updates: AppUpdates = {
      // The status now, answered only when the test says: after an announcement it predates.
      status: () => new Promise((resolve) => (answer = resolve)),
      check: () => Promise.resolve(idle),
      restart: vi.fn(() => Promise.resolve()),
      dismiss: vi.fn(() => Promise.resolve(announce({ ...idle, version: "0.7.0" }))),
      watch: (on) => {
        announce = (s) => act(() => on(s));
        return Promise.resolve(() => {});
      },
    };
    const openUrl = vi.fn(() => Promise.resolve());
    const { store, sync } = mount(new FakeDaemon({ inbox: [] }), undefined, { appUpdates: updates, openUrl });
    const counts: number[] = [];
    watchInbox(store, { notify: () => {}, count: (n) => counts.push(n) });
    watchAppUpdates(store, updates);
    await waitFor(() => expect(screen.getByTestId("all-clear")).toBeTruthy());
    await waitFor(() => expect(answer).not.toBeNull());
    const kind = () => screen.queryByTestId("app-update-card")?.dataset.kind ?? null;

    // Installed in the background, then the older first status arrives: the update still waits for
    // the person's restart (never the app's own), on the Inbox and its badge alike.
    announce({ ...idle, pending: "0.7.0" });
    await act(async () => answer!(idle));
    expect(kind()).toBe("pending");
    expect(screen.queryByTestId("all-clear")).toBeNull();
    expect(counts.at(-1)).toBe(1);
    const pending = screen.getByTestId("app-update-card");
    fireEvent.click(within(pending).getByRole("button", { name: "What's new" }));
    expect(openUrl).toHaveBeenCalledWith(releaseNotesUrl("0.7.0"));
    fireEvent.click(within(pending).getByRole("button", { name: "Restart to update" }));
    expect(updates.restart).toHaveBeenCalled();

    // Restarted on it: once, until dismissed.
    announce({ ...idle, version: "0.7.0", updated: "0.7.0" });
    expect(kind()).toBe("updated");
    fireEvent.click(within(screen.getByTestId("app-update-card")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(kind()).toBeNull());
    expect(counts.at(-1)).toBe(0);

    // An rc or a staff build installs nothing by itself, and nothing waits for it.
    announce({ ...idle, automatic: false, pending: "0.7.1-rc.1" });
    expect(kind()).toBeNull();
    expect(counts.at(-1)).toBe(0);
    sync.stop();
  });
});
