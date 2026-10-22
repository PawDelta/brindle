/* The demo (dev only, `npm run build:demo`): the real app over the fake
   daemon (src/test/fakeDaemon.ts) with the renderings' fixtures
   (shots/fixtures.ts) and a little life: terminals that keep printing, a
   sign-in that completes a few seconds after its browser "opens", a code
   the CLI asks for that is accepted, keys that store, every inbox item
   decidable (a permission answered from the sidebar or the Inbox page
   shows up in the worker's pane), routing editable and saved, the theme switch. A floating
   Demo switcher flips the account's plan, the org policy on and off, and
   the providers' usage (normal, near a limit, several near, a reset soon).
   Everything the screens do goes through the same client and routes as in
   the app; only the daemon is pretend. */

import { useState } from "react";
import { createRoot } from "react-dom/client";
import App from "../src/App";
import { PtyViewsProvider } from "../src/api/ptyView";
import type { InboxItemDTO } from "../src/api/schema";
import { field } from "../src/components/ui";
import { createAppStore } from "../src/store/store";
import { startSync } from "../src/store/sync";
import { FakeDaemon } from "../src/test/fakeDaemon";
import { usageScenario, type UsageScenario } from "../src/test/usage";
import { BRINDLE_HOME, ORGS, README_BASE, README_DIFF, README_FILE, data } from "../shots/fixtures";
import "./demo.css";

const daemon = new FakeDaemon(structuredClone(data));
// brindle's home is Sam's, so the scratch session under it reads as one (its ⋯ offers Move to a repo…).
daemon.data.meta = { ...daemon.data.meta, db_path: `${BRINDLE_HOME}/brindle.db` };
const base = structuredClone(daemon.data.account);

// Every kind of thing that can wait on a person, in one inbox.
const more: InboxItemDTO[] = [
  { kind: "question", reason: "has a question", agent_id: "sup-42", workspace_id: "pawdelta", root_id: "sup-42" },
  { kind: "merge_failed", reason: "merge failed: desktop/src/App.tsx conflicts with dev/desktop-app", agent_id: "c58d1e77", workspace_id: "pawdelta/docs-readme", root_id: "sup-42" },
  { kind: "login", reason: "cloud login expired", agent_id: "sup-42", workspace_id: "pawdelta", root_id: "sup-42", detail: "brindle account login" },
  // brindle's setup on this computer, which the engine repairs itself: one Fix (a job; the card goes when it is done).
  { kind: "setup", fix: "codex_hook_trust", reason: "Codex's trust of brindle's permission hook was removed from Codex's config", action: { method: "POST", path: "/v1/setup/codex-hook" } },
  // Codex's own prompt on screen, whose selection brindle can't read: answered in its terminal only.
  {
    kind: "permission",
    reason: "asks permission",
    agent_id: "d02f44a1",
    workspace_id: "pawdelta/docs-readme",
    root_id: "sup-42",
    detail: "Allow this command?",
    permission: { via: "prompt", request_id: "p-7c3e", wording: "exec (needs approval)\n$ uv run alembic upgrade head\nAllow this command?", terminal: "its prompt doesn't show which option is selected", choices: [] },
  },
];
daemon.data.inbox = [...daemon.data.inbox, ...more];

const store = createAppStore();
const client = daemon.client();
const sync = startSync({ client, store, connect: daemon.connect });

/* The worker at work keeps going: a tool call and its result land every few seconds, as Claude Code prints them. */
const LINES = [
  "⏺ Read(desktop/src/screens/Branch/DiffView.tsx)\r\n  ⎿  Read 246 lines\r\n",
  "⏺ Update(desktop/src/screens/Session/SessionScreen.tsx)\r\n  ⎿  Updated desktop/src/screens/Session/SessionScreen.tsx with 14 additions and 3 removals\r\n",
  "⏺ Bash(npm --prefix desktop test -- --run src/screens/Session)\r\n  ⎿   ✓ src/screens/Session/SessionScreen.test.tsx (7 tests) 1.1s\r\n      Tests  7 passed (7)\r\n",
  "⏺ The diff follows the selected worker now; wiring the keys (j / k between hunks) next.\r\n",
  "⏺ Update(desktop/src/screens/Session/SessionScreen.test.tsx)\r\n  ⎿  Updated desktop/src/screens/Session/SessionScreen.test.tsx with 22 additions\r\n",
];
let line = 0;
setInterval(() => {
  daemon.print("a7aaa807", `\r\n${LINES[line++ % LINES.length]}`);
}, 6000);

/* A sign-in that completes on its own: a few seconds after the CLI "opened
   the browser" the provider reads signed in and the CLI exits. The Claude
   one asks for a code first (its screen says so); the code typed in is
   accepted and then it ends the same way. */
const seen = new Set<string>();
setInterval(() => {
  for (const action of daemon.actions) {
    if (seen.has(action.id) || !action.alive) continue;
    seen.add(action.id);
    const [kind, name] = action.kind.split(" ");
    if (kind === "login") {
      const finish = () => {
        daemon.data.routingProfiles = daemon.data.routingProfiles.map((p) => (p.provider === name ? { ...p, installed: true, signed_in: true } : p));
        daemon.data.inbox = daemon.data.inbox.filter((i) => i.kind !== "credentials");
        daemon.data.workspaces = daemon.data.workspaces.map((ws) => ({ ...ws, agents: ws.agents?.map((a) => (a.id === "e9a0b6c3" ? { ...a, status: "processing" } : a)) }));
        daemon.print(action.id, "\r\n✓ Signed in.\r\n");
        setTimeout(() => {
          daemon.exitAction(action.id);
          daemon.publish("agent.status", { agent_id: "e9a0b6c3", payload: { status: "processing" } });
        }, 400);
      };
      if (name === "claude") {
        // Waits for the code; the fake echoes what is typed, so watch the inputs.
        const before = daemon.inputs.length;
        const poll = setInterval(() => {
          if (daemon.inputs.length > before && daemon.inputs.at(-1)?.terminal === action.id) {
            clearInterval(poll);
            setTimeout(finish, 800);
          }
        }, 300);
      } else {
        setTimeout(finish, 4500);
      }
    }
    // The upgrade's progress and end come from the fake daemon itself, on the bus.
    if (kind === "upgrade") daemon.data.doctor = daemon.data.doctor.filter((c) => c.name !== "brindle update");
  }
}, 300);

/* The session goes through its goal's one lifecycle: twenty seconds in,
   every milestone passes and the workers are done (In review); a few
   seconds later brindle judges sup-42 ready for a PR (the calm notice, and
   the goal panel's Ready for PR with its Review & open PR, the sheet over
   the session's diff); the PR opened on a pretend host reads PR open, with
   its link; fifteen seconds after that the goal is Done, every milestone
   still listed. */
daemon.data.sessionFiles["sup-42"] = { "desktop/README.md": README_FILE };
daemon.data.sessionBase["sup-42"] = { "desktop/README.md": README_BASE };
setTimeout(() => {
  if (daemon.data.goal) {
    daemon.data.goal = { ...daemon.data.goal, state: "done", goal_phase: "in_review", milestones: daemon.data.goal.milestones?.map((m) => ({ ...m, status: "passed", state: "passed", state_why: null, checked_at: Date.now() / 1000 })) };
    daemon.publish("autopilot.milestone", { root_id: "sup-42", payload: { status: "passed" } });
  }
  daemon.data.workspaces = daemon.data.workspaces.map((ws) => ({ ...ws, agents: ws.agents?.map((a) => (a.parent_id === "sup-42" && a.status === "processing" ? { ...a, status: "idle", reported: true } : a)) }));
  daemon.publish("agent.status", { agent_id: "a7aaa807", payload: { status: "idle" } });
  setTimeout(() => daemon.readyForPr("sup-42", { branch: "dev/desktop-app", base: "main", diff: README_DIFF }), 5000);
}, 20_000);
const merged = setInterval(() => {
  if (!daemon.prs.some((p) => p.root === "sup-42")) return;
  clearInterval(merged);
  setTimeout(() => daemon.completeGoal("sup-42"), 15_000);
}, 500);

/* A permission answered from the app: the worker's pane shows the pick and goes on (or stops). */
let answered = 0;
setInterval(() => {
  for (const a of daemon.permissionAnswers.slice(answered)) {
    answered++;
    const pick = a.choice === "deny" ? "No" : "Yes";
    daemon.print(a.agent, `\r\n  ❯ ${pick}\r\n\r\n${a.choice === "deny" ? "● Understood: I'll take screenshots another way." : "● Downloading Chromium 131…\r\n  ⎿  161 MB, ~40s"}\r\n`);
    daemon.publish("agent.status", { agent_id: a.agent, payload: { status: a.choice === "deny" ? "idle" : "processing" } });
  }
}, 300);

/* The floating switcher: the plan and the org policy, then the app starts afresh on them. */
type Plan = "free" | "pro" | "team" | "enterprise";
const PLAN_ACCOUNT: Record<Plan, () => typeof base> = {
  free: () => ({ signed_in: true, name: "sam@example.com", plan: "free", org_id: "org_7f3a9c2e", status: "active", features: [], seats: 1, role: "owner", in_grace: false, grace_until: null, learning: false, learning_note: null, orgs: ORGS }),
  pro: () => base,
  team: () => ({ ...base, plan: "team", org_id: "org_acme", role: "member", seats: 12, features: ["ci", "team", "federation", "org_budgets", "cost", "learning", "services", "multi_repo", "coordinator", "settings_sync", "ci_fix"], orgs: ORGS.map((o) => ({ ...o, current: o.id === "org_acme" })) }),
  enterprise: () => ({ ...base, name: "cto@sovereign.example", plan: "enterprise", org_id: "org_sov", role: "admin", seats: 400, features: ["airgap", "audit", "managed_rollout", "cost_centers", "ci", "team", "federation", "org_budgets", "cost", "learning", "services", "multi_repo", "coordinator", "settings_sync", "ci_fix"], orgs: [{ id: "org_sov", name: "Sovereign Systems", role: "admin", plan: "enterprise", personal: false, current: true }] }),
};
const policy = structuredClone(daemon.data.routingPolicy);

/* Each provider's usage, as design/usage drew it: near a limit to start, so the sidebar's reading shows. */
daemon.data.usage = usageScenario("near");

function Demo() {
  const [plan, setPlan] = useState<Plan>("pro");
  const [policed, setPoliced] = useState(true);
  const [usage, setUsage] = useState<UsageScenario>("near");
  const [key, setKey] = useState(0);
  const pickUsage = (next: UsageScenario) => {
    daemon.data.usage = usageScenario(next);
    setUsage(next);
    setKey((k) => k + 1);
  };
  const apply = (nextPlan: Plan, nextPoliced: boolean) => {
    daemon.data.account = PLAN_ACCOUNT[nextPlan]();
    daemon.data.costEntitled = nextPlan !== "free";
    daemon.data.routingPolicy = nextPoliced ? structuredClone(policy) : null;
    setPlan(nextPlan);
    setPoliced(nextPoliced);
    setKey((k) => k + 1);
    void sync.refresh();
  };
  return (
    <>
      <App key={key} store={store} client={client} refresh={sync.refresh} openUrl={(url) => (window.open(url, "_blank", "noopener"), Promise.resolve())} />
      <div className="fixed bottom-s2 left-[23.5rem] z-10 flex items-center gap-s1 rounded-md border border-line bg-surface px-s2 py-1 text-xs text-ink shadow-lg" data-testid="demo-switcher">
        <span className="font-semibold">Demo</span>
        <label className="flex items-center gap-1">
          plan
          <select className={`${field} py-0.5 text-xs`} value={plan} onChange={(e) => apply(e.target.value as Plan, policed)}>
            <option value="free">Free</option>
            <option value="pro">Pro</option>
            <option value="team">Team</option>
            <option value="enterprise">Enterprise</option>
          </select>
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={policed} onChange={(e) => apply(plan, e.target.checked)} />
          org policy
        </label>
        <label className="flex items-center gap-1">
          usage
          <select className={`${field} py-0.5 text-xs`} value={usage} onChange={(e) => pickUsage(e.target.value as UsageScenario)}>
            <option value="normal">Normal</option>
            <option value="near">Near a limit</option>
            <option value="several">Several near</option>
            <option value="reset">Reset soon</option>
          </select>
        </label>
        <span className="text-ink-3">· fake engine, nothing real</span>
      </div>
    </>
  );
}

// The panes (agents' windows, a sign-in's CLI) through the fake daemon's pty views, as the shell's show them.
createRoot(document.getElementById("root")!).render(
  <PtyViewsProvider value={daemon.ptyViews()}>
    <Demo />
  </PtyViewsProvider>,
);
