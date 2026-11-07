import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { STEPS, WALKTHROUGH_KEY, Walkthrough, openWalkthrough } from "./Walkthrough";

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("opens once after setup, follows its control as it scrolls, advances step by step, and once dismissed stays closed until reopened", () => {
  // jsdom lays nothing out: the control and its scrolling column (y 80–600, under a banner) report where
  // they are (the window is 1024×768).
  let at = { top: 100, left: 20, width: 80, height: 20 };
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid === "scroller") return DOMRect.fromRect({ x: 0, y: 80, width: 1024, height: 520 });
    return DOMRect.fromRect({ x: at.left, y: at.top, width: at.width, height: at.height });
  });
  const step = () => screen.getByTestId("walkthrough").getAttribute("data-step");
  const ring = () => screen.queryByTestId("walkthrough-highlight") as HTMLElement | null;
  const { unmount } = render(
    <>
      <div data-testid="scroller" style={{ overflowY: "auto" }}>
        <button type="button" data-walkthrough={STEPS[0].target}>
          New session
        </button>
      </div>
      <Walkthrough autoStart />
    </>,
  );
  expect(step()).toBe(STEPS[0].target);
  expect([ring()?.style.top, ring()?.style.height]).toEqual(["96px", "28px"]);
  // The column scrolls it half under its top edge: the ring follows and hugs the part still showing.
  at = { ...at, top: 70 };
  fireEvent.scroll(screen.getByTestId("scroller"));
  expect([ring()?.style.top, ring()?.style.height]).toEqual(["76px", "18px"]);
  // Wholly behind the column's edge, though still in the window: no ring over the banner, the card centred.
  at = { ...at, top: 40 };
  fireEvent.scroll(screen.getByTestId("scroller"));
  expect(ring()).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(step()).toBe(STEPS[1].target);
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(step()).toBe(STEPS[0].target);
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  expect(screen.queryByTestId("walkthrough")).toBeNull();
  expect(localStorage.getItem(WALKTHROUGH_KEY)).toBe("done");
  unmount();

  // Dismissed: a later first-run mount doesn't open it again, but Help / the profile menu does.
  render(<Walkthrough autoStart />);
  expect(screen.queryByTestId("walkthrough")).toBeNull();
  act(() => openWalkthrough());
  for (const s of STEPS.slice(0, -1)) {
    expect(step()).toBe(s.target);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
  }
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByTestId("walkthrough")).toBeNull();
});
