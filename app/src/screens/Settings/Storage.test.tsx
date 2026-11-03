import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StorageReport, StorageWorktree } from "../../api/client";
import { currentReclaimable, forgetReclaimable } from "../../store/storage";
import { Storage } from "./Storage";

afterEach(() => {
  cleanup();
  forgetReclaimable();
});

const GB = 1024 ** 3;
const tree = (branch: string, over: Partial<StorageWorktree> = {}): StorageWorktree => ({
  path: `/wt/${branch}`, repo_root: "/repo", branch, sha: "a".repeat(40), state: "abandoned", last_activity: Date.now() / 1000 - 30 * 86400,
  dirty: false, live: false, owned: true, size: 2 * GB, build_size: GB, workspace_id: null, removable: true, reason: null, ...over,
});
const REPORT: StorageReport = {
  worktrees: [tree("old-idea"), tree("wip", { dirty: true, removable: false, reason: "it has uncommitted changes" }), tree("today", { state: "active", removable: false, reason: "it is still in use" })],
  categories: [{ name: "worktrees", size: 6 * GB, detail: "3 worktree(s)" }, { name: "logs", size: 50 * 1024 ** 2, detail: "~/.brindle/*.log" }],
  reclaimable: 2 * GB, free_bytes: 80 * GB, low_disk: null, dead_branches: [],
};

describe("Storage", () => {
  it("shows disk use and the abandoned worktrees, and cleans one only once confirmed", async () => {
    const client = {
      storage: vi.fn().mockResolvedValue(REPORT),
      cleanStorage: vi.fn().mockResolvedValue({ removed: [{ path: "/wt/old-idea", repo_root: "/repo", branch: "old-idea", state: "abandoned", size: 2 * GB, archived: "refs/brindle-archive/old-idea", branch_kept: null }], kept: [] }),
    };
    render(<Storage client={client} />);

    const categories = await screen.findByRole("list", { name: "Disk use by category" });
    expect(within(categories).getByText("6.0 GB")).toBeTruthy();
    expect(within(categories).getByText("50 MB")).toBeTruthy();
    const rows = screen.getAllByTestId("storage-worktree");
    expect(rows.map((r) => r.dataset.path)).toEqual(["/wt/old-idea", "/wt/wip"]);
    expect(rows[0].textContent).toContain("30d ago");
    expect(rows[1].textContent).toContain("Kept: it has uncommitted changes.");
    expect((within(rows[1]).getByRole("button", { name: "Clean wip" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(rows[0]).getByRole("button", { name: "Clean old-idea" }));
    expect(client.cleanStorage).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole("group", { name: "Confirm clean" })).getByRole("button", { name: "Remove" }));

    expect(await screen.findByText(/Removed 1 worktree, 2\.0 GB given back/)).toBeTruthy();
    expect(client.cleanStorage).toHaveBeenCalledWith({ path: "/wt/old-idea" });
    expect(client.storage).toHaveBeenCalledTimes(2);
  });

  it("Clean all sends exactly the removable worktrees it showed, one path each", async () => {
    const report = { ...REPORT, worktrees: [...REPORT.worktrees, tree("stale-2", { size: GB })] };
    // The re-read after cleaning lists a worktree that was never shown: nothing is sent for it.
    const client = {
      storage: vi.fn().mockResolvedValueOnce(report).mockResolvedValue({ ...REPORT, worktrees: [tree("appeared-later")] }),
      cleanStorage: vi.fn(async ({ path }: { path: string }) => ({ removed: [{ path, repo_root: "/repo", branch: path.slice(4), state: "abandoned" as const, size: GB, archived: null, branch_kept: null }], kept: [] })),
    };
    render(<Storage client={client} />);

    fireEvent.click(await screen.findByRole("button", { name: "Clean all" }));
    expect(client.cleanStorage).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole("group", { name: "Confirm clean" })).getByRole("button", { name: "Remove" }));

    expect(await screen.findByText(/Removed 2 worktrees/)).toBeTruthy();
    expect(client.cleanStorage.mock.calls.map(([t]) => t)).toEqual([{ path: "/wt/old-idea" }, { path: "/wt/stale-2" }]);
  });

  it("shows only the newest read when two land out of order, and keeps the shared figure from it", async () => {
    const reads: Record<string, (r: StorageReport) => void> = {};
    const client = { storage: vi.fn((repo?: string) => new Promise<StorageReport>((resolve) => (reads[repo!] = resolve))), cleanStorage: vi.fn() };
    const { rerender } = render(<Storage client={client} repo="old" />);
    rerender(<Storage client={client} repo="new" />);

    await act(async () => reads.new({ ...REPORT, worktrees: [], reclaimable: 0 }));
    await act(async () => reads.old({ ...REPORT, reclaimable: 9 * GB }));

    expect(screen.queryAllByTestId("storage-worktree")).toEqual([]);
    expect(screen.getByText(/0 MB could be given back/)).toBeTruthy();
    expect(currentReclaimable()).toBe(0);
  });
});
