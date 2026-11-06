import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { STEPS, WALKTHROUGH_KEY, Walkthrough, openWalkthrough } from "./Walkthrough";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

it("opens once after setup, advances step by step, and once dismissed stays closed until reopened", () => {
  const step = () => screen.getByTestId("walkthrough").getAttribute("data-step");
  const { unmount } = render(<Walkthrough autoStart />);
  expect(step()).toBe(STEPS[0].target);
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
