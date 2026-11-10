import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import type { DaemonClient, FolderGroup } from "../api/client";
import { useLibraryFolders } from "./useLibraryFolders";

const group = (name: string): FolderGroup[] => [{ scope: { kind: "user", id: "u" }, folders: [{ id: name, name, visibility: "private" }] }];
const names = (r: { current: { folders: FolderGroup[] } }) => r.current.folders.map((g) => g.folders[0].name);

/* A client whose reads and creates stay outstanding until the test settles them. */
function fake() {
  const reads: { ok(v: FolderGroup[]): void; fail(): void }[] = [];
  const creates: (() => void)[] = [];
  const client = {
    libraryFolders: () => new Promise((ok, fail) => reads.push({ ok: (scopes) => ok({ scopes }), fail: () => fail(new Error("x")) })),
    createLibraryFolder: () => new Promise<void>((done) => creates.push(done)),
  } as unknown as DaemonClient;
  return { client, reads, creates };
}
const flush = () => act(async () => { await Promise.resolve(); });

it("an older read that succeeds or fails after a newer one never replaces the newer list", async () => {
  const { client, reads, creates } = fake();
  const { result, rerender } = renderHook(({ active }) => useLibraryFolders(client, active), { initialProps: { active: true } });
  await act(async () => reads[0].ok(group("previous")));
  expect(names(result)).toEqual(["previous"]);
  rerender({ active: false });
  rerender({ active: true });
  expect(reads).toHaveLength(2);
  rerender({ active: false });
  await act(async () => reads[1].ok(group("after close")));
  expect(names(result)).toEqual(["previous"]);

  rerender({ active: true });
  act(() => { void result.current.create("user", "a", "private", "owner"); });
  act(() => { void result.current.create("user", "b", "private", "owner"); });
  await act(async () => creates.forEach((done) => done()));
  await flush();
  expect(reads).toHaveLength(5); // the open read, close/reopen read, and two create refreshes
  await act(async () => reads[4].ok(group("newest")));
  await act(async () => reads[3].ok(group("stale success")));
  await act(async () => reads[2].fail());
  await flush();
  expect(names(result)).toEqual(["newest"]);
});

it("a create finishing after the client changes or the Library closes refreshes nothing", async () => {
  const a = fake();
  const b = fake();
  const { result, rerender } = renderHook(({ client, active }) => useLibraryFolders(client, active), { initialProps: { client: a.client, active: true } });
  let pending!: Promise<void>;
  act(() => { pending = result.current.create("user", "x", "private", "owner"); });
  rerender({ client: b.client, active: true });
  await act(async () => b.reads[0].ok(group("new client")));
  await act(async () => { a.creates[0](); await pending; });
  expect(a.reads).toHaveLength(1); // no refresh of the old client
  expect(names(result)).toEqual(["new client"]);

  act(() => { pending = result.current.create("user", "y", "private", "owner"); });
  rerender({ client: b.client, active: false });
  await act(async () => { b.creates[0](); await pending; });
  expect(b.reads).toHaveLength(1); // nothing reads once closed
});
