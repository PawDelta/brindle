import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountInfo } from "../../api/client";
import { TeamDestination } from "./TeamDestination";

afterEach(cleanup);

function setup(role: string | null) {
  const client = {
    account: vi.fn().mockResolvedValue({ signed_in: true, role } as AccountInfo),
    docDestination: vi.fn().mockResolvedValue({ kind: "sharepoint", target_name: "Legal / Docs", connected_at: 1760000000 }),
    docExportConfig: vi.fn().mockResolvedValue({ google_client_id: "g", microsoft_client_id: "m" }),
    setDocDestination: vi.fn(),
    clearDocDestination: vi.fn().mockResolvedValue({}),
  };
  render(<TeamDestination client={client} />);
  return client;
}

describe("TeamDestination", () => {
  it.each(["owner", "admin"])("shows what is connected to a %s, who can revoke it", async (role) => {
    const client = setup(role);
    expect(await screen.findByText("Legal / Docs")).toBeTruthy();
    screen.getByRole("button", { name: "Revoke" }).click();
    await waitFor(() => expect(client.clearDocDestination).toHaveBeenCalled());
  });

  it.each(["member", null])("shows nothing to a role of %s", async (role) => {
    const client = setup(role);
    await waitFor(() => expect(client.account).toHaveBeenCalled());
    expect(screen.queryByTestId("team-destination")).toBeNull();
    expect(client.docDestination).not.toHaveBeenCalled();
  });
});
