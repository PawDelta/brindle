import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import type { DaemonClient, FolderGroup } from "../api/client";
import { useLibraryFolders } from "./useLibraryFolders";

const group = (name: string): FolderGroup[] => [{ scope: { kind: "user", id: "u" }, folders: [{ id: name, name, visibility: "private" }] }];

it("an older read finishing after a newer one, or failing after it, never replaces the newer list", async () => {
  const reads: { ok(v: FolderGroup[]): void; fail(): void }[] = [];
  const client = {
    libraryFolders: () => new Promise((ok, fail) => reads.push({ ok: (scopes) => ok({ scopes }), fail: () => fail(new Error("x")) })),
    createLibraryFolder: () => Promise.resolve({}),
  } as unknown as DaemonClient;
  const { result, rerender } = renderHook(({ active }) => useLibraryFolders(client, active), { initialProps: { active: true } });
  let created!: Promise<void>;
  act(() => {
    created = result.current.create("user", "new", "private", "owner");
  });
  await act(async () => {
    await Promise.resolve();
  });
  expect(reads).toHaveLength(2); // the open read, and the refresh after the create
  await act(async () => {
    reads[1].ok(group("new"));
    await created;
  });
  await act(async () => reads[0].fail()); // the slow first read fails late
  expect(result.current.folders[0].folders[0].name).toBe("new");
  await act(async () => reads[0].ok(group("old")));
  expect(result.current.folders[0].folders[0].name).toBe("new");

  rerender({ active: false }); // closing makes a read in flight obsolete
  rerender({ active: true });
  rerender({ active: false });
  await act(async () => reads[2].ok(group("late")));
  expect(result.current.folders[0].folders[0].name).toBe("new");
});
