import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DaemonContext } from "../../api/context";
import { createAppStore } from "../../store/store";
import { startSync, type Sync } from "../../store/sync";
import { StoreContext, useAppState } from "../../store/useStore";
import { FakeDaemon, fakeBlob } from "../../test/fakeDaemon";
import { worker } from "../../test/fixtures";
import { BUSY, DiffView, EditPill, useDiff, type EditAccess } from "./DiffView";

/* src/ls.py as it stands in the worktree: what the diff's first hunk shows, with lines around it. */
const LS_PY = "import json\nprint(json.dumps(x))\nprint('done')\n\ndef main():\n    pass\n";

const DIFF = [
  "diff --git a/src/ls.py b/src/ls.py",
  `index 1111111..${fakeBlob(LS_PY).slice(0, 7)} 100644`,
  "--- a/src/ls.py",
  "+++ b/src/ls.py",
  "@@ -1,2 +1,3 @@",
  " import json",
  "-print(x)",
  "+print(json.dumps(x))",
  "+print('done')",
  "diff --git a/tests/test_ls.py b/tests/test_ls.py",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/tests/test_ls.py",
  "@@ -0,0 +1 @@",
  "+def test_ls(): pass",
  // git C-quotes a name with a quote or a non-ASCII character, and ends the "+++" name with a tab.
  'diff --git "a/docs/say \\"hi\\" caf\\303\\251.md" "b/docs/say \\"hi\\" caf\\303\\251.md"',
  "new file mode 100644",
  "--- /dev/null",
  '+++ "b/docs/say \\"hi\\" caf\\303\\251.md"\t',
  "@@ -0,0 +1 @@",
  "+bonjour",
  "",
].join("\n");

const WS = worker.id;
const route = (action: string) => `/workspaces/${WS}/${action}`;
const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

let sync: Sync | null = null;
afterEach(() => {
  sync?.stop();
  cleanup();
});

/* The diff as the review sheet shows it for a review item: edits allowed while no agent in the workspace is busy. */
function Harness() {
  const workspace = useAppState((s) => s.workspaces.find((w) => w.id === WS));
  const state = useDiff(WS, workspace?.base_branch);
  const working = (workspace?.agents ?? []).filter((a) => BUSY.has(a.status));
  const access: EditAccess = { workspaceId: WS, editable: working.length === 0, why: "worker busy" };
  return (
    <div className="relative">
      <EditPill access={access} />
      <DiffView state={state} access={access} />
    </div>
  );
}

function mount(daemon: FakeDaemon) {
  const client = daemon.client();
  const store = createAppStore();
  sync = startSync({ client, store, connect: daemon.connect });
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <Harness />
      </StoreContext.Provider>
    </DaemonContext.Provider>,
  );
}

const diffOf = (diff: string) => ({ workspace_id: WS, base: "main", diff, stat: "" });
const withDiff = () => new FakeDaemon({ workspaces: [worker], diffs: { [WS]: diffOf(DIFF) }, files: { [WS]: { "src/ls.py": LS_PY } } });

describe("DiffView", () => {
  it("edits a hunk in place while the worker is idle: ⌘S commits it on the branch, a stale file is refused inline", async () => {
    const daemon = withDiff();
    mount(daemon);
    await screen.findByRole("list", { name: "Changed files" });
    // The worker is mid-turn: the pill says so, and no hunk opens.
    expect(screen.getByTestId("edit-pill").textContent).toContain("editing off · worker busy");
    const hunk = () => screen.getAllByTestId("hunk")[0];
    expect(within(hunk()).getByRole("button", { name: /@@ -1,2 \+1,3 @@/ })).toHaveProperty("disabled", true);

    // The worker goes idle: editing is on; the hunk's new side becomes a box to edit.
    daemon.data.workspaces = daemon.data.workspaces.map((w) => (w.id === WS ? { ...w, agents: w.agents!.map((a) => ({ ...a, status: "idle" })) } : w));
    daemon.publish("agent.status", { agent_id: "w1", workspace_id: WS, payload: { status: "idle" } });
    await waitFor(() => expect(screen.getByTestId("edit-pill").textContent).toContain("editable · worker idle"));
    fireEvent.click(within(hunk()).getByRole("button", { name: /@@ -1,2 \+1,3 @@/ }));
    const box = within(hunk()).getByLabelText("Edit src/ls.py @@ -1,2 +1,3 @@") as HTMLTextAreaElement;
    expect(box.value).toBe("import json\nprint(json.dumps(x))\nprint('done')");
    expect(hunk().textContent).not.toContain("-print(x)"); // the old side gives way to the box

    // Escape discards; ⌘S saves: the file is read ({path, content, base}), the hunk's lines replaced in it,
    // the whole put back pinned to the blob the diff named.
    fireEvent.keyDown(box, { key: "Escape" });
    expect(within(hunk()).queryByTestId("hunk-editor")).toBeNull();
    fireEvent.click(within(hunk()).getByRole("button", { name: /@@ -1,2 \+1,3 @@/ }));
    const again = within(hunk()).getByLabelText("Edit src/ls.py @@ -1,2 +1,3 @@");
    fireEvent.change(again, { target: { value: "import json\nprint(json.dumps(x, indent=2))\nprint('done')" } });
    const diffs = () => daemon.requests.filter((r) => r.path === route("diff")).length;
    const before = diffs();
    fireEvent.keyDown(again, { key: "s", metaKey: true });
    await waitFor(() => expect(daemon.edits).toHaveLength(1));
    expect(daemon.requests.find((r) => r.method === "GET" && r.path === route("file"))?.query).toEqual({ path: "src/ls.py" });
    expect(daemon.edits[0]).toEqual({ workspace: WS, path: "src/ls.py", content: "import json\nprint(json.dumps(x, indent=2))\nprint('done')\n\ndef main():\n    pass\n" });
    expect(daemon.requests.find((r) => r.method === "PUT")!.body).toMatchObject({ path: "src/ls.py", base: fakeBlob(LS_PY).slice(0, 7) });
    await waitFor(() => expect(diffs()).toBe(before + 1)); // the diff is fetched again
    await waitFor(() => expect(within(hunk()).queryByTestId("hunk-editor")).toBeNull());

    // The file moved on under the diff shown: the read's base no longer matches, nothing is written.
    daemon.data.files[WS]["src/ls.py"] = "something else\n";
    fireEvent.click(within(hunk()).getByRole("button", { name: /@@ -1,2 \+1,3 @@/ }));
    fireEvent.click(within(hunk()).getByRole("button", { name: /Save/ }));
    const bar = within(hunk()).getByTestId("save-bar");
    await waitFor(() => expect(within(bar).getByRole("alert").textContent).toContain("Stale"));
    expect(daemon.edits).toHaveLength(1);
    expect(daemon.requests.filter((r) => r.method === "PUT")).toHaveLength(1);

    // The worker gets busy again: the engine's 409 busy shows in the bar, and the box can be discarded.
    daemon.data.files[WS]["src/ls.py"] = LS_PY;
    daemon.data.workspaces = daemon.data.workspaces.map((w) => (w.id === WS ? { ...w, agents: w.agents!.map((a) => ({ ...a, status: "processing" })) } : w));
    fireEvent.click(within(hunk()).getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(within(bar).getByRole("alert").textContent).toContain("Busy"));
    expect(daemon.edits).toHaveLength(1);
  });

  it("lists the changed files and shows the selected one's unified diff, with a retry when loading fails", async () => {
    const daemon = withDiff();
    daemon.data.workspaces = [];
    mount(daemon);
    await waitFor(() => expect(screen.getByText("no such workspace")).toBeTruthy());

    daemon.data.workspaces = [worker];
    fireEvent.click(button("Retry"));
    const files = await screen.findByRole("list", { name: "Changed files" });
    expect(files.textContent).toContain("src/ls.py+2-1");
    expect(files.textContent).toContain("tests/test_ls.py+1-0");
    expect(screen.getByTestId("file-diff").textContent).toContain("+print(json.dumps(x))");

    fireEvent.click(screen.getByTitle("tests/test_ls.py"));
    expect(screen.getByTestId("file-diff").textContent).toContain("+def test_ls(): pass");
    expect(screen.getByTestId("file-diff").textContent).not.toContain("print(x)");

    // The quoted file is its own entry, under its real name, and adds nothing to the one before it.
    expect(files.textContent).toContain('docs/say "hi" café.md+1-0');
    fireEvent.click(screen.getByTitle('docs/say "hi" café.md'));
    expect(screen.getByTestId("file-diff").textContent).toContain("+bonjour");
    fireEvent.click(screen.getByTitle("tests/test_ls.py"));
    expect(screen.getByTestId("file-diff").textContent).not.toContain("bonjour");
  });

  it("shows the engine's untracked-files summary on its own, and keeps it out of the last file's patch", async () => {
    const summary = "Untracked files (3):\n  notes.md\n  \"caf\\303\\251.txt\"\n  docs/ (1 file)";
    const daemon = new FakeDaemon({ workspaces: [worker], diffs: { [WS]: diffOf(summary) } });
    mount(daemon);
    const list = await screen.findByRole("region", { name: "Untracked files" });
    expect(list.textContent).toContain("Untracked files (3)");
    expect([...list.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["notes.md", "café.txt", "docs/ (1 file)"]);
    expect(screen.queryByText("No changes against main.")).toBeNull();
    expect(screen.queryByRole("list", { name: "Changed files" })).toBeNull();
    cleanup();
    sync?.stop();

    const both = new FakeDaemon({ workspaces: [worker], diffs: { [WS]: diffOf(`${DIFF}\nUntracked files (1):\n  notes.md`) } });
    mount(both);
    const files = await screen.findByRole("list", { name: "Changed files" });
    expect(files.textContent).toContain('docs/say "hi" café.md+1-0');
    fireEvent.click(screen.getByTitle('docs/say "hi" café.md'));
    expect(screen.getByTestId("file-diff").textContent).not.toContain("Untracked");
    expect(screen.getByRole("region", { name: "Untracked files" }).textContent).toContain("notes.md");
  });
});
