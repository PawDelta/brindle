import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import type { DaemonClient } from "../api/client";
import { docStages, stageCounts } from "../screens/Knowledge/libraryStages";
import { recentEntries, useLibraryDocs } from "./useLibraryDocs";

it("library sidebar recent and stages: the newest designs are Recent, and the queue only decides Needs you over each design's own review state", async () => {
  const docs = [1, 2, 3, 4].map((n) => ({ id: `d${n}`, title: n === 2 ? "" : `Design ${n}`, updated_at: n * 10 }));
  const review = { d1: { ready: true }, d3: { ready: false, reviewers: [{ sub: "sam" }] }, d4: { ready: false } };
  const client = { docs: () => Promise.resolve({ docs }), doc: (id: string) => Promise.resolve({ design: review[id as keyof typeof review] ?? null }) } as unknown as DaemonClient;
  const { result, rerender } = renderHook(({ active }) => useLibraryDocs(client, active), { initialProps: { active: false } });
  expect(result.current.docs).toEqual([]);     // nothing is read while the Library is closed
  rerender({ active: true });
  await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
  expect(recentEntries(result.current.docs)).toEqual([{ id: "d4", title: "Design 4" }, { id: "d3", title: "Design 3" }, { id: "d2", title: "d2" }]);

  const queue = docStages([
    { kind: "review_requested", reason: "r", doc: "d3" },
    { kind: "plan", reason: "r" },                      // no doc: not a Library design
  ]);
  // d3 needs the person; d4 is in review without an Inbox card; d1 is approved; d2 is personal.
  expect(stageCounts(result.current.docs, queue, result.current.designs)).toEqual({ "needs-you": 1, "in-review": 1, approved: 1 });
});
