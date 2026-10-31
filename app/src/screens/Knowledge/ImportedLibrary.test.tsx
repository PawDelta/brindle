import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DaemonClient, type KbSource } from "../../api/client";
import { ImportedLibrary } from "./ImportedLibrary";

afterEach(cleanup);

const SOURCE: KbSource = { id: "s1", kind: "folder", scope: "/home/u/notes" };
const PLAN = {
  files: [{ name: "notes.md", bytes: 30 }],
  skipped: [{ name: "prod-secrets.md", reason: "its name looks like a secret" }],
  allowed: true,
  token: "tok",
};

describe("imported folder library", () => {
  it.each([
    ["local", "Add to library"],
    ["adding", "Resume adding"],
  ] as const)("sends nothing until the listed files are confirmed, then uploads (%s folder)", async (library_state, start) => {
    const calls: { method: string; body: unknown }[] = [];
    const fetch: typeof globalThis.fetch = (_input, init) => {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      calls.push({ method: String(init?.method), body });
      return Promise.resolve(new Response(JSON.stringify(body.dry_run ? PLAN : {}), { status: 200, headers: { "Content-Type": "application/json" } }));
    };
    const client = new DaemonClient({ baseUrl: "http://daemon.test", token: "t", fetch });
    render(<ImportedLibrary client={client} repo="/r" source={{ ...SOURCE, library_state }} disabled={false} onChanged={() => {}} />);

    if (library_state === "adding") expect(screen.getByRole("button", { name: "Remove" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: start }));
    const plan = await screen.findByTestId("library-plan");
    expect(within(within(plan).getByLabelText("Will upload")).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["notes.md"]);
    expect(within(within(plan).getByLabelText("Skipped")).getByText(/prod-secrets\.md/)).toBeTruthy();
    expect(calls).toEqual([{ method: "POST", body: { dry_run: true } }]);

    fireEvent.click(screen.getByRole("button", { name: "Upload 1 file" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toEqual({ method: "POST", body: { token: "tok" } });
  });

  it("keeps Finish removing until the folder is finalized, even with nothing left queued", () => {
    const client = new DaemonClient({ baseUrl: "http://daemon.test", token: "t", fetch: () => Promise.reject(new Error("unused")) });
    render(<ImportedLibrary client={client} repo="/r" source={{ ...SOURCE, library_state: "removing", library_removing: 0 }} disabled={false} onChanged={() => {}} />);
    expect(screen.getByRole("button", { name: "Finish removing" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add to library" })).toBeNull();
  });
});
