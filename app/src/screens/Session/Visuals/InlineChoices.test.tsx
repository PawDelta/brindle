import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { VisualDTO } from "../../../api/client";
import { VisualReader } from "./VisualReader";

afterEach(cleanup);

const visual: VisualDTO = {
  id: "v_inline", session: "supervisor", by: "supervisor", kind: "visual", rev: 1,
  title: "Two choices", answer: null, kept: false, unread: false, created_at: 0,
  inline_html: `
    <style>
      #recommendation { border: 2px solid purple; background: lavender; }
      fieldset button span { display: none !important; }
    </style>
    <fieldset data-choice="first">
      <legend>First question</legend>
      <button id="recommendation" data-option="A" data-selected style="border-width: 3px !important">Option A</button>
      <button data-option="B">Option B</button>
    </fieldset>
    <fieldset data-choice="second">
      <legend>Second question</legend>
      <button data-option="C">Option C</button>
    </fieldset>`,
  choices: [
    { id: "first", question: "First question", options: [{ id: "A", label: "Option A" }, { id: "B", label: "Option B" }] },
    { id: "second", question: "Second question", options: [{ id: "C", label: "Option C" }] },
  ],
};

function openChoices() {
  const client = {
    visuals: vi.fn(), answerVisual: vi.fn(), dismissVisual: vi.fn(),
    keepVisual: vi.fn(), unkeepVisual: vi.fn(), markVisualRead: vi.fn(),
  };
  render(<VisualReader client={client} visual={visual} onBack={() => {}} onAnswered={() => {}} />);
  const frame = screen.getByTitle<HTMLIFrameElement>(visual.title);
  const doc = frame.contentDocument!;
  // jsdom does not load srcdoc into the frame.
  doc.documentElement.innerHTML = new DOMParser().parseFromString(frame.srcdoc, "text/html").documentElement.innerHTML;
  fireEvent.load(frame);
  return doc;
}

it("marks only the user's pick when the page highlights a different option", () => {
  const doc = openChoices();
  const recommended = doc.querySelector<HTMLButtonElement>('[data-option="A"]')!;
  const picked = doc.querySelector<HTMLButtonElement>('[data-option="B"]')!;

  const originalStyle = recommended.getAttribute("style");
  fireEvent.click(recommended);
  const selectedBorder = doc.defaultView!.getComputedStyle(recommended).borderWidth;
  fireEvent.click(picked);

  expect(Array.from(doc.querySelectorAll("button[data-selected]"))).toEqual([picked]);
  expect(selectedBorder).toBe("2px");
  expect(recommended.getAttribute("style")).toBe(originalStyle);
  const filled = picked.querySelector('[data-choice-mark="selected"]')!;
  const empty = recommended.querySelector('[data-choice-mark="empty"]')!;
  expect(doc.defaultView!.getComputedStyle(filled).display).toBe("inline-block");
  expect(doc.defaultView!.getComputedStyle(empty).display).toBe("inline-block");
});

it("clears each legend's needs-answer state as the remaining count drops", () => {
  const doc = openChoices();
  const first = doc.querySelector('[data-choice="first"] legend')!;
  const second = doc.querySelector('[data-choice="second"] legend')!;
  const status = first.querySelector("[data-choice-status]")!;
  const send = screen.getByTestId<HTMLButtonElement>("visual-send");

  expect(first.hasAttribute("data-needs-answer")).toBe(true);
  expect(doc.defaultView!.getComputedStyle(status).display).toBe("inline-block");
  expect(second.hasAttribute("data-needs-answer")).toBe(true);
  expect(screen.getByTestId("visual-unpicked").dataset.remaining).toBe("2");
  expect(send.disabled).toBe(true);

  fireEvent.click(doc.querySelector('[data-option="B"]')!);

  expect(first.hasAttribute("data-needs-answer")).toBe(false);
  expect(doc.defaultView!.getComputedStyle(status).display).toBe("none");
  expect(second.hasAttribute("data-needs-answer")).toBe(true);
  expect(screen.getByTestId("visual-unpicked").dataset.remaining).toBe("1");
  expect(send.disabled).toBe(true);

  fireEvent.click(doc.querySelector('[data-option="C"]')!);

  expect(second.hasAttribute("data-needs-answer")).toBe(false);
  expect(screen.queryByTestId("visual-unpicked")).toBeNull();
  expect(send.disabled).toBe(false);
});
