import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountInfo } from "../../api/client";
import { TeamDestination } from "./TeamDestination";

afterEach(cleanup);

function setup(role: string | null) {
  const client = {
    account: vi.fn().mockResolvedValue({ signed_in: true, role } as AccountInfo),
    docDestination: vi.fn().mockResolvedValue({ kind: "sharepoint", target_name: "Legal / Docs", connected_at: 1760000000 }),
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

  /* Fills the named fields, presses Connect and waits for the press to finish (the button is enabled again). */
  async function connect(client: ReturnType<typeof setup>, kind: string, fields: Record<string, string>) {
    await screen.findByText("Legal / Docs");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: kind } });
    for (const [name, value] of Object.entries(fields)) fireEvent.change(screen.getByLabelText(name), { target: { value } });
    const press = screen.getByRole("button", { name: "Connect" });
    const calls = client.setDocDestination.mock.calls.length;
    fireEvent.click(press);
    await waitFor(() => expect((press as HTMLButtonElement).disabled).toBe(false));
    return client.setDocDestination.mock.calls.length > calls;
  }

  const KEY = '{"type":"service_account","client_email":"a@b","private_key":"k"}';

  it.each([
    ["no key", { "Shared drive id": "drv" }],
    ["a key but no drive id", { "Service account JSON key": KEY }],
  ])("sends nothing to Google Drive with %s", async (_, fields) => {
    const client = setup("admin");
    expect(await connect(client, "google_drive", fields)).toBe(false);
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("connects Google Drive with the key and a drive id, then forgets the key", async () => {
    const client = setup("admin");
    client.setDocDestination.mockResolvedValue({ kind: "google_drive", target_name: "Shared", connected_at: 1 });
    expect(await connect(client, "google_drive", { "Service account JSON key": KEY, "Shared drive id": "drv" })).toBe(true);
    expect(client.setDocDestination.mock.calls[0][0]).toEqual({ kind: "google_drive", target: { drive_id: "drv" }, credential: KEY });
    expect((screen.getByLabelText("Service account JSON key") as HTMLTextAreaElement).value).toBe("");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each([
    ["no secret", { "Tenant id": "t", "Client id": "c", "Site URL or id": "https://x.sharepoint.com/sites/a" }],
    ["no site", { "Tenant id": "t", "Client id": "c", "Client secret": "s" }],
  ])("sends nothing to SharePoint with %s", async (_, fields) => {
    const client = setup("admin");
    expect(await connect(client, "sharepoint", fields)).toBe(false);
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("connects SharePoint with an app credential and one site, then forgets the secret", async () => {
    const client = setup("owner");
    client.setDocDestination.mockResolvedValue({ kind: "sharepoint", target_name: "Site A", connected_at: 1 });
    const site = "https://x.sharepoint.com/sites/a";
    expect(await connect(client, "sharepoint", { "Tenant id": "t", "Client id": "c", "Client secret": "s3cret", "Site URL or id": site, "Folder (optional)": "Docs" })).toBe(true);
    expect(client.setDocDestination.mock.calls[0][0]).toEqual({ kind: "sharepoint", target: { site, folder: "Docs" }, credential: { tenant_id: "t", client_id: "c", client_secret: "s3cret" } });
    expect((screen.getByLabelText("Client secret") as HTMLInputElement).value).toBe("");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each(["member", null])("shows nothing to a role of %s", async (role) => {
    const client = setup(role);
    await waitFor(() => expect(client.account).toHaveBeenCalled());
    expect(screen.queryByTestId("team-destination")).toBeNull();
    expect(client.docDestination).not.toHaveBeenCalled();
  });
});
