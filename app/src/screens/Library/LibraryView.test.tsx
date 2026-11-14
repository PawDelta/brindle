import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { AnalysisDoc } from "../../api/client";
import { DocsView, type DocsClient } from "../Knowledge/DocsView";
import { docQueue } from "./libraryStages";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

const TEAM = { kind: "group", id: "t1", name: "Founding Team" };
const docs: AnalysisDoc[] = [
  { id: "cloud", title: "Cloud workers", summary: "Workers on the customer's machine.", scope: TEAM, updated_at: 900 },
  { id: "coedit", title: "Live co-editing", scope: TEAM, updated_at: 800 },
  { id: "outbox", title: "Outbox for reports", scope: TEAM, updated_at: 700 },
  { id: "release", title: "Release flow", scope: { kind: "user", id: "u1" }, updated_at: 600 },
];
const folder = (id: string) => ({ folder: { scope: TEAM, id } });
const details: Record<string, object> = {
  cloud: { design: { rev: 3, ready: false, reviewers: [{ sub: "dana@x.com" }, { sub: "sam@x.com" }], sign_offs: [{ sub: "dana@x.com", rev: 3 }], ...folder("eng") }, decisions: [{ id: "a", title: "A", options: [] }] },
  coedit: { design: { ready: true, ...folder("app") }, tasks: [{ id: "w1", title: "w1", state: "merged" }, { id: "w2", title: "w2", state: "in review" }] },
  outbox: { design: { ready: true, ...folder("eng") }, tasks: [{ id: "w1", title: "w1", state: "merged" }], work_item: { key: "AB#4610", state: "Closed", tone: "done" } },
  release: {},
};
const client = {
  docs: async () => ({ docs }),
  doc: async (id: string) => ({ id, files: [], mentions: [], shared_with: [], ...details[id] }),
  docScopes: async () => ({ scopes: [TEAM] }),
  docResults: async () => ({ days: 30, minimum: 5, enough: true, briefed: 18, large: 31, compared: 18, similar: 11, with_brief: 1.4, without_brief: 2.3 }),
  libraryFolders: async () => ({ scopes: [{ scope: TEAM, folders: [{ id: "eng", name: "Engine", visibility: "shared", repo: "/src/pawdelta" }, { id: "app", name: "Desktop app", visibility: "shared", repo: "/src/pawdelta" }] }] }),
} as unknown as DocsClient;
const repos = [{ name: "pawdelta", repo_root: "/src/pawdelta" }];
const queue = docQueue([{ kind: "review_requested", reason: "r", doc: "cloud" }]);
const rows = () => [...screen.getByTestId("library-list").querySelectorAll("li[data-doc]")].map((li) => li.getAttribute("data-doc"));

it("lists designs with Needs you pinned and the rest by folder, and Board shows the same Library by stage, keeping the search and folder", async () => {
  const { rerender } = render(<DocsView client={client} queue={queue} repos={repos} />);
  await waitFor(() => expect(screen.getByTestId("library-list").querySelectorAll("section[data-group]").length).toBe(4));
  // Engine is placed by its newest design, the one pinned above.
  expect([...screen.getByTestId("library-list").querySelectorAll("section[data-group]")].map((s) => s.getAttribute("aria-label"))).toEqual(["Needs you", "pawdelta / Engine", "pawdelta / Desktop app", "Not in a folder"]);
  expect(rows()).toEqual(["cloud", "outbox", "coedit", "release"]);
  const row = (id: string) => screen.getByTestId("library-list").querySelector<HTMLElement>(`li[data-doc="${id}"]`)!;
  expect(row("cloud").querySelector("[data-stage]")!.getAttribute("data-stage")).toBe("needs-you");
  expect(row("cloud").querySelector("[data-signed]")!.getAttribute("data-signed")).toBe("1/2");
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
  const column = (stage: string) => [...board.querySelectorAll(`section[data-column="${stage}"] [data-doc]`)].map((c) => c.getAttribute("data-doc"));
  expect(column("shipped")).toEqual(["outbox"]);
  expect((screen.getByRole("searchbox", { name: "Search the library" }) as HTMLInputElement).value).toBe("outbox");

  // The view is remembered; the whole Library on the board puts each design in its stage's column.
  cleanup();
  render(<DocsView client={client} queue={queue} repos={repos} />);
  const all = await screen.findByTestId("library-board");
  await waitFor(() => expect(all.querySelectorAll("[data-doc]").length).toBe(4));
  const columns = Object.fromEntries([...all.querySelectorAll("section[data-column]")].map((s) => [s.getAttribute("data-column"), [...s.querySelectorAll("[data-doc]")].map((c) => c.getAttribute("data-doc"))]));
  expect(columns).toEqual({ drafting: ["release"], "in-review": ["cloud"], approved: [], building: ["coedit"], shipped: ["outbox"] });
  expect(all.querySelector('[data-doc="cloud"]')!.getAttribute("data-needs-you")).toBe("review");
});
