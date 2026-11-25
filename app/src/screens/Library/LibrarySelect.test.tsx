import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonClient, type AnalysisDoc, type BulkDocResult, type BulkDocsRequest, type DocScope } from "../../api/client";
import { DocsView, type DocsClient } from "../Knowledge/DocsView";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const team: DocScope = { kind: "group", id: "t1" };
const folder = { scope: team, id: "engine" };
const docs: AnalysisDoc[] = [
  { id: "first", title: "First plan", updated_at: 3 },
  { id: "second", title: "Second plan", updated_at: 2 },
  { id: "third", title: "Third plan", updated_at: 1 },
];

const archiveBytes = "PK\x03\x04";
const archiveName = "selected-plans.zip";

function library({ failure, hiddenShare }: { failure?: BulkDocResult; hiddenShare?: string } = {}) {
  let remaining = docs;
  const inFolder = new Set(["first", "second"]);
  const shared = new Set(["second"]);
  const daemon = new DaemonClient({
    baseUrl: "http://daemon.test",
    token: "t",
    fetch: async (url, init) => {
      const path = new URL(String(url)).pathname;
      if (path.endsWith("/library/docs/bulk")) {
        const request = JSON.parse(String(init?.body)) as BulkDocsRequest;
        const { action, confirm, ids, scope } = request;
        const results = ids.map((id) => {
          if (action === "delete") {
            if (!confirm && (shared.has(id) || id === hiddenShare)) {
              return { id, ok: false, code: "confirm_shared", error: "This plan is still shared." };
            }
            if (id === failure?.id) return failure;
            remaining = remaining.filter((doc) => doc.id !== id);
          } else if (action === "move" && scope === "team:t1" && request.folder === folder.id) {
            inFolder.add(id);
          } else if (action === "share" && scope === "team:t1") {
            shared.add(id);
          } else {
            throw new Error("Unexpected bulk action");
          }
          return { id, ok: true };
        });
        return Response.json({ results });
      }
      if (path.endsWith("/library/docs/export")) {
        const { ids, format } = JSON.parse(String(init?.body));
        if (format === "zip" && ids.join(",") === "first,third") {
          return Response.json({ filename: archiveName, content_b64: btoa(archiveBytes), skipped: [] });
        }
        return Response.json({ error: { message: "Unexpected export selection" } }, { status: 400 });
      }
      throw new Error(`Unexpected daemon route ${path}`);
    },
  });
  const client: DocsClient = {
    docs: async () => ({ docs: remaining }),
    docScopes: async () => ({ scopes: [{ ...team, can_share: true }] }),
    docResults: async () => ({ days: 30, minimum: 5, enough: false, briefed: 0, large: 0, compared: 0, similar: 0, with_brief: null, without_brief: null }),
    doc: async (id) => ({
      ...docs.find((doc) => doc.id === id), id, files: [], mentions: [],
      shared_with: shared.has(id) ? [team] : [],
      design: inFolder.has(id) ? { folder } : undefined,
    }),
    libraryFolders: async () => ({ scopes: [{ scope: team, folders: [{ id: "engine", name: "Engine", visibility: "shared" }] }] }),
    shareDoc: async () => ({}),
    unshareDoc: async () => ({}),
    deleteDoc: async () => ({}),
    exportDoc: async () => ({ filename: "" }),
    docPolicy: async () => ({}),
    docExportConfig: async () => ({ google_client_id: null, google_client_secret: null, microsoft_client_id: null }),
    bulkDocs: daemon.bulkDocs.bind(daemon),
    exportDocs: daemon.exportDocs.bind(daemon),
  };
  render(<DocsView client={client} />);
}

const rowCheck = (id: string) => within(document.querySelector<HTMLElement>(`[data-doc="${id}"]`)!).getByRole<HTMLInputElement>("checkbox");
const checkedRows = () => docs.filter(({ id }) => document.querySelector(`[data-doc="${id}"] input:checked`)).map(({ id }) => id);

function submitDelete() {
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: /delete/i }));
}

function confirmDelete() {
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Delete" }));
  submitDelete();
}

it("shift-selects a range and confirms one bulk delete for the shared selection", async () => {
  library();
  await screen.findByRole("region", { name: "Engine" });
  fireEvent.click(rowCheck("first"));
  fireEvent.click(rowCheck("third"), { shiftKey: true });
  expect(checkedRows()).toEqual(["first", "second", "third"]);
  confirmDelete();
  await waitFor(() => expect(screen.queryAllByRole("checkbox")).toHaveLength(0));
  expect(screen.queryByRole("toolbar")).toBeNull();
});

it("selects only the rows in the chosen group and clears them on Escape", async () => {
  library();
  const group = await screen.findByRole("region", { name: "Engine" });
  const [all] = within(group).getAllByRole("checkbox");
  fireEvent.click(all);
  expect(checkedRows()).toEqual(["first", "second"]);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(checkedRows()).toEqual([]);
});

it("refreshes the list and keeps only the failed row selected after a partial delete", async () => {
  library({ failure: { id: "second", ok: false, code: "forbidden", error: "Only the owner can delete this plan." } });
  await screen.findByRole("region", { name: "Engine" });
  fireEvent.click(rowCheck("first"));
  fireEvent.click(rowCheck("second"));
  confirmDelete();
  await waitFor(() => expect(document.querySelector('[data-doc="first"]')).toBeNull());
  expect(checkedRows()).toEqual(["second"]);
  expect(screen.getByRole("toolbar")).toBeTruthy();
  const row = document.querySelector<HTMLElement>('[data-doc="second"]')!;
  expect(within(row).getByRole("alert").getAttribute("data-code")).toBe("forbidden");
  expect(within(document.querySelector<HTMLElement>('[data-doc="third"]')!).queryByRole("alert")).toBeNull();
});

it("asks for shared deletion confirmation when the bulk API discovers a share", async () => {
  library({ hiddenShare: "first" });
  await screen.findByRole("region", { name: "Engine" });
  fireEvent.click(rowCheck("first"));
  confirmDelete();
  const row = document.querySelector<HTMLElement>('[data-doc="first"]')!;
  expect((await within(row).findByRole("alert")).getAttribute("data-code")).toBe("confirm_shared");
  expect(checkedRows()).toEqual(["first"]);
  submitDelete();
  await waitFor(() => expect(document.querySelector('[data-doc="first"]')).toBeNull());
});

it("downloads the selected plans as a ZIP through the daemon client", async () => {
  library();
  let blob: Blob | undefined;
  let filename = "";
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL(value: Blob) { blob = value; return "blob:library"; }
    static revokeObjectURL() {}
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    filename = this.download;
  });
  await screen.findByRole("region", { name: "Engine" });
  fireEvent.click(rowCheck("first"));
  fireEvent.click(rowCheck("third"));
  fireEvent.click(screen.getByRole("button", { name: "Download" }));
  await waitFor(() => expect(filename).toBe(archiveName));
  expect(blob?.type).toBe("application/zip");
  expect(await blob?.text()).toBe(archiveBytes);
});

it("moves selected plans into the chosen folder and refreshes the groups", async () => {
  library();
  const group = await screen.findByRole("region", { name: "Engine" });
  const third = rowCheck("third");
  expect(group.contains(third)).toBe(false);
  fireEvent.click(third);
  fireEvent.click(screen.getByRole("button", { name: "Move to folder" }));
  fireEvent.click(within(screen.getByRole("group", { name: "Move to folder" })).getByRole("button", { name: "Engine" }));
  await waitFor(() => expect(group.contains(rowCheck("third"))).toBe(true));
});

it("offers to stop sharing a plan after it has been bulk shared", async () => {
  library();
  await screen.findByRole("region", { name: "Engine" });
  fireEvent.click(rowCheck("first"));
  fireEvent.click(screen.getByRole("button", { name: "Share / Stop sharing" }));
  fireEvent.click(within(screen.getByRole("group", { name: "Share with" })).getByRole("button", { name: /^Share with / }));
  await waitFor(() => expect(checkedRows()).toEqual([]));
  fireEvent.click(rowCheck("first"));
  fireEvent.click(screen.getByRole("button", { name: "Share / Stop sharing" }));
  expect(await within(screen.getByRole("group", { name: "Share with" })).findByRole("button", { name: /^Stop sharing with / })).toBeTruthy();
});
