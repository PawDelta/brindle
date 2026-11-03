import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DocExportConfig, ExportDestination } from "../../api/client";
import { ExportMenu } from "./ExportMenu";

afterEach(cleanup);

const ALL: ExportDestination[] = ["download", "google_docs", "sharepoint"];
const BOTH: DocExportConfig = { google_client_id: "g", google_client_secret: null, microsoft_client_id: "m" };

describe("ExportMenu", () => {
  it.each([
    ["policy excludes SharePoint", ["download", "google_docs"] as ExportDestination[], BOTH, "Google Docs", "SharePoint"],
    ["policy leaves downloads out", ["sharepoint"] as ExportDestination[], BOTH, "SharePoint", "Word"],
    ["no client id for a provider", ALL, { ...BOTH, microsoft_client_id: null }, "Google Docs", "SharePoint"],
  ])("only offers what is allowed: %s", async (_, export_destinations, config, shown, hidden) => {
    const client = { exportDoc: vi.fn(), docPolicy: vi.fn().mockResolvedValue({ export_destinations }), docExportConfig: vi.fn().mockResolvedValue(config) };
    render(<ExportMenu client={client} id="d1" onSaid={() => undefined} />);
    expect(await screen.findByRole("button", { name: shown })).toBeTruthy();
    expect(screen.queryByRole("button", { name: hidden })).toBeNull();
  });
});
