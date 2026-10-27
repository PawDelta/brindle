import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../../api/client";
import { DaemonContext } from "../../api/context";
import { scopesFor } from "../../api/permissions";
import type { AskDTO, AskGroupDTO, InboxItemDTO, SessionDTO } from "../../api/schema";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { BASE_URL, FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { REPO, permission, permissionInTerminal, question, root, waiting, worker } from "../../test/fixtures";
import { InboxScreen } from "./InboxScreen";

afterEach(cleanup);

const notice: InboxItemDTO = { kind: "org_notice", reason: "Reviewer profiles now default to reviewer-codex.", detail: "admin", notice_id: "n-18" };
const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

function mount(daemon: FakeDaemon, client: DaemonClient = daemon.client()) {
  const store = createAppStore();
  const sync = startSync({ client, store, connect: daemon.connect });
  const onOpen = vi.fn();
  const onOpenRule = vi.fn();
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <InboxScreen client={client} refresh={sync.refresh} onOpen={onOpen} onOpenRule={onOpenRule} />
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
      inbox: [question, waiting, notice],
    });
    const { sync, onOpen } = mount(daemon);
    const group = await screen.findByRole("region", { name: "Ship ls --json" });
    expect(within(group).getAllByTestId("inbox-card")).toHaveLength(2);
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
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [plan, checks, review] });
    const { sync, onOpen } = mount(daemon);
    const group = await screen.findByRole("region", { name: "Ship ls --json" });
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
    expect(within(held).getByTestId("permission-why").textContent).toContain("Your rule asks: ");
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

  it("Always for <group item> sets that subcategory's override through the group baselines, then resumes the request", async () => {
    const install: AskGroupDTO = {
      key: `${REPO}|bash|npm install`, repo: REPO, kind: "bash", title: "Allow npm install?", general: scopesFor("bash", "npm install left-pad")[0],
      scopes: scopesFor("bash", "npm install left-pad"), blocked: false, rule: null, limits: null, modes: ["once", "deny", "session"],
      items: [{ ...ask("w1", "req-1", "bash", "npm install left-pad", 60, "d-none"), rule: null, limits: null, reason: "the shell/install baseline is ask" }],
    };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [install] });
    const { sync } = mount(daemon);
    const item = await screen.findByRole("article", { name: "Allow npm install?" });
    expect(within(item).getByTestId("permission-chip").textContent).toBe("Shell › Package installs");
    fireEvent.click(within(item).getByRole("button", { name: "More ways to allow" }));
    fireEvent.click(within(item).getByRole("menuitem", { name: /^Always for Shell › Package installs/ }));
    await waitFor(() => expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/permissions/groups")?.body).toEqual({ groups: { shell: { subcategories: { install: "allow" } } } }));
    await waitFor(() => expect(screen.getByTestId("permission-resolution").textContent).toContain("Always allowed: Shell › Package installs. Saved as an override in Settings › Permissions. 1 request resumed"));
    // An override, not a saved action.
    expect(daemon.data.policy.saved).toEqual([]);
    expect(daemon.data.policy.groups?.shell.subcategories).toEqual({ install: "allow" });
    sync.stop();
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
});
