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
  it("sends nothing until the listed files are confirmed, then uploads", async () => {
    const calls: { method: string; body: unknown }[] = [];
    const fetch: typeof globalThis.fetch = (_input, init) => {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      calls.push({ method: String(init?.method), body });
      return Promise.resolve(new Response(JSON.stringify(body.dry_run ? PLAN : {}), { status: 200, headers: { "Content-Type": "application/json" } }));
    };
    const client = new DaemonClient({ baseUrl: "http://daemon.test", token: "t", fetch });
    render(<ImportedLibrary client={client} repo="/r" source={SOURCE} disabled={false} onChanged={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Add to library" }));
    const plan = await screen.findByTestId("library-plan");
    expect(within(within(plan).getByLabelText("Will upload")).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["notes.md"]);
    expect(within(within(plan).getByLabelText("Skipped")).getByText(/prod-secrets\.md/)).toBeTruthy();
    expect(calls).toEqual([{ method: "POST", body: { dry_run: true } }]);

    fireEvent.click(screen.getByRole("button", { name: "Upload 1 file" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toEqual({ method: "POST", body: { token: "tok" } });
  });
});
