import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonError } from "../../api/client";
import type { InboxItemDTO } from "../../api/schema";
import { FakeDaemon } from "../../test/fakeDaemon";
import { root } from "../../test/fixtures";
import { useInboxActions, type InboxActions } from "./useInboxActions";

afterEach(cleanup);

const plan: InboxItemDTO = { kind: "plan", reason: "plan awaiting approval", agent_id: "sup1", workspace_id: "proj", detail: "1. Keep the plan." };

/* The hook over a given inbox, its actions handed out for the test to call. */
function Harness({ daemon, inbox, out }: { daemon: FakeDaemon; inbox: InboxItemDTO[]; out: { actions?: InboxActions } }) {
  const actions = useInboxActions(daemon.client(), () => Promise.resolve(), 0, inbox);
  out.actions = actions;
  const note = actions.notes["plan:sup1:proj"];
  return <p data-testid="note">{note?.text ?? ""}</p>;
}

describe("inbox actions", () => {
  it("rejects changed permission digests and keeps a repeated action attached to its in-flight request", async () => {
    const item: InboxItemDTO = { ...plan, kind: "permission", permission: { via: "hook", request_id: "r", digest: "old" } };
    const daemon = new FakeDaemon({ workspaces: [root], inbox: [item] });
    const out: { actions?: InboxActions } = {};
    const { rerender } = render(<Harness daemon={daemon} inbox={[item]} out={out} />);
    const changed = { ...item, permission: { ...item.permission!, digest: "new" } };
    rerender(<Harness daemon={daemon} inbox={[changed]} out={out} />);
    act(() => out.actions!.run(item, "permission", { choice: "allow_once" }));
    expect(daemon.requests.filter((r) => r.method === "POST")).toHaveLength(0);
    act(() => {
      out.actions!.run(changed, "permission", { choice: "allow_once" });
      out.actions!.run(changed, "permission", { choice: "deny" });
    });
    await waitFor(() => expect(out.actions!.busy.size).toBe(0));
    expect(daemon.requests.filter((r) => r.method === "POST")).toHaveLength(1);
  });

  it("binds a decision to what the card showed: an item the inbox has since replaced isn't acted on, and the card says so", async () => {
    const daemon = new FakeDaemon({ workspaces: [root], inbox: [plan] });
    const out: { actions?: InboxActions } = {};
    const { rerender } = render(<Harness daemon={daemon} inbox={[plan]} out={out} />);
    // The engine now lists a different plan under the same key; the decision was made on the old one.
    rerender(<Harness daemon={daemon} inbox={[{ ...plan, detail: "1. Keep the plan.\n2. Also delete the database." }]} out={out} />);
    act(() => out.actions!.run(plan, "approve", { feedback: "" }));
    await waitFor(() => expect(screen.getByTestId("note").textContent).toContain("This changed while you decided"));
    expect(daemon.requests.filter((r) => r.method === "POST")).toEqual([]);
    // The one shown now is approved at once.
    const shown = { ...plan, detail: "1. Keep the plan.\n2. Also delete the database." };
    act(() => out.actions!.run(shown, "approve", { feedback: "" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST").map((r) => r.path)).toEqual(["/inbox/sup1/approve"]));
  });

  it("drops the card and refetches when the engine says nothing waits on it any more, but keeps it for another refusal", async () => {
    const daemon = new FakeDaemon({ workspaces: [root], inbox: [plan] });
    let refused = "sup1 has no plan awaiting a decision";
    const client = daemon.client();
    vi.spyOn(client, "inboxAction").mockImplementation(() => Promise.reject(new DaemonError("conflict", refused, 409)));
    let refreshes = 0;
    const out: { actions?: InboxActions } = {};
    const inbox = [plan];   // one array: a new one each render would read as a fresh snapshot
    function Probe() {
      out.actions = useInboxActions(client, () => Promise.resolve(void refreshes++), 0, inbox);
      return <p data-testid="note">{out.actions.notes["plan:sup1:proj"]?.text ?? ""}|{[...out.actions.hidden].join()}</p>;
    }
    render(<Probe />);
    act(() => out.actions!.run(plan, "approve", {}));
    await waitFor(() => expect(refreshes).toBe(1));
    expect(screen.getByTestId("note").textContent).toBe("|plan:sup1:proj");
    // A refusal about the action itself still brings the card back with the message.
    refused = "sup1's plan couldn't be sent: its pane is gone";
    cleanup();
    render(<Probe />);
    act(() => out.actions!.run(plan, "approve", {}));
    await waitFor(() => expect(screen.getByTestId("note").textContent).toBe(`${refused}|`));
  });
});
