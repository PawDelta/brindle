import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppUpdateStatus, AppUpdates, ReleaseProgress } from "../api/tauri";
import { createAppStore } from "../store/store";
import { StoreContext } from "../store/useStore";
import { AppUpdateBanner, AppUpdatePill, AppUpdateProvider } from "./AppUpdateBanner";

afterEach(cleanup);

describe("AppUpdateBanner", () => {
  it("offers the update, installs and restarts on Update now, folds into the sidebar pill on Later, and the pill updates too", async () => {
    const offered: AppUpdateStatus = {
      version: "0.2.10",
      automatic: true,
      pending: null,
      available: { version: "0.2.11", notes: "## Fixes\n- Fixes slowdowns that showed os error 35.\n- Adds wait-for-room." },
      later: false,
      updated: null,
      last_checked: null,
      checking: false,
    };
    const store = createAppStore();
    store.dispatch({ type: "app_update", status: offered });
    let progress: (p: ReleaseProgress) => void = () => {};
    let installed: (version: string | null) => void = () => {};
    const updates: AppUpdates = {
      status: () => Promise.resolve(offered),
      check: () => Promise.resolve(offered),
      updateNow: vi.fn((report: (p: ReleaseProgress) => void) => {
        progress = report;
        return new Promise<string | null>((resolve) => (installed = resolve));
      }),
      // The shell sets it aside and announces it so.
      later: vi.fn(async () => store.dispatch({ type: "app_update", status: { ...offered, later: true } })),
      restart: () => Promise.resolve(),
      dismiss: () => Promise.resolve(),
      watch: () => Promise.resolve(() => {}),
    };
    const openUrl = vi.fn(() => Promise.resolve());
    const view = () =>
      render(
        <StoreContext.Provider value={store}>
          <AppUpdateProvider updates={updates}>
            <AppUpdateBanner openUrl={openUrl} />
            <AppUpdatePill />
          </AppUpdateProvider>
        </StoreContext.Provider>,
      );
    const banner = () => screen.queryByTestId("app-update-banner");

    // Offered: the headline, the notes' first line and what keeps running; no pill yet.
    view();
    expect(banner()!.textContent).toContain("brindle 0.2.11 is available");
    expect(banner()!.textContent).toContain("Fixes slowdowns that showed os error 35. Your sessions and agents keep running while brindle restarts.");
    expect(screen.queryByTestId("app-update-pill")).toBeNull();

    // Update now: progress on the strip, then the shell restarts into it.
    fireEvent.click(screen.getByRole("button", { name: "Update now" }));
    expect(updates.updateNow).toHaveBeenCalledTimes(1);
    act(() => progress({ kind: "downloading", downloaded: 6, total: 10 }));
    expect(banner()!.textContent).toContain("Downloading brindle 0.2.11… 60%");
    expect(screen.queryByRole("button", { name: "Later" })).toBeNull();
    await act(async () => installed("0.2.11"));
    expect(banner()!.textContent).toContain("Restarting into brindle 0.2.11…");
    cleanup();

    // Later: the strip folds into the pill, whose click is the same Update now.
    view();
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    await act(async () => {});
    expect(updates.later).toHaveBeenCalled();
    expect(banner()).toBeNull();
    fireEvent.click(screen.getByTestId("app-update-pill"));
    expect(updates.updateNow).toHaveBeenCalledTimes(2);
    expect(banner()!.dataset.phase).toBe("downloading");
    expect(screen.queryByTestId("app-update-pill")).toBeNull();
  });
});
