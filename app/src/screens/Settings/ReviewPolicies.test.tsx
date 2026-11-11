import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DaemonClient } from "../../api/client";
import { BASE_URL, TOKEN } from "../../test/fakeDaemon";
import { ReviewPolicies } from "./ReviewPolicies";

afterEach(cleanup);

it("lists the org's rules with who they need and what they cover now, and saves an edit and a new rule as one list", async () => {
  const security = { id: "security", name: "Security review", when: { kind: "paths", paths: ["billing/**", "auth/**"] },
    needs: { kind: "team", id: "t_1", name: "Security" }, approvals: 1, eligible: 6, note: "Security reviews money.", set_by: "dana", set_at: 7 };
  const staff = { id: "staff", name: "Staff sign-off", when: { kind: "any" }, needs: { kind: "role", id: "staff", name: "Staff" }, approvals: 1, eligible: 4 };
  const read = { org_id: "org_1", version: 4, policies: [security, staff],
    directory: { teams: [{ id: "t_1", name: "Security" }, { id: "t_2", name: "Finance" }], roles: [{ id: "staff", name: "Staff" }] },
    in_review: { policies: [{ policy: "security", count: 3, designs: [{ doc_id: "a", title: "Billing meter" }] }, { policy: "staff", count: 0, designs: [] }] } };
  const puts: unknown[] = [];
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: async (input, init) => {
    if (init?.method === "PUT") { puts.push(JSON.parse(String(init.body))); return Response.json({ version: 5, policies: [] }); }
    return Response.json(String(input).includes("library/people") ? { people: [] } : read);
  } });
  render(<ReviewPolicies client={client} />);
  fireEvent.click(screen.getByRole("button", { name: "Review policies" }));

  const row = await screen.findByTestId("review-policy-security");
  expect(row.dataset.count).toBe("3");
  expect(within(row).getByText(/any of 6 people/)).toBeTruthy();
  expect(screen.getByTestId("review-policy-staff").dataset.count).toBe("0");
  expect(screen.queryByTestId("review-policy-applies")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Edit Security review" }));
  expect(screen.getByTestId("review-policy-applies").dataset.count).toBe("3");
  fireEvent.change(screen.getByLabelText("Approvals"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "Save policy" }));
  await waitFor(() => expect(puts).toHaveLength(1));
  expect(puts[0]).toMatchObject({ version: 4, policies: [{ id: "security", approvals: 2, needs: { kind: "team", id: "t_1" } }, { id: "staff" }] });

  await screen.findByRole("form", { name: "New policy" });
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Payments review" } });
  fireEvent.change(screen.getByLabelText("Paths"), { target: { value: "payments/**, billing/ledger/**" } });
  fireEvent.change(screen.getByLabelText("Approvers"), { target: { value: "team:t_2" } });
  fireEvent.click(screen.getByRole("button", { name: "Save policy" }));
  await waitFor(() => expect(puts).toHaveLength(2));
  expect((puts[1] as { policies: unknown[] }).policies).toHaveLength(3);
  expect(puts[1]).toMatchObject({ policies: [{ id: "security" }, { id: "staff" },
    { id: "payments-review", when: { kind: "paths", paths: ["payments/**", "billing/ledger/**"] }, needs: { kind: "team", id: "t_2" }, approvals: 1 }] });
});
