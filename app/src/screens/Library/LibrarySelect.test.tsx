import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { AnalysisDoc, BulkDocsRequest, DocScope } from "../../api/client";
import { DocsView, type DocsClient } from "../Knowledge/DocsView";

afterEach(cleanup);

const team: DocScope = { kind: "group", id: "t1" };
const docs: AnalysisDoc[] = [
  { id: "first", title: "First plan", updated_at: 3 },
  { id: "second", title: "Second plan", updated_at: 2 },
  { id: "third", title: "Third plan", updated_at: 1 },
];

function library(failed?: string) {
  let remaining = docs;
  const bulkDocs = vi.fn(async ({ ids }: BulkDocsRequest) => {
    remaining = remaining.filter((doc) => !ids.includes(doc.id) || doc.id === failed);
    return { results: ids.map((id) => ({ id, ok: id !== failed })) };
  });
  const client: DocsClient = {
    docs: async () => ({ docs: remaining }),
    docScopes: async () => ({ scopes: [team] }),
    docResults: async () => ({ days: 30, minimum: 5, enough: false, briefed: 0, large: 0, compared: 0, similar: 0, with_brief: null, without_brief: null }),
    doc: async (id) => ({
      ...docs.find((doc) => doc.id === id), id, files: [], mentions: [],
      shared_with: id === "second" ? [team] : [],
      design: id === "third" ? undefined : { folder: { scope: team, id: "engine" } },
    }),
    libraryFolders: async () => ({ scopes: [{ scope: team, folders: [{ id: "engine", name: "Engine", visibility: "shared" }] }] }),
    shareDoc: async () => ({}),
    unshareDoc: async () => ({}),
    deleteDoc: async () => ({}),
    exportDoc: async () => ({ filename: "" }),
    docPolicy: async () => ({}),
    docExportConfig: async () => ({ google_client_id: null, google_client_secret: null, microsoft_client_id: null }),
    bulkDocs,
  };
  render(<DocsView client={client} />);
  return bulkDocs;
}

const rowCheck = (id: string) => within(document.querySelector<HTMLElement>(`[data-doc="${id}"]`)!).getByRole<HTMLInputElement>("checkbox");
const checkedRows = () => docs.filter(({ id }) => document.querySelector(`[data-doc="${id}"] input:checked`)).map(({ id }) => id);

function confirmDelete() {
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Delete" }));
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: /delete/i }));
}

it("shift-selects a range and confirms one bulk delete for the shared selection", async () => {
  const bulkDocs = library();
  await screen.findByRole("region", { name: "Engine" });
  fireEvent.click(rowCheck("first"));
  fireEvent.click(rowCheck("third"), { shiftKey: true });
  expect(checkedRows()).toEqual(["first", "second", "third"]);
  confirmDelete();
  await waitFor(() => expect(screen.queryAllByRole("checkbox")).toHaveLength(0));
  expect(bulkDocs.mock.calls.map(([request]) => request)).toEqual([
    { action: "delete", ids: ["first", "second", "third"], confirm: true },
  ]);
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
  library("second");
  await screen.findByRole("region", { name: "Engine" });
  fireEvent.click(rowCheck("first"));
  fireEvent.click(rowCheck("second"));
  confirmDelete();
  await waitFor(() => expect(document.querySelector('[data-doc="first"]')).toBeNull());
  expect(checkedRows()).toEqual(["second"]);
  expect(screen.getByRole("toolbar")).toBeTruthy();
});
