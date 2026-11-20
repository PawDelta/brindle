import { describe, expect, it, vi } from "vitest";
import { watchAppTheme } from "./theme";
import type { DaemonClient } from "./client";

describe("watchAppTheme", () => {
  it("posts one request at a time and ends on the latest theme", async () => {
    document.documentElement.dataset.theme = "dark";
    const pending: Array<() => void> = [];
    const sent: string[] = [];
    const putAppTheme = vi.fn((theme: string) => {
      sent.push(theme);
      return new Promise<void>((resolve) => pending.push(resolve));
    });
    const stop = watchAppTheme({ putAppTheme } as unknown as DaemonClient);
    expect(sent).toEqual(["dark"]);
    document.documentElement.dataset.theme = "light"; // flips while the first is still in flight
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    expect(sent).toEqual(["dark"]); // not sent concurrently
    pending[0]();
    await vi.waitFor(() => expect(sent).toEqual(["dark", "light"]));
    stop();
    delete document.documentElement.dataset.theme;
  });
});
