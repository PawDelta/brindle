/* Dev-only entry for the visualizer screenshots (shots/visuals-shots.mjs): the app over the fake
   daemon on the "session" scene, with a session that has visuals. The fake daemon has no visuals
   routes, so the client's visuals/export methods are answered here from an in-memory list.
   ?theme=light|dark */

import { createRoot } from "react-dom/client";
import App from "../src/App";
import { PtyViewsProvider } from "../src/api/ptyView";
import { THEME_KEY, applyTheme } from "../src/components/ThemeToggle";
import { markOnboarded } from "../src/api/providers";
import { DaemonError, type Plan, type PlanVerdict, type VisualDTO } from "../src/api/client";
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
  { id: "v_31aa", session: "sup-42", title: "Worker fan-out for the chat view", by: "supervisor", kind: "plan", rev: 1, answer: null, kept: false, unread: false, created_at: now - 3600 },
  { id: "v_old1", session: "sup-42", title: "Sidebar rail widths", by: "a7aaa807", kind: "visual", rev: 1, question: "Which rail width?", options: [{ id: "narrow", label: "Narrow" }, { id: "wide", label: "Wide" }], answer: { option: "narrow", note: "keeps the chat readable", at: now - 7200 }, kept: true, unread: false, created_at: now - 9000 },
  { id: "v_old2", session: "sup-42", title: "Permission prompt layout", by: "b31c9f02", kind: "visual", rev: 3, question: "Inline or sheet?", options: [{ id: "inline", label: "Inline" }, { id: "sheet", label: "Sheet" }], answer: { option: "sheet", note: "", at: now - 20000 }, kept: false, unread: false, created_at: now - 30000 },
];

// The plan row's doc (shots/plan-shots.mjs): a decision with three alternatives, and a split where
// two workers change one file. Approved, the board shows a test in each state.
let plan: Plan = {
  id: "v_31aa", session: "sup-42", title: "Worker fan-out for the chat view", rev: 1, state: "open", markdown: "## Risks\nSign-in rules.", created_at: now - 3600, queued: 3,
  ask: "People on an API key get a real chat with a message box. Subscription logins keep the live CLI.",
  lede: "Three workers build the chat view behind its gate: a headless runner, the route that streams its replies, and the view itself.",
  decisions: [{ id: "stream", title: "How should the chat stream its replies?", mine: "sse", challenge: "A second transport is one more thing to keep alive; the socket already carries every event.", options: [
    { id: "sse", label: "A · Server-sent events", pros: ["One route, plain HTTP", "Reconnects by itself"], cons: ["A second transport beside the socket"], touches: ["daemon/app.py", "daemon/events.py", "api/client.ts"] },
    { id: "socket", label: "B · The existing socket", pros: ["Nothing new to keep alive"], cons: ["Every reply shares the event bus", "Harder to replay a missed reply"], touches: ["daemon/events.py", "store/sync.ts"] },
    { id: "both", label: "C · A first, B later", pros: ["Ships the simple path first"], cons: ["Two paths to maintain"], touches: ["daemon/app.py", "daemon/events.py"] },
  ] }],
  sections: [{ id: "risks", title: "Risks", markdown: "- The terms allow headless runs on API keys only: the gate has to hold.\n- A long reply must survive a reconnect." }],
  split: [
    { id: "w1", title: "Headless runner", files: ["spawn.py", "native/tools.py"], test: "test_headless_run" },
    { id: "w2", title: "Stream route for replies", after: ["w1"], files: ["daemon/app.py", "daemon/events.py"], test: "test_chat_stream" },
    { id: "w3", title: "Chat view and its gate", after: ["w2"], files: ["ChatView.tsx", "daemon/events.py"], test: "ChatView.test.tsx" },
  ],
};
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
  decidePlan: async (_id: string, verdict: PlanVerdict) => (plan = { ...plan, state: verdict.approved ? "approved" : "redirected", verdict: { ...verdict, at: Date.now() / 1000 },
    split: verdict.approved ? plan.split.map((w, i) => ({ ...w, ...live[i] })) : plan.split }),
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
