import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../api/client";
import { checkEngine, compatibility } from "../api/engine";
import { BASE_URL, FakeDaemon, TOKEN } from "../test/fakeDaemon";
import { EngineBanner, UPGRADE_COMMAND } from "./EngineBanner";

afterEach(cleanup);

describe("compatibility", () => {
  it.each([
    ["1.0", "ok"],
    ["1.12", "ok"],
    ["0.9", "engine-too-old"],
    ["2.0", "app-too-old"],
    ["3.1", "app-too-old"],
    ["1", "engine-too-old"],
    ["", "engine-too-old"],
    [undefined, "engine-too-old"],
  ])("judges api_version %o as %s against >=1.0,<2", (version, expected) => {
    expect(compatibility(version)).toBe(expected);
  });
});

async function banner(client: DaemonClient, onRetry?: () => void) {
  const status = await checkEngine(client);
  const view = render(<EngineBanner status={status} onRetry={onRetry} />);
  return { status, view };
}

describe("EngineBanner", () => {
  it("shows daemon health for an engine in range", async () => {
    const { status, view } = await banner(new FakeDaemon().client());
    expect(status.state).toBe("ok");
    expect(view.queryByTestId("engine-health")).toBeNull();
    expect(view.queryByTestId("engine-banner")).toBeNull();
    const reconnecting = render(<EngineBanner status={status} connection="connecting" />);
    expect(reconnecting.getByTestId("connection").textContent).toBe("daemon · connecting…");
  });

  it("offers the upgrade for an older engine: a button when the app can ask the engine, the command otherwise", async () => {
    const daemon = new FakeDaemon();
    daemon.data.meta = { ...daemon.data.meta, engine_version: "0.4.1", api_version: "0.9" };
    const { status } = await banner(daemon.client());
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("brindle 0.4.1 (API 0.9) is older than this app supports");
    expect(alert.textContent).toContain(UPGRADE_COMMAND);
    cleanup();
    const onUpgrade = vi.fn();
    render(<EngineBanner status={status} onUpgrade={onUpgrade} />);
    expect(screen.getByRole("alert").textContent).not.toContain(UPGRADE_COMMAND);
    fireEvent.click(screen.getByRole("button", { name: "Upgrade the engine" }));
    expect(onUpgrade).toHaveBeenCalledOnce();
    // While it runs the button waits; after a failure it offers the retry; the command never comes back.
    cleanup();
    render(<EngineBanner status={status} onUpgrade={onUpgrade} upgrading="running" />);
    expect(screen.getByRole("button", { name: "Upgrading…" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("alert").textContent).not.toContain(UPGRADE_COMMAND);
    cleanup();
    render(<EngineBanner status={status} onUpgrade={onUpgrade} upgrading="failed" />);
    fireEvent.click(screen.getByRole("button", { name: "Upgrade failed · Retry" }));
    expect(onUpgrade).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("alert").textContent).not.toContain(UPGRADE_COMMAND);
  });

  it("asks for an app update, without blocking, for a newer engine", async () => {
    const daemon = new FakeDaemon();
    daemon.data.meta = { ...daemon.data.meta, engine_version: "1.0.0", api_version: "2.0" };
    await banner(daemon.client());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("update the app");
  });

  it("says when the daemon refuses an agent's process", async () => {
    const refusing = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: async () =>
        new Response(JSON.stringify({ error: { code: "agent_caller", message: "refused: the caller runs under brindle agent w1" } }), {
          status: 403,
        }),
    });
    const { status } = await banner(refusing);
    expect(status.state).toBe("refused");
    expect(screen.getByRole("alert").textContent).toContain("the caller runs under brindle agent w1");
  });

  it("says when no daemon answers, with a retry", async () => {
    const down = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: () => Promise.reject(new TypeError("connection refused")) });
    const onRetry = vi.fn();
    await banner(down, onRetry);
    expect(screen.getByRole("alert").textContent).toContain("Can't reach the brindle engine: connection refused");
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
