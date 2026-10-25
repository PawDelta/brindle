import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, expect, it } from "vitest";
import { StrictMode } from "react";
import { DaemonClient, type PermissionEditorReview, type PermissionEditorRule } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { REPO } from "../../test/fixtures";
import { PermissionRules } from "./PermissionRules";

beforeAll(() => {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value(this: HTMLDialogElement) { this.open = true; } },
    close: { configurable: true, value(this: HTMLDialogElement) { this.open = false; queueMicrotask(() => this.dispatchEvent(new Event("close"))); } },
  });
});
afterEach(cleanup);

const rule = (values: Partial<PermissionEditorRule> = {}): PermissionEditorRule => ({
  id: "old", kind: "bash", match: "npm test", match_type: "exact", decision: "allow", source: "user", created: 1,
  repo_root: null, expires_at: null, exclusions: [], editable: true, protected: false, ...values,
});
function setup(daemon = new FakeDaemon()) {
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: daemon.fetch });
  render(<StrictMode><PermissionRules client={client} repo={REPO} /></StrictMode>);
  return daemon;
}
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const change = (name: string, value: string) => fireEvent.change(screen.getByLabelText(name), { target: { value } });
const apply = async () => { click("Save resolved rules"); await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); };

it("persists reviewed scoped CRUD, searches the live list and action inventory, and closes native controls outside", async () => {
  const daemon = new FakeDaemon();
  daemon.permissionEditor.restrictions.permission_policy = "off";
  setup(daemon);
    await screen.findByRole("button", { name: "Add rule" });
    expect(screen.getByRole("status", { name: "Permission enforcement" })).toBeDefined();
    click("Add rule");
    await screen.findByRole("dialog");
    expect(within(screen.getByRole("dialog")).getByRole("status", { name: "Permission enforcement" })).toBeDefined();
    const picker = screen.getByLabelText("Actions").closest("details")!;
    picker.open = true;
    change("Search actions", "Read");
    expect(within(picker).queryByRole("button", { name: /Run commands/ })).toBeNull();
    fireEvent.pointerDown(screen.getByRole("heading", { name: "Add rule" }));
    expect(picker.open).toBe(false);
    change("Target", "npm test");
    change("Repository", REPO);
    change("Lifetime", "time");
    change("Duration in minutes", "45");
    click("Save rule");
    await screen.findByRole("region", { name: "Review rule changes" });
    expect(daemon.permissionEditor.rules).toEqual([]);
    await apply();
    const saved = daemon.permissionEditor.rules[0];
    expect(saved).toMatchObject({ match: "npm test", repo_root: REPO, decision: "ask" });
    expect(saved.expires_at).toBeGreaterThan(Date.now() / 1000 + 44 * 60);
    expect(screen.getByRole("status", { name: "Permission enforcement" })).toBeDefined();
    change("Search rules", "unmatched");
    expect(screen.queryByTestId("permission-rule")).toBeNull();
    change("Search rules", "npm");
    click(`Edit rule ${saved.id}`);
    change("Target", "npm run build");
    click("Save rule");
    await screen.findByRole("button", { name: "Save resolved rules" });
    await apply();
    expect(daemon.permissionEditor.rules).toHaveLength(1);
    expect(daemon.permissionEditor.rules[0]).toMatchObject({ match: "npm run build", expires_at: saved.expires_at });
    click(`Edit rule ${saved.id}`);
    change("Duration in minutes", "90"); click("Save rule");
    daemon.permissionEditor.restrictions.permission_policy = "on";
    await screen.findByRole("button", { name: "Back to editing" }); click("Back to editing");
    await waitFor(() => expect(screen.queryByRole("status", { name: "Permission enforcement" })).toBeNull());
    await screen.findByRole("button", { name: "Save rule" }); click("Save rule");
    await screen.findByRole("button", { name: "Save resolved rules" }); await apply();
    expect(daemon.permissionEditor.rules[0].expires_at).toBeGreaterThan(Date.now() / 1000 + 89 * 60);
    click(`Remove rule ${saved.id}`);
    await screen.findByRole("button", { name: "Save resolved rules" });
    expect(daemon.permissionEditor.rules).toHaveLength(1);
    await apply();
    expect(daemon.permissionEditor.rules).toEqual([]);
    click("Add rule");
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { bubbles: true, cancelable: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
    click("Add rule");
    fireEvent.click(screen.getByRole("dialog"), { clientX: -1, clientY: -1 });
    expect(screen.queryByRole("dialog")).toBeNull();
});

it("compares backend scopes and saves the actual updated and removed rules after resolving", async () => {
  const daemon = new FakeDaemon();
  const existing = rule({ match: "docs/", kind: "read", match_type: "prefix" });
  const shadowed = rule({ id: "shadowed", kind: "read", match: "docs/private", repo_root: REPO });
  const incoming = rule({ id: "new", kind: "read", match: "docs/private", decision: "deny" });
  const narrowed = { ...existing, exclusions: [incoming] };
  const conflict = { left: "new", right: "old", overlap: "yes" as const, protected: false };
  daemon.permissionEditor.rules = [existing, shadowed];
  const manual: PermissionEditorReview = { revision: "editor-1", proposal: "manual", rules: [existing, shadowed, incoming], can_save: false,
    conflicts: [conflict], unresolved: [conflict], changes: [{ type: "added", id: "new", before: null, after: incoming }] };
  daemon.permissionReviews.push(manual, { ...manual, proposal: "replace", can_save: true, unresolved: [], rules: [incoming], changes: [
    { type: "added", id: "new", before: null, after: incoming }, { type: "removed", id: "old", before: existing, after: null }, { type: "removed", id: "shadowed", before: shadowed, after: null },
  ] }, { ...manual, proposal: "resolved", can_save: true, unresolved: [], rules: [narrowed, incoming], changes: [
    { type: "added", id: "new", before: null, after: incoming }, { type: "updated", id: "old", before: existing, after: narrowed }, { type: "removed", id: "shadowed", before: shadowed, after: null },
  ] });
  setup(daemon);
  await screen.findAllByTestId("permission-rule");
  await screen.findByRole("button", { name: "Add rule" });
  click("Add rule");
  change("Target", "docs/private");
  change("Decision", "deny");
  click("Save rule");
  await screen.findByRole("button", { name: "Use new rule" });
  expect(screen.getByRole("button", { name: "Save resolved rules" })).toHaveProperty("disabled", true);
  const review = screen.getByRole("region", { name: "Review rule changes" });
  expect(within(review).getAllByText(/docs\/private/).length).toBeGreaterThan(0);
  click("Use new rule");
  await waitFor(() => expect(within(review).getAllByText("Will be removed")).toHaveLength(2));
  expect(daemon.requests.at(-1)?.body).toMatchObject({ resolution: "new" });
  click("Auto resolve");
  await waitFor(() => expect(within(review).getAllByText("Will be removed")).toHaveLength(1));
  expect(daemon.requests.at(-1)?.body).toMatchObject({ resolution: "auto" });
  const removed = within(review).getByText("Will be removed").closest("article")!;
  expect(within(removed).getByText(REPO)).toBeDefined();
  await apply();
  expect(daemon.permissionEditor.rules).toEqual([narrowed, incoming]);
  expect(daemon.permissionEditor.rules.find((item) => item.id === "old")?.exclusions).toEqual([incoming]);
  const retained = daemon.permissionEditor.rules;
  const current = { ...manual, revision: daemon.permissionEditor.revision, rules: retained };
  daemon.permissionReviews.push(current, { ...current, proposal: "keep-existing", can_save: true, changes: [] });
  click("Add rule"); change("Target", "docs/private"); click("Save rule");
  await screen.findByRole("button", { name: "Keep existing" });
  click("Keep existing");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save resolved rules" })).toHaveProperty("disabled", false));
  await apply();
  expect(daemon.permissionEditor.rules).toEqual(retained);
  click("Edit rule old"); change("Target", "docs/archive"); click("Save rule");
  await screen.findByRole("button", { name: "Save resolved rules" }); await apply();
  expect(daemon.permissionEditor.rules.find((item) => item.id === "old")?.exclusions).toEqual([incoming]);
  click("Edit rule old");
  screen.getByLabelText("Actions").closest("details")!.open = true;
  click("Run commands builtin"); change("Target", "npm test"); click("Save rule");
  await screen.findByRole("button", { name: "Save resolved rules" }); await apply();
  expect(daemon.permissionEditor.rules.find((item) => item.id === "old")).toMatchObject({ kind: "bash", exclusions: [] });
});

it("blocks unknown and follow-on conflicts, then invalidates a stale reviewed proposal", async () => {
  const daemon = new FakeDaemon();
  const existing = rule();
  const incoming = rule({ id: "new", decision: "deny" });
  const unknown = { left: "new", right: "old", overlap: "unknown" as const, protected: false };
  const followOn = { ...unknown, overlap: "yes" as const, right: "other" };
  daemon.permissionEditor.rules = [existing];
  const review: PermissionEditorReview = { revision: "changed-before-review", proposal: "unknown", rules: [existing, incoming], changes: [], conflicts: [unknown], unresolved: [unknown], can_save: false };
  daemon.permissionReviews.push(review, { ...review, proposal: "follow-on", unresolved: [followOn] }, { ...review, proposal: "resolved", conflicts: [], unresolved: [], can_save: true, changes: [{ type: "added", id: "new", before: null, after: incoming }] });
  setup(daemon);
  await screen.findByRole("button", { name: "Add rule" });
  click("Add rule"); change("Target", "npm test");
  daemon.permissionEditor.revision = "changed-before-review";
  click("Save rule");
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Target")).toHaveProperty("value", "npm test");
  click("Save rule");
  await screen.findByRole("button", { name: "Use new rule" });
  expect(screen.getByRole("button", { name: "Use new rule" })).toHaveProperty("disabled", true);
  click("Keep existing");
  await waitFor(() => expect(daemon.requests.filter((request) => request.path.endsWith("/review"))).toHaveLength(3));
  expect(screen.getByRole("button", { name: "Save resolved rules" })).toHaveProperty("disabled", true);
  click("Back to editing");
  await screen.findByRole("button", { name: "Save rule" }); click("Save rule");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save resolved rules" })).toHaveProperty("disabled", false));
  daemon.permissionEditor.revision = "changed-elsewhere";
  click("Save resolved rules");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save resolved rules" })).toHaveProperty("disabled", true));
  expect(daemon.permissionEditor.rules).toEqual([existing]);
  expect(screen.getByRole("dialog")).toBeDefined();
});

it("uses backend presets as drafts and verifies exact MCP tools before saving custom actions", async () => {
  const daemon = new FakeDaemon();
  daemon.permissionEditor.presets = [{ id: "npm-prefix", name: "npm commands", rule: rule({ match: "npm ", match_type: "prefix" }) }];
  daemon.permissionEditor.catalog.push({ id: "mcp__brindle__status", kind: "mcp", name: "status", tool: "mcp__brindle__status", source: "brindle" });
  const legacy = [rule({ id: "prefix", kind: "mcp", match: "mcp__brindle__status", match_type: "prefix" }), rule({ id: "glob", kind: "mcp", match: "mcp__brindle__*", match_type: "glob" })];
  daemon.permissionEditor.rules = legacy;
  setup(daemon);
  await screen.findAllByTestId("permission-rule");
  for (const original of legacy) {
    click(`Edit rule ${original.id}`); change("Decision", "deny"); click("Save rule");
    await screen.findByRole("button", { name: "Save resolved rules" }); await apply();
    expect(daemon.permissionEditor.rules.find((item) => item.id === original.id)).toEqual({ ...original, decision: "deny" });
    click(`Edit rule ${original.id}`); change("Target", "mcp__unknown__changed"); click("Save rule");
    await screen.findByRole("alert");
    expect(daemon.permissionEditor.rules.find((item) => item.id === original.id)).toEqual({ ...original, decision: "deny" });
    click("Close rule editor");
  }
  await screen.findByRole("button", { name: "Presets" }); click("Presets"); click("Use npm commands");
  expect(daemon.permissionEditor.rules).toEqual(legacy.map((original) => ({ ...original, decision: "deny" })));
  click("Save rule");
  await screen.findByRole("button", { name: "Save resolved rules" }); await apply();
  expect(daemon.permissionEditor.rules[2]).toMatchObject({ match: "npm ", match_type: "prefix" });
  click("Add rule");
  expect(screen.getByRole("button", { name: "Roles" })).toHaveProperty("disabled", true);
  screen.getByLabelText("Actions").closest("details")!.open = true;
  change("Search actions", "mcp__brindle__status");
  expect(screen.getByRole("button", { name: /status mcp__brindle__status/ })).toBeDefined();
  click("Add custom action");
  change("Action name", "Check status"); change("Tool identifier", "mcp__unknown__status"); click("Verify tool");
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Use verified action" })).toHaveProperty("disabled", true);
  change("Tool identifier", "mcp__brindle__status"); click("Verify tool");
  await waitFor(() => expect(screen.getByRole("button", { name: "Use verified action" })).toHaveProperty("disabled", false));
  change("Action name", "Current status");
  expect(screen.getByRole("button", { name: "Use verified action" })).toHaveProperty("disabled", true);
  click("Verify tool");
  await waitFor(() => expect(screen.getByRole("button", { name: "Use verified action" })).toHaveProperty("disabled", false));
  click("Use verified action");
  expect(screen.getByLabelText("Target")).toHaveProperty("readOnly", true);
  click("Save rule");
  await screen.findByRole("button", { name: "Save resolved rules" }); await apply();
  expect(daemon.permissionEditor.rules[3]).toMatchObject({ kind: "mcp", match: "mcp__brindle__status", match_type: "exact" });
});

it("honors backend read-only ownership for managed policy and protected rules", async () => {
  const daemon = new FakeDaemon();
  daemon.permissionEditor.rules = [rule({ id: "org", source: "org", editable: false, protected: true })];
  daemon.permissionEditor.restrictions.editable = false;
  setup(daemon);
  await screen.findByTestId("permission-rule");
  expect(screen.getByRole("button", { name: "Add rule" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Edit rule org" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Remove rule org" })).toHaveProperty("disabled", true);
});
