/* Dev-only entry for the visualizer screenshots (shots/visuals-shots.mjs): the app over the fake
   daemon on the "session" scene, with a session that has visuals. The fake daemon has no visuals
   routes, so the client's visuals/export methods are answered here from an in-memory list.
   ?theme=light|dark */

import { createRoot } from "react-dom/client";
import App from "../src/App";
import { PtyViewsProvider } from "../src/api/ptyView";
import { THEME_KEY, applyTheme } from "../src/components/ThemeToggle";
import { markOnboarded } from "../src/api/providers";
import type { VisualDTO } from "../src/api/client";
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
localStorage.removeItem("brindle.visuals.focus");

const now = Date.now() / 1000;
let rows: VisualDTO[] = [
  { id: "v_8f2c", session: "sup-42", title: "Release flow: tag first or gate on CI", by: "supervisor", kind: "visual", rev: 2, question: "Which flow should 0.6.3 use?", options: [{ id: "A", label: "A · Tag first", detail: "Tag, then let CI build" }, { id: "B", label: "B · Gate on CI", detail: "Build, then tag" }], answer: null, kept: false, unread: true, created_at: now - 240 },
  { id: "v_31aa", session: "sup-42", title: "Worker fan-out for the chat view", by: "supervisor", kind: "plan", rev: 1, answer: null, kept: false, unread: false, created_at: now - 3600 },
  { id: "v_old1", session: "sup-42", title: "Sidebar rail widths", by: "a7aaa807", kind: "visual", rev: 1, question: "Which rail width?", options: [{ id: "narrow", label: "Narrow" }, { id: "wide", label: "Wide" }], answer: { option: "narrow", note: "keeps the chat readable", at: now - 7200 }, kept: true, unread: false, created_at: now - 9000 },
  { id: "v_old2", session: "sup-42", title: "Permission prompt layout", by: "b31c9f02", kind: "visual", rev: 3, question: "Inline or sheet?", options: [{ id: "inline", label: "Inline" }, { id: "sheet", label: "Sheet" }], answer: { option: "sheet", note: "", at: now - 20000 }, kept: false, unread: false, created_at: now - 30000 },
];

const daemon = new FakeDaemon(structuredClone(data));
const client = daemon.client();
Object.assign(client, {
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
  docPolicy: async () => ({ export_destinations: ["download", "google_docs", "sharepoint"] }),
  docExportConfig: async () => ({ google_client_id: "preview-google", microsoft_client_id: "preview-microsoft" }),
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
