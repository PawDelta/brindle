import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import type { DaemonClient } from "../api/client";
import { docStages, stageCounts } from "../screens/Knowledge/libraryStages";
import { useLibraryRecent } from "./useLibraryRecent";

it("library sidebar recent pinned and stages: the newest designs are Recent, and shared-doc inbox cards place designs at a stage", async () => {
  const docs = [1, 2, 3, 4].map((n) => ({ id: `d${n}`, title: n === 2 ? "" : `Design ${n}`, updated_at: n * 10 }));
  const client = { docs: () => Promise.resolve({ docs }) } as unknown as DaemonClient;
  const { result, rerender } = renderHook(({ active }) => useLibraryRecent(client, active), { initialProps: { active: false } });
  expect(result.current).toEqual([]);     // nothing is read while the Library is closed
  rerender({ active: true });
  await act(async () => { await Promise.resolve(); });
  expect(result.current).toEqual([{ id: "d4", title: "Design 4" }, { id: "d3", title: "Design 3" }, { id: "d2", title: "d2" }]);

  const stages = docStages([
    { kind: "review_requested", reason: "r", doc: "d1" },
    { kind: "changes_requested", reason: "r", doc: "d2" },
    { kind: "signed_off", reason: "r", doc: "d3" },
    { kind: "signed_off", reason: "r", doc: "d1" },     // already needs the person: stays Needs you
    { kind: "plan", reason: "r" },                      // no doc: not a Library design
  ]);
  expect(stageCounts(stages)).toEqual({ "needs-you": 2, approved: 1 });
});
