import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("connects Google Drive with a service account key and a drive id, and keeps nothing", async () => {
    const client = setup("admin");
    client.setDocDestination.mockResolvedValue({ kind: "google_drive", target_name: "Shared", connected_at: 1 });
    await screen.findByText("Legal / Docs");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "google_drive" } });
    const connect = () => fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    connect();
    fireEvent.change(screen.getByLabelText("Service account JSON key"), { target: { value: '{"type":"service_account","client_email":"a@b","private_key":"k"}' } });
    connect();
    await screen.findAllByRole("alert");
    expect(client.setDocDestination).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Shared drive id"), { target: { value: "drv" } });
    connect();
    await waitFor(() => expect(client.setDocDestination).toHaveBeenCalled());
    expect(client.setDocDestination.mock.calls[0][0]).toMatchObject({ kind: "google_drive", target: { drive_id: "drv" } });
    await waitFor(() => expect((screen.getByLabelText("Service account JSON key") as HTMLTextAreaElement).value).toBe(""));
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each(["member", null])("shows nothing to a role of %s", async (role) => {
    const client = setup(role);
    await waitFor(() => expect(client.account).toHaveBeenCalled());
    expect(screen.queryByTestId("team-destination")).toBeNull();
    expect(client.docDestination).not.toHaveBeenCalled();
  });
});
