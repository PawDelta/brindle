import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonClient, type RoutingGoalDTO, type RoutingWeights } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { REPO } from "../../test/fixtures";
import { BL, TOP, weightsKey } from "./goal";
import { DEBOUNCE_MS, RoutingGoal } from "./RoutingGoal";

/* Only the debounce runs on the fake clock; replies settle on a real turn of the event loop. */
beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const aim: RoutingWeights = { cost: 0.5, speed: 0.2, first_try: 0.3 };
const GOAL: RoutingGoalDTO = {
  weights: aim,
  scope: "repo",
  locked: false,
  preview: {
    aim,
    landed: { cost: 0.49, speed: 0.1, first_try: 0.41 },
    you: { usd: 1.4, minutes: 30, first_try: 0.54, today: { usd: 3.1, minutes: 31, first_try: 0.46 } },
    references: [
      { key: "you", name: "You", measured: true, point: null, tasks: [{ task: "large", label: "Large feature", usd: "$11.11", minutes: "62", first_try: "7 of 9 branches", basis: "measured once" }] },
      { key: "fable", name: "Fable 5.2", measured: true, point: { cost: 1 / 3, speed: 1 / 3, first_try: 1 / 3 }, tasks: [{ task: "large", label: "Large feature", usd: "$14.54–19.65", minutes: "36–51", first_try: "1 of 3 runs", basis: "measured, 3 runs" }] },
    ],
    roles: [{ key: "medium", label: "Medium", value: "Codex Sol", provider: "codex", was: "Sonnet", note: "cheaper here" }],
  },
};

const settle = () => act(() => new Promise<void>((resolve) => setImmediate(resolve)));
const rest = () => act(() => vi.advanceTimersByTime(DEBOUNCE_MS));

/* `hold`: PUT replies wait until released, in whatever order the test picks. */
async function setup(goal: Partial<RoutingGoalDTO> = {}, hold = false) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], routingGoal: { ...GOAL, ...goal } });
  const held: (() => void)[] = [];
  const fetch: typeof globalThis.fetch = (input, init) =>
    hold && init?.method === "PUT" ? daemon.fetch(input, init).then((r) => new Promise<Response>((resolve) => held.push(() => resolve(r)))) : daemon.fetch(input, init);
  render(<RoutingGoal client={new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch })} repo={REPO} />);
  await settle();
  await settle();
  const puts = () => daemon.requests.filter((r) => r.method === "PUT" && r.path === "/routing/goal").map((r) => (r.body as { weights: RoutingWeights }).weights);
  return { daemon, held, puts, slider: screen.getByRole("slider", { name: "Goal mix" }) };
}

const shown = (slider: HTMLElement) => slider.getAttribute("data-weights");
const preview = () => screen.getByTestId("goal-preview");

describe("routing goal", () => {
  it("arrow keys move the aim and PUT it once the point rests", async () => {
    const { puts, slider } = await setup();
    fireEvent.keyDown(slider, { key: "ArrowUp" });
    fireEvent.keyDown(slider, { key: "ArrowUp" });
    expect(puts()).toHaveLength(0);
    await rest();
    await settle();
    expect(puts()).toHaveLength(1);
    expect(puts()[0].first_try).toBeGreaterThan(aim.first_try);
    expect(weightsKey(puts()[0])).toBe(shown(slider));
    expect(preview().getAttribute("data-state")).toBe("current");
  });

  it("a drag PUTs where the point was let go", async () => {
    const { puts, slider } = await setup();
    // jsdom lays nothing out, so client coordinates are the triangle's own.
    fireEvent.pointerDown(slider, { pointerId: 1, clientX: TOP[0], clientY: TOP[1] });
    fireEvent.pointerMove(slider, { pointerId: 1, clientX: BL[0], clientY: BL[1] });
    fireEvent.pointerUp(slider, { pointerId: 1, clientX: BL[0], clientY: BL[1] });
    await rest();
    await settle();
    expect(puts()).toEqual([{ cost: 1, speed: 0, first_try: 0 }]);
  });

  it("a reply for an aim moved away from doesn't overwrite the newer one", async () => {
    const { held, slider } = await setup({}, true);
    fireEvent.keyDown(slider, { key: "ArrowLeft" });
    await rest();
    const first = shown(slider);
    fireEvent.keyDown(slider, { key: "ArrowUp" });
    await rest();
    const newer = shown(slider);
    expect(newer).not.toBe(first);
    await settle();
    expect(held).toHaveLength(2);
    held[1]();
    await settle();
    held[0]();
    await settle();
    expect(shown(slider)).toBe(newer);
    expect(preview().getAttribute("data-preview-for")).toBe(newer);
    expect(preview().getAttribute("data-state")).toBe("current");
  });

  it("an org lock disables the control", async () => {
    const { puts, slider } = await setup({ locked: true, lock_reason: "Your org sets this" });
    expect(screen.getByTestId("routing-goal").getAttribute("data-state")).toBe("locked");
    expect(slider.getAttribute("aria-disabled")).toBe("true");
    const before = shown(slider);
    fireEvent.keyDown(slider, { key: "ArrowLeft" });
    fireEvent.pointerDown(slider, { pointerId: 1, clientX: BL[0], clientY: BL[1] });
    fireEvent.click(document.querySelector<HTMLButtonElement>('[data-action="reset"]')!);
    await rest();
    await settle();
    expect(shown(slider)).toBe(before);
    expect(puts()).toHaveLength(0);
  });
});
