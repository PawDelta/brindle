import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LibraryList } from "./LibraryList";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const doc = {
  id: "plan",
  title: "Keep the full Library title visible when the sidebar takes up part of the window",
  updated_at: 1,
};

it.each([
  { width: 408, layout: "stacked", initialWidth: 1088 },
  { width: 1088, layout: "table", initialWidth: 928 },
])("lays out the Library row as $layout in a $width px container", ({ width, layout, initialWidth }) => {
  let resize: ((width: number) => void) | undefined;
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: ResizeObserverCallback) {}

    observe(target: Element) {
      resize = (width) => this.callback(
        [{ target, contentRect: { width } } as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
      resize(initialWidth);
    }

    disconnect() {}
  });
  render(<LibraryList client={{}} groups={[{ key: "all", kind: "other", label: "", docs: [doc], above: 0 }]}
    queue={new Map()} records={new Map()} scopes={[]} folders={[]} onOpen={() => {}} />);
  const row = screen.getByRole("listitem");
  expect(row.dataset.layout).toBe(layout === "stacked" ? "table" : "stacked");

  act(() => resize?.(width));

  expect(row.dataset.layout).toBe(layout);
  const fields = ["stage", "sign-offs", "open", "shared-with"];
  const names = (parent: Element) => Array.from(parent.children, (field) => field.getAttribute("data-field"));
  const button = row.querySelector("button")!;
  if (layout === "stacked") {
    expect(names(button)).toEqual(["title", "changed", "metadata"]);
    expect(names(button.lastElementChild!)).toEqual(fields);
  } else {
    expect(names(button)).toEqual(["title", ...fields, "changed"]);
  }
  expect(button.querySelector("[data-plan-title]")?.textContent).toBe(doc.title);
});
