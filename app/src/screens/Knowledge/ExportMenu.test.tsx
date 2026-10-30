import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DocExportConfig, ExportDestination } from "../../api/client";
import { ExportMenu } from "./ExportMenu";

afterEach(cleanup);

const ALL: ExportDestination[] = ["download", "google_docs", "sharepoint"];
const BOTH: DocExportConfig = { google_client_id: "g", microsoft_client_id: "m" };

function menu(export_destinations: ExportDestination[], config: DocExportConfig) {
  const client = { exportDoc: vi.fn(), docPolicy: vi.fn().mockResolvedValue({ export_destinations }), docExportConfig: vi.fn().mockResolvedValue(config) };
  render(<ExportMenu client={client} id="d1" onSaid={() => undefined} />);
  return client;
}

describe("ExportMenu", () => {
  it("hides a destination the policy excludes", async () => {
    menu(["download", "google_docs"], BOTH);
    expect(await screen.findByRole("button", { name: "Google Docs" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "SharePoint" })).toBeNull();
    expect(screen.getByRole("button", { name: "Word" })).toBeTruthy();
  });

  it("hides downloads when the policy leaves them out", async () => {
    const client = menu(["sharepoint"], BOTH);
    expect(await screen.findByRole("button", { name: "SharePoint" })).toBeTruthy();
    expect(client.docPolicy).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Word" })).toBeNull();
  });

  it("hides the button of a provider whose client id is null", async () => {
    menu(ALL, { google_client_id: "g", microsoft_client_id: null });
    expect(await screen.findByRole("button", { name: "Google Docs" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "SharePoint" })).toBeNull();
  });
});
