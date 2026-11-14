/* The preview entry (dev only, served by `vite` at /shots/preview.html): the
   app over the fake daemon with fixtures.ts, opened on the scene the query
   names, in the theme it names. shots.mjs loads it for each scene and theme
   and screenshots the page once it marks itself ready.

     ?scene=new | session | session-review | inbox | agents ...   (default: new)
     ?theme=light | dark                                 (default: dark)
     ?renderer=dom    xterm.js's DOM renderer, not WebGL (its rows are text to read) */

import { createRoot } from "react-dom/client";
import App from "../src/App";
import { PtyViewsProvider, wsPtyView, type PtyViews } from "../src/api/ptyView";
import type { AppUpdateStatus, AppUpdates } from "../src/api/tauri";
import { THEME_KEY, applyTheme } from "../src/components/ThemeToggle";
import { ONBOARDED_KEY, markOnboarded } from "../src/api/providers";
import type { Route } from "../src/routes";
import { createAppStore } from "../src/store/store";
import { startSync } from "../src/store/sync";
import { usageScenario } from "../src/test/usage";
import { FakeDaemon, freshProviders } from "../src/test/fakeDaemon";
import { ORGS, REPO, data } from "./fixtures";
import { installLibrary, libraryInbox } from "./library";
import { installProbe, probeSocket, swipe, swipeDeltas } from "./probe";
import "../src/styles.css";

// A page that breaks still renders: the error is shown and the page marks itself ready.
const broke = (text: string) => {
  const pre = document.createElement("pre");
  pre.style.cssText = "position:fixed;left:0;right:0;bottom:0;margin:0;padding:12px;background:#b4532a;color:#fff;font:13px/1.4 monospace;white-space:pre-wrap;z-index:9";
  pre.textContent = text;
  document.body.appendChild(pre);
  document.documentElement.dataset.ready = "1";
};
addEventListener("error", (e) => broke(`${e.message}\n${e.error?.stack ?? ""}`));
addEventListener("unhandledrejection", (e) => broke(`unhandled rejection: ${e.reason?.stack ?? String(e.reason)}`));

const params = new URLSearchParams(location.search);
const scene = params.get("scene") ?? "new";
const theme = params.get("theme") === "light" ? "light" : "dark";
// As the person's own pick: the theme switch reads it back and keeps it applied.
localStorage.setItem(THEME_KEY, theme);
applyTheme(theme);
// An app already past its first launch (the real gate in App.tsx still runs): only the first-run walk-through starts without it.
if (scene === "flow") localStorage.removeItem(ONBOARDED_KEY);
else markOnboarded();
// No WebGL context to be had: the terminal falls back to the DOM renderer.
if (params.get("renderer") === "dom") {
  const getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, id: string, ...rest: unknown[]) {
    return id === "webgl2" || id === "webgl" ? null : getContext.call(this, id as "2d", ...(rest as []));
  } as typeof getContext;
}

const SCENES: Record<string, Route> = {
  new: { screen: "new" },
  "new-first": { screen: "new" },
  // The first run with Clone expanded; the composer with its project or CLI menu open.
  "new-first-clone": { screen: "new" },
  "new-projects": { screen: "new" },
  "new-cli": { screen: "new" },
  // A practice run under way, and finished: the session's repo is brindle's practice repo.
  practice: { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  "practice-done": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  flow: { screen: "new" },
  "new-composer": { screen: "new" },
  "new-conflict": { screen: "new" },
  "new-codex-only": { screen: "new" },
  inbox: { screen: "inbox" },
  "session-failed": { screen: "session", root: "sup-42", repo: REPO, agent: "e9a0b6c3" },
  "settings-key": { screen: "settings" },
  "settings-signin": { screen: "settings" },
  "settings-login": { screen: "settings" },
  "settings-health": { screen: "settings" },
  "knowledge-empty": { screen: "knowledge" },
  "knowledge-add": { screen: "knowledge" },
  "account-team": { screen: "account" },
  "account-enterprise": { screen: "account" },
  agents: { screen: "agents" },
  "agents-make": { screen: "agents" },
  knowledge: { screen: "knowledge" },
  // The Library open from the foot's button: the sidebar is its navigation; then one of its Sources.
  library: { screen: "knowledge" },
  // The same Library as columns by stage (the List / Board switch, remembered).
  "library-board": { screen: "knowledge" },
  "library-decisions": { screen: "knowledge", view: "decisions" },
  "knowledge-map": { screen: "knowledge", view: "map" },
  "knowledge-record": { screen: "knowledge" },
  "knowledge-search": { screen: "knowledge" },
  settings: { screen: "settings" },
  "settings-edit": { screen: "settings" },
  account: { screen: "account" },
  "account-free": { screen: "account" },
  "account-signed-out": { screen: "account" },
  session: { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "sidebar-foot-calm": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "sidebar-foot-busy": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "sidebar-resize": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "session-review": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42", sheet: { kind: "review" } },
  // One scene per agent of the session: the supervisor, each worker, the reviewer, in the state its row shows.
  "agent-supervisor": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  "agent-working": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "agent-waiting": { screen: "session", root: "sup-42", repo: REPO, agent: "b31c9f02" },
  "agent-done": { screen: "session", root: "sup-42", repo: REPO, agent: "c58d1e77" },
  "agent-reviewer": { screen: "session", root: "sup-42", repo: REPO, agent: "d02f44a1" },
  "agent-failed": { screen: "session", root: "sup-42", repo: REPO, agent: "e9a0b6c3" },
  "session-waiting": { screen: "session", root: "sup-42", repo: REPO, agent: "b31c9f02" },
  pty:{ screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "session-paused": { screen: "session", root: "sup-39", repo: REPO },
  "session-done": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  // The app's own update offered (the strip on every screen), Update now under way, set aside.
  "update-inbox": { screen: "inbox" },
  "update-downloading": { screen: "inbox" },
  "update-later": { screen: "inbox" },
  "update-session": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
};

const daemon = new FakeDaemon(structuredClone(data));
// Keep the common sidebar shots representative of full requests and descriptive session names.
if (scene === "session" || scene === "new") {
  const longGoal = "Update the desktop sidebar so a detailed goal stays readable beside the active session, even when the request describes several milestones, review requirements, project constraints, and the final checks needed before shipping the finished work.";
  daemon.data.sessions[0].goal = longGoal;
  daemon.data.sessions[1].name = "Investigate terminal rendering and preserve shell state across project switches and application restarts";
  daemon.data.sessions[3].name = "Prepare comprehensive release notes covering desktop improvements, command line changes, and migration guidance";
  if (daemon.data.goal) {
    daemon.data.goal.goal = longGoal;
    daemon.data.goal.note = "The latest audit found that the implementation covers the primary desktop workflow, but several details still need review: project navigation, session recovery, provider status, keyboard shortcuts, and the final checks for each milestone. Keep the full audit available while leaving enough room to see completed work and the active session tree.";
  }
}
if (scene === "new-conflict") daemon.data.sessions[0].name = "Desktop polish";
if (scene.startsWith("sidebar-foot-")) {
  const busy = scene === "sidebar-foot-busy";
  daemon.data.usage = usageScenario(busy ? "several" : "normal");
  const claude = daemon.data.usage.providers.find((p) => p.provider === "claude")!;
  claude.windows.find((w) => w.minutes === 10080)!.used = busy ? 88 : 41;
  daemon.data.providers = freshProviders({ claude: { path: "/bin/claude", version: "2" } });
}
if (scene === "knowledge-map") {
  // The reported 0.5.12 account has no hosted docs in any of its scopes.
  daemon.data.docScopes = [
    { kind: "user", id: "personal", name: "Personal" },
    { kind: "group", id: "t_cc0acb7c5cfb368c", name: "Founding Team" },
    { kind: "org", id: "org_6e83ecddb131dfc35d43d5a8", name: "PawDelta" },
  ];
  daemon.data.knowledgeMap = { nodes: [], edges: [] };
  daemon.data.kbDocuments = [
    { id: "local:kb:1", kind: "doc", title: "State architecture", local_path: `${REPO}/docs/architecture.md`, paths: ["docs/architecture.md"], topics: ["state"], updated_at: 1793610000 },
    { id: "local:kb:2", kind: "doc", title: "Terminal streaming", local_path: `${REPO}/docs/terminals.md`, paths: ["docs/terminals.md"], topics: ["terminals", "state"], updated_at: 1793610000 },
    { id: "local:kb:3", kind: "doc", title: "Credential storage", local_path: "/home/sam/Notes/pawdelta-design/storage.md", paths: ["/home/sam/Notes/pawdelta-design/storage.md"], topics: ["secrets"], updated_at: 1793610000 },
  ];
  daemon.data.decisions.push({ ...daemon.data.decisions[0], id: "daemon-reads", topic: "Read-only daemon access", scope: ["docs/**", "src/brindle/daemon/**"] });
}
// Resize coverage: a goal with enough rows to exercise both sidebar scrollers.
if (scene === "sidebar-resize" && daemon.data.goal) {
  daemon.data.goal.summary = "Make the desktop sidebar work at every window size";
  daemon.data.goal.milestones = Array.from({ length: 15 }, (_, i) => ({
    ...daemon.data.goal!.milestones[i % 5],
    id: i + 1,
    position: i + 1,
    title: `Milestone ${i + 1}: verify the sidebar layout with several sessions`,
    status: i < 9 ? "passed" : i === 9 ? "running" : "pending",
    state: i < 9 ? "passed" : "not_done",
  }));
}
// First run: no project yet.
if (scene === "new-first" || scene === "new-first-clone") daemon.data.repos = [];
if (scene === "practice" || scene === "practice-done") daemon.data.repos = daemon.data.repos.map((r) => (r.repo_root === REPO ? { ...r, demo: true } : r));
if (scene === "practice-done") daemon.completeGoal("sup-42");
// Only Codex on this Mac: Claude Code neither installed nor signed in.
if (scene === "new-codex-only") daemon.data.providers = freshProviders({ codex: { path: "/opt/homebrew/bin/codex", version: "0.48.0" } });
// The first-run walk-through: no project yet, only Codex on this Mac, and the folder the picker returns isn't a git repository.
if (scene === "flow") {
  daemon.data.repos = [];
  daemon.data.providers = freshProviders({ codex: { path: "/opt/homebrew/bin/codex", version: "0.48.0" } });
  daemon.notGitFolders = ["/home/sam/Notes/runbooks"];
  // As the engine's preflight answers (brindle/preflight.py): Claude Code not on PATH; Antigravity installed, not signed in.
  daemon.data.preflight = {
    claude: [{ id: "claude.install", kind: "setup" as never, label: "Install Claude Code", detail: "Claude Code (`claude`) isn't on PATH. Install: curl -fsSL https://claude.ai/install.sh | bash", done: false }],
    antigravity: [{ id: "antigravity.signin", kind: "signin", label: "Sign in to Antigravity", detail: "Antigravity isn't signed in on this Mac.", done: false }],
  };
}
if (scene === "session-review") {
  daemon.data.sessionReview["sup-42"] = { ready: true, reasons: [], branch: "desktop/ux-revamp", base: "main", diff: "" };
  daemon.data.sessionBase["sup-42"] = { "src/session.ts": "export const ready = false;\n" };
  daemon.data.sessionFiles["sup-42"] = { "src/session.ts": "export const ready = true;\n", "src/review.ts": "export const review = 'PR';\n" };
  if (daemon.data.goal) {
    daemon.data.goal.goal_phase = "ready_for_pr";
    daemon.data.goal.milestones.forEach((m) => { m.status = "passed"; m.state = "passed"; });
  }
}
if (scene === "settings-login" || scene === "settings-signin") {
  daemon.data.providers = freshProviders({
    claude: { path: "/home/u/.local/bin/claude", version: "2.1.47" },
    codex: { path: "/opt/homebrew/bin/codex", version: "0.48.0", signed_in: false },
  });
}
// A repo with no knowledge yet: the empty state that says how it gets there.
if (scene === "knowledge-empty") {
  daemon.data.decisions = [];
  daemon.data.kbSources = [];
}
// The free tier: signed out, with cost reports locked by the engine.
if (scene === "account-signed-out") {
  daemon.data.account = { signed_in: false };
  daemon.data.costEntitled = false;
}
// Signed in on the free plan: no entitlement to speak of, cost reports locked by the engine.
if (scene === "account-free") {
  daemon.data.account = { signed_in: true, name: "sam@example.com", plan: "free", org_id: "org_7f3a9c2e", status: "active", features: [], seats: 1, role: "owner", in_grace: false, grace_until: null, learning: false, learning_note: null };
  daemon.data.costEntitled = false;
}
// Under the team org: its plan, its seats, the orgs listed with it current.
if (scene === "account-team") {
  daemon.data.account = { ...daemon.data.account, plan: "team", org_id: "org_acme", role: "member", seats: 12, features: ["ci", "team", "federation", "org_budgets", "cost", "learning", "services", "multi_repo", "coordinator", "settings_sync", "ci_fix"], orgs: ORGS.map((o) => ({ ...o, current: o.id === "org_acme" })) };
}
// An Enterprise admin: nothing for sale at all.
if (scene === "account-enterprise") {
  daemon.data.account = { ...daemon.data.account, name: "cto@sovereign.example", plan: "enterprise", org_id: "org_sov", role: "admin", seats: 400, features: ["airgap", "audit", "managed_rollout", "cost_centers", "ci", "team", "federation", "org_budgets", "cost", "learning", "services", "multi_repo", "coordinator", "settings_sync", "ci_fix"], orgs: [{ id: "org_sov", name: "Sovereign Systems", role: "admin", plan: "enterprise", personal: false, current: true }] };
}
// The session's goal reached: Done, with its close in the sidebar and the goal panel.
if (scene === "session-done") daemon.completeGoal("sup-42");
// Every pane through the fake daemon's pty views: its screen, as a tmux client draws it.
let views: PtyViews = daemon.ptyViews();
// The worker's pane through a real tmux client on a pty, as in the app, but
// over the e2e tests' bridge (src-tauri/examples/pty_ws.rs, the same Rust
// as the shell's pty_view commands) to a private tmux server they started:
//   &bridge=ws://127.0.0.1:<port>&bridgeToken=<hex>&server=<tmux -L name or socket path>&target=<session:window>[&shell=<session:window>]
// What tmux copies lands in `window.copied`. e2e/pty.e2e.ts drives it.
// `&probe=1`: the terminal and the bridge's socket timed (probe.ts), and
// `swipe` and `swipeDeltas` on the page, for its smooth-scroll test.
if (scene === "pty") {
  const worker = daemon.data.workspaces.flatMap((w) => w.agents ?? []).find((a) => a.id === "a7aaa807")!;
  worker.window = params.get("target");
  worker.tmux_server = params.get("server");
  // The app's clipboard, as the shell holds it (pty_view_copy, pty_view_paste): `window.clipboard`, what a view's ⌘V
  // pastes, and set by each copy.
  const copied: string[] = [];
  Object.assign(window, { copied, clipboard: "" });
  const held = window as unknown as { clipboard: string };
  const probed = params.get("probe") === "1" ? probeSocket(installProbe()) : undefined;
  if (probed) Object.assign(window, { swipe, swipeDeltas });
  views = { status: { kind: "ready", tmux: "e2e" }, open: wsPtyView(params.get("bridge") ?? "", params.get("bridgeToken") ?? "", probed), copy: (text) => void copied.push((held.clipboard = text)), paste: () => Promise.resolve(held.clipboard), recheck() {} };
  // &shell=<session:window>: a shell on the same server, what the Terminal's POST /terminals/shell starts (e2e/terminal.e2e.ts).
  const shell = params.get("shell");
  if (shell) daemon.shellPanes.push({ server: params.get("server"), target: shell });
}
// The fake daemon on the page, for e2e/flows.e2e.ts: what a view wrote to its pane (`views`), and `print` into one.
Object.assign(window, { fake: daemon });
const store = createAppStore();
/* The update scenes: the shell's app updates as a release build sees one on offer (updates.rs), its
   Update now downloading and never done, Later setting it aside. */
let appUpdates: AppUpdates | undefined;
if (scene.startsWith("update-") || scene === "sidebar-foot-busy") {
  const offered: AppUpdateStatus = {
    version: "0.2.10",
    automatic: true,
    pending: null,
    available: { version: "0.2.11", notes: "## Fixes\n- Fixes slowdowns that showed os error 35 under load.\n- Adds wait-for-room for forced starts." },
    later: scene === "update-later" || scene === "sidebar-foot-busy",
    updated: null,
    last_checked: Math.floor(Date.now() / 1000) - 120,
    checking: false,
  };
  store.dispatch({ type: "app_update", status: offered });
  appUpdates = {
    status: () => Promise.resolve(offered),
    check: () => Promise.resolve(offered),
    updateNow: (progress) => {
      progress({ kind: "downloading", downloaded: 23_400_000, total: 37_800_000 });
      return new Promise(() => {});
    },
    later: () => Promise.resolve(store.dispatch({ type: "app_update", status: { ...offered, later: true } })),
    restart: () => Promise.resolve(),
    dismiss: () => Promise.resolve(),
    watch: () => Promise.resolve(() => {}),
  };
}
// The Library's designs (shots/library.ts), two waiting on the person; the board scene opens on Board.
const libraryScene = scene === "library" || scene === "library-board";
if (libraryScene) {
  daemon.data.inbox = [...libraryInbox, ...daemon.data.inbox];
  localStorage.setItem("brindle.library.view", scene === "library-board" ? "board" : "list");
}
const client = daemon.client();
if (libraryScene) installLibrary(client);
const sync = startSync({ client, store, connect: daemon.connect });

// No StrictMode here: its second mount trips an xterm.js timer on the disposed
// first terminal (harmless, dev only), which the overlay above would show.
// The native folder picker, as the shell's would answer it.
const pickFolder = () => Promise.resolve("/home/sam/Notes/runbooks");
createRoot(document.getElementById("root")!).render(
  <PtyViewsProvider value={views}>
    <App store={store} client={client} refresh={sync.refresh} initialRoute={SCENES[scene] ?? SCENES.new} pickFolder={pickFolder} appUpdates={appUpdates} />
  </PtyViewsProvider>,
);

/* Some scenes show a flow mid-way: once the screen is up, press the button
   that opens it (an inbox row's sheet, a sign-in's terminal), named by its
   text or, from "css:", a selector. */
const OPENS: Record<string, string> = {
  "settings-key": "API keys (optional, for advanced setups)",
  "settings-signin": "Sign in to Codex",
  "agents-make": 'css:button[aria-label="Medium runs on"]',
  "knowledge-record": "Record a decision",
  "new-composer": "Options ▾",
  "new-first-clone": "Clone a repository…",
  "new-projects": 'css:button[aria-label^="Project:"]',
  "new-cli": 'css:button[aria-label^="Supervisor CLI:"]',
  "new-conflict": 'css:button[aria-label="Other ways to start"]',
  "new-codex-only": "Options ▾",
  "knowledge-add": "Add folder",
  "update-downloading": 'css:[data-testid="app-update-now"]',
};

const press = (label: string) => {
  const button = label.startsWith("css:")
    ? document.querySelector<HTMLButtonElement>(label.slice(4))
    : [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  button?.click();
};

/* Some scenes show the flow a step further: the custom agent builder opened from Medium's list. */
const THEN: Record<string, () => void> = {
  "settings-key": () => press('css:button[aria-label="Set GEMINI_API_KEY"]'),
  "agents-make": () => [...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("+ Make a custom agent"))?.click(),
  // The settings page scrolled to its foot: the engine's health list.
  "settings-health": () => document.getElementById("engine")?.scrollIntoView({ block: "end" }),
  // The sign-in card, up in its provider's row, brought into view.
  "settings-signin": () => document.querySelector('[data-testid="provider-codex"]')?.scrollIntoView({ block: "center" }),
  // A search that finds a decision and the documents' chunks alike.
  "knowledge-search": () => {
    const box = document.querySelector<HTMLInputElement>('input[aria-label="Search the Library"]');
    if (!box) return;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(box, "tmux");
    box.dispatchEvent(new Event("input", { bubbles: true }));
  },
  // The settings with an edit pending: the checks gained a line, workers at once went up.
  "settings-edit": () => {
    const set = (el: HTMLInputElement | HTMLTextAreaElement | null, value: string) => {
      if (!el) return;
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    };
    set(document.querySelector<HTMLTextAreaElement>("#setting-checks"), "npm --prefix desktop test -- --run\nnpm --prefix desktop run build\nbash desktop/scripts/smoke_bundle.sh");
    set(document.querySelector<HTMLInputElement>("#setting-max_agents"), "6");
  },
};

// Ready once the first snapshot is in and the terminal has drawn its pane.
void sync.idle().then(() =>
  setTimeout(() => {
    if (OPENS[scene]) press(OPENS[scene]);
    // What the press opened is on screen by now; what THEN asks the daemon for (a search) too, after its debounce.
    setTimeout(() => {
      if (Object.hasOwn(THEN, scene)) THEN[scene]();
      setTimeout(() => {
        document.documentElement.dataset.ready = "1";
      }, 600);
    }, 200);
  }, 1200),
);
