import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { createAppStore } from "../store/store";
import { startSync } from "../store/sync";
import { FakeDaemon } from "../test/fakeDaemon";
import { useEngineUpgrade } from "./EngineUpgrade";

function setup(fails = false) {
  const daemon = new FakeDaemon();
  daemon.deferUpgrade = true;
  daemon.upgradeFails = fails;
  const store = createAppStore();
  const client = daemon.client();
  const sync = startSync({ client, store, connect: daemon.connect });
  const { result } = renderHook(() => useEngineUpgrade(client, store));
  return { daemon, result, stop: () => sync.stop() };
}

describe("useEngineUpgrade", () => {
  it("follows the engine's job on the bus: running while uv runs, done on job.done, failed with uv's tail on job.failed", async () => {
    const ok = setup();
    await act(() => ok.result.current.start());
    expect(ok.daemon.requests.find((r) => r.method === "POST")).toMatchObject({ path: "/engine/upgrade" });
    await waitFor(() => expect(ok.result.current.state).toMatchObject({ kind: "running", stage: "upgrading" }));
    ok.daemon.completeUpgrade();
    await waitFor(() => expect(ok.result.current.state.kind).toBe("done"));
    expect(ok.daemon.data.meta.engine_version).toBe("0.5.1");
    ok.stop();

    const bad = setup(true);
    await act(() => bad.result.current.start());
    await waitFor(() => expect(bad.result.current.state.kind).toBe("running"));
    bad.daemon.completeUpgrade();
    await waitFor(() => expect(bad.result.current.state.kind).toBe("failed"));
    const state = bad.result.current.state;
    expect(state.kind === "failed" && state.message).toContain("exited 1");
    expect(state.kind === "failed" && state.lines.join("\n")).toContain("network unreachable");
    act(() => bad.result.current.dismiss());
    expect(bad.result.current.state.kind).toBe("idle");
    bad.stop();
  });
});
