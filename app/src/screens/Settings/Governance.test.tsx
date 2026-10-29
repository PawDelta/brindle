import { describe, expect, it } from "vitest";
import type { GovernanceStatusDTO } from "../../api/schema";
import { statusLine } from "./Governance";

describe("statusLine", () => {
  it.each<[GovernanceStatusDTO, string | null]>([
    [{ status: "managed", org_name: "Acme", verified: true, source: "device" }, "Managed by Acme · policy verified · from this device"],
    [{ status: "managed", org_name: "Acme", verified: true, source: "account" }, "Managed by Acme · policy verified · from your account"],
    [{ status: "problem", org_id: "org_1", verified: false, source: "offline" }, "Managed by org_1 · policy not verified · from an offline policy export"],
    [{ status: "unmanaged" }, "Not managed by an organization."],
    // A problem with no organization to name (an unusable device file): the problem itself says it.
    [{ status: "problem", problem: "This device's managed device file can't be used: bad signature" }, null],
  ])("%j reads %s", (status, line) => {
    expect(statusLine(status)).toBe(line);
  });
});
