/* Dev-only entry for the visualizer screenshots (shots/visuals-shots.mjs): the app over the fake
   daemon on the "session" scene, with a session that has visuals. The fake daemon has no visuals
   routes, so the client's visuals/export methods are answered here from an in-memory list.
   ?theme=light|dark */

import { createRoot } from "react-dom/client";
import App from "../src/App";
import { PtyViewsProvider } from "../src/api/ptyView";
import { THEME_KEY, applyTheme } from "../src/components/ThemeToggle";
import { markOnboarded } from "../src/api/providers";
import { DaemonError, type Plan, type PlanAnchor, type PlanChange, type PlanVerdict, type VisualDTO } from "../src/api/client";
import { createAppStore } from "../src/store/store";
import { startSync } from "../src/store/sync";
import { FakeDaemon } from "../src/test/fakeDaemon";
import { REPO, data } from "./fixtures";
import "../src/styles.css";

const params = new URLSearchParams(location.search);
const theme = params.get("theme") === "light" ? "light" : "dark";
localStorage.setItem(THEME_KEY, theme);
applyTheme(theme);
markOnboarded();
// Off Pro (?free=1) the analysis-docs routes answer 402, as the engine does when not entitled.
const free = params.get("free") === "1";
const notEntitled = () => Promise.reject(new DaemonError("not_entitled", "brindle Pro is needed for this.", 402));

const now = Date.now() / 1000;
let rows: VisualDTO[] = [
  { id: "v_8f2c", session: "sup-42", title: "Release flow: tag first or gate on CI", by: "supervisor", kind: "visual", rev: 2, question: "Which flow should 0.6.3 use?", options: [{ id: "A", label: "A · Tag first", detail: "Tag, then let CI build" }, { id: "B", label: "B · Gate on CI", detail: "Build, then tag" }], answer: null, kept: false, unread: true, created_at: now - 240 },
  { id: "v_31aa", session: "sup-42", title: "Cloud workers for Pro", by: "supervisor", kind: "plan", rev: 3, answer: null, kept: false, unread: false, created_at: now - 3600, answer_agent_id: "sup-42", accepts_files: true },
  { id: "v_old1", session: "sup-42", title: "Sidebar rail widths", by: "a7aaa807", kind: "visual", rev: 1, question: "Which rail width?", options: [{ id: "narrow", label: "Narrow" }, { id: "wide", label: "Wide" }], answer: { option: "narrow", note: "keeps the chat readable", at: now - 7200 }, kept: true, unread: false, created_at: now - 9000 },
  { id: "v_old2", session: "sup-42", title: "Permission prompt layout", by: "b31c9f02", kind: "visual", rev: 3, question: "Inline or sheet?", options: [{ id: "inline", label: "Inline" }, { id: "sheet", label: "Sheet" }], answer: { option: "sheet", note: "", at: now - 20000 }, kept: false, unread: false, created_at: now - 30000 },
];

// The plan row's design doc (shots/plan-shots.mjs): text sections, a diagram,
// code with a suggestion on it, a table, an open question, three
// decisions (one confirmed), comment threads, and a split where two workers change one file.
// Approved, the board shows a test in each state.
const SUMMARY = "Let Pro users start a goal, close the laptop, and come back to reviewed pull requests.\n\nWorkers run on a machine the customer controls, so code never passes through PawDelta.";
const API = "```\nPOST /v1/hosts/{id}/sessions\n{ \"goal\": \"…\", \"repo\": \"…\", \"token\": \"…\" }\n```";
const QUESTIONS = "> **How long does a lease last when the host loses network?** The supervisor proposes 5 minutes; no one has weighed in yet.";
const DIAGRAM = `<svg viewBox="0 0 700 156" role="img" aria-label="Sequence: the app starts a goal, the daemon relays the session to the worker host, the host pushes a branch to git and reports back with a PR link"><g font-family="ui-monospace, Menlo, monospace" font-size="11" text-anchor="middle" fill="currentColor"><text x="70" y="16">app</text><text x="260" y="16">daemon</text><text x="450" y="16">worker host</text><text x="630" y="16">git</text><text x="165" y="43">start goal</text><text x="355" y="73">relay session</text><text x="540" y="103">push branch</text><text x="355" y="131">report + PR link</text></g><path d="M70 26V150M260 26V150M450 26V150M630 26V150" stroke="currentColor" stroke-dasharray="3 4" fill="none" opacity=".5"/><path d="M70 50H254M260 80H444M450 110H624M450 138H266" stroke="currentColor" fill="none"/><path d="M249 46.5L257 50L249 53.5zM439 76.5L447 80L439 83.5zM619 106.5L627 110L619 113.5zM271 134.5L263 138L271 141.5z"/></svg>`;
const anchored = (section: string, body: string, quote: string) => ({ kind: "text" as const, section, start: body.indexOf(quote), end: body.indexOf(quote) + quote.length, quote, state: "current" as const });
let plan: Plan = {
  id: "v_31aa", session: "sup-42", title: "Cloud workers for Pro", rev: 3, state: "open", markdown: `## Summary\n\n${SUMMARY}`, created_at: now - 3600, queued: 3, can_edit: true,
  ask: "Pro users start a goal, close the laptop, and come back to reviewed pull requests, without their code leaving machines they control.",
  lede: "",
  sections: [
    { id: "summary", title: "Summary", markdown: SUMMARY },
    { id: "goals", title: "Goals and non-goals", markdown: "**Goals**\n\n- A session keeps running with the laptop shut\n- Results land as reviewed PRs\n\n**Non-goals**\n\n- Hosting code on PawDelta servers\n- Windows worker hosts in v1" },
    { id: "architecture", title: "Architecture", kind: "svg", markdown: DIAGRAM, caption: "The daemon relays a session to the customer's worker host; the app never talks to the host directly." },
    { id: "api", title: "API sketch", markdown: API },
    { id: "data", title: "Data model", markdown: "| Table | Field | Why |\n| --- | --- | --- |\n| `hosts` | `lease_expires_at` | A host's session lease, renewed every minute |\n| `sessions` | `host_id` | Which host runs it |" },
    { id: "questions", title: "Open questions", markdown: QUESTIONS },
  ],
  decisions: [
    { id: "relay", title: "Relay through the daemon", mine: "daemon", confirmed: "daemon", votes: { daemon: ["person"] }, options: [
      { id: "daemon", label: "A · Through the daemon", pros: ["One connection to secure"], cons: ["The daemon must stay up"], touches: ["daemon/app.py"] },
      { id: "direct", label: "B · App to host", pros: ["One hop fewer"], cons: ["The app holds host keys"], touches: ["api/client.ts"] },
    ] },
    { id: "where", title: "Where cloud workers run", mine: "own", challenge: "customers without a spare machine can't use A at all.", votes: {}, options: [
      { id: "own", label: "A · Your own server", pros: ["Code never leaves the customer"], cons: ["They set up a machine"], touches: ["spawn.py"] },
      { id: "hosted", label: "B · PawDelta-hosted", pros: ["One click"], cons: ["We hold customers' code"], touches: ["daemon/app.py"] },
      { id: "both", label: "C · A first, B later", pros: ["Ships the safe path first"], cons: ["Two paths to maintain"], touches: ["spawn.py"] },
    ] },
    { id: "lease", title: "How long a lease lasts", mine: "five", votes: {}, options: [
      { id: "five", label: "A · 5 minutes", pros: ["A lost host frees its session fast"], cons: ["Short for a laptop on a train"], touches: ["spawn.py"] },
      { id: "fifteen", label: "B · 15 minutes", pros: ["Rides out a tunnel"], cons: ["A dead host holds its session longer"], touches: ["spawn.py"] },
    ] },
  ],
  revisions: [1, 2, 3].map((rev) => ({ rev, author: "supervisor", source: "proposal", at: now - 4000 + rev * 100, changed: ["summary"] })),
  threads: [
    { id: 1, kind: "suggestion", rev: 3, author: "supervisor", status: "open", at: now - 900, text: "Replace the token with a short lease; tokens in the body end up in logs.", before: "\"token\": \"…\"", after: "\"lease\": \"…\"", anchor: anchored("api", API, "\"token\": \"…\"") },
    { id: 2, kind: "comment", rev: 3, author: "person", status: "open", at: now - 800, sent: true, text: "Does \"a machine the customer controls\" include a cloud VM they rent?", anchor: anchored("summary", SUMMARY, "a machine the customer controls"),
      replies: [{ id: 3, author: "supervisor", at: now - 700, text: "Yes; any machine they hold the keys to. I'll say so in §1." }] },
    { id: 4, kind: "comment", rev: 3, author: "person", status: "open", at: now - 600, text: "I'd ship C: customers without a server shouldn't wait for hosted.", attachments: ["hosting-costs.xlsx"], anchor: { kind: "decision", decision: "where", state: "current" } },
    { id: 5, kind: "comment", rev: 3, author: "person", status: "open", at: now - 500, text: "Five minutes is short for a laptop on a train. Fifteen?", anchor: anchored("questions", QUESTIONS, "no one has weighed in yet") },
    { id: 6, kind: "comment", rev: 2, author: "person", status: "resolved", at: now - 2000, sent: true, text: "Label the arrows.", anchor: { kind: "diagram", section: "architecture", state: "current" } },
  ],
  split: [
    { id: "w1", title: "Remote launch", files: ["spawn.py", "tmux.py"], test: "test_remote_spawn" },
    { id: "w2", title: "Host leases", after: ["w1"], files: ["daemon/app.py", "spawn.py"], test: "test_host_lease" },
    { id: "w3", title: "Hosts in Settings", after: ["w2"], files: ["HostsSection.tsx"], test: "HostsSection.test.tsx" },
  ],
};
// The doc's writes (brindle/plan_docs.py), enough of them for the shots: each changes the plan above.
let threadId = 10;
const bodyOf = (section: string) => (section === "lede" ? plan.lede : plan.sections.find((s) => s.id === section)?.markdown ?? "");
const thread = (id: number, to: Partial<NonNullable<Plan["threads"]>[number]>) => (plan = { ...plan, threads: plan.threads!.map((t) => (t.id === id ? { ...t, ...to } : t)) });
const changed = (change: PlanChange, source: "edit" | "suggestion") => {
  const body = bodyOf(change.section);
  const at = body.startsWith(change.before, change.start) ? change.start : body.indexOf(change.before);
  const text = body.slice(0, at) + change.after + body.slice(at + change.before.length);
  return (plan = { ...plan, rev: plan.rev + 1, lede: change.section === "lede" ? text : plan.lede, sections: plan.sections.map((s) => (s.id === change.section ? { ...s, markdown: text } : s)),
    revisions: [...plan.revisions!, { rev: plan.rev + 1, author: "person", source, at: Date.now() / 1000, changed: [change.section] }] });
};
// ?doc=spine: the same row as a plan on the design-doc spine (shots/plan-doc-shots.mjs), the
// webhook example of the approved planning-doc design (plan-doc-redesign.html).
if (params.get("doc") === "spine") {
  const DESIGN = "Every state change writes an event row in the same transaction as the change itself, so a crash cannot drop one. A background dispatcher reads the rows that are due, signs each payload and POSTs it to the team's endpoint.";
  const RETRIES = "Retries back off exponentially (10s, 1m, 5m, 30m, 2h) with jitter. 4xx responses except 429 fail immediately, since sending the same payload again will not help. After 6 attempts the row moves to `dead_letter`.";
  const RISKS = "**Hard to undo:** the payload shape and the `X-Brindle-Sig` header. Once teams verify against them, changing either breaks their receivers. Both are versioned as `v1` from the first day.\n\n**Risks**\n\n- A dead endpoint could flood the Inbox with failures. The decision below settles how loud that is.\n- The outbox table grows without bound unless it is pruned. Delivered rows are removed 7 days after delivery.";
  const FLOW = `<svg viewBox="0 0 660 176" role="img" aria-label="A state change writes an outbox row; the dispatcher signs it and posts it to the team endpoint; failures are tried again, then go to the dead-letter log"><g fill="none" stroke="currentColor" opacity=".35"><rect x="8" y="14" width="124" height="46" rx="4"/><rect x="178" y="14" width="124" height="46" rx="4"/><rect x="348" y="14" width="124" height="46" rx="4"/><rect x="518" y="14" width="134" height="46" rx="4"/><rect x="348" y="118" width="124" height="46" rx="4"/><rect x="518" y="118" width="134" height="46" rx="4"/></g><g font-size="12" text-anchor="middle" fill="currentColor"><text x="70" y="41">Something changes</text><text x="240" y="41">Outbox row</text><text x="410" y="41">Dispatcher</text><text x="585" y="41">Team endpoint</text><text x="410" y="145">Retry schedule</text><text x="585" y="145">Dead-letter log</text></g><path d="M132 37H172M302 37H342M472 37H512M410 60V112M472 141H512" stroke="currentColor" fill="none" opacity=".6"/></svg>`;
  plan = {
    id: "v_31aa", session: "sup-42", title: "Webhook delivery with retries", rev: 2, state: "open", markdown: "…", created_at: now - 3600, queued: 3, can_edit: true, reference: "DES-0042",
    lede: "Deliver signed workspace events to team webhook URLs, with exponential backoff and a dead-letter log, without blocking the daemon's event loop.",
    sections: [
      { id: "context", title: "Context", markdown: "Teams already wire brindle into chat and CI by polling `brindle status --json`. Polling misses short-lived states, such as a worker that fails and is retried within a minute, and it costs each team a cron job. Three customers have asked for push notifications instead." },
      { id: "audience", title: "Audience", markdown: "Team admins who configure integrations in Settings, and the services that receive the events. Individual developers never see this feature directly." },
      { id: "goals", title: "Goals and non-goals", markdown: "**Goals**\n\n- No event is lost if the daemon crashes.\n- Receivers can verify every payload.\n- A failing endpoint never slows the event loop.\n\n**Non-goals**\n\n- Exactly-once delivery. Receivers dedupe by event id.\n- Per-event filtering rules. That is a later plan." },
      { id: "proposed-design", title: "Proposed design", markdown: `${DESIGN}\n\n${RETRIES}` },
      { id: "architecture", title: "Architecture", kind: "svg", markdown: FLOW, caption: "The path of one event." },
      { id: "tech-stack", title: "Tech stack", markdown: "| Part | Choice |\n| --- | --- |\n| Storage | The daemon's existing SQLite file, with one new table |\n| HTTP | The async `httpx` client brindle already ships |\n| Signing | HMAC-SHA256 from the standard library, secrets in the OS keychain |" },
      { id: "alternatives", title: "Alternatives considered", markdown: "- **Keep polling, add a cursor.** Cheaper to build, but every team still runs a cron job.\n- **A third-party relay.** Event payloads would leave the machine.\n- **A server-sent events stream.** The receivers asked for here only take POSTs." },
      { id: "risks", title: "Risks and what's hard to undo", markdown: RISKS },
      { id: "rollout", title: "Rollout and tasks", markdown: "1. **Lands dark.** The outbox and signing ship first. Events are written but never sent.\n2. **Reaches teams.** The dispatcher and the Settings page ship, switched on the way you pick below.\n3. **Stable.** After a week with no dead-letter spikes, the payload is documented as stable `v1`.\n\n### What gets built\n\n- The outbox and payload signing\n- The dispatcher with backoff, once the outbox is in\n- The Settings page for webhooks, with the dead-letter view" },
      { id: "open-questions", title: "Open questions", markdown: "- Should a 429 honour the receiver's `Retry-After` before our own schedule?\n- Is 256 KB the right payload cap, or should large events send a link instead?" },
    ],
    decisions: [
      { id: "store", title: "Where should delivery state live?", section: "proposed-design", mine: "db", challenge: "if an org emits more than 10k CI events a minute, database write locks could stall delivery.", options: [
        { id: "db", label: "A table in the existing database", pros: ["Written together with the event, and no new service to run"], cons: ["Tops out near 200 deliveries a second on one machine"] },
        { id: "redis", label: "A dedicated Redis queue", pros: ["Fans out across replicas"], cons: ["A new service to install and run on every machine"] },
      ] },
      { id: "loud", title: "How should failed deliveries show up?", section: "risks", mine: "one", challenge: "an admin who never opens Settings will not learn which events were lost.", options: [
        { id: "one", label: "One Inbox notice per endpoint, details in a log", pros: ["One notice, however many events fail"], cons: ["Admins open Settings to see which events were lost"] },
        { id: "every", label: "An Inbox notice for every failed event", pros: ["Nothing is hidden"], cons: ["Floods the Inbox during an outage"] },
      ] },
      { id: "reach", title: "How should it reach teams?", section: "rollout", mine: "off", challenge: "opt-in features often go untested until a customer finds the first bug.", options: [
        { id: "off", label: "Off until an admin adds a URL", pros: ["Nothing changes for teams that don't opt in"], cons: ["Slower feedback from real endpoints"] },
        { id: "staff", label: "Staff teams first, then everyone", pros: ["Real traffic before the release"], cons: ["Needs a staged switch and a second release"] },
      ] },
    ],
    split: [],
    revisions: [{ rev: 1, author: "supervisor", source: "proposal", at: now - 3600 }, { rev: 2, author: "supervisor", source: "proposal", at: now - 240 }],
    threads: [
      { id: 1, kind: "comment", rev: 2, author: "person", status: "open", at: now - 120, text: "Should a 429 honour Retry-After before falling back to our schedule?",
        anchor: anchored("proposed-design", `${DESIGN}\n\n${RETRIES}`, "4xx responses except 429 fail immediately"),
        replies: [{ id: 2, author: "person", from_supervisor: true, at: now - 60, text: "Yes, if it is under 2h. I've listed it under Open questions until you confirm." }] },
      { id: 3, kind: "suggestion", rev: 2, author: "maya.chen@pawdelta.com", status: "open", at: now - 360, text: "Support asks for deliveries up to a month back.", before: "7 days", after: "30 days",
        anchor: anchored("risks", RISKS, "7 days") },
    ],
  };
}
// Where each worker is once the plan is approved.
const live: Partial<Plan["split"][number]>[] = [
  { task_id: "t_41", agent_id: "a7aaa807", state: "merged", test_state: "passing" },
  { task_id: "t_42", agent_id: "b31c9f02", state: "in review", test_state: "failing" },
  { task_id: "t_43", state: "queued", test_state: "not run" },
];

const daemon = new FakeDaemon(structuredClone(data));
const client = daemon.client();
Object.assign(client, {
  plan: async () => plan,
  decidePlan: async (_id: string, verdict: PlanVerdict) => (plan = { ...plan, state: verdict.approved ? "approved" : "redirected", can_edit: false, verdict: { ...verdict, at: Date.now() / 1000 },
    split: verdict.approved ? plan.split.map((w, i) => ({ ...w, ...live[i] })) : plan.split }),
  commentPlan: async (_id: string, rev: number, anchor: PlanAnchor, text: string) => (plan = { ...plan, threads: [...plan.threads!, { id: threadId++, kind: "comment", rev, author: "person", status: "open", at: Date.now() / 1000, text,
    anchor: anchor.kind === "text" ? { ...anchor, end: anchor.start + anchor.quote.length, state: "current" } : { ...anchor, state: "current" } }] }),
  replyPlan: async (_id: string, id: number, text: string) => thread(id, { replies: [...(plan.threads!.find((t) => t.id === id)?.replies ?? []), { id: threadId++, author: "person", at: Date.now() / 1000, text }] }),
  resolvePlanThread: async (_id: string, id: number, resolved = true) => thread(id, { status: resolved ? "resolved" : "open" }),
  suggestPlan: async (_id: string, rev: number, change: PlanChange, text = "") => (plan = { ...plan, threads: [...plan.threads!, { id: threadId++, kind: "suggestion", rev, author: "person", status: "open", at: Date.now() / 1000, text,
    before: change.before, after: change.after, anchor: { kind: "text", section: change.section, start: change.start, end: change.start + change.before.length, quote: change.before, state: "current" } }] }),
  acceptPlanSuggestion: async (_id: string, id: number) => {
    const t = plan.threads!.find((s) => s.id === id)!;
    changed({ section: t.anchor.section!, start: t.anchor.start!, before: t.before ?? "", after: t.after ?? "" }, "suggestion");
    return thread(id, { status: "accepted" });
  },
  rejectPlanSuggestion: async (_id: string, id: number) => thread(id, { status: "rejected" }),
  editPlan: async (_id: string, _rev: number, change: PlanChange) => changed(change, "edit"),
  confirmPlan: async (_id: string, _rev: number, decision: string, option: string) => (plan = { ...plan, decisions: plan.decisions.map((d) => (d.id === decision ? { ...d, confirmed: option, votes: { [option]: ["person"] } } : d)) }),
  visuals: async () => rows,
  markVisualRead: async (id: string) => {
    rows = rows.map((v) => (v.id === id ? { ...v, unread: false } : v));
    return {};
  },
  answerVisual: async (id: string, _rev: number, option: string, note: string) => {
    rows = rows.map((v) => (v.id === id ? { ...v, answer: { option, note, at: Date.now() / 1000 } } : v));
    return rows.find((v) => v.id === id)!;
  },
  keepVisual: async (id: string) => (rows = rows.map((v) => (v.id === id ? { ...v, kept: true } : v))).find((v) => v.id === id)!,
  unkeepVisual: async (id: string) => (rows = rows.map((v) => (v.id === id ? { ...v, kept: false } : v))).find((v) => v.id === id)!,
  docPolicy: async () => (free ? notEntitled() : { export_destinations: ["download", "google_docs", "sharepoint"] }),
  docExportConfig: async () => (free ? notEntitled() : { google_client_id: "preview-google", microsoft_client_id: "preview-microsoft" }),
  exportDoc: async () => ({ filename: "doc.md", mime: "text/markdown" }),
  exportVisual: async () => ({ filename: "visual.html", mime: "text/html", content_b64: "" }),
});

Object.assign(window, { fake: daemon });
const store = createAppStore();
const sync = startSync({ client, store, connect: daemon.connect });
createRoot(document.getElementById("root")!).render(
  <PtyViewsProvider value={daemon.ptyViews()}>
    <App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup-42", repo: REPO, agent: "sup-42" }} pickFolder={() => Promise.resolve(null)} />
  </PtyViewsProvider>,
);
void sync.idle().then(() => setTimeout(() => (document.documentElement.dataset.ready = "1"), 1500));
