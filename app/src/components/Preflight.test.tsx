import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DaemonContext } from "../api/context";
import { FakeDaemon } from "../test/fakeDaemon";
import { Preflight } from "./Preflight";

afterEach(cleanup);

describe("Preflight consent", () => {
  it("is a real checkbox: clicking it records the agreement and refreshes the card", async () => {
    const daemon = new FakeDaemon({ preflight: { claude: [{ id: "consent", kind: "consent", label: "Agree to Claude Code's terms", done: false }] } });
    const client = daemon.client();
    const refreshed: unknown[] = [];
    render(
      <DaemonContext.Provider value={client}>
        <Preflight preflight={{ ok: false, provider: "claude", items: daemon.data.preflight.claude }} onRefresh={(next) => refreshed.push(next)} onLaunch={() => undefined} />
      </DaemonContext.Provider>,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /I agree to Claude Code's terms/ }));
    await waitFor(() => expect(refreshed).toHaveLength(1));
  });
});
