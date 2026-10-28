import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { OrgApprovals } from "./OrgApprovals";

afterEach(cleanup);

const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const change = (name: string, value: string) => fireEvent.change(screen.getByLabelText(name), { target: { value } });

it("reads nothing until opened", async () => {
  const urls: string[] = [];
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: async (input) => {
    urls.push(String(input));
    return Response.json({ error: { code: "not_found", message: "Not in an organization" } }, { status: 404 });
  } });
  render(<OrgApprovals client={client} />);
  expect(urls).toEqual([]);
  click("Organization approvals");
  await screen.findByRole("alert");
  expect(urls.some((u) => u.includes("/org-permissions"))).toBe(true);
});

it("edits and clears a constructor role without reading inherited object properties", async () => {
  // The org's rules: a grouping, a push (the push matcher) and a pattern.
  const controls = [{ decision: "ask", group: "git/local", reason: "audited" },
    { decision: "deny", rule: "bash push * → main", reason: "main is protected" }, { decision: "ask", rule: "bash prefix npm publish", reason: "" }];
  const grant = { allow: true, kinds: ["bash"], overridable: [], max_minutes: 60, max_uses: 20,
    deny: [], allow_persistent: false, lifetimes: ["session", "time"], scopes: ["branch"], controls, org_name: "Acme" };
  const state = { identity: { sub: "actor", org_id: "org", role: "admin", policy_role: null },
    roles: [{ name: "constructor", builtin: false, permissions: [], holders: [] }], members: [],
    policy: { version: 3, permission_grants: grant, roles: {} as Record<string, unknown> },
    can_edit_policy: true, policy_readonly_reason: null, inventory_error: null as string | null };
  let refusal: { code: string; message: string } | null = null;
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: async (_input, init) => {
    if (init?.method === "PUT") {
      if (refusal) return Response.json({ error: refusal }, { status: 422 });
      const body = JSON.parse(String(init.body));
      state.policy.version++;
      if (body.role === null) state.policy.permission_grants = body.permission_grants;
      else if (body.permission_grants === null) delete state.policy.roles[body.role];
      else state.policy.roles[body.role] = body.permission_grants;
      return Response.json(state.policy);
    }
    return Response.json(state);
  } });
  render(<OrgApprovals client={client} />); click("Organization approvals");
  await screen.findByLabelText("Approval roles");
  const selectRole = (name: RegExp) => {
    screen.getByLabelText("Approval roles").closest("details")!.open = true;
    fireEvent.click(within(screen.getByRole("group", { name: "Available approval roles" })).getByRole("button", { name }));
  };
  selectRole(/^constructor /); change("Maximum uses", "7");
  selectRole(/^Organization default$/); selectRole(/^constructor /);
  expect(screen.getByLabelText("Maximum uses")).toHaveProperty("value", "7");
  click("Save approval limits");
  await waitFor(() => expect(Object.getOwnPropertyDescriptor(state.policy.roles, "constructor")?.value).toMatchObject({ max_uses: 7 }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Use inherited limits" })).toHaveProperty("disabled", false));
  expect(screen.getByLabelText("Maximum uses")).toHaveProperty("value", "7");
  click("Use inherited limits");
  await waitFor(() => expect(Object.hasOwn(state.policy.roles, "constructor")).toBe(false));
  await waitFor(() => expect(screen.getByLabelText("Maximum uses")).toHaveProperty("value", "20"));
  state.roles = []; state.inventory_error = "Inventory unavailable";
  selectRole(/^Organization default$/); click("Reload policy and keep drafts");
  await waitFor(() => expect(screen.queryByLabelText("Assignment roles")).toBeNull());
  expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", false);
  change("Maximum uses", "9");
  // The rule editor's own matchers: a pattern becomes a named matcher, the push and the grouping stay as they were.
  fireEvent.change(within(screen.getAllByTestId("org-pattern")[1]).getByLabelText("Match"), { target: { value: "git-force" } });
  click("Save approval limits");
  await waitFor(() => expect(state.policy.permission_grants.max_uses).toBe(9));
  expect(state.policy.permission_grants.controls).toEqual([controls[0], controls[1],
    { decision: "ask", rule: "bash git-force a git command with a force flag, or a push that forces or deletes a branch", reason: "" }]);
  // A rejected field can be corrected and saved again; a server without the
  // scoped save stops further saves until a reload.
  refusal = { code: "invalid_request", message: "The account rejected the approval limits (permission_grants)." };
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", false));
  change("Maximum uses", "11"); click("Save approval limits");
  await screen.findByRole("alert");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", false));
  refusal = { code: "scoped_save_unsupported", message: "The account server does not support saving approval limits alone yet." };
  click("Save approval limits");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", true));
  expect(screen.getByRole("alert")).toBeTruthy();
  expect(screen.getByLabelText("Maximum uses")).toHaveProperty("value", "11");
  expect(state.policy.permission_grants.max_uses).toBe(9);
});

it("searches account members and roles, assigns the selected role and retains a stale policy draft", async () => {
  const daemon = new FakeDaemon();
  const grant = { allow: true, kinds: ["bash"], overridable: [], max_minutes: 60, max_uses: 20,
    deny: ["bash prefix rm "], allow_persistent: false, lifetimes: ["session", "time"], scopes: ["branch"] };
  const state = { identity: { sub: "actor", org_id: "org", role: "admin", policy_role: null },
    roles: [{ name: "admin", builtin: true, permissions: ["roles", "control"], holders: ["actor"] },
      { name: "reviewer", builtin: false, permissions: [], holders: [] }],
    members: [{ sub: "person", email: "person@example.test", role: "member", policy_role: null as string | null }],
    policy: { version: 3, permission_grants: grant, roles: {} as Record<string, unknown> },
    can_edit_policy: true, policy_readonly_reason: null as string | null };
  let conflict = true;
  const writes: Record<string, unknown>[] = [];
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: async (input, init) => {
    const url = String(input);
    if (!url.includes("/org-permissions")) return daemon.fetch(input, init);
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body)); writes.push(body);
      if (url.endsWith("/role-assignment")) {
        if (body.role === "admin") return Response.json({ error: { code: "forbidden", message: "Role ceiling" } }, { status: 403 });
        state.members[0].policy_role = body.role;
        return Response.json({ org_id: "org", sub: "person", role: body.role });
      }
      if (conflict) { state.policy.version = 4; conflict = false;
        return Response.json({ error: { code: "version_conflict", message: "Changed elsewhere", version: 4 } }, { status: 409 }); }
      state.policy.version++;
      if (body.role === null) state.policy.permission_grants = body.permission_grants;
      else state.policy.roles[body.role] = body.permission_grants;
      return Response.json(state.policy);
    }
    return Response.json(state);
  } });
  render(<OrgApprovals client={client} />);
  click("Organization approvals");
  await screen.findByLabelText("Members");
  screen.getByLabelText("Members").closest("details")!.open = true;
  change("Search members", "nobody");
  expect(screen.queryByRole("button", { name: /person@example/ })).toBeNull();
  change("Search members", "person"); click("person@example.test person · member");
  screen.getByLabelText("Assignment roles").closest("details")!.open = true;
  change("Search assignment roles", "reviewer");
  const assignments = within(screen.getByRole("group", { name: "Available assignment roles" }));
  expect(assignments.queryByRole("button", { name: /^admin / })).toBeNull();
  fireEvent.click(assignments.getByRole("button", { name: /^reviewer / })); click("Assign role");
  await waitFor(() => expect(state.members[0].policy_role).toBe("reviewer"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Assign role" })).toHaveProperty("disabled", true));
  screen.getByLabelText("Approval roles").closest("details")!.open = true;
  fireEvent.click(within(screen.getByRole("group", { name: "Available approval roles" })).getByRole("button", { name: /^reviewer / }));
  change("Maximum uses", "7"); change("Denied targets", ""); click("Save approval limits");
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Maximum uses")).toHaveProperty("value", "7");
  expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", true);
  // Hidden and shown again: the draft is still there.
  click("Hide organization approvals"); click("Organization approvals");
  expect(screen.getByLabelText("Maximum uses")).toHaveProperty("value", "7");
  click("Reload policy and keep drafts");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", false));
  expect(screen.getByLabelText("Denied targets")).toHaveProperty("value", "");
  click("Save approval limits");
  await waitFor(() => expect(state.policy.roles.reviewer).toMatchObject({ max_uses: 7, deny: [] }));
  expect(writes[0]).toEqual({ org_id: "org", actor_sub: "actor", sub: "person", role: "reviewer" });
  expect(writes[2]).toMatchObject({ if_version: 4, role: "reviewer", permission_grants: { max_uses: 7, deny: [] } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Use inherited limits" })).toHaveProperty("disabled", false));
  click("Use inherited limits");
  await waitFor(() => expect(state.policy.roles.reviewer).toBeNull());
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", false));
  screen.getByLabelText("Approval roles").closest("details")!.open = true;
  click("Organization default"); change("Maximum minutes", "120");
  fireEvent.click(screen.getByLabelText("all")); fireEvent.click(screen.getByLabelText("forever"));
  click("Save approval limits");
  await waitFor(() => expect(state.policy.permission_grants).toMatchObject({ max_minutes: 120, scopes: ["branch", "all"], lifetimes: ["session", "time", "forever"] }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", false));
  state.can_edit_policy = false; state.policy_readonly_reason = "Control access removed";
  click("Reload policy and keep drafts");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save approval limits" })).toHaveProperty("disabled", true));
  expect(screen.getByLabelText("Maximum minutes").closest("fieldset")).toHaveProperty("disabled", true);
  screen.getByLabelText("Assignment roles").closest("details")!.open = true;
  change("Search assignment roles", "admin");
  fireEvent.click(within(screen.getByRole("group", { name: "Available assignment roles" })).getByRole("button", { name: /^admin / }));
  click("Assign role");
  await screen.findByRole("alert");
  expect(state.members[0].policy_role).toBe("reviewer");
  expect(screen.getByLabelText("Assignment roles").textContent).toBe("admin");
});
