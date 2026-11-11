import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DaemonClient } from "../../api/client";
import { BASE_URL, TOKEN } from "../../test/fakeDaemon";
import { ReviewPolicies } from "./ReviewPolicies";

afterEach(cleanup);

const security = { id: "security", name: "Security review", when: { kind: "paths", paths: ["billing/**", "auth/**"] },
  needs: { kind: "team", id: "t_1", name: "Security" }, approvals: 1, eligible: 6, note: "Security reviews money.", set_by: "dana", set_at: 7 };
const staff = { id: "staff", name: "Staff sign-off", when: { kind: "any" }, needs: { kind: "role", id: "staff", name: "Staff" }, approvals: 1, eligible: 4 };

/* The Library as the daemon relays it: a version that moves on every save, and draft-impact replies held until released. */
function library() {
  const lib = {
    version: 4, org: "org_1", saveStatus: 200, stored: null as unknown[] | null, puts: [] as { org_id: string; version: number; policies: { id: string }[] }[],
    impacts: [] as { names: string[]; reply(count: number, truncated?: boolean): void }[],
  };
  const read = () => ({ org_id: lib.org, version: lib.version, policies: lib.stored ?? [security, staff],
    directory: { teams: [{ id: "t_1", name: "Security" }, { id: "t_2", name: "Finance" }], roles: [{ id: "staff", name: "Staff" }] },
    in_review: { policies: [{ policy: "security", count: 3, designs: [] }, { policy: "staff", count: 0, designs: [] }] } });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: async (input, init) => {
    const url = String(input);
    if (init?.method === "PUT") {
      lib.puts.push(JSON.parse(String(init.body)));
      if (lib.saveStatus === 200) lib.stored = lib.puts.at(-1)!.policies;
      return lib.saveStatus === 200 ? Response.json({ version: ++lib.version, policies: [] })
        : Response.json({ error: { code: "conflict", message: "the rules changed" } }, { status: lib.saveStatus });
    }
    if (url.includes("draft-impact")) {
      const names = (JSON.parse(String(init?.body)).policies as { name: string }[]).map((p) => p.name);
      return new Promise<Response>((resolve) => lib.impacts.push({ names, reply: (count, truncated = false) =>
        resolve(Response.json({ org_id: lib.org, truncated, count, designs: [{ doc_id: "a", title: "Billing meter" }], reopens_gates: false, reopened: 0 })) }));
    }
    return Response.json(url.includes("library/people") ? { people: [{ sub: "u_sam", email: "sam@x" }], me: "u_me" } : read());
  } });
  return { lib, client };
}

const open = async (client: DaemonClient) => {
  render(<ReviewPolicies client={client} />);
  fireEvent.click(screen.getByRole("button", { name: "Review policies" }));
  return screen.findByTestId("review-policy-security");
};
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const save = () => fireEvent.click(screen.getByRole("button", { name: "Save policy" }));

it("lists the rules with who they need and what they cover, and saves edits and new rules for the org it loaded", async () => {
  const { lib, client } = library();
  const row = await open(client);
  expect(row.dataset.count).toBe("3");
  expect(within(row).getByText(/any of 6 people/)).toBeTruthy();
  expect(screen.getByTestId("review-policy-staff").dataset.count).toBe("0");

  fireEvent.click(screen.getByRole("button", { name: "Edit Security review" }));
  type("Approvals", "2");
  save();
  await waitFor(() => expect(lib.puts).toHaveLength(1));
  expect(lib.puts[0]).toMatchObject({ org_id: "org_1", version: 4, policies: [{ id: "security", approvals: 2 }, { id: "staff" }] });

  // A name past the Library's 32-character id limit, twice: the id is capped, with the suffix inside the cap.
  const long = "Payments and ledger review for every billing change";
  await screen.findByRole("form", { name: "New policy" });
  for (const _ of [1, 2]) {
    type("Name", long);
    type("Paths", "payments/**, billing/ledger/**");
    type("Approvers", "person:u_me"); // the signed-in admin can name themself
    save();
    await waitFor(() => expect(lib.puts.length).toBeGreaterThan(1));
    await screen.findByRole("form", { name: "New policy" });
  }
  const ids = lib.puts.slice(1).map((p) => p.policies.at(-1)!.id);
  expect(ids[0]).toHaveLength(32);
  expect(ids[1]).toHaveLength(32);
  expect(ids[1]).not.toBe(ids[0]);
  expect(lib.puts[1].policies.at(-1)).toMatchObject({ needs: { kind: "person", id: "u_me" }, approvals: 1 });
});

it("shows what saving would do only for the draft on the form, and holds saves after a conflict until the person reloads", async () => {
  const { lib, client } = library();
  await open(client);
  type("Name", "Finance");
  type("Paths", "payments/**");
  type("Approvers", "team:t_2");
  await waitFor(() => expect(lib.impacts).toHaveLength(1));
  // The draft changes before the first answer arrives: that answer is of a draft no longer on the form.
  type("Name", "Finance 2");
  fireEvent.blur(screen.getByLabelText("Name"));
  await waitFor(() => expect(lib.impacts).toHaveLength(2));
  // Only the draft rule is evaluated: the org's other rules (one covers every design) would be counted into it.
  expect(lib.impacts.map((i) => i.names)).toEqual([["Finance"], ["Finance 2"]]);
  await act(async () => lib.impacts[0].reply(9));
  expect(screen.queryByTestId("review-policy-applies")).toBeNull();
  await act(async () => lib.impacts[1].reply(5, true));
  const note = screen.getByTestId("review-policy-applies");
  expect([note.dataset.count, note.dataset.truncated]).toEqual(["5", "true"]);

  // Another admin saved first: the 409 is shown, the draft stays, and saving waits for a reload.
  lib.saveStatus = 409;
  save();
  await screen.findByRole("alert");
  expect((screen.getByRole("button", { name: "Save policy" }) as HTMLButtonElement).disabled).toBe(true);
  lib.saveStatus = 200;
  lib.version = 5;
  fireEvent.click(screen.getByRole("button", { name: "Reload policies and keep my draft" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "Save policy" }) as HTMLButtonElement).disabled).toBe(false));
  expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Finance 2");
  save();
  await waitFor(() => expect(lib.puts.at(-1)).toMatchObject({ version: 5 }));
});

it("removes a rule only once the list-without-rule impact is shown, and a removal opened for one org is dropped when another loads", async () => {
  const { lib, client } = library();
  await open(client);
  const confirm = () => screen.getByRole("button", { name: "Confirm remove Security review" }) as HTMLButtonElement;
  fireEvent.click(screen.getByRole("button", { name: "Remove Security review" }));
  await waitFor(() => expect(lib.impacts).toHaveLength(1));
  // The impact asked of the Library is the list without the removed rule. Confirm waits for its answer.
  expect(lib.impacts[0].names).toEqual(["Staff sign-off"]);
  expect(confirm().disabled).toBe(true);
  fireEvent.click(confirm());
  expect(lib.puts).toHaveLength(0);
  // Another org loads with the same rule id: the removal opened under org_1 is gone, not carried over.
  lib.org = "org_2";
  fireEvent.click(screen.getByRole("button", { name: "Reload policies" }));
  await waitFor(() => expect(screen.queryByTestId("review-policy-remove-security")).toBeNull());
  // Removing again under org_2: after the count, the save goes to org_2 at the version read.
  fireEvent.click(screen.getByRole("button", { name: "Remove Security review" }));
  await waitFor(() => expect(lib.impacts).toHaveLength(2));
  await act(async () => lib.impacts[1].reply(2));
  await waitFor(() => expect(confirm().disabled).toBe(false));
  // Keep, then reopen the same removal: its earlier count must not enable Confirm while the new one is pending.
  fireEvent.click(screen.getByRole("button", { name: "Keep" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove Security review" }));
  await waitFor(() => expect(lib.impacts).toHaveLength(3));
  expect(confirm().disabled).toBe(true);
  fireEvent.click(confirm());
  expect(lib.puts).toHaveLength(0);
  await act(async () => lib.impacts[2].reply(3));
  expect(screen.getByTestId("review-policy-remove-applies").dataset.count).toBe("3");
  await waitFor(() => expect(confirm().disabled).toBe(false));
  fireEvent.click(confirm());
  await waitFor(() => expect(lib.puts).toHaveLength(1));
  expect(lib.puts[0]).toMatchObject({ org_id: "org_2", version: 4, policies: [{ id: "staff" }] });
  expect(lib.puts[0].policies).toHaveLength(1);
});

it("overlapping reloads keep the newest read, and a draft for the org just loaded survives the older one finishing", async () => {
  const reads: { answer(r: Response): void }[] = [];
  const puts: { org_id: string; version: number }[] = [];
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: async (input, init) => {
    if (init?.method === "PUT") { puts.push(JSON.parse(String(init.body))); return Response.json({ version: 9, policies: [] }); }
    if (String(input).includes("library/people")) return Response.json({ people: [] });
    return new Promise<Response>((resolve) => reads.push({ answer: resolve }));
  } });
  const answer = (i: number, org: string, version: number) => reads[i].answer(Response.json(
    { org_id: org, version, policies: [], directory: { teams: [{ id: "t_1", name: "Security" }], roles: [] }, in_review: { policies: [] } }));
  render(<ReviewPolicies client={client} />);
  fireEvent.click(screen.getByRole("button", { name: "Review policies" }));
  await waitFor(() => expect(reads).toHaveLength(1));
  await act(async () => answer(0, "org_a", 1));
  await screen.findByRole("form", { name: "New policy" });

  fireEvent.click(screen.getByRole("button", { name: "Reload policies" }));
  fireEvent.click(screen.getByRole("button", { name: "Reload policies" }));
  await waitFor(() => expect(reads).toHaveLength(3));
  // The newer reload loads org B and the person starts a draft for it; the older one then finishes, with
  // org B's earlier version. It is dropped: it neither erases the draft nor puts older state under it.
  await act(async () => answer(2, "org_b", 5));
  type("Name", "B draft");
  await act(async () => answer(1, "org_b", 4));
  expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("B draft");
  type("Paths", "b/**");
  type("Approvers", "team:t_1");
  save();
  await waitFor(() => expect(puts).toHaveLength(1));
  expect(puts[0]).toMatchObject({ org_id: "org_b", version: 5 });
});
