import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient, type DocFormat as Format, type DocFormatView } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { DocFormat } from "./DocFormat";

afterEach(cleanup);

const EMPTY: Format = { templates: [], naming: { title_hint: null, reference: null }, blocks: ["sign_off"] };
const view = (resolved: Format, next: string | null = null): DocFormatView => ({ level: "org", format: resolved, version: 0, resolved, source: "local", editable: true, next_reference: next, used: {} });

const titles = () => screen.getAllByLabelText("Section title").map((el) => (el as HTMLInputElement).value);
const checked = (name: string | RegExp) => (screen.getByRole("checkbox", { name }) as HTMLInputElement).checked;

describe("DocFormat", () => {
  it("saves a template, its naming and what it blocks, and opens again with them", async () => {
    const daemon = new FakeDaemon();
    const open = () => render(<DocFormat client={new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: daemon.fetch })} />);
    const first = open();
    fireEvent.click(await screen.findByRole("button", { name: "+ New" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Architecture is required" }));
    fireEvent.click(screen.getByRole("button", { name: "Move Rollout up" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Open questions" }));
    fireEvent.change(screen.getByLabelText("Reference"), { target: { value: "DES-{number}" } });
    fireEvent.change(screen.getByLabelText("Title hint"), { target: { value: "What it does" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Sharing is blocked/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Sign-off is blocked/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true));
    first.unmount();

    // Opened again, everything comes back from the daemon as it was saved.
    open();
    expect((await screen.findByRole("option", { name: /Design doc/ })).getAttribute("aria-selected")).toBe("true");
    expect(titles()).toEqual(["Summary", "Goals and non-goals", "Rollout", "Architecture"]);
    expect([checked("Summary is required"), checked("Goals and non-goals is required"), checked("Rollout is required"), checked("Architecture is required")]).toEqual([true, true, true, true]);
    expect(checked("New docs start from it")).toBe(true);
    expect((screen.getByLabelText("Reference") as HTMLInputElement).value).toBe("DES-{number}");
    expect((screen.getByLabelText("Title hint") as HTMLInputElement).value).toBe("What it does");
    expect([checked(/Sign-off is blocked/), checked(/Sharing is blocked/)]).toEqual([false, true]);
    expect(screen.getByTestId("next-reference").textContent).toContain("DES-0001");
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
    const setDocFormat = vi.fn().mockRejectedValue(new Error("refused"));
    render(<DocFormat client={{ docFormat: vi.fn().mockResolvedValue(view(EMPTY)), setDocFormat }} />);
    fireEvent.change(await screen.findByLabelText("Reference"), { target: { value: "DES-" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("alert");
    expect((screen.getByLabelText("Reference") as HTMLInputElement).value).toBe("DES-");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
