import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient, type DocExportConfig, type ExportDestination } from "../../api/client";
import { ExportMenu } from "./ExportMenu";
import { ExportSession, type ExportDeps } from "./ExportSession";
import { ReaderChrome } from "../Session/Visuals/VisualReader";
import type { VisualFrameProps } from "../Session/Visuals/frame";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const ALL: ExportDestination[] = ["download", "google_docs", "sharepoint"];
const BOTH: DocExportConfig = { google_client_id: "g", google_client_secret: null, microsoft_client_id: "m" };

describe("ExportMenu", () => {
  it.each([["visual", "upload"], ["plan", "upload"], ["visual", "stale"], ["visual", "free"], ["visual", "reader"]] as const)("exports a %s source (%s)", async (kind, mode) => {
    const stale = mode === "stale";
    const content = kind === "visual" ? "<p>rendered snapshot</p>" : "# Plan\n\nShip it.";
    const mime = kind === "visual" ? "text/html" : "text/markdown";
    const ext = kind === "visual" ? "html" : "md";
    let opened = new URL("https://example.test");
    let release: () => void = () => undefined;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const requests: { url: string; init?: RequestInit }[] = [];
    const deps: ExportDeps = {
      listen: async () => 5151,
      openUrl: async (u) => { opened = new URL(u); },
      wait: async () => `code=ok&state=${opened.searchParams.get("state")}`,
      cancel: vi.fn(),
      fetch: async (url, init) => {
        requests.push({ url: String(url), init });
        return new Response(JSON.stringify(String(url).includes("token") ? { access_token: "ACCESS" } : String(url).includes("?search=") ? { value: [{ id: "site", name: "Team" }] } : {}));
      },
    };
    const client = new DaemonClient({ baseUrl: "http://daemon.test", token: "t", fetch: async (url, init) => {
      const path = String(url);
      if (path.endsWith("/analysis-docs/policy")) {
        return new Response(JSON.stringify({ export_destinations: ALL }), { status: mode === "free" ? 402 : 200 });
      }
      if (path.endsWith("/analysis-docs/export-config")) return new Response(JSON.stringify(BOTH));
      if (!path.endsWith("/visuals/v1/snapshot")) throw new Error("Unexpected daemon route");
      if (stale) await pending;
      const payload = JSON.parse(String(init!.body));
      return new Response(JSON.stringify({ filename: `Release.${ext}`, mime, content_b64: btoa(payload.markdown ?? payload.html) }));
    } });
    const source = kind === "visual" ? { kind, id: "v1", rev: 1, html: content } : { kind, id: "v1", rev: 1, markdown: content };
    const session = new ExportSession(deps);
    const completed = vi.fn();
    let post: VisualFrameProps["onMessage"] = () => undefined;
    let requested = 0;
    const Frame = (props: VisualFrameProps) => { post = props.onMessage; requested = props.snapshotRequest ?? 0; return <div />; };
    const view = render(mode === "reader"
      ? <ReaderChrome title="Release" source={{ kind: "session", id: "v1", rev: 1 }} meta="" exportClient={client} Frame={Frame} onBack={() => undefined} />
      : <ExportMenu client={client} source={source} session={session} onSaid={() => undefined} onComplete={completed} />);
    if (mode === "reader") {
      fireEvent.click(screen.getByRole("button", { name: /Export/ }));
      expect(requested).toBeGreaterThan(0);
      act(() => post({ type: "brindle.snapshot", html: "wrong" }, { kind: "session", id: "other", rev: 1 }));
      act(() => post({ type: "brindle.snapshot", html: "old" }, { kind: "session", id: "v1", rev: 0 }));
      expect(screen.queryByRole("button", { name: "Download" })).toBeNull();
      act(() => post({ type: "brindle.snapshot", html: content }, { kind: "session", id: "v1", rev: 1 }));
    }
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
    if (!stale) {
      let blob: Blob | undefined;
      let filename = "";
      vi.stubGlobal("URL", class extends URL {
        static createObjectURL(value: Blob) { blob = value; return "blob:export"; }
        static revokeObjectURL() {}
      });
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { filename = this.download; });
      fireEvent.click(screen.getByRole("button", { name: "Download" }));
      await waitFor(() => expect(filename).toBe(`Release.${ext}`));
      expect(blob!.type).toBe(mime);
      expect(await blob!.text()).toBe(content);
      if (mode === "reader") return;
      if (mode === "free") {
        // Off Pro the online destinations show, greyed, with a Pro badge; Download is for everyone.
        for (const name of ["Google Drive", "SharePoint"]) {
          const gated = await screen.findByRole("button", { name }) as HTMLButtonElement;
          expect(gated.disabled).toBe(true);
          expect(gated.textContent).toContain("Pro");
        }
        return;
      }
    }
    fireEvent.click(await screen.findByRole("button", { name: "Google Drive" }));
    if (stale) {
      view.rerender(<ExportMenu client={client} source={{ ...source, id: "v2" }} session={session} onSaid={() => undefined} />);
      release();
      await waitFor(() => expect((screen.getByRole("button", { name: "Google Drive" }) as HTMLButtonElement).disabled).toBe(false));
      expect(requests).toHaveLength(0);
      expect(completed).not.toHaveBeenCalled();
      return;
    }
    await waitFor(() => expect(requests.some((r) => r.url.includes("upload/drive"))).toBe(true));
    const drive = requests.find((r) => r.url.includes("upload/drive"))!;
    const body = await (drive.init!.body as Blob).text();
    expect(body).toContain('"mimeType":"application/vnd.google-apps.document"');
    expect(body).toContain(`Content-Type: ${mime}`);
    expect(body).toContain(content);
    await waitFor(() => expect((screen.getByRole("button", { name: "SharePoint" }) as HTMLButtonElement).disabled).toBe(false));
    expect(completed).toHaveBeenCalledTimes(2); // Download and Drive each finish an export.
    fireEvent.click(screen.getByRole("button", { name: "SharePoint" }));
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save here" }));
    expect(completed).toHaveBeenCalledTimes(2); // Finding a folder must leave the menu open.
    await waitFor(() => expect(requests.some((r) => r.init?.method === "PUT")).toBe(true));
    const sharepoint = requests.find((r) => r.init?.method === "PUT")!;
    expect(sharepoint.url).toContain(`/Release.${ext}:/content`);
    expect(sharepoint.init!.headers).toMatchObject({ "Content-Type": mime });
    expect(await (sharepoint.init!.body as Blob).text()).toBe(content);
    await waitFor(() => expect(completed).toHaveBeenCalledTimes(3));
  });

  it.each([
    ["policy excludes SharePoint", ["download", "google_docs"] as ExportDestination[], BOTH, "Google Drive", "SharePoint"],
    ["policy leaves downloads out", ["sharepoint"] as ExportDestination[], BOTH, "SharePoint", "Word"],
    ["no client id for a provider", ALL, { ...BOTH, microsoft_client_id: null }, "Google Drive", "SharePoint"],
  ])("only offers what is allowed: %s", async (_, export_destinations, config, shown, hidden) => {
    const client = { exportDoc: vi.fn(), docPolicy: vi.fn().mockResolvedValue({ export_destinations }), docExportConfig: vi.fn().mockResolvedValue(config) };
    render(<ExportMenu client={client} id="d1" onSaid={() => undefined} />);
    expect(await screen.findByRole("button", { name: shown })).toBeTruthy();
    expect(screen.queryByRole("button", { name: hidden })).toBeNull();
  });
});
