import { waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { InboxItemDTO } from "../../api/schema";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { FakeDaemon } from "../../test/fakeDaemon";
import { question, root, worker } from "../../test/fixtures";
import { watchInbox } from "./notify";

const plan: InboxItemDTO = { kind: "plan", reason: "plan awaiting approval", agent_id: "w1", workspace_id: "proj/feat-ls", detail: "1. Keep the plan." };

describe("watchInbox", () => {
  it("notifies only items that arrive after the first snapshot and keeps the badge at the count", async () => {
    const daemon = new FakeDaemon({ workspaces: [root, worker], inbox: [question] });
    const store = createAppStore();
    const notes: string[] = [];
    const counts: number[] = [];
    watchInbox(store, { notify: (title, body) => notes.push(`${title} | ${body}`), count: (n) => counts.push(n) });
    const sync = startSync({ client: daemon.client(), store, connect: daemon.connect });
    await waitFor(() => expect(counts).toEqual([1]));
    daemon.data.inbox = [question, plan];
    daemon.publish("agent.status", { agent_id: "w1", payload: {} });
    await waitFor(() => expect(counts).toEqual([1, 2]));
    expect(notes).toEqual([expect.stringContaining("w1")]);
    sync.stop();
  });
});
