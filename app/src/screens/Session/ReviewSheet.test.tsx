import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../../App";
import type { GoalDTO, SessionDTO } from "../../api/schema";
import type { Route } from "../../routes";
import { createAppStore } from "../../store/store";
import { startSync, type Sync } from "../../store/sync";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN, fakeBlob, type DaemonData } from "../../test/fakeDaemon";
import { PtyViewsProvider } from "../../api/ptyView";
import { REPO, root, worker } from "../../test/fixtures";
import { standInViews } from "../../test/panes";
import { READY_TITLE, watchInbox } from "../Inbox/notify";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

beforeEach(() => localStorage.clear());
let sync: Sync | null = null;
afterEach(() => {
  sync?.stop();
  cleanup();
});

const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: Date.now() / 1000 - 600, goal: "Ship ls --json" };
const goal: GoalDTO = {
  root_id: "sup1",
  enabled: true,
  goal: "Ship ls --json",
  detail: "A --json flag on ls, with a test.",
  state: "done",
  goal_phase: "in_review",
  progress: 4,
  milestones: [
    { id: 1, position: 1, title: "Parse the flag", status: "passed", check_cmd: "npm test" },
    { id: 2, position: 2, title: "Print JSON", status: "passed", check_cmd: "npm run build" },
  ],
};

/* src/ls.py as the PR base has it and as the session's checkout has it: the fake daemon's diff is made from
   the two on every read, as the engine's follows the branch, so an edit shows in the next read. */
const LS_BASE = "import json\nprint(x)\n\ndef main():\n    pass\n";
const LS_PY = "import json\nprint(json.dumps(x))\nprint('done')\n\ndef main():\n    pass\n";
const TEST_PY = "def test_ls(): pass\n";
const HUNK = /@@ -1,5 \+1,6 @@/;

const READY = { ready: true, reasons: [], branch: "dev/ls", base: "main", diff: "" };
const onSession: Route = { screen: "session", root: "sup1", repo: REPO, agent: "sup1" };

function mount(data: Partial<DaemonData>, route: Route = onSession, fetch?: (daemon: FakeDaemon) => typeof globalThis.fetch) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], goal, sessionBase: { sup1: { "src/ls.py": LS_BASE } }, sessionFiles: { sup1: { "src/ls.py": LS_PY, "tests/test_ls.py": TEST_PY } }, ...data });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: fetch?.(daemon) ?? daemon.fetch });
  const store = createAppStore();
  sync = startSync({ client, store, connect: daemon.connect });
  const openUrl = vi.fn(() => Promise.resolve());
  render(
    <PtyViewsProvider value={standInViews}>
      <App store={store} client={client} refresh={sync.refresh} initialRoute={route} openUrl={openUrl} />
    </PtyViewsProvider>,
  );
  return { daemon, store, openUrl };
}

const nav = () => screen.getByRole("complementary", { name: "Sidebar" });
const sheet = () => screen.getByRole("dialog", { name: "Review & open PR" });
const hunk = () => within(sheet()).getAllByTestId("hunk")[0];
const gets = (daemon: FakeDaemon, path: string) => daemon.requests.filter((r) => r.method === "GET" && r.path === path).length;

describe("review", () => {
  it("shows nothing of the review before the engine says the session is ready; at that moment a notification and the goal panel's Ready for PR step, which opens the sheet", async () => {
    let readinessFails = true;
    let refused = 0;
    const { daemon, store } = mount({}, onSession, (daemon) => (input, init) => {
      // The readiness route failing (an engine without it) reads as unknown: nothing shown either.
      if (readinessFails && String(input).endsWith("/sessions/sup1/pr/readiness")) {
        refused++;
        return Promise.reject(new TypeError("connection refused"));
      }
      return daemon.fetch(input, init);
    });
    const notes: string[] = [];
    watchInbox(store, { notify: (title) => notes.push(title), count: () => undefined });
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    await waitFor(() => expect(refused).toBeGreaterThan(0));
    // Every milestone passed, but readiness can't be read: nothing about a PR anywhere, not from the milestones.
    const context = () => within(nav()).getByTestId("session-context");
    await waitFor(() => expect(context().textContent).toContain("Ship ls --json"));
    expect(context().textContent).not.toMatch(/PR/);
    expect(within(context()).getByTestId("goal-phase").textContent).toBe("In review");
    expect(screen.queryByTestId("goal-review")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    // Readable, and not ready: still nothing.
    readinessFails = false;
    daemon.publish("autopilot.milestone", { root_id: "sup1", payload: {} });
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/pr/readiness")).toBeGreaterThan(0));
    await new Promise((r) => setTimeout(r, 50));
    expect(context().textContent).not.toMatch(/PR/);
    expect(screen.queryByTestId("goal-review")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();

    // The engine judges it ready: session.pr_ready on the bus, the goal's phase ready_for_pr. A system
    // notification and the goal panel's step; nothing pops up over the session and nothing opens by itself.
    daemon.readyForPr("sup1", READY);
    await waitFor(() => expect(notes).toEqual([READY_TITLE]));
    await waitFor(() => expect(within(context()).getByTestId("goal-phase").textContent).toBe("Ready for PR"));
    expect(within(context()).getByTestId("goal-review").textContent).toContain("Review & open PR");
    // The PR step lives in the goal panel alone: no separate Ready for PR banner beside it.
    expect(screen.queryByTestId("pr-ready")).toBeNull();
    expect(context().textContent!.match(/Ready for PR/g)).toHaveLength(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();

    // The step opens the one sheet over the session: the session's diff, the files down the left, no worker named.
    fireEvent.click(within(context()).getByTestId("goal-review"));
    const dialog = await screen.findByRole("dialog", { name: "Review & open PR" });
    expect(dialog.textContent).toContain("dev/ls → main");
    const files = await within(dialog).findByRole("navigation", { name: "Changed files" });
    expect(files.textContent).toContain("src/ls.py+2-1");
    expect(files.textContent).toContain("tests/test_ls.py+1-0");
    expect(dialog.textContent).not.toContain("supervisor wrote");
    expect(dialog.textContent).not.toContain("w1");
    expect(screen.getByTestId("terminal")).toBeTruthy(); // still there, under the sheet
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("edits straight from the diff: a hunk, the full file, a new file and a deletion each become a commit on the session branch; a stale file and a busy supervisor are refused inline", async () => {
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    expect(within(sheet()).getByTestId("review-edit-pill").textContent).toContain("fully editable · supervisor idle");
    // The selected file is read from the checkout: the blob every edit of it is pinned to.
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/file")).toBe(1));
    expect(daemon.requests.find((r) => r.path === "/sessions/sup1/file")?.query).toEqual({ path: "src/ls.py" });

    // Click into the hunk: its new side is a box; ⌘S reads the file again, splices the lines in and writes the
    // whole back pinned to that blob; the diff is read again and shows the edit.
    expect(within(hunk()).getByRole("button", { name: HUNK })).toBeTruthy();
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    const box = within(hunk()).getByLabelText(/Edit src\/ls\.py @@/) as HTMLTextAreaElement;
    expect(box.value).toBe("import json\nprint(json.dumps(x))\nprint('done')\n\ndef main():\n    pass");
    fireEvent.change(box, { target: { value: "import json\nprint(json.dumps(x, indent=2))\nprint('done')\n\ndef main():\n    pass" } });
    const diffs = gets(daemon, "/sessions/sup1/diff");
    fireEvent.keyDown(box, { key: "s", metaKey: true });
    await waitFor(() => expect(daemon.sessionEdits).toHaveLength(1));
    expect(daemon.sessionEdits[0]).toEqual({ root: "sup1", path: "src/ls.py", content: "import json\nprint(json.dumps(x, indent=2))\nprint('done')\n\ndef main():\n    pass\n" });
    expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/sessions/sup1/file")!.body).toEqual({ path: "src/ls.py", content: daemon.sessionEdits[0].content, base: fakeBlob(LS_PY) });
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/diff")).toBeGreaterThan(diffs));
    await waitFor(() => expect(within(hunk()).queryByTestId("hunk-editor")).toBeNull());
    await waitFor(() => expect(hunk().textContent).toContain("+print(json.dumps(x, indent=2))"));
    expect(hunk().textContent).not.toContain("+print(json.dumps(x))\n");
    // The fresh diff pins the file again.
    await waitFor(() => expect(within(hunk()).getByRole("button", { name: HUNK })).toHaveProperty("disabled", false));

    // The file moves on under the sheet: the save reads a different blob, so nothing is written; the box keeps
    // its text and says so in one line, with Copy my edit / Discard / Reload.
    daemon.data.sessionFiles.sup1["src/ls.py"] = "something else\n";
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    fireEvent.change(within(hunk()).getByLabelText(/Edit src\/ls\.py @@/), { target: { value: "my edit" } });
    fireEvent.click(within(hunk()).getByRole("button", { name: /Save/ }));
    const bar = () => within(hunk()).getByTestId("save-bar");
    await waitFor(() => expect(within(bar()).getByTestId("draft-conflict").textContent).toBe("Changed since you started editing"));
    expect((within(hunk()).getByLabelText(/Edit src\/ls\.py @@/) as HTMLTextAreaElement).value).toBe("my edit");
    expect(within(bar()).queryByRole("button", { name: /Save/ })).toBeNull();
    expect(within(bar()).getByRole("button", { name: "Copy my edit" })).toBeTruthy();
    expect(daemon.sessionEdits).toHaveLength(1);
    fireEvent.click(within(bar()).getByRole("button", { name: "Discard" }));
    expect(within(hunk()).queryByTestId("hunk-editor")).toBeNull();

    // The supervisor got busy meanwhile: the engine's 409 busy shows in the bar (not a conflict).
    daemon.data.sessionFiles.sup1["src/ls.py"] = daemon.sessionEdits[0].content!;
    daemon.data.workspaces = daemon.data.workspaces.map((w) => ({ ...w, agents: w.agents?.map((a) => (a.id === "sup1" ? { ...a, status: "processing" } : a)) }));
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    fireEvent.click(within(hunk()).getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(within(bar()).getByRole("alert").textContent).toContain("Busy"));
    expect(daemon.sessionEdits).toHaveLength(1);
    fireEvent.keyDown(within(hunk()).getByLabelText(/Edit src\/ls\.py @@/), { key: "Escape" });
    expect(within(hunk()).queryByTestId("hunk-editor")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Review & open PR" })).toBeTruthy(); // Escape in the box didn't close the sheet
    daemon.data.workspaces = daemon.data.workspaces.map((w) => ({ ...w, agents: w.agents?.map((a) => (a.id === "sup1" ? { ...a, status: "idle" } : a)) }));

    // Full file: the whole file in a box, saved pinned to the blob it was read at.
    fireEvent.click(within(sheet()).getByRole("switch", { name: "Full file" }));
    const whole = within(sheet()).getByLabelText("Edit src/ls.py") as HTMLTextAreaElement;
    expect(whole.value).toBe(daemon.sessionEdits[0].content);
    fireEvent.change(whole, { target: { value: "import json\nprint(json.dumps(x, indent=2))\n" } });
    fireEvent.keyDown(whole, { key: "s", metaKey: true });
    await waitFor(() => expect(daemon.sessionEdits).toHaveLength(2));
    expect(daemon.sessionEdits[1]).toEqual({ root: "sup1", path: "src/ls.py", content: "import json\nprint(json.dumps(x, indent=2))\n" });
    expect(daemon.requests.filter((r) => r.method === "PUT").at(-1)!.body).toMatchObject({ base: fakeBlob(daemon.sessionEdits[0].content!) });
    await waitFor(() => expect(within(sheet()).queryByTestId("file-editor")).toBeNull());
    // The whole file is two lines now: the list says so.
    await waitFor(() => expect(within(sheet()).getByTitle("src/ls.py").closest("button")!.textContent).toContain("+1-4"));

    // A new file: its path and content, written with no base; it joins the list.
    fireEvent.click(within(sheet()).getByTestId("new-file"));
    fireEvent.change(within(sheet()).getByLabelText("New file path"), { target: { value: "docs/ls.md" } });
    fireEvent.change(within(sheet()).getByLabelText("New file content"), { target: { value: "# ls --json\n" } });
    fireEvent.click(within(sheet()).getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(daemon.sessionEdits).toHaveLength(3));
    expect(daemon.sessionEdits[2]).toEqual({ root: "sup1", path: "docs/ls.md", content: "# ls --json\n" });
    expect(daemon.requests.filter((r) => r.method === "PUT").at(-1)!.body).toEqual({ path: "docs/ls.md", content: "# ls --json\n" });
    await waitFor(() => expect(within(sheet()).getByTitle("docs/ls.md")).toBeTruthy());
    expect(within(sheet()).getByRole("navigation", { name: "Changed files" }).textContent).toContain("docs/ls.md+1-0");

    // A file's ✕ deletes it from the branch, pinned to the blob it was read at; it leaves the list.
    await waitFor(() => expect(within(sheet()).getByRole("button", { name: "Delete tests/test_ls.py" })).toBeTruthy());
    fireEvent.click(within(sheet()).getByTitle("tests/test_ls.py"));
    await waitFor(() => expect(within(sheet()).getByRole("button", { name: "Delete tests/test_ls.py" })).toHaveProperty("disabled", false));
    fireEvent.click(within(sheet()).getByRole("button", { name: "Delete tests/test_ls.py" }));
    await waitFor(() => expect(daemon.sessionEdits).toHaveLength(4));
    expect(daemon.sessionEdits[3]).toEqual({ root: "sup1", path: "tests/test_ls.py", content: null });
    expect(daemon.requests.find((r) => r.method === "DELETE")!.body).toEqual({ path: "tests/test_ls.py", base: fakeBlob(TEST_PY) });
    expect(daemon.data.sessionFiles.sup1["tests/test_ls.py"]).toBeUndefined();
    await waitFor(() => expect(within(sheet()).queryByTitle("tests/test_ls.py")).toBeNull());
  });

  it("keeps an unsaved edit through the engine re-checking the session and the diff being read again, and reads a diff refused after readiness was already ready", async () => {
    // The first diff read after the save is refused although readiness says ready the whole time (the two
    // answers crossed): the sheet reads it again on its own, a few times at most.
    let refuse = 0;
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } }, (daemon) => async (input, init) => {
      if ((init?.method ?? "GET") === "GET" && String(input).endsWith("/sessions/sup1/diff") && refuse > 0) {
        refuse--;
        daemon.requests.push({ method: "GET", path: "/sessions/sup1/diff", query: {}, body: undefined });
        return new Response(JSON.stringify({ error: { code: "not_ready", message: "the session isn't ready for a PR" }, reasons: ["a merge into the session branch is under way"] }), { status: 409, headers: { "Content-Type": "application/json" } });
      }
      return daemon.fetch(input, init);
    });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/file")).toBe(1));
    // An edit typed, not saved; then the diff is read again (a stale notice, say) and refused once while ready.
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    const box = () => within(hunk()).getByLabelText(/Edit src\/ls\.py @@/) as HTMLTextAreaElement;
    fireEvent.change(box(), { target: { value: "UNSAVED USER EDIT" } });
    // The engine judges the session again (readiness not ready); ready again, the diff is read afresh, and that
    // read is the one refused although readiness already says ready.
    daemon.data.sessionReview.sup1 = { ...READY, ready: false, reasons: ["re-checking"] };
    daemon.publish("session.edited", { root_id: "sup1", agent_id: "sup1", payload: { path: "src/ls.py", commit: "c", deleted: false } });
    await within(sheet()).findByTestId("review-rechecking");
    expect(box().value).toBe("UNSAVED USER EDIT"); // the box stays, with what was typed
    refuse = 1;
    daemon.readyForPr("sup1", READY);
    // Ready: the diff is read afresh, refused this once (readiness already says ready), and read again until it
    // loads; the box is there throughout, with the edit, and Save comes back once the file is pinned again.
    await waitFor(() => expect(refuse).toBe(0));
    expect(box().value).toBe("UNSAVED USER EDIT");
    await waitFor(() => expect(within(sheet()).queryByTestId("review-rechecking")).toBeNull(), { timeout: 4000 });
    expect(box().value).toBe("UNSAVED USER EDIT");
    await waitFor(() => expect(within(hunk()).getByRole("button", { name: /Save/ })).toHaveProperty("disabled", false));
    // Discard clears it; opening the hunk again shows the file's own lines.
    fireEvent.click(within(hunk()).getByRole("button", { name: /Discard/ }));
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    expect(box().value).toBe("import json\nprint(json.dumps(x))\nprint('done')\n\ndef main():\n    pass");
  });

  it("never puts the engine's text into an open box: a file that changed while brindle re-checked shows a conflict line on a hunk draft (kept with its original lines where the hunk moved) and on a full-file draft, each with the edit intact", async () => {
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/file")).toBe(1));

    // A hunk draft, typed; while the engine re-checks, a worker's commit puts two lines above and changes the file.
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    const box = () => within(sheet()).getByLabelText(/Edit src\/ls\.py @@/) as HTMLTextAreaElement;
    fireEvent.change(box(), { target: { value: "MY HUNK EDIT" } });
    daemon.data.sessionReview.sup1 = { ...READY, ready: false, reasons: ["re-checking"] };
    daemon.publish("session.edited", { root_id: "sup1", agent_id: "sup1", payload: { path: "src/ls.py", commit: "c", deleted: false } });
    await within(sheet()).findByTestId("review-rechecking");
    daemon.data.sessionFiles.sup1["src/ls.py"] = "# one\n# two\n" + LS_PY;
    daemon.readyForPr("sup1", READY);
    await waitFor(() => expect(within(sheet()).queryByTestId("review-rechecking")).toBeNull(), { timeout: 4000 });
    // The fresh diff's hunk is elsewhere: the draft shows above the hunks with the lines it was made from, its
    // text untouched, and the conflict line instead of Save.
    const moved = await within(sheet()).findByTestId("draft-moved");
    expect(within(moved).getByTestId("draft-snapshot").textContent).toContain("print(json.dumps(x))");
    expect(box().value).toBe("MY HUNK EDIT");
    await within(moved).findByTestId("draft-conflict");
    expect(within(moved).queryByRole("button", { name: /Save/ })).toBeNull();
    expect(daemon.sessionEdits).toHaveLength(0);
    // Reload reads the diff and file again; the draft is still there, still in conflict; Discard drops it.
    fireEvent.click(within(moved).getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/file")).toBeGreaterThan(2));
    expect(box().value).toBe("MY HUNK EDIT");
    fireEvent.click(within(within(sheet()).getByTestId("draft-moved")).getByRole("button", { name: "Discard" }));
    expect(within(sheet()).queryByTestId("draft-moved")).toBeNull();
    await waitFor(() => expect(within(hunk()).getByRole("button", { name: /@@/ })).toHaveProperty("disabled", false));

    // A full-file draft, the same way: the file changes under it, the text stays, the conflict line shows.
    fireEvent.click(within(sheet()).getByRole("switch", { name: "Full file" }));
    const whole = () => within(sheet()).getByLabelText("Edit src/ls.py") as HTMLTextAreaElement;
    expect(whole().value).toBe("# one\n# two\n" + LS_PY);
    fireEvent.change(whole(), { target: { value: "MY WHOLE FILE" } });
    daemon.data.sessionReview.sup1 = { ...READY, ready: false, reasons: ["re-checking"] };
    daemon.publish("session.edited", { root_id: "sup1", agent_id: "sup1", payload: { path: "src/ls.py", commit: "c2", deleted: false } });
    await within(sheet()).findByTestId("review-rechecking");
    daemon.data.sessionFiles.sup1["src/ls.py"] = "# three\n" + LS_PY;
    daemon.readyForPr("sup1", READY);
    await waitFor(() => expect(within(sheet()).queryByTestId("review-rechecking")).toBeNull(), { timeout: 4000 });
    await waitFor(() => expect(within(sheet()).getByTestId("draft-conflict").textContent).toBe("Changed since you started editing"));
    expect(whole().value).toBe("MY WHOLE FILE");
    expect(daemon.sessionEdits).toHaveLength(0);
    // A save is never sent against a changed file: the only PUT the whole session saw is none.
    expect(daemon.requests.filter((r) => r.method === "PUT")).toHaveLength(0);
  });

  it("keeps a draft whose file dropped out of the diff (reverted to the base, or deleted) listed, marked, with its original lines and the conflict line, until it is discarded", async () => {
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    const list = () => within(sheet()).getByRole("navigation", { name: "Changed files" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/file")).toBe(1));

    // A hunk draft on src/ls.py; while brindle re-checks, the file goes back to what the base has: it is not in
    // the next diff at all. The draft keeps it in the list, marked, with the lines the edit was made from.
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    const box = () => within(sheet()).getByLabelText(/Edit src\/ls\.py @@/) as HTMLTextAreaElement;
    fireEvent.change(box(), { target: { value: "MY HUNK EDIT" } });
    daemon.data.sessionReview.sup1 = { ...READY, ready: false, reasons: ["re-checking"] };
    daemon.publish("session.edited", { root_id: "sup1", agent_id: "sup1", payload: { path: "src/ls.py", commit: "c", deleted: false } });
    await within(sheet()).findByTestId("review-rechecking");
    daemon.data.sessionFiles.sup1["src/ls.py"] = LS_BASE;
    daemon.readyForPr("sup1", READY);
    await waitFor(() => expect(within(sheet()).queryByTestId("review-rechecking")).toBeNull(), { timeout: 4000 });
    const entry = await within(list()).findByTestId("draft-orphan");
    expect(entry.textContent).toContain("src/ls.py");
    expect(entry.textContent).toContain("not in the diff anymore");
    expect(within(list()).queryByRole("button", { name: "Delete src/ls.py" })).toBeNull();
    const moved = await within(sheet()).findByTestId("draft-moved");
    expect(within(moved).getByTestId("draft-snapshot").textContent).toContain("print(json.dumps(x))");
    expect(box().value).toBe("MY HUNK EDIT");
    await waitFor(() => expect(within(moved).getByTestId("draft-conflict").textContent).toBe("Changed since you started editing"));
    expect(within(moved).getByRole("button", { name: "Copy my edit" })).toBeTruthy();
    expect(within(moved).queryByRole("button", { name: /Save/ })).toBeNull();
    expect(within(sheet()).getByTestId("file-gone").textContent).toContain("not in the diff anymore");
    // Discard: the file leaves the list; what the diff does have is shown.
    fireEvent.click(within(moved).getByRole("button", { name: "Discard" }));
    expect(within(list()).queryByTestId("draft-orphan")).toBeNull();
    expect(within(sheet()).queryByTitle("src/ls.py")).toBeNull();
    await waitFor(() => expect(within(sheet()).getByTestId("file-diff").textContent).toContain("tests/test_ls.py"));

    // A full-file draft the same way, on a file then deleted from the checkout (back to the base, which lacks
    // it): the diff is empty, the file is listed for its draft alone, the text kept, the conflict line shown.
    await waitFor(() => expect(within(sheet()).getByRole("switch", { name: "Full file" })).toHaveProperty("disabled", false));
    fireEvent.click(within(sheet()).getByRole("switch", { name: "Full file" }));
    const whole = () => within(sheet()).getByLabelText("Edit tests/test_ls.py") as HTMLTextAreaElement;
    fireEvent.change(whole(), { target: { value: "MY WHOLE FILE" } });
    daemon.data.sessionReview.sup1 = { ...READY, ready: false, reasons: ["re-checking"] };
    daemon.publish("session.edited", { root_id: "sup1", agent_id: "sup1", payload: { path: "tests/test_ls.py", commit: "c2", deleted: true } });
    await within(sheet()).findByTestId("review-rechecking");
    delete daemon.data.sessionFiles.sup1["tests/test_ls.py"];
    daemon.readyForPr("sup1", READY);
    await waitFor(() => expect(within(sheet()).queryByTestId("review-rechecking")).toBeNull(), { timeout: 4000 });
    await waitFor(() => expect(within(list()).getByTestId("draft-orphan").textContent).toContain("tests/test_ls.py"));
    expect(whole().value).toBe("MY WHOLE FILE");
    await waitFor(() => expect(within(sheet()).getByTestId("draft-conflict").textContent).toBe("Changed since you started editing"));
    expect(within(sheet()).getByRole("button", { name: "Copy my edit" })).toBeTruthy();
    expect(daemon.requests.filter((r) => r.method === "PUT")).toHaveLength(0);
    fireEvent.click(within(sheet()).getByRole("button", { name: "Discard" }));
    expect(within(list()).queryByTestId("draft-orphan")).toBeNull();
    expect(within(sheet()).getByText("No changes against main.")).toBeTruthy();
  });

  it("keeps the new file being written (its path and text) while another file or the PR step is looked at, until it is saved or discarded", async () => {
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    fireEvent.click(within(sheet()).getByTestId("new-file"));
    fireEvent.change(within(sheet()).getByLabelText("New file path"), { target: { value: "docs/ls.md" } });
    fireEvent.change(within(sheet()).getByLabelText("New file content"), { target: { value: "# ls --json\n" } });
    // Another file: the new file stays in the list, unsaved, and comes back as it was.
    fireEvent.click(within(sheet()).getByTitle("src/ls.py"));
    expect(within(sheet()).queryByTestId("new-file-editor")).toBeNull();
    expect(within(sheet()).getByTestId("new-file-draft").textContent).toContain("docs/ls.md");
    fireEvent.click(within(sheet()).getByTestId("new-file-draft"));
    expect((within(sheet()).getByLabelText("New file path") as HTMLInputElement).value).toBe("docs/ls.md");
    expect((within(sheet()).getByLabelText("New file content") as HTMLTextAreaElement).value).toBe("# ls --json\n");
    // The PR step and back: the same.
    fireEvent.click(within(sheet()).getByTestId("open-pr-step"));
    await within(sheet()).findByLabelText("PR title");
    fireEvent.click(within(sheet()).getByRole("button", { name: "‹ Back" }));
    expect((within(sheet()).getByLabelText("New file path") as HTMLInputElement).value).toBe("docs/ls.md");
    expect((within(sheet()).getByLabelText("New file content") as HTMLTextAreaElement).value).toBe("# ls --json\n");
    expect(daemon.requests.filter((r) => r.method === "PUT")).toHaveLength(0);
    // Discard drops it for good: "+ New file…" starts empty.
    fireEvent.click(within(sheet()).getByRole("button", { name: /Discard/ }));
    expect(within(sheet()).queryByTestId("new-file-draft")).toBeNull();
    fireEvent.click(within(sheet()).getByTestId("new-file"));
    expect((within(sheet()).getByLabelText("New file path") as HTMLInputElement).value).toBe("");
  });

  it("locks the new file for the length of its write wherever the person goes meanwhile, and clears the draft only once the write lands", async () => {
    // The PUT of the new file is held until the test lets it through.
    let release: (() => void) | null = null;
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } }, (daemon) => async (input, init) => {
      if ((init?.method ?? "GET") === "PUT" && String(input).endsWith("/sessions/sup1/file")) await new Promise<void>((r) => (release = r));
      return daemon.fetch(input, init);
    });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    fireEvent.click(within(sheet()).getByTestId("new-file"));
    fireEvent.change(within(sheet()).getByLabelText("New file path"), { target: { value: "docs/ls.md" } });
    fireEvent.change(within(sheet()).getByLabelText("New file content"), { target: { value: "# ls --json\n" } });
    fireEvent.click(within(sheet()).getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(release).not.toBeNull());
    const pathBox = () => within(sheet()).getByLabelText("New file path") as HTMLInputElement;
    const textBox = () => within(sheet()).getByLabelText("New file content") as HTMLTextAreaElement;
    expect(pathBox().disabled).toBe(true);
    expect(textBox().disabled).toBe(true);
    // Away to another file and back: still locked, with what was sent; nothing can be typed over it.
    fireEvent.click(within(sheet()).getByTitle("src/ls.py"));
    fireEvent.click(within(sheet()).getByTestId("new-file-draft"));
    expect(pathBox().disabled).toBe(true);
    expect(textBox().disabled).toBe(true);
    expect(textBox().value).toBe("# ls --json\n");
    expect(within(sheet()).getByRole("button", { name: /Save/ })).toHaveProperty("disabled", true);
    expect(within(sheet()).getByRole("button", { name: /Discard/ })).toHaveProperty("disabled", true);
    expect(daemon.sessionEdits).toHaveLength(0);
    // The write lands: the draft clears, the file joins the list.
    release!();
    await waitFor(() => expect(daemon.sessionEdits).toHaveLength(1));
    await waitFor(() => expect(within(sheet()).queryByTestId("new-file-draft")).toBeNull());
    expect(within(sheet()).queryByTestId("new-file-editor")).toBeNull();
    await waitFor(() => expect(within(sheet()).getByTitle("docs/ls.md")).toBeTruthy());
  });

  it("keeps showing saved edits while the engine judges readiness again, then opens the PR when ready", async () => {
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } }, (daemon) => async (input, init) => {
      // Readiness changes before the edit event; the diff remains available throughout.
      if ((init?.method ?? "GET") === "PUT" && String(input).endsWith("/sessions/sup1/file")) daemon.data.sessionReview.sup1 = { ...READY, ready: false, reasons: ["the completion audit hasn't approved the session's current commit"] };
      return daemon.fetch(input, init);
    });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/file")).toBe(1));
    fireEvent.click(within(hunk()).getByRole("button", { name: HUNK }));
    const box = within(hunk()).getByLabelText(/Edit src\/ls\.py @@/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "import json\nprint(json.dumps(x, indent=2))\nprint('done')\n\ndef main():\n    pass" } });
    fireEvent.keyDown(box, { key: "s", metaKey: true });
    await waitFor(() => expect(daemon.sessionEdits).toHaveLength(1));
    // The sheet shows the saved edit even while PR readiness is being checked.
    const note = await within(sheet()).findByTestId("review-rechecking");
    expect(note.textContent).toContain("the completion audit hasn't approved");
    await waitFor(() => expect(hunk().textContent).toContain("+print(json.dumps(x, indent=2))"));
    const diffsWhileRechecking = gets(daemon, "/sessions/sup1/diff");
    expect(within(sheet()).getByRole("navigation", { name: "Changed files" })).toBeTruthy();
    // Ready again: its hunks remain editable and the PR step drafts.
    daemon.readyForPr("sup1", READY);
    await waitFor(() => expect(within(sheet()).queryByTestId("review-rechecking")).toBeNull());
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/diff")).toBeGreaterThan(diffsWhileRechecking));
    await waitFor(() => expect(hunk().textContent).toContain("+print(json.dumps(x, indent=2))"));
    await waitFor(() => expect(within(hunk()).getByRole("button", { name: HUNK })).toHaveProperty("disabled", false));
    expect(within(sheet()).queryByTestId("file-stale")).toBeNull();
    fireEvent.click(within(sheet()).getByTestId("open-pr-step"));
    const title = (await within(sheet()).findByLabelText("PR title")) as HTMLInputElement;
    await waitFor(() => expect(title.value).toBe("Ship ls --json"));
  });

  it("refuses to edit from hunks that no longer describe the file (it changed after the diff was read, before the file was), reads the diff again once, and keeps the sheet up while brindle re-checks the session", async () => {
    // The file moves on between the diff being read and the file being read for its blob: the hunk's lines are
    // not where the diff says they are, so splicing them in would write the wrong thing.
    const { daemon } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } }, (daemon) => (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/sessions/sup1/file") && (init?.method ?? "GET") === "GET" && daemon.data.sessionFiles.sup1["src/ls.py"] === LS_PY) {
        daemon.data.sessionFiles.sup1["src/ls.py"] = "# a line slipped in above\n" + LS_PY;
      }
      return daemon.fetch(input, init);
    });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    // Nothing is spliced from hunks that don't describe the file: the diff is read again (once), and the fresh
    // one, made from the file as it now is, shows the line that slipped in and can be edited again.
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/diff")).toBe(2));
    await waitFor(() => expect(gets(daemon, "/sessions/sup1/file")).toBe(2));
    await waitFor(() => expect(hunk().textContent).toContain("+# a line slipped in above"));
    expect(within(sheet()).queryByTestId("file-stale")).toBeNull();
    expect(within(hunk()).getByRole("button", { name: /@@/ })).toHaveProperty("disabled", false);
    await new Promise((r) => setTimeout(r, 150));
    expect(gets(daemon, "/sessions/sup1/diff")).toBe(2);
    expect(daemon.sessionEdits).toHaveLength(0);
    expect(daemon.requests.filter((r) => r.method === "PUT")).toHaveLength(0);

    // The engine judges the session again while the sheet is open (readiness no longer ready): the sheet stays,
    // with what it shows, and says so quietly.
    daemon.data.sessionReview.sup1 = { ...READY, ready: false, reasons: ["the completion audit hasn't approved the session's current commit"] };
    daemon.publish("session.edited", { root_id: "sup1", agent_id: "sup1", payload: { path: "src/ls.py", commit: "c", deleted: false } });
    const note = await within(sheet()).findByTestId("review-rechecking");
    expect(note.textContent).toContain("brindle is re-checking the session: the completion audit hasn't approved");
    expect(screen.getByRole("dialog", { name: "Review & open PR" })).toBeTruthy();
    expect(within(sheet()).getByRole("navigation", { name: "Changed files" })).toBeTruthy();
    expect(within(sheet()).queryByTestId("review-not-ready")).toBeNull();
    // Ready again: the note goes.
    daemon.readyForPr("sup1", READY);
    await waitFor(() => expect(within(sheet()).queryByTestId("review-rechecking")).toBeNull());
    expect(screen.getByRole("dialog", { name: "Review & open PR" })).toBeTruthy();
  });

  it("opens the PR with the title and body pre-filled from the engine's draft; when the person's settings let brindle open it, shows that it did, with the link, and keeps the diff editable", async () => {
    const { daemon, openUrl } = mount({ sessionReview: { sup1: READY } }, { ...onSession, sheet: { kind: "review" } });
    await screen.findByRole("dialog", { name: "Review & open PR" });
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    fireEvent.click(within(sheet()).getByTestId("open-pr-step"));
    const title = (await within(sheet()).findByLabelText("PR title")) as HTMLInputElement;
    await waitFor(() => expect(title.value).toBe("Ship ls --json"));
    expect((within(sheet()).getByLabelText("PR description") as HTMLTextAreaElement).value).toBe("A --json flag on ls, with a test.\n\nMilestones, each verified by its check:\n- Parse the flag\n- Print JSON");
    fireEvent.change(title, { target: { value: "ls: a --json flag" } });
    fireEvent.click(within(sheet()).getByTestId("open-pr"));
    await waitFor(() => expect(daemon.prs).toHaveLength(1));
    expect(daemon.prs[0]).toMatchObject({ root: "sup1", title: "ls: a --json flag", body: "A --json flag on ls, with a test.\n\nMilestones, each verified by its check:\n- Parse the flag\n- Print JSON", draft: false });
    // The job's end brings the URL: a link, opened in the system browser.
    const opened = await within(sheet()).findByTestId("pr-opened");
    expect(opened.textContent).toContain(daemon.prs[0].url);
    fireEvent.click(within(sheet()).getByRole("button", { name: "Open it ↗" }));
    expect(openUrl).toHaveBeenCalledWith(daemon.prs[0].url);
    // The goal moves on to PR open, its link the PR's; the Review & open PR step is gone.
    const context = () => within(nav()).getByTestId("session-context");
    await waitFor(() => expect(within(context()).getByTestId("goal-phase").textContent).toBe("PR open"));
    expect(within(context()).queryByTestId("goal-review")).toBeNull();
    fireEvent.click(within(context()).getByTestId("goal-pr"));
    expect(openUrl).toHaveBeenLastCalledWith(daemon.prs[0].url);
    sync?.stop();
    cleanup();

    // brindle opened the PR itself (the engine's auto-PR setting): the sheet says so, with the link and no Open
    // PR of its own, and the diff stays editable: edits go to the branch, which is the PR's.
    const auto = mount(
      {
        sessionReview: { sup1: { ...READY, pr: { url: "https://github.com/example/proj/pull/7" } } },
        config: { user: {}, repo: { auto_pr: true }, local: {}, keys: [] },
        goal: { ...goal, goal_phase: "pr_open", pr_url: "https://github.com/example/proj/pull/7" },
      },
      { ...onSession, sheet: { kind: "review" } },
    );
    await screen.findByRole("dialog", { name: "Review & open PR" });
    const notice = await within(sheet()).findByTestId("pr-opened");
    expect(notice.textContent).toContain("brindle opened the PR");
    expect(notice.textContent).toContain("https://github.com/example/proj/pull/7");
    expect(within(sheet()).queryByTestId("open-pr-step")).toBeNull();
    fireEvent.click(within(sheet()).getByRole("button", { name: "Open the PR ↗" }));
    expect(auto.openUrl).toHaveBeenCalledWith("https://github.com/example/proj/pull/7");
    await within(sheet()).findByRole("navigation", { name: "Changed files" });
    await waitFor(() => expect(within(hunk()).getByRole("button", { name: HUNK })).toHaveProperty("disabled", false));
    // The sidebar says it once, in the goal's lifecycle: PR open, with its link.
    expect(within(nav()).getByTestId("goal-phase").textContent).toBe("PR open");
    expect(within(nav()).getByTestId("goal-pr").title).toBe("https://github.com/example/proj/pull/7");
    expect(screen.queryByTestId("pr-ready")).toBeNull();
  });
});
