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
import { THEME_KEY, applyTheme } from "../src/components/ThemeToggle";
import type { Route } from "../src/routes";
import { createAppStore } from "../src/store/store";
import { startSync } from "../src/store/sync";
import { FakeDaemon, freshProviders } from "../src/test/fakeDaemon";
import { ORGS, REPO, data } from "./fixtures";
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
// No WebGL context to be had: the terminal falls back to the DOM renderer.
if (params.get("renderer") === "dom") {
  const getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, id: string, ...rest: unknown[]) {
    return id === "webgl2" || id === "webgl" ? null : getContext.call(this, id as "2d", ...(rest as []));
  } as typeof getContext;
}

const SCENES: Record<string, Route> = {
  new: { screen: "new" },
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
  "knowledge-record": { screen: "knowledge" },
  "knowledge-search": { screen: "knowledge" },
  settings: { screen: "settings" },
  "settings-edit": { screen: "settings" },
  account: { screen: "account" },
  "account-free": { screen: "account" },
  "account-signed-out": { screen: "account" },
  session: { screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" },
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
};

const daemon = new FakeDaemon(structuredClone(data));
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
const client = daemon.client();
const sync = startSync({ client, store, connect: daemon.connect });

// No StrictMode here: its second mount trips an xterm.js timer on the disposed
// first terminal (harmless, dev only), which the overlay above would show.
// The native folder picker, as the shell's would answer it.
const pickFolder = () => Promise.resolve("/home/sam/Notes/runbooks");
createRoot(document.getElementById("root")!).render(
  <PtyViewsProvider value={views}>
    <App store={store} client={client} refresh={sync.refresh} initialRoute={SCENES[scene] ?? SCENES.new} pickFolder={pickFolder} />
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
  "knowledge-add": "Add folder",
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
    const box = document.querySelector<HTMLInputElement>('input[aria-label="Search knowledge"]');
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
