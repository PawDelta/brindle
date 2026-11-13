import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { RECENT_EVENTS, createAppStore } from "../store/store";
import { StoreContext } from "../store/useStore";
import type { DaemonClient, Job } from "./client";
import { useInstalls } from "./providersInstall";

/* A client whose install 202s arrive when the test says, and a bus the test writes. */
function setup(done?: string[]) {
  const store = createAppStore();
  const answers: Record<string, (job: Job) => void> = {};
  const client = {
    installProvider: (provider: string) => new Promise<Job>((resolve) => (answers[provider] = resolve)),
  } as unknown as DaemonClient;
  let id = 0;
  const publish = (kind: string, payload: Record<string, unknown>) =>
    act(() => store.dispatch({ type: "stream", message: { type: "event", id: ++id, ts: 0, kind, repo_root: null, root_id: null, agent_id: null, workspace_id: null, task_id: null, payload } }));
  const wrapper = ({ children }: { children: ReactNode }) => <StoreContext.Provider value={store}>{children}</StoreContext.Provider>;
  const { result } = renderHook(() => useInstalls(client, (provider) => done?.push(provider)), { wrapper });
  const answer = (provider: string, job: string) =>
    act(async () => {
      answers[provider]({ job, kind: "provider.install" });
      await Promise.resolve();
    });
  return { result, publish, answer };
}

describe("useInstalls", () => {
  it("providers: an install whose job ends before its 202 still ends after more events than the store keeps", async () => {
    const done: string[] = [];
    const { result, publish, answer } = setup(done);
    act(() => {
      void result.current.start("opencode");
    });
    await answer("opencode", "jA");
    // B fails and C finishes, both before their 202s; then A prints more lines than the store's history holds.
    act(() => {
      void result.current.start("antigravity");
      void result.current.start("codex");
    });
    publish("job.progress", { job: "jB", text: "Downloading Antigravity 1.3.0" });
    publish("job.failed", { job: "jB", error: { message: "Antigravity's installer exited 1" } });
    publish("job.progress", { job: "jC", text: "Downloading codex" });
    publish("job.done", { job: "jC" });
    for (let n = 0; n < RECENT_EVENTS + 10; n++) publish("job.progress", { job: "jA", text: `line ${n}` });
    expect(result.current.installs.antigravity.status).toBe("starting");
    await answer("antigravity", "jB");
    await answer("codex", "jC");
    expect(result.current.installs.antigravity).toMatchObject({ status: "failed", job: "jB", lines: ["Downloading Antigravity 1.3.0"], error: "Antigravity's installer exited 1" });
    expect(result.current.installs.codex).toMatchObject({ status: "done", lines: ["Downloading codex"] });
    expect(done).toEqual(["codex"]);
    expect(result.current.installs.opencode.lines).toEqual(Array.from({ length: RECENT_EVENTS + 10 }, (_, n) => `line ${n}`));
    expect(result.current.installs.opencode.status).toBe("running");
    publish("job.done", { job: "jA" });
    expect(result.current.installs.opencode.status).toBe("done");
    // Settling removes successful jobs while retaining the failed one.
    act(() => result.current.settle());
    expect(Object.keys(result.current.installs)).toEqual(["antigravity"]);
  });
});
