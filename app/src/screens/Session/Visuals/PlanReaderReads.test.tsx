import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, expect, it, vi } from "vitest";
import type { Plan, VisualDTO } from "../../../api/client";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { connectDaemon, inTestStore } from "../../../test/harness";
import { PlanReader } from "./PlanReader";
import { SharedDesignReader } from "./SharedDesignReader";

const { docCommit } = vi.hoisted(() => ({ docCommit: vi.fn() }));
vi.mock("./PlanReader", async (importOriginal) => {
  const real = await importOriginal<typeof import("./PlanReader")>();
  return {
    ...real,
    DesignDoc: (props: Parameters<typeof real.DesignDoc>[0]) => (
      <Profiler id="shared-design" onRender={docCommit}><real.DesignDoc {...props} /></Profiler>
    ),
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  docCommit.mockClear();
});

const visual: VisualDTO = {
  id: "v_plan", session: "boss", title: "Webhook delivery", by: "supervisor",
  kind: "plan", rev: 1, kept: false, unread: false, created_at: 1,
};
const plan: Plan = {
  id: visual.id, session: visual.session, title: visual.title, rev: visual.rev, state: "open",
  markdown: "Deliver signed events.", lede: "", sections: [{ id: "context", title: "Context", markdown: "Teams poll today." }],
  decisions: [], split: [], queued: 0, created_at: 1,
};
const thread: NonNullable<Plan["threads"]>[number] = {
  id: 1, rev: plan.rev, kind: "comment", author: "u_author", text: "Can we phase this in?", status: "open", at: 2,
  anchor: { kind: "section", section: "context", state: "current" },
};
const back = () => {};

function seededDaemon() {
  const daemon = new FakeDaemon();
  daemon.seedLive(plan.id, {}, { plan });
  return daemon;
}

it("reads again for a change to this plan, without reading on unrelated inbox and plan events", async () => {
  const daemon = seededDaemon();
  const { client, store, sync } = connectDaemon(daemon);
  const reads = () => daemon.requests.filter(({ method, path }) => method === "GET" && path === `/plans/${plan.id}`).length;
  await act(async () => { render(inTestStore(<PlanReader client={client} visual={visual} onBack={back} onChanged={back} />, store)); });
  await act(async () => sync.idle());
  expect(screen.getByTestId("plan-doc")).toBeTruthy();
  const initial = reads();
  expect(initial).toBeGreaterThan(0);
  for (let i = 0; i < 10; i++) {
    await act(async () => { daemon.publish("inbox.changed", { root_id: "elsewhere" }); });
    await act(async () => sync.idle());
  }
  await act(async () => {
    daemon.publish("visual.plan_changed", { root_id: "elsewhere", payload: { visual: "v_other" } });
    daemon.publish("visual.plan_changed", { root_id: visual.session, payload: { visual: "v_other" } });
    daemon.publish("inbox.changed", { root_id: visual.session });
  });
  expect(reads()).toBe(initial);

  await act(async () => { daemon.publish("visual.plan_changed", { root_id: visual.session, payload: { visual: plan.id } }); });
  expect(reads()).toBe(initial + 1);
});

it("recovers same-revision threads when the reconnect cannot replay the plan change", async () => {
  vi.useFakeTimers();
  const daemon = seededDaemon();
  const { client, store, sync } = connectDaemon(daemon);
  const reads = () => daemon.requests.filter(({ method, path }) => method === "GET" && path === `/plans/${plan.id}`).length;
  await act(async () => { render(inTestStore(<PlanReader client={client} visual={visual} onBack={back} onChanged={back} />, store)); });
  await act(async () => { daemon.publish("inbox.changed"); });
  await act(async () => sync.idle());
  expect(screen.queryByTestId("thread-1")).toBeNull();
  const initial = reads();

  await act(async () => daemon.drop());
  daemon.seedLive(plan.id, {}, { plan: { ...plan, threads: [thread] } });
  daemon.publish("visual.plan_changed", { root_id: visual.session, payload: { visual: plan.id } });
  const retained = daemon.publish("inbox.changed");
  daemon.events = [retained];

  await act(async () => { await vi.advanceTimersToNextTimerAsync(); });
  await act(async () => sync.idle());
  expect(screen.queryByTestId("thread-1")?.dataset.status).toBe("open");
  expect(reads()).toBe(initial + 1);

  await act(async () => { daemon.send({ type: "resnapshot", reason: "events_lost" }); });
  await act(async () => sync.idle());
  expect(reads()).toBe(initial + 1);
});

it("loads History when opened and reuses the timeline for the same revision", async () => {
  const daemon = seededDaemon();
  const client = daemon.client();
  const history = vi.spyOn(client, "planHistory").mockResolvedValue({ events: [] });
  const reader = (row: VisualDTO) => <PlanReader client={client} visual={row} onBack={back} onChanged={back} />;
  const view = render(reader(visual));
  await act(async () => {});
  const trigger = screen.getByTestId("plan-rev").closest("button")!;
  expect(history).toHaveBeenCalledTimes(0);
  await act(async () => fireEvent.click(trigger));
  expect(screen.getByTestId("doc-history").dataset.state).toBe("empty");
  expect(history).toHaveBeenCalledTimes(1);
  fireEvent.click(trigger);
  await act(async () => fireEvent.click(trigger));
  expect(history).toHaveBeenCalledTimes(1);

  fireEvent.click(trigger);
  const rev = plan.rev + 1;
  daemon.seedLive(plan.id, {}, { plan: { ...plan, rev } });
  await act(async () => view.rerender(reader({ ...visual, rev })));
  expect(history).toHaveBeenCalledTimes(1);
  await act(async () => fireEvent.click(trigger));
  expect(history).toHaveBeenCalledTimes(2);
});

it("keeps the document stable on an unchanged shared read and pauses reads while hidden", async () => {
  vi.useFakeTimers();
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const daemon = seededDaemon();
  const client = daemon.client();
  const reads = () => daemon.requests.filter(({ method, path }) => method === "GET" && path === `/shared-designs/${plan.id}`).length;
  const shared = {
    sharedDesign: client.sharedDesign.bind(client), writeSharedDesign: client.writeSharedDesign.bind(client), libraryPeople: client.libraryPeople.bind(client),
  };
  await act(async () => { render(<SharedDesignReader client={shared} docId={plan.id} onBack={back} />); });
  expect(screen.getByTestId("plan-doc")).toBeTruthy();
  expect(reads()).toBe(1);
  const initial = docCommit.mock.calls.length;
  expect(initial).toBeGreaterThan(0);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(reads()).toBe(2);
  expect(docCommit).toHaveBeenCalledTimes(initial);

  visibility.mockReturnValue("hidden");
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(reads()).toBe(2);
  visibility.mockReturnValue("visible");
  await act(async () => fireEvent(document, new Event("visibilitychange")));
  expect(reads()).toBe(3);
  expect(docCommit).toHaveBeenCalledTimes(initial);

  daemon.seedLive(plan.id, {}, { plan: { ...plan, threads: [thread] } });
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByTestId("thread-1").dataset.status).toBe("open");
});
