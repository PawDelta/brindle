import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { VisualDTO } from "../../../api/client";
import { FileIcon } from "../../../components/icons";
import { VisualsButton } from "./VisualsButton";

afterEach(cleanup);

const now = Date.now() / 1000;

function doc(over: Partial<VisualDTO> & { id: string }): VisualDTO {
  return {
    session: "sup1", answer_agent_id: "sup1", accepts_files: false, title: "Invite email", by: "supervisor",
    kind: "visual", rev: 1, kept: false, unread: false, created_at: now - 60, holding: 0, ...over,
  };
}

const plan = (over: Partial<VisualDTO> = {}) => doc({ id: "plan_1", kind: "plan", title: "Bulk invite: plan", holding: 3, ...over });

function renderButton(visuals: VisualDTO[], onOpen = (_id: string) => {}) {
  return render(<VisualsButton visuals={visuals} onOpen={onOpen} />);
}

function openPanel() {
  fireEvent.click(screen.getByTestId("visuals-button"));
  return screen.getByTestId("visuals-panel");
}

describe("VisualsButton", () => {
  it("pins a holding plan first in the list, and one click opens it", () => {
    const opened: string[] = [];
    renderButton([doc({ id: "v_new", unread: true, created_at: now - 5 }), plan()], (id) => opened.push(id));
    const panel = openPanel();
    const rows = within(panel).getAllByRole("button");
    expect(rows[0]!.dataset.testid).toBe("visual-holding-plan_1");
    expect(rows[1]!.dataset.testid).toBe("visual-row-v_new");
    fireEvent.click(rows[0]!);
    expect(opened).toEqual(["plan_1"]);
  });

  it("keeps the dot while the plan holds work, after the plan is read", () => {
    const { rerender } = renderButton([plan({ unread: true })]);
    expect(screen.getByTestId("visuals-dot").getAttribute("aria-label")).toBe("holding");
    rerender(<VisualsButton visuals={[plan({ unread: false })]} onOpen={() => {}} />);
    expect(screen.getByTestId("visuals-dot").getAttribute("aria-label")).toBe("holding");
  });

  it("clears the dot once the plan stops holding", () => {
    const { rerender } = renderButton([plan({ holding: 3 })]);
    rerender(<VisualsButton visuals={[plan({ holding: 0 })]} onOpen={() => {}} />);
    expect(screen.queryByTestId("visuals-dot")).toBeNull();
  });

  it("keeps the unread dot for an unread doc that holds nothing", () => {
    renderButton([doc({ id: "v_1", unread: true })]);
    expect(screen.getByTestId("visuals-dot").getAttribute("aria-label")).toBe("unread");
  });

  it("renders the page icon without a visible label and names the button accessibly", () => {
    const { container } = renderButton([doc({ id: "v_1" })]);
    const button = screen.getByTestId("visuals-button");
    expect(button.textContent).toBe("");
    expect(button.getAttribute("aria-label")).toBe("Documents");
    expect(button.getAttribute("title")).toBe("Documents");
    const icon = button.querySelector("svg")!;
    const expectedIcon = render(<FileIcon />).container.querySelector("svg")!;
    expect(icon.getAttribute("viewBox")).toBe(expectedIcon.getAttribute("viewBox"));
    expect(icon.querySelector("path")?.getAttribute("d")).toBe(expectedIcon.querySelector("path")?.getAttribute("d"));
    expect(icon.querySelectorAll("path")[1]?.getAttribute("d")).toBe(expectedIcon.querySelectorAll("path")[1]?.getAttribute("d"));
    expect(container.querySelector("svg")).toBe(button.querySelector("svg"));
  });
});
