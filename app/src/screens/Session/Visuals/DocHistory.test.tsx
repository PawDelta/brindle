import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { DocHistoryEntry, DocRevisionComparison } from "../../../api/client";
import { DocumentHistory, type HistoryClient } from "./DocHistory";

afterEach(cleanup);

const comparison = (rev: number): DocRevisionComparison => ({
  revision: { rev, lede: "", sections: [{ id: "summary", title: "Summary", kind: "markdown", markdown: "🦊 The new text." }], markdown: "", decisions: [], split: [] },
  previous: { rev: rev - 1, lede: "", sections: [{ id: "summary", title: "Summary", kind: "markdown", markdown: "🦊 The old text." }], markdown: "", decisions: [], split: [] },
  changes: [
    { section: "summary", before: { title: "Summary", markdown: "🦊 The old text." }, after: { title: "Summary", markdown: "🦊 The new text." }, spans: [
      { kind: "replace", before_start: 6, before_end: 9, start: 6, end: 9, before: "old", after: "new" },
    ] },
    { section: "removed", before: { title: "Removed", markdown: "Earlier passage" }, after: null, spans: [
      { kind: "delete", before_start: 0, before_end: 15, start: 0, end: 0, before: "Earlier passage", after: "" },
    ] },
  ],
});
const events: DocHistoryEntry[] = [
  { id: "r2", kind: "revision", actor: "supervisor", at: 2, rev: 2, version: null },
  { id: "s2", kind: "sign_off", actor: "teammate:Dana", at: 4, rev: 2, version: 8 },
  { id: "f2", kind: "freeze", actor: "person", at: 3, rev: 2, version: 8 },
  { id: "r3", kind: "revision", actor: "person", at: 5, rev: 3, version: null },
  { id: "o2", kind: "reopen", actor: "person", at: 6, rev: 2, version: 8 },
];
const settle = async () => { await act(async () => {}); };

it("loads one timeline and lets a revision show its changes and its read-only original", async () => {
  const client: HistoryClient = { planHistory: async () => ({ events }), planRevisionChanges: async (_, rev) => comparison(rev) };
  const { container } = render(<DocumentHistory client={client} planId="doc" />);
  await settle();
  const timeline = screen.getByTestId("doc-history");
  expect([...timeline.querySelectorAll("li")].map((li) => li.dataset.kind)).toEqual(["reopen", "revision", "sign_off", "freeze", "revision"]);
  expect(timeline.querySelectorAll("button")).toHaveLength(2);
  fireEvent.click(timeline.querySelectorAll("button")[1]);
  await settle();
  expect(screen.getByRole("region", { name: "Revision comparison" }).dataset.revision).toBe("2");
  expect(container.querySelector("ins")?.textContent).toBe("new");
  expect(container.querySelector('[data-section="summary"] .whitespace-pre-wrap')?.textContent).toBe("🦊 The oldnew text.");
  expect([...container.querySelectorAll("del")].map((node) => node.textContent)).toEqual(expect.arrayContaining(["old", "Earlier passage"]));
  fireEvent.click(screen.getByRole("button", { name: "Next change" }));
  expect(container.querySelector('[data-active="true"]')?.getAttribute("data-section")).toBe("removed");
  fireEvent.click(screen.getByRole("button", { name: "Previous change" }));
  expect(container.querySelector('[data-active="true"]')?.getAttribute("data-section")).toBe("summary");
  fireEvent.click(screen.getByRole("button", { name: "Open revision as it was" }));
  expect(container.querySelectorAll("ins, del")).toHaveLength(0);
  expect(container.querySelector('[data-section="removed"]')).toBeNull();
  expect(screen.getByRole("region", { name: "Revision comparison" }).dataset.state).toBe("original");
  fireEvent.click(screen.getByRole("button", { name: "Back to the latest" }));
  expect(screen.queryByRole("region", { name: "Revision comparison" })).toBeNull();
});

it("discards out-of-order revision responses and responses for a document that was closed", async () => {
  const pending = new Map<number, (result: DocRevisionComparison) => void>();
  const client: HistoryClient = {
    planHistory: async () => ({ events }),
    planRevisionChanges: (_, rev) => new Promise((resolve) => pending.set(rev, resolve)),
  };
  const onCompare = vi.fn();
  const view = render(<DocumentHistory client={client} planId="one" onCompare={onCompare} />);
  await settle();
  const buttons = screen.getByTestId("doc-history").querySelectorAll("button");
  fireEvent.click(buttons[1]);
  fireEvent.click(buttons[0]);
  await act(async () => pending.get(3)!(comparison(3)));
  await act(async () => pending.get(2)!(comparison(2)));
  expect(onCompare.mock.calls.filter(([value]) => value).map(([value]) => value.revision.rev)).toEqual([3]);
  fireEvent.click(buttons[1]);
  view.rerender(<DocumentHistory client={client} planId="two" onCompare={onCompare} />);
  await act(async () => pending.get(2)!(comparison(2)));
  expect(onCompare.mock.calls.filter(([value]) => value)).toHaveLength(1);
});
