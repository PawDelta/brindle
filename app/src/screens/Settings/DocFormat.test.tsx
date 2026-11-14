import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DocFormat as Format, DocFormatView } from "../../api/client";
import { DocFormat } from "./DocFormat";

afterEach(cleanup);

const EMPTY: Format = { templates: [], naming: { title_hint: null, reference: null }, blocks: ["sign_off"] };
const view = (resolved: Format, next: string | null = null): DocFormatView => ({ level: "org", format: resolved, resolved, source: "local", editable: true, next_reference: next, used: {} });

describe("DocFormat", () => {
  it("makes a first template from the starter, edits a section, the naming and what is blocked, and saves it", async () => {
    const setDocFormat = vi.fn(async (format: Format) => view(format, "DES-0001"));
    render(<DocFormat client={{ docFormat: vi.fn().mockResolvedValue(view(EMPTY)), setDocFormat }} />);

    fireEvent.click(await screen.findByRole("button", { name: "+ New" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Architecture is required" }));
    fireEvent.click(screen.getByRole("button", { name: "Move Rollout up" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Open questions" }));
    fireEvent.change(screen.getByLabelText("Reference"), { target: { value: "DES-{number}" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Sharing is blocked/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(setDocFormat).toHaveBeenCalledTimes(1));
    const saved = setDocFormat.mock.calls[0][0];
    const [template] = saved.templates;
    expect(template.default).toBe(true);
    expect(template.sections.map((s) => [s.title, s.required])).toEqual([
      ["Summary", true],
      ["Goals and non-goals", true],
      ["Rollout", true],
      ["Architecture", true],
    ]);
    expect(saved.naming.reference).toBe("DES-{number}");
    expect(saved.blocks).toEqual(["sign_off", "share"]);
    expect((await screen.findByTestId("next-reference")).textContent).toContain("DES-0001");
  });

  it("keeps what was typed while a save was on its way", async () => {
    let answer: (v: DocFormatView) => void = () => {};
    const setDocFormat = vi.fn(() => new Promise<DocFormatView>((resolve) => (answer = resolve)));
    render(<DocFormat client={{ docFormat: vi.fn().mockResolvedValue(view(EMPTY)), setDocFormat }} />);
    fireEvent.change(await screen.findByLabelText("Reference"), { target: { value: "DES-{number}" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.change(screen.getByLabelText("Title hint"), { target: { value: "What it does" } });
    await act(async () => answer(view({ ...EMPTY, naming: { title_hint: null, reference: "DES-{number}" } }, "DES-0001")));
    expect((screen.getByLabelText("Title hint") as HTMLInputElement).value).toBe("What it does");
    expect(screen.getByTestId("next-reference").textContent).toContain("DES-0001");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows the daemon's refusal and keeps the draft", async () => {
    const setDocFormat = vi.fn().mockRejectedValue(new Error("the reference pattern needs {number} once"));
    render(<DocFormat client={{ docFormat: vi.fn().mockResolvedValue(view(EMPTY)), setDocFormat }} />);
    fireEvent.change(await screen.findByLabelText("Reference"), { target: { value: "DES-" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toContain("{number}");
    expect((screen.getByLabelText("Reference") as HTMLInputElement).value).toBe("DES-");
  });
});
