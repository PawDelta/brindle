import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it } from "vitest";
import type { AnalysisDoc, DaemonClient, DocScope, LibraryFolder } from "../../api/client";
import { LibraryRail } from "../../components/LibraryRail";
import { useLibraryFolders } from "../../components/useLibraryFolders";
import type { Route } from "../../routes";
import { setReaderDoc } from "../../store/libraryReader";
import { DocsView, type DocsClient } from "../Knowledge/DocsView";
import { docQueue } from "./libraryStages";

afterEach(() => {
  cleanup();
  setReaderDoc(null);
  localStorage.clear();
});

const TEAM: DocScope = { kind: "group", id: "t1", name: "Founding Team" };
const docs: AnalysisDoc[] = [
  { id: "cloud", title: "Cloud workers", summary: "Workers on the customer's machine.", scope: TEAM, updated_at: 900 },
  { id: "coedit", title: "Live co-editing", scope: TEAM, updated_at: 800 },
  { id: "outbox", title: "Outbox for reports", scope: TEAM, updated_at: 700 },
  { id: "release", title: "Release flow", scope: { kind: "user", id: "u1" }, updated_at: 600 },
];
const repos = [{ name: "pawdelta", repo_root: "/src/pawdelta" }];
const queue = docQueue([{ kind: "review_requested", reason: "r", doc: "cloud" }]);

/* The Library as the daemon keeps it, changing as folders are made and designs moved. */
function library() {
  const folders: LibraryFolder[] = [{ id: "eng", name: "Engine", visibility: "shared", repo: "/src/pawdelta" }, { id: "app", name: "Desktop app", visibility: "shared", repo: "/src/pawdelta" }];
  const placed: Record<string, string | undefined> = { cloud: "eng", coedit: "app", outbox: "eng" };
  const details: Record<string, object> = {
    // A shared design's decisions are its review state's (GET /analysis-docs/{id} `design.decisions`), confirmed per reader.
    cloud: { design: { rev: 3, ready: false, reviewers: [{ sub: "dana@x.com" }, { sub: "sam@x.com" }], sign_offs: [{ sub: "dana@x.com", rev: 3 }], decisions: [{ id: "where", confirmed: null }, { id: "relay", confirmed: "daemon" }] } },
    coedit: { design: { ready: true }, tasks: [{ id: "w1", title: "w1", state: "merged" }, { id: "w2", title: "w2", state: "in review" }] },
    // A plan of this machine's: its decisions come from the local plan, top-level.
    outbox: { design: { ready: false, reviewers: [{ sub: "sam@x.com" }] }, decisions: [{ id: "a", title: "A", options: [] }], work_item: { key: "AB#4610", state: "Closed", tone: "done" } },
    release: {},
  };
  const detail = (id: string) => {
    const d = details[id] as { design?: object };
    return { ...d, design: { ...(d.design ?? {}), ...(placed[id] ? { folder: { scope: TEAM, id: placed[id] } } : {}) } };
  };
  return {
    docs: async () => ({ docs }),
    doc: async (id: string) => ({ ...docs.find((d) => d.id === id), files: [{ name: "", md: "Body." }], mentions: [], shared_with: [], ...detail(id) }),
    docScopes: async () => ({ scopes: [TEAM] }),
    docResults: async () => ({ days: 30, minimum: 5, enough: true, briefed: 18, large: 31, compared: 18, similar: 11, with_brief: 1.4, without_brief: 2.3 }),
    libraryFolders: async () => ({ scopes: [{ scope: TEAM, folders: [...folders] }] }),
    createLibraryFolder: async (_scope: string, name: string, visibility: "private" | "shared", _canAdd: string, repo?: string) => {
      const made = { id: name.toLowerCase(), name, visibility, repo };
      folders.push(made);
      return made;
    },
    moveDocToFolder: async (id: string, _scope: string, folder: string) => {
      placed[id] = folder;
      return {};
    },
  } as unknown as DocsClient & DaemonClient;
}

const rows = () => [...screen.getByTestId("library-list").querySelectorAll("li[data-doc]")].map((li) => li.getAttribute("data-doc"));
const row = (id: string) => screen.getByTestId("library-list").querySelector<HTMLElement>(`li[data-doc="${id}"]`)!;

it("lists designs with Needs you pinned and the rest by folder, and Board shows the same Library by stage, keeping the search and folder", async () => {
  const client = library();
  const { rerender } = render(<DocsView client={client} queue={queue} repos={repos} />);
  await waitFor(() => expect(screen.getByTestId("library-list").querySelectorAll("section[data-group]").length).toBe(4));
  // Engine is placed by its newest design, the one pinned above.
  expect([...screen.getByTestId("library-list").querySelectorAll("section[data-group]")].map((s) => s.getAttribute("aria-label"))).toEqual(["Needs you", "pawdelta / Engine", "pawdelta / Desktop app", "Not in a folder"]);
  expect(rows()).toEqual(["cloud", "outbox", "coedit", "release"]);
  expect(row("cloud").querySelector("[data-stage]")!.getAttribute("data-stage")).toBe("needs-you");
  expect(row("cloud").querySelector("[data-signed]")!.getAttribute("data-signed")).toBe("1/2");
  // Open choices: a shared design's own (one of two unconfirmed), and a local plan's.
  await waitFor(() => expect(row("cloud").getAttribute("data-to-decide")).toBe("1"));
  expect(row("outbox").getAttribute("data-to-decide")).toBe("1");
  expect(row("coedit").getAttribute("data-stage")).toBe("building");
  expect(row("outbox").querySelector("[data-work-item]")!.getAttribute("data-work-item")).toBe("AB#4610");
  expect(row("release").querySelector("[data-work-item]")).toBeNull();     // no link yet: nothing shown

  // A folder from the sidebar, then a search; the board keeps both.
  rerender(<DocsView client={client} queue={queue} repos={repos} folder="group:t1/eng" />);
  await waitFor(() => expect(rows()).toEqual(["cloud", "outbox"]));
  fireEvent.change(screen.getByRole("searchbox", { name: "Search the library" }), { target: { value: "outbox" } });
  await waitFor(() => expect(rows()).toEqual(["outbox"]));
  fireEvent.click(within(screen.getByRole("group", { name: "View" })).getByRole("button", { name: "Board" }));
  const board = await screen.findByTestId("library-board");
  expect([...board.querySelectorAll('section[data-column="in-review"] [data-doc]')].map((c) => c.getAttribute("data-doc"))).toEqual(["outbox"]);
  expect((screen.getByRole("searchbox", { name: "Search the library" }) as HTMLInputElement).value).toBe("outbox");

  // The view is remembered; the whole Library on the board puts each design in its stage's column.
  cleanup();
  render(<DocsView client={client} queue={queue} repos={repos} />);
  const all = await screen.findByTestId("library-board");
  await waitFor(() => expect(all.querySelectorAll("[data-doc]").length).toBe(4));
  const columns = Object.fromEntries([...all.querySelectorAll("section[data-column]")].map((s) => [s.getAttribute("data-column"), [...s.querySelectorAll("[data-doc]")].map((c) => c.getAttribute("data-doc"))]));
  expect(columns).toEqual({ drafting: ["release"], "in-review": ["cloud", "outbox"], approved: [], building: ["coedit"], shipped: [] });
  expect(all.querySelector('[data-doc="cloud"]')!.getAttribute("data-needs-you")).toBe("review");
});

type LibraryRoute = Extract<Route, { screen: "knowledge" }>;
/* The sidebar's rail and the designs side by side, as the app has them, over one daemon. */
function Library({ client }: { client: DocsClient & DaemonClient }) {
  const [route, setRoute] = useState<LibraryRoute>({ screen: "knowledge" });
  const { folders, create, move } = useLibraryFolders(client, true);
  return (
    <>
      <LibraryRail route={route} folders={folders} repos={repos} navigate={(to) => setRoute(to as LibraryRoute)} onBack={() => undefined} onCreateFolder={create} onMoveDoc={move} />
      <DocsView client={client} stage={route.stage} folder={route.folder} inRepo={route.repo} queue={queue} repos={repos} />
    </>
  );
}

it("a folder made and a design moved there from the reader show in that folder at once, and picking a sidebar row closes the reader", async () => {
  render(<Library client={library()} />);
  await waitFor(() => expect(rows()).toHaveLength(4));
  fireEvent.click(within(row("release")).getByRole("button"));
  expect(await screen.findByTestId("reader")).toBeTruthy();

  fireEvent.click(screen.getByText("+ New"));
  fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: "Launch" } });
  await act(async () => fireEvent.submit(screen.getByRole("form", { name: "New folder" })));
  fireEvent.click(await screen.findByLabelText("Move here: Launch"));
  // The folder is picked while the design is still open: the reader closes on the designs in it.
  await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
  fireEvent.click(screen.getByRole("button", { name: "Launch" }));
  await waitFor(() => expect(screen.queryByTestId("reader")).toBeNull());
  await waitFor(() => expect(rows()).toEqual(["release"]));
  expect(screen.getByTestId("library-list").querySelector("section[data-group]")!.getAttribute("aria-label")).toBe("pawdelta / Launch");

  // Open again, then All designs: the reader closes on the whole Library.
  fireEvent.click(within(row("release")).getByRole("button"));
  expect(await screen.findByTestId("reader")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /^All designs/ }));
  await waitFor(() => expect(rows()).toHaveLength(4));
});
