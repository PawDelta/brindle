import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import type { DaemonClient } from "../api/client";
import { useLibraryDocs } from "./useLibraryDocs";

it("library sidebar designs: nothing is read while the Library is closed, then each design's record, and again when asked", async () => {
  const docs = [1, 2].map((n) => ({ id: `d${n}`, title: `Design ${n}` }));
  let reads = 0;
  const client = {
    docs: () => { reads++; return Promise.resolve({ docs }); },
    doc: (id: string) => (id === "d2" ? Promise.reject(new Error("gone")) : Promise.resolve({ design: { ready: true }, tasks: [{ id: "w1", title: "w1", state: "merged" }] })),
  } as unknown as DaemonClient;
  const { result, rerender } = renderHook(({ active, again }) => useLibraryDocs(client, active, again), { initialProps: { active: false, again: 0 } });
  expect(result.current.docs).toEqual([]);
  rerender({ active: true, again: 0 });
  await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
  expect(result.current.docs).toEqual(docs);
  // A detail that can't be read is skipped, not fatal to the rest.
  expect([...result.current.records.keys()]).toEqual(["d1"]);
  expect(result.current.records.get("d1")?.tasks?.[0].state).toBe("merged");
  rerender({ active: true, again: 1 });
  await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
  expect(reads).toBe(2);
});
