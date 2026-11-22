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
import { inboxMain, inboxMany, inboxNews, installThreads, seedDoneToday } from "./inbox";
import { installLibraryUx, libraryUxScene, playLibraryUx, prepareLibraryUx } from "./libraryUx";
import { wideCheckout, wideDiff } from "./wideDiff";
import { installProbe, probeSocket, swipe, swipeDeltas } from "./probe";
import { ChatPaneScene } from "./chatPane";
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
  "inbox-news": { screen: "inbox" },
  "inbox-clear": { screen: "inbox" },
  "inbox-loading": { screen: "inbox" },
  "inbox-many": { screen: "inbox" },
  "inbox-thread": { screen: "inbox" },
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
  "library-kept": { screen: "knowledge", view: "kept" },
  "library-decisions": { screen: "knowledge", view: "decisions" },
  "knowledge-map": { screen: "knowledge", view: "map" },
  "knowledge-record": { screen: "knowledge" },
  "knowledge-search": { screen: "knowledge" },
  settings: { screen: "settings" },
  // Settings › Library for the Library and sharing UX review (shots/libraryUx.ts).
  "settings-library": { screen: "settings" },
  "settings-library-servers": { screen: "settings" },
  "settings-library-locked": { screen: "settings" },
  "settings-library-admin": { screen: "settings" },
  "settings-usage": { screen: "settings", tab: "usage" },
  "settings-edit": { screen: "settings" },
  account: { screen: "account" },
  "account-free": { screen: "account" },
  "account-signed-out": { screen: "account" },
  session: { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  // The docked Terminal opened under the session, a shell in it printing in the 16 ANSI colours (SHELL_OUTPUT).
  terminal: { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "sidebar-foot-calm": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "sidebar-foot-busy": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "sidebar-resize": { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
  "session-review": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42", sheet: { kind: "review" } },
  "session-review-diff": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42", sheet: { kind: "review" } },
  "session-review-diff-split": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42", sheet: { kind: "review" } },
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
  // The supervisor's message box: empty, a draft with files and an image, a long draft, a large paste
  // with its preview open, messages queued behind a working Claude, and files dragged over the session.
  "chat-empty": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  "chat-typing": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  "chat-long": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  "chat-paste": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  "chat-queued": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
  "chat-drop": { screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" },
};

/* The long message of the chat scenes: 25 lines. */
const LONG_MESSAGE = [
  "Before you start milestone 4, tighten the sidebar: at 240px, 280px and 320px wide I found five problems, listed below with the fix I want for each.",
  "",
  "Problems:",
  "- The Changes row overlaps the milestone list at 240px.",
  "- The milestone list pushes the sessions out of view when the goal has more than eight rows.",
  "- The counter wraps under the title at 280px.",
  "- The resize handle is 2px tall and hard to grab.",
  "- The selected row loses its border in light mode.",
  "",
  "Fixes:",
  "- Keep the Changes row on one line at every width, and cut the branch name with an ellipsis first.",
  "- Let the milestone list scroll inside its panel, with the panel capped at 240px tall.",
  "- Put the counter on its own line when the title wraps.",
  "- Make the handle 6px tall, with the hover colour of the sidebar's border.",
  "- Give the selected row the stronger border in both modes.",
  "",
  "Rules for the workers:",
  "- One branch per fix, so I can take them one at a time.",
  "- A test for each width in Sidebar.test.tsx; the file already has a helper that sets the width.",
  "- No change to the session bar or the Inbox.",
  "- Dark and light screenshots of the sidebar at 240px, 280px and 320px with each branch.",
  "- Keep each branch under 200 changed lines.",
  "",
  "Tell me when the first branch is ready for review.",
  "If any of this clashes with milestone 3's goal panel, stop and ask me before merging either one.",
].join("\n");

/* The message box of the chat scenes, driven as a person's typing, pasting and dragging would. */
const chatEditor = () => document.querySelector<HTMLTextAreaElement>('[data-testid="chat-box"] textarea');
const chatTyped = (text: string) => {
  const el = chatEditor();
  if (!el) return;
  el.focus();
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(el, text);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  requestAnimationFrame(() => (el.scrollTop = el.scrollHeight));
};
const carrying = (...files: File[]) => {
  const data = new DataTransfer();
  files.forEach((file) => data.items.add(file));
  return data;
};
const chatPasted = (data: DataTransfer) => chatEditor()?.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
/* A screenshot's worth of pixels: a window with a sidebar and some lines. */
const screenshot = () =>
  new Promise<File>((done) => {
    const canvas = Object.assign(document.createElement("canvas"), { width: 1440, height: 900 });
    const pen = canvas.getContext("2d")!;
    pen.fillStyle = "#0a100d";
    pen.fillRect(0, 0, 1440, 900);
    pen.fillStyle = "#121a16";
    pen.fillRect(0, 0, 360, 900);
    for (let row = 0; row < 14; row++) {
      pen.fillStyle = row % 4 === 1 ? "#34d399" : "#889a90";
      pen.fillRect(420, 70 + row * 56, 300 + ((row * 173) % 520), 16);
      pen.fillStyle = row === 5 ? "#e8875a" : "#afbfb6";
      pen.fillRect(40, 70 + row * 56, 120 + ((row * 97) % 160), 14);
    }
    canvas.toBlob((blob) => done(new File([blob!], "Screenshot.png", { type: "image/png" })), "image/png");
  });

const daemon = new FakeDaemon(structuredClone(data));
if (scene === "settings-usage") {
  daemon.data.usage = usageScenario("normal");
  const agy = daemon.data.usage.providers.find((q) => q.provider === "antigravity")!;
  const now = Date.now() / 1000;
  Object.assign(agy, {
    known: true,
    updated_at: now - 35,
    windows: [
      { name: "Gemini Pro 5h", short: "Gemini Pro 5h", minutes: 300, used: 42, resets_at: now + 7200 },
      { name: "Gemini Flash wk", short: "Gemini Flash wk", minutes: 10080, used: 67, resets_at: now + 172800 },
    ],
  });
}
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
if (scene === "library-kept") {
  daemon.keptVisualRows = [{ id: "k_1", title: "Release flow", from_session_name: "Ship it", repo: REPO, answer: null, kept_at: Date.now() / 1000 }];
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
if (scene === "session-review" || scene.startsWith("session-review-diff")) {
  daemon.data.sessionReview["sup-42"] = { ready: true, reasons: [], branch: "desktop/ux-revamp", base: "main", diff: "" };
  daemon.data.sessionBase["sup-42"] = { "src/session.ts": "export const ready = false;\n" };
  daemon.data.sessionFiles["sup-42"] = { "src/session.ts": "export const ready = true;\n", "src/review.ts": "export const review = 'PR';\n" };
  if (scene.startsWith("session-review-diff")) {
    // A realistic diff (shots/wideDiff.ts): the plain two-file one hides layout overlaps.
    delete daemon.data.sessionBase["sup-42"];
    daemon.data.sessionReview["sup-42"].diff = wideDiff();
    daemon.data.sessionFiles["sup-42"] = wideCheckout();
  }
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
// The message box shows only on an engine that says it takes its messages.
if (scene.startsWith("chat-")) daemon.data.meta = { ...daemon.data.meta, chat: true };
// Claude at work with two messages waiting behind it: a short one, and the long one on its one line.
if (scene === "chat-queued") {
  Object.assign(daemon.data.workspaces.flatMap((w) => w.agents ?? []).find((a) => a.id === "sup-42")!, {
    status: "processing",
    chat_queue: [
      { id: "q1", text: "When docs/readme merges, check the sidebar at 280px wide before you start milestone 4.", attachments: [] },
      { id: "q2", text: LONG_MESSAGE, attachments: [] },
    ],
  });
}
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
  // &chat=1: the message box under the pane, Claude Code's own prompt box cropped from it (ChatPane), as in a
  // session whose engine takes messages.
  if (params.get("chat") === "1") daemon.data.meta = { ...daemon.data.meta, chat: true };
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
// The Inbox's frames (shots/inbox.ts): the queue, news only, all clear, loading, many items.
const inboxScene = scene.startsWith("inbox");
if (inboxScene) {
  daemon.data.inbox = scene === "inbox-clear" ? [] : scene === "inbox-news" ? inboxNews : scene === "inbox-many" ? inboxMany : inboxMain;
  if (scene === "inbox" || scene === "inbox-clear" || scene === "inbox-thread") seedDoneToday();
  else localStorage.removeItem("brindle.inbox.triage");
}
// Settings › Library (shots/libraryUx.ts): the person's Library switches, which the fake daemon doesn't serve.
if (libraryUxScene(scene)) prepareLibraryUx(daemon, scene);
const client = daemon.client();
if (libraryUxScene(scene)) installLibraryUx(client, scene);
if (libraryScene) installLibrary(client);
if (inboxScene) installThreads(client);
// Loading: the first inbox never arrives.
if (scene === "inbox-loading") Object.assign(client, { inbox: () => new Promise(() => {}) });
const sync = startSync({ client, store, connect: daemon.connect });

// No StrictMode here: its second mount trips an xterm.js timer on the disposed
// first terminal (harmless, dev only), which the overlay above would show.
// The native folder picker, as the shell's would answer it.
const pickFolder = () => Promise.resolve("/home/sam/Notes/runbooks");
createRoot(document.getElementById("root")!).render(
  <PtyViewsProvider value={views}>
    {scene.startsWith("chat-pane")
      ? <ChatPaneScene scene={scene} theme={theme} />
      : <App store={store} client={client} refresh={sync.refresh} initialRoute={SCENES[scene] ?? SCENES.new} pickFolder={pickFolder} appUpdates={appUpdates} />}
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
  "session-review-diff": "Review changes →",
  "session-review-diff-split": "Review changes →",
  "update-downloading": 'css:[data-testid="app-update-now"]',
  terminal: 'css:[data-testid="terminal-toggle"]',
};

const press = (label: string) => {
  const button = label.startsWith("css:")
    ? document.querySelector<HTMLButtonElement>(label.slice(4))
    : [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  button?.click();
};

/* Some scenes show the flow a step further: the custom agent builder opened from Medium's list. */
/* What the terminal scene's shell prints: git, ls and a test run in the 16 ANSI colours, faint text and a 256-colour grey. */
const E = "\x1b[";
const SHELL_OUTPUT = [
  `${E}1;32m~/Projects/pawdelta${E}0m on ${E}1;35mdesktop/ux-revamp${E}0m % git status --short`,
  ` ${E}31mM${E}0m desktop/src/components/Sidebar.tsx`,
  `${E}32mA${E}0m  desktop/src/components/Sidebar.test.tsx`,
  `${E}31m??${E}0m desktop/notes.md`,
  `% git diff --stat`,
  ` desktop/src/components/Sidebar.tsx | 3 ${E}32m++${E}0m${E}31m-${E}0m`,
  `% ls`,
  `${E}1;34mdesktop${E}0m  ${E}1;34mdocs${E}0m  ${E}1;32mmake.sh${E}0m  package.json  ${E}36mlink -> docs${E}0m  ${E}33mREADME.md${E}0m`,
  `% npm test`,
  ` ${E}32m✓${E}0m src/store/overview.test.tsx ${E}90m(13 tests) 41ms${E}0m`,
  ` ${E}31m✗${E}0m src/components/Sidebar.test.tsx ${E}2m(1 failed)${E}22m`,
  ` ${E}93mwarn${E}0m ${E}38;5;244mdeprecated option --run-in-band${E}0m  ${E}95mnote${E}0m ${E}96minfo${E}0m ${E}94mhint${E}0m`,
  `% `,
].join("\r\n");

const THEN: Record<string, () => void> = {
  terminal: () => {
    // The shell's screen, for a view that opens from now on, and printed into one already open.
    const shell = daemon.actions.filter((a) => a.kind === "shell").at(-1);
    if (!shell) return;
    daemon.data.terminals[shell.id] = { live: true, screen: SHELL_OUTPUT, cursor: [2, 12] };
    daemon.print(shell.id, `${E}2J${E}H${SHELL_OUTPUT}`);
  },
  "chat-empty": () => chatEditor()?.focus(),
  "chat-queued": () => chatEditor()?.focus(),
  "chat-long": () => chatTyped(LONG_MESSAGE),
  // Two files and a screenshot from the clipboard, then two lines typed.
  "chat-typing": () =>
    void screenshot().then((shot) => {
      chatPasted(carrying(new File(["export function Sidebar() {}\n"], "Sidebar.tsx"), new File(["it('fits', () => {});\n"], "Sidebar.test.tsx"), shot));
      chatTyped("The sidebar's Changes row overlaps the milestone list at narrow widths (see the screenshot).\nKeep the row on one line and let the list scroll instead; the test file shows the widths we support.");
    }),
  // A 412-line test log pasted: over the limit, so a chip, opened to check it.
  "chat-paste": () => {
    const log = ["$ npm run test -- --run", "", "> desktop@0.6.3 test", "> vitest --run --reporter=verbose", "", " RUN  v2.1.8 ~/Projects/pawdelta/desktop", "", " ✓ src/store/overview.test.tsx (13)"];
    for (let i = log.length; i < 412; i++) log.push(`   ✓ overview > case ${i - 7} keeps its order on refresh ${5 + ((i * 7) % 19)}ms`);
    const data = new DataTransfer();
    data.setData("text/plain", log.join("\n"));
    chatPasted(data);
    chatTyped("This is the log from the run that failed on desktop/sessions. The first error is near line 180: what broke?");
    // Once the chip is drawn.
    requestAnimationFrame(() => requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('[data-testid="paste-chip"] button')?.click()));
  },
  // A screenshot's thumbnail dragged over the session, not yet let go.
  "chat-drop": () =>
    void screenshot().then((shot) =>
      document.querySelector('[data-testid="terminal-surface"]')?.dispatchEvent(new DragEvent("dragenter", { dataTransfer: carrying(shot), bubbles: true, cancelable: true }))),
  "settings-key": () => press('css:button[aria-label="Set GEMINI_API_KEY"]'),
  // The Inbox with the comment thread open: read it and reply without leaving.
  "inbox-thread": () => document.querySelector<HTMLElement>('[data-testid="inbox-row"][data-kind="shared_doc_comment"] [data-testid="row-summary"]')?.click(),
  "agents-make": () => [...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("+ Make a custom agent"))?.click(),
  // The settings page scrolled to its foot: the engine's health list.
  "settings-health": () => document.getElementById("engine")?.scrollIntoView({ block: "end" }),
  // The PR diff in its Split layout.
  "session-review-diff-split": () => press("Split"),
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
      if (libraryUxScene(scene)) playLibraryUx(scene);
      setTimeout(() => {
        document.documentElement.dataset.ready = "1";
      }, 600);
    }, 200);
  }, 1200),
);
