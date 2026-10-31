import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonClient, DaemonError } from "../../api/client";
import { createAppStore } from "../../store/store";
import { useSessionDiff } from "./review";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it("shares reads across views, refreshes on changes, backs off failures and pauses when hidden", async () => {
  vi.useFakeTimers();
  let hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  const client = new DaemonClient({ baseUrl: "http://daemon", token: "test" });
  const read = vi.spyOn(client, "sessionDiff").mockResolvedValue({ root_id: "s", head: "a", base: "main", base_sha: "base", branch: "s", files: [] });
  const store = createAppStore();
  function View() {
    const state = useSessionDiff(client, "s", store);
    return <span>{state.diff?.head}</span>;
  }
  const change = (id: number, root = "s") => store.dispatch({ type: "stream", message: { type: "event", id, ts: id, kind: "session.changed", root_id: root, agent_id: null, repo_root: null, workspace_id: null, task_id: null, payload: {} } });
  render(<><View /><View /></>);
  await act(async () => {});
  expect(read).toHaveBeenCalledTimes(1);
  expect(screen.getAllByText("a")).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(59_000); change(1, "other"); });
  expect(read).toHaveBeenCalledTimes(1);
  await act(async () => { change(2); });
  expect(read).toHaveBeenCalledTimes(2);
  read.mockRejectedValueOnce(new DaemonError("busy", "busy", 503));
  await act(async () => { change(3); });
  expect(read).toHaveBeenCalledTimes(3);
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); change(4); });
  expect(read).toHaveBeenCalledTimes(3);
  read.mockRejectedValueOnce(new Error("offline"));
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(read).toHaveBeenCalledTimes(4);
  await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
  expect(read).toHaveBeenCalledTimes(4);
  await act(async () => { hidden = true; document.dispatchEvent(new Event("visibilitychange")); change(5); await vi.advanceTimersByTimeAsync(300_000); });
  expect(read).toHaveBeenCalledTimes(4);
  await act(async () => { hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
  expect(read).toHaveBeenCalledTimes(5);
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(read).toHaveBeenCalledTimes(6);
  cleanup();
  await act(async () => { await vi.advanceTimersByTimeAsync(300_000); });
  expect(read).toHaveBeenCalledTimes(6);
});

it("coalesces in-flight changes and discards responses after changing roots", async () => {
  const client = new DaemonClient({ baseUrl: "http://daemon", token: "test" });
  type Diff = Awaited<ReturnType<typeof client.sessionDiff>>;
  const pending: { root: string; resolve(diff: Diff): void }[] = [];
  vi.spyOn(client, "sessionDiff").mockImplementation((root) => new Promise((resolve) => pending.push({ root, resolve })));
  const store = createAppStore();
  const first = renderHook(() => useSessionDiff(client, "a", store));
  const second = renderHook(({ root }) => useSessionDiff(client, root, store), { initialProps: { root: "a" } });
  for (const id of [1, 2]) {
    act(() => store.dispatch({ type: "stream", message: { type: "event", id, ts: id, kind: "session.changed", root_id: "a", agent_id: null, repo_root: null, workspace_id: null, task_id: null, payload: {} } }));
  }
  expect(pending.map((p) => p.root)).toEqual(["a"]);
  const diff = (head: string): Diff => ({ root_id: "s", head, base: "main", base_sha: "base", branch: "s", files: [] });
  await act(async () => pending[0].resolve(diff("old")));
  expect(pending.map((p) => p.root)).toEqual(["a", "a"]);
  first.unmount();
  second.rerender({ root: "b" });
  expect(second.result.current.diff).toBeNull();
  await act(async () => pending[1].resolve(diff("stale")));
  expect(second.result.current.diff).toBeNull();
  await act(async () => pending[2].resolve(diff("current")));
  expect(second.result.current.diff?.head).toBe("current");
});
