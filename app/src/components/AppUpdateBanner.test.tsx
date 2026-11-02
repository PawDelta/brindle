import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppUpdateStatus, AppUpdates, ReleaseProgress } from "../api/tauri";
import { createAppStore } from "../store/store";
import { StoreContext } from "../store/useStore";
import { AppUpdateBanner, AppUpdatePill, AppUpdateProvider } from "./AppUpdateBanner";

afterEach(cleanup);

describe("AppUpdateBanner", () => {
  it("installs and restarts on Update now, folds into the sidebar pill on Later (a failed attempt too), and the pill updates again", async () => {
    const offered: AppUpdateStatus = {
      version: "0.2.10",
      automatic: true,
      pending: null,
      available: { version: "0.2.11", notes: null },
      later: false,
      updated: null,
      last_checked: null,
      checking: false,
    };
    const store = createAppStore();
    store.dispatch({ type: "app_update", status: offered });
    let progress: (p: ReleaseProgress) => void = () => {};
    let settle: { installed: (version: string | null) => void; failed: (e: Error) => void } = { installed: () => {}, failed: () => {} };
    const updates: AppUpdates = {
      status: () => Promise.resolve(offered),
      check: () => Promise.resolve(offered),
      updateNow: vi.fn((report: (p: ReleaseProgress) => void) => {
        progress = report;
        return new Promise<string | null>((installed, failed) => (settle = { installed, failed }));
      }),
      // The shell sets it aside and announces it so.
      later: vi.fn(async () => store.dispatch({ type: "app_update", status: { ...offered, later: true } })),
      restart: () => Promise.resolve(),
      dismiss: () => Promise.resolve(),
      watch: () => Promise.resolve(() => {}),
    };
    const view = () =>
      render(
        <StoreContext.Provider value={store}>
          <AppUpdateProvider updates={updates}>
            <AppUpdateBanner openUrl={() => Promise.resolve()} />
            <AppUpdatePill />
          </AppUpdateProvider>
        </StoreContext.Provider>,
      );
    const banner = () => screen.queryByTestId("app-update-banner");
    const pill = () => screen.queryByTestId("app-update-pill");

    // Offered: the strip with its actions, no pill yet.
    view();
    expect(banner()!.dataset.phase).toBe("idle");
    expect(pill()).toBeNull();

    // Update now: the bar follows the download, no Later meanwhile, then the shell restarts into it.
    fireEvent.click(screen.getByTestId("app-update-now"));
    expect(updates.updateNow).toHaveBeenCalledTimes(1);
    act(() => progress({ kind: "downloading", downloaded: 6, total: 10 }));
    expect(screen.getByTestId("app-update-bar").style.width).toBe("60%");
    expect(screen.queryByTestId("app-update-later")).toBeNull();
    await act(async () => settle.installed("0.2.11"));
    expect(banner()!.dataset.phase).toBe("restarting");
    cleanup();

    // An Update now that fails, then Later: the strip folds into the pill all the same.
    view();
    fireEvent.click(screen.getByTestId("app-update-now"));
    await act(async () => settle.failed(new Error("offline")));
    expect(banner()!.dataset.phase).toBe("failed");
    fireEvent.click(screen.getByTestId("app-update-later"));
    await act(async () => {});
    expect(updates.later).toHaveBeenCalledTimes(1);
    expect(banner()).toBeNull();

    // The pill's click is the same Update now, the strip back to show it.
    fireEvent.click(pill()!);
    expect(updates.updateNow).toHaveBeenCalledTimes(3);
    expect(banner()!.dataset.phase).toBe("downloading");
    expect(pill()).toBeNull();
  });
});
