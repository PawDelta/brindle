import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { STEPS, WALKTHROUGH_KEY, Walkthrough, openWalkthrough } from "./Walkthrough";

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("opens once after setup, follows its control as it scrolls, advances step by step, and once dismissed stays closed until reopened", () => {
  // jsdom lays nothing out: the first step's control reports where it is (the window is 1024×768).
  let at = { top: 100, left: 20, width: 80, height: 20 };
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => DOMRect.fromRect({ x: at.left, y: at.top, width: at.width, height: at.height }));
  const step = () => screen.getByTestId("walkthrough").getAttribute("data-step");
  const ring = () => screen.queryByTestId("walkthrough-highlight") as HTMLElement | null;
  const { unmount } = render(
    <>
      <div data-testid="scroller">
        <button type="button" data-walkthrough={STEPS[0].target}>
          New session
        </button>
      </div>
      <Walkthrough autoStart />
    </>,
  );
  expect(step()).toBe(STEPS[0].target);
  expect(ring()?.style.top).toBe("96px");
  // A scrolling ancestor moves it: the ring follows; scrolled half out, it hugs the visible part.
  at = { ...at, top: -10 };
  fireEvent.scroll(screen.getByTestId("scroller"));
  expect([ring()?.style.top, ring()?.style.height]).toEqual(["-4px", "18px"]);
  // Scrolled wholly out of the window: no ring pointing off-screen, the card centred instead.
  at = { ...at, top: -200 };
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
