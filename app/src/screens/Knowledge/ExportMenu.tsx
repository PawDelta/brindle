/* The reader's Export menu: Download (Markdown, Word, zip with appendices),
   and Google Docs and SharePoint. A destination shows only if the org's policy
   lists it (GET /v1/analysis-docs/policy), and Google Docs and SharePoint only
   when the app has a client id for them (GET /v1/analysis-docs/export-config;
   null hides the button). Until the policy is known, or if it can't be had, only
   Download shows. The two online destinations sign
   the person in for this session (ExportSession) and upload the Word export. */

import { useEffect, useRef, useState } from "react";
import type { DaemonClient, DocExportConfig, DocPolicy, ExportDestination } from "../../api/client";
import type { PickFolder } from "../../api/open";
import { plain } from "../../api/wording";
import { button, card, field } from "../../components/ui";
import { ExportSession, type SharePointSite } from "./ExportSession";

export type ExportClient = Pick<DaemonClient, "exportDoc" | "docPolicy" | "docExportConfig">;

export interface ExportMenuProps {
  client: ExportClient;
  id: string;
  title?: string;
  repo?: string;
  pickFolder?: PickFolder;
  /* Tells the reader what happened (or why not). */
  onSaid(text: string): void;
  /* Where the tokens of this session live; the menu's own by default. */
  session?: ExportSession;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const bytesOf = (b64: string | undefined) => Uint8Array.from(atob(b64 ?? ""), (c) => c.charCodeAt(0));

export function ExportMenu({ client, id, title, repo, pickFolder, onSaid, session }: ExportMenuProps) {
  const [policy, setPolicy] = useState<DocPolicy | null>(null);
  const [config, setConfig] = useState<DocExportConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [sites, setSites] = useState<SharePointSite[] | null>(null);
  const [search, setSearch] = useState("");
  const [site, setSite] = useState("");
  const [folder, setFolder] = useState("");
  const own = useRef<ExportSession | undefined>(undefined);
  const sess = session ?? (own.current ??= new ExportSession());

  useEffect(() => {
    let live = true;
    // A client without the routes (or an engine that lacks them) gets downloads only.
    Promise.resolve()
      .then(() => client.docPolicy(repo))
      .then(
        (p) => live && setPolicy(p),
        () => undefined,
      );
    Promise.resolve()
      .then(() => client.docExportConfig(repo))
      .then(
        (c) => live && setConfig(c),
        () => undefined,
      );
    return () => {
      live = false;
    };
  }, [client, repo]);

  const allowed = (d: ExportDestination) => (policy ? (policy.export_destinations ?? ["download"]).includes(d) : d === "download");
  const showDownload = allowed("download");
  const google = allowed("google_docs") && !!config?.google_client_id;
  const microsoft = allowed("sharepoint") && !!config?.microsoft_client_id;

  const act = async (what: () => Promise<string>) => {
    setBusy(true);
    try {
      onSaid(await what());
    } catch (e) {
      onSaid(message(e));
    } finally {
      setBusy(false);
    }
  };

  const download = (format: "md" | "docx" | "zip") =>
    act(async () => {
      if (pickFolder) {
        const dir = await pickFolder("Export the doc to a folder");
        if (!dir) return "Export cancelled.";
        const got = await client.exportDoc(id, format, dir);
        return `Exported to ${got.path ?? got.filename}.`;
      }
      const got = await client.exportDoc(id, format);
      const url = URL.createObjectURL(new Blob([bytesOf(got.content_b64)]));
      const a = document.createElement("a");
      a.href = url;
      a.download = got.filename;
      a.click();
      URL.revokeObjectURL(url);
      return `Exported ${got.filename}.`;
    });

  const docx = async (destination: ExportDestination) => {
    const got = await client.exportDoc(id, "docx", undefined, destination);
    return { name: (title || id).replace(/[\\/:*?"<>|]+/g, "-"), bytes: bytesOf(got.content_b64) };
  };

  const toGoogle = () =>
    act(async () => {
      if (!sess.signedIn("google")) await sess.signIn("google", config!.google_client_id!, config!.google_client_secret);
      const file = await docx("google_docs");
      const up = await sess.uploadToGoogle(file.name, file.bytes);
      return up.url ? `Opened as a Google Doc: ${up.url}` : "Saved as a Google Doc in your Drive.";
    });

  const findSites = () =>
    act(async () => {
      if (!sess.signedIn("microsoft")) await sess.signIn("microsoft", config!.microsoft_client_id!);
      const found = await sess.findSites(search);
      setSites(found);
      setSite(found[0]?.id ?? "");
      return found.length ? "Pick the site and folder." : "No site matches.";
    });

  const toSharePoint = () =>
    act(async () => {
      const file = await docx("sharepoint");
      const up = await sess.uploadToSharePoint(site, folder.trim(), `${file.name}.docx`, file.bytes);
      return up.url ? `Saved to SharePoint: ${up.url}` : "Saved to SharePoint.";
    });

  return (
    <div className={`${card} flex flex-col gap-1 px-s2 py-s1 text-sm`} role="group" aria-label="Export as">
      <div className="flex flex-wrap items-center gap-1">
        {showDownload &&
          (["md", "docx", "zip"] as const).map((f) => (
            <button key={f} type="button" className={button} disabled={busy} onClick={() => void download(f)}>
              {f === "md" ? "Markdown" : f === "docx" ? "Word" : "Zip with appendices"}
            </button>
          ))}
        {google && (
          <button type="button" className={button} disabled={busy} onClick={() => void toGoogle()}>
            Google Docs
          </button>
        )}
        {microsoft && (
          <button type="button" className={button} disabled={busy} onClick={() => void findSites()} aria-expanded={sites !== null}>
            SharePoint
          </button>
        )}
      </div>
      {microsoft && (
        <div className="flex flex-wrap items-center gap-1" aria-label="SharePoint site and folder">
          <input type="search" className={`${field} min-w-48`} aria-label="Find a SharePoint site" placeholder="Site name, then press SharePoint" value={search} onChange={(e) => setSearch(e.target.value)} />
          {sites !== null && sites.length > 0 && (
            <>
              <select className={field} aria-label="SharePoint site" value={site} onChange={(e) => setSite(e.target.value)}>
                {sites.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <input type="text" className={`${field} min-w-40`} aria-label="Folder" placeholder="Folder (blank: the top)" value={folder} onChange={(e) => setFolder(e.target.value)} />
              <button type="button" className={button} disabled={busy || !site} onClick={() => void toSharePoint()}>
                Save here
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
