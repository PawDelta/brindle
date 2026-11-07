import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LoadingButton } from "./LoadingButton";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function slowAction() {
  let finish = () => {};
  const action = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
  return { action, finish: () => act(async () => finish()) };
}

describe("LoadingButton", () => {
  it("shows nothing for 300ms, then dims with the sweep and aria-busy until the action settles", async () => {
    const { action, finish } = slowAction();
    render(<LoadingButton className="btn btn-primary" onClick={action}>Start</LoadingButton>);
    const button = screen.getByRole("button", { name: "Start" });
    const wrap = button.parentElement!;
    expect(wrap.dataset.tone).toBe("primary");

    fireEvent.click(button);
    act(() => vi.advanceTimersByTime(299));
    expect(wrap.classList.contains("slow")).toBe(false);
    expect(button.getAttribute("aria-busy")).toBeNull();

    act(() => vi.advanceTimersByTime(1));
    expect(wrap.classList.contains("slow")).toBe(true);
    expect(button.getAttribute("aria-busy")).toBe("true");

    await finish();
    expect(wrap.classList.contains("slow")).toBe(false);
    expect(button.getAttribute("aria-busy")).toBeNull();
  });

  it("ignores a second click while the action is out, and runs again once it settles", async () => {
    const { action, finish } = slowAction();
    render(<LoadingButton onClick={action}>Allow</LoadingButton>);
    const button = screen.getByRole("button", { name: "Allow" });

    fireEvent.click(button);
    fireEvent.click(button);
    expect(action).toHaveBeenCalledTimes(1);

    await finish();
    fireEvent.click(button);
    expect(action).toHaveBeenCalledTimes(2);
  });
});
