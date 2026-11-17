import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DesignWorkItems, TrackerLink } from "../../../api/client";
import { WorkPanel, useWork, type WorkClient } from "./WorkPanel";

afterEach(cleanup);

const link = (over: Partial<TrackerLink>): TrackerLink => ({ link_id: "wl_1", tracker: "azure_devops", key: "4810", display: "AB#4810", target: { kind: "design" }, state: "Active", done: false, read_at: 1, ...over });
const split = [{ id: "w1", title: "Remote launch" }, { id: "w2", title: "Host leases" }];

function Harness({ client }: { client: WorkClient }) {
  const work = useWork(client, "doc1");
  return work ? <WorkPanel work={work} split={split} can /> : null;
}

/* The Library, as far as the tab can tell: links are kept, a write changes what the next read says. */
function library(start: TrackerLink[], opts: { connected?: boolean; unreachable?: boolean; refuse?: string } = {}) {
  let items = start;
  const read = (): DesignWorkItems => {
    const shown = opts.unreachable ? items.map((i) => ({ ...i, stale: true })) : items;
    return { items: shown, connected: opts.connected === false ? {} : { azure_devops: true },
      epic: shown.find((i) => i.target.kind === "design") ?? null, done: 0, total: shown.length, unreachable: opts.unreachable };
  };
  const client: WorkClient = {
    designWorkItems: async () => read(),
    createWorkItems: async (_doc, body) => {
      if (opts.refuse) throw new Error(opts.refuse);
      const made = (body.kind === "task" ? (body.task ? [body.task] : split.map((w) => w.id).filter((id) => !items.some((i) => i.target.id === id))) : ["x"])
        .map((id, n) => link({ link_id: `wl_n${items.length + n}`, key: `49${n}`, display: `AB#49${n}${id}`, target: { kind: "task", id }, state: "New" }));
      items = [...items, ...made];
      return { created: made };
    },
    linkWorkItem: async (_doc, item, target) => {
      const made = link({ link_id: `wl_l${items.length}`, key: item, display: item, target: target ?? { kind: "design" }, state: "New" });
      items = [...items, made];
      return made;
    },
    unlinkWorkItem: async (_doc, id) => { items = items.filter((i) => i.link_id !== id); return { unlinked: true }; },
  } as WorkClient;
  return client;
}

const taskRow = (id: string) => document.querySelector(`[data-task="${id}"]`)!;

describe("the Tasks tab's work items", () => {
  it("creates a task's item and pairs it with the task, links an item to another task, and unlinks", async () => {
    render(<Harness client={library([link({})])} />);
    await screen.findByText("AB#4810 · Active");
    expect(taskRow("w1").querySelector("[data-work-item]")).toBeNull();
    fireEvent.click(taskRow("w1").querySelector("button")!);                            // Create item, on the first task
    await waitFor(() => expect(taskRow("w1").querySelector("[data-work-item]")).not.toBeNull());
    expect(taskRow("w2").querySelector("[data-work-item]")).toBeNull();
    fireEvent.change(screen.getByLabelText("Link an existing item"), { target: { value: "AB#4823" } });
    fireEvent.change(screen.getByLabelText("Link it to"), { target: { value: "w2" } });
    fireEvent.click(screen.getByRole("button", { name: "Link" }));
    await waitFor(() => expect(taskRow("w2").querySelector("[data-work-item='AB#4823']")).not.toBeNull());
    expect((screen.getByLabelText("Link an existing item") as HTMLInputElement).value).toBe("");
    fireEvent.click(taskRow("w2").querySelector("button:not([disabled])")!.parentElement!.querySelector("button")!);   // Unlink
    await waitFor(() => expect(taskRow("w2").querySelector("[data-work-item]")).toBeNull());
  });

  it("creates every missing task's item at once", async () => {
    render(<Harness client={library([link({})])} />);
    await screen.findByText("AB#4810 · Active");
    fireEvent.click(screen.getByRole("button", { name: /missing/ }));
    await waitFor(() => expect(document.querySelectorAll("[data-task] [data-work-item]").length).toBe(2));
  });

  it("keeps the last read on screen, marked old, when the tracker can't be reached", async () => {
    render(<Harness client={library([link({})], { unreachable: true })} />);
    expect(await screen.findByTestId("work-unreachable")).toBeTruthy();
    expect(screen.getByText("AB#4810 · Active").getAttribute("data-stale")).toBe("true");
  });

  it("offers nothing to create without a tracker, and shows a refused create as an alert", async () => {
    render(<Harness client={library([], { connected: false })} />);
    await waitFor(() => expect(screen.getByTestId("doc-work-items").getAttribute("data-state")).toBe("ready"));
    expect(document.querySelectorAll("[data-task] button").length).toBe(0);
    cleanup();
    render(<Harness client={library([link({})], { refuse: "not mapped" })} />);
    await screen.findByText("AB#4810 · Active");
    fireEvent.click(taskRow("w1").querySelector("button")!);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(taskRow("w1").querySelector("[data-work-item]")).toBeNull();
  });
});
