import { useEffect, useState } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ExportButton } from "./ExportButton";
import { ReaderChrome } from "./VisualReader";
import type { VisualFrameProps } from "./frame";
import type { ExportClient } from "../../Knowledge/ExportMenu";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function Page({ covered, snapshotRequest, onMessage, source }: VisualFrameProps) {
  useEffect(() => {
    if (snapshotRequest) onMessage({ type: "brindle.snapshot", html: "<p>Chart</p>" }, source);
  }, [snapshotRequest]);
  return <article hidden={covered} aria-label="Chart">Chart</article>;
}

function client(): ExportClient {
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL() { return "blob:export"; }
    static revokeObjectURL() {}
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  return {
    docPolicy: async () => ({ export_destinations: ["download"] }),
    docExportConfig: async () => ({ google_client_id: null, google_client_secret: null, microsoft_client_id: null }),
    exportDoc: vi.fn(),
    exportVisual: vi.fn().mockResolvedValue({ filename: "Chart.html", mime: "text/html", content_b64: btoa("<p>Chart</p>") }),
  };
}

it("restores the same visual after a download or export failure and keeps the result visible", async () => {
  const api = client();
  render(<ReaderChrome title="Chart" meta="r1" source={{ kind: "session", id: "v1", rev: 1 }}
    onBack={() => {}} exportClient={api} Frame={Page} />);
  const page = screen.getByRole("article");
  await act(async () => fireEvent.click(screen.getByTestId("visual-export")));
  expect(screen.queryByRole("article")).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download" })));
  expect(screen.getByRole("article")).toBe(page);
  expect(screen.getByRole("status").textContent).toContain("Chart.html");
  expect(screen.queryByTestId("export-menu")).toBeNull();

  vi.mocked(api.exportVisual!).mockRejectedValueOnce(new Error("Disk unavailable"));
  await act(async () => fireEvent.click(screen.getByTestId("visual-export")));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download" })));
  expect(screen.getByRole("article")).toBe(page);
  expect(screen.getByRole("status").textContent).not.toContain("Chart.html");
  expect(screen.getByRole("status").textContent).not.toBe("");
  expect(screen.queryByTestId("export-menu")).toBeNull();
});

it("keeps a plan and its Markdown export confirmation visible after closing the menu", async () => {
  const api = client();
  vi.mocked(api.exportVisual!).mockResolvedValue({ filename: "Plan.md", mime: "text/markdown", content_b64: btoa("# Plan") });
  function Plan() {
    const [open, setOpen] = useState(false);
    return <><ExportButton client={api} source={{ kind: "plan", id: "p1", rev: 1, markdown: "# Plan" }} open={open} onOpenChange={setOpen} /><article>Plan</article></>;
  }
  render(<Plan />);
  const page = screen.getByRole("article");
  await act(async () => fireEvent.click(screen.getByTestId("visual-export")));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Download" })));
  expect(screen.getByRole("article")).toBe(page);
  expect(screen.queryByTestId("export-menu")).toBeNull();
  expect(screen.getByRole("status").textContent).toContain("Plan.md");
});
