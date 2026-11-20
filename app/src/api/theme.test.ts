import { describe, expect, it, vi } from "vitest";
import { watchAppTheme } from "./theme";
import type { DaemonClient } from "./client";

describe("watchAppTheme", () => {
  it("posts the theme at start and again when it flips", async () => {
    document.documentElement.dataset.theme = "dark";
    const putAppTheme = vi.fn().mockResolvedValue({});
    const stop = watchAppTheme({ putAppTheme } as unknown as DaemonClient);
    expect(putAppTheme).toHaveBeenLastCalledWith("dark");
    document.documentElement.dataset.theme = "light";
    await vi.waitFor(() => expect(putAppTheme).toHaveBeenLastCalledWith("light"));
    expect(putAppTheme).toHaveBeenCalledTimes(2);
    stop();
    delete document.documentElement.dataset.theme;
  });
});
