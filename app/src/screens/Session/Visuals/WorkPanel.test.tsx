import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DesignWorkItems, TrackerLink } from "../../../api/client";
import { WorkPanel, useWork, type WorkClient } from "./WorkPanel";

afterEach(cleanup);

const link = (over: Partial<TrackerLink>): TrackerLink => ({ link_id: "wl_1", tracker: "azure_devops", key: "4810", display: "AB#4810", target: { kind: "design" }, state: "Active", done: false, read_at: 1, ...over });
const split = [{ id: "w1", title: "Remote launch" }, { id: "w2", title: "Host leases" }];

function Harness({ client }: { client: WorkClient }) {
  const work = useWork(client, "doc1");
  return work ? <WorkPanel work={work} split={split} can /> : null;
}

function fake(read: Partial<DesignWorkItems>): WorkClient & { createWorkItems: ReturnType<typeof vi.fn>; linkWorkItem: ReturnType<typeof vi.fn> } {
  const items = read.items ?? [];
  const state: DesignWorkItems = { items, connected: { azure_devops: true }, epic: items.find((i) => i.target.kind === "design") ?? null, done: 0, total: 0, ...read };
  return {
    designWorkItems: vi.fn().mockResolvedValue(state),
    createWorkItems: vi.fn().mockResolvedValue({ created: [] }),
    linkWorkItem: vi.fn().mockResolvedValue({}),
    unlinkWorkItem: vi.fn().mockResolvedValue({}),
  } as never;
}

describe("the Tasks tab's work items", () => {
  it("pairs each task with its item, offers to create the missing one, and links an existing item", async () => {
    const client = fake({ items: [link({}), link({ link_id: "wl_2", key: "4822", display: "AB#4822", target: { kind: "task", id: "w1" }, type: "User Story" })] });
    render(<Harness client={client} />);
    expect(await screen.findByText("AB#4810 · Active")).toBeTruthy();
    expect(screen.getByText("AB#4822 · Active").closest("[data-task]")?.getAttribute("data-task")).toBe("w1");
    fireEvent.click(screen.getByRole("button", { name: "Create item" }));
    await waitFor(() => expect(client.createWorkItems).toHaveBeenCalledWith("doc1", { kind: "task", task: "w2" }, undefined));
    fireEvent.change(screen.getByLabelText("Link an existing item"), { target: { value: "AB#4823" } });
    fireEvent.change(screen.getByLabelText("Link it to"), { target: { value: "w2" } });
    fireEvent.click(screen.getByRole("button", { name: "Link" }));
    await waitFor(() => expect(client.linkWorkItem).toHaveBeenCalledWith("doc1", "AB#4823", { kind: "task", id: "w2" }, undefined));
  });

  it("keeps the last read on screen, said to be old, when the tracker can't be reached", async () => {
    render(<Harness client={fake({ items: [link({ stale: true })], unreachable: true, read_at: 1 })} />);
    expect(await screen.findByTestId("work-unreachable")).toBeTruthy();
    expect(screen.getByText("AB#4810 · Active").getAttribute("data-stale")).toBe("true");
  });

  it("says why a create was refused and offers nothing when no tracker is connected", async () => {
    const client = fake({ items: [], connected: {} });
    render(<Harness client={client} />);
    expect(await screen.findByText(/connects one in Settings/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create item" })).toBeNull();
    cleanup();
    const refusing = fake({ items: [link({})] });
    refusing.createWorkItems.mockRejectedValue(new Error("Map this design's repo to a project first."));
    render(<Harness client={refusing} />);
    fireEvent.click((await screen.findAllByRole("button", { name: "Create item" }))[0]);
    expect((await screen.findByRole("alert")).textContent).toContain("Map this design's repo");
  });
});
