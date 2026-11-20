import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Plan } from "../../../api/client";
import type { PlanWorkerDTO } from "../../../api/schema";
import { Tasks, overlaps } from "./PlanCards";

afterEach(cleanup);

function planWith(split: PlanWorkerDTO[]): Plan {
  return {
    id: "p1",
    session: "s1",
    rev: 1,
    state: "open",
    title: "Plan",
    lede: "",
    markdown: "",
    created_at: 0,
    queued: 0,
    sections: [],
    decisions: [],
    split,
  };
}

describe("Tasks file overlaps", () => {
  it("does not flag shared files ordered directly or transitively with after", () => {
    const plan = planWith([
      { id: "w1", title: "Base", files: ["chain.py"] },
      { id: "w2", title: "Policy", after: ["w1"], files: ["analysis_policy.py"] },
      { id: "w3", title: "Other", after: ["w1"], files: ["other.py"] },
      { id: "w4", title: "Wire up", after: ["w2", "w3"], files: ["analysis_policy.py", "chain.py"] },
    ]);
    const shared = overlaps(plan);
    expect([...shared]).toEqual([]);

    render(<Tasks plan={plan} shared={shared} />);
    expect(screen.queryByTestId("split-overlaps")).toBeNull();
    expect(within(screen.getByTestId("task-w1")).getByText("chain.py").getAttribute("data-overlap")).toBeNull();
    expect(within(screen.getByTestId("task-w2")).getByText("analysis_policy.py").getAttribute("data-overlap")).toBeNull();
    expect(within(screen.getByTestId("task-w4")).getByText("analysis_policy.py").getAttribute("data-overlap")).toBeNull();
    expect(within(screen.getByTestId("task-w4")).getByText("chain.py").getAttribute("data-overlap")).toBeNull();
  });

  it("flags only unordered counterparts when a file mixes ordered and unordered workers", () => {
    const plan = planWith([
      { id: "w1", title: "First", files: ["shared.py"] },
      { id: "w2", title: "Second", after: ["w1"], files: ["shared.py"] },
      { id: "w3", title: "Parallel", files: ["shared.py"] },
      { id: "w4", title: "Final", after: ["w2", "w3"], files: ["shared.py"] },
    ]);
    const shared = overlaps(plan);
    expect([...shared]).toEqual([["shared.py", ["w1", "w2", "w3"]]]);

    render(<Tasks plan={plan} shared={shared} />);
    const banner = screen.getByTestId("split-overlaps").textContent ?? "";
    expect(banner).toContain("shared.py");
    expect(banner).not.toContain("w4");

    const w1Chip = within(screen.getByTestId("task-w1")).getByText(/shared\.py/);
    expect(w1Chip.getAttribute("data-overlap")).toBe("true");
    expect(w1Chip.textContent).toContain("w3");
    expect(w1Chip.textContent).not.toContain("w2");
    expect(w1Chip.textContent).not.toContain("w4");

    const w2Chip = within(screen.getByTestId("task-w2")).getByText(/shared\.py/);
    expect(w2Chip.getAttribute("data-overlap")).toBe("true");
    expect(w2Chip.textContent).toContain("w3");
    expect(w2Chip.textContent).not.toContain("w1");
    expect(w2Chip.textContent).not.toContain("w4");

    const w3Chip = within(screen.getByTestId("task-w3")).getByText(/shared\.py/);
    expect(w3Chip.getAttribute("data-overlap")).toBe("true");
    expect(w3Chip.textContent).toContain("w1");
    expect(w3Chip.textContent).toContain("w2");
    expect(w3Chip.textContent).not.toContain("w4");

    const w4Chip = within(screen.getByTestId("task-w4")).getByText("shared.py");
    expect(w4Chip.getAttribute("data-overlap")).toBeNull();
  });
});
