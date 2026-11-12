/* The reader's Export menu: Download (Markdown, Word, zip with appendices),
   and Google Drive and SharePoint. A destination shows only if the org's policy
   lists it (GET /v1/analysis-docs/policy), and Google Drive and SharePoint only
   when the app has a client id for them (GET /v1/analysis-docs/export-config;
   null hides the button). Until the policy is known, or if it can't be had, only
   Download shows; for a visual or a plan off Pro (the policy answers 402), the
   two online destinations show greyed with a Pro badge. The two online
   destinations sign the person in for this session (ExportSession). Docs upload
   Word; visuals upload inert HTML and plans upload Markdown.

   One panel, one row per destination: an icon, the name, a line on what it
   makes. SharePoint opens a step under its row to find the site and folder. */

import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { DaemonError, type DaemonClient, type DocExportConfig, type DocPolicy, type ExportDestination } from "../../api/client";
import type { PickFolder } from "../../api/open";
import { plain } from "../../api/wording";
import { ArchiveIcon, ChevronDownIcon, ChevronRightIcon, DownloadIcon, DriveIcon, FileIcon, SitesIcon } from "../../components/icons";
import { button, field, label, panel, pill } from "../../components/ui";
import { ExportSession, type SharePointSite } from "./ExportSession";

export type ExportClient = Pick<DaemonClient, "exportDoc" | "docPolicy" | "docExportConfig"> & Partial<Pick<DaemonClient, "exportVisual">>;

export type ExportSource =
  | { kind: "doc"; id: string }
  | { kind: "visual"; id: string; rev: number; html: string }
  | { kind: "plan"; id: string; rev: number; markdown: string };

export interface ExportMenuProps {
  client: ExportClient;
  id?: string;
  source?: ExportSource;
  title?: string;
  repo?: string;
  pickFolder?: PickFolder;
  /* Tells the reader what happened (or why not). */
  onSaid(text: string): void;
  /* An export finished (success or failure); folder searches keep the menu open. */
  onComplete?(): void;
  /* What happened last, shown at the panel's foot (the reader keeps it). */
  status?: string;
  /* Where the tokens of this session live; the menu's own by default. */
  session?: ExportSession;
}

/* The panel the menu (and the reader's "Preparing" state) draws in: it floats in look, but sits in
   the layout, since a visual's native surface would cover anything laid over it. */
export const exportPanel = `${panel} flex w-80 max-w-full flex-col self-end p-1 shadow-lg`;

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const bytesOf = (b64: string | undefined) => Uint8Array.from(atob(b64 ?? ""), (c) => c.charCodeAt(0));

type Running = "md" | "docx" | "zip" | "google" | "sites" | "sharepoint";

export function ExportMenu({ client, id, source, title, repo, pickFolder, onSaid, onComplete, status, session }: ExportMenuProps) {
  const src = source ?? { kind: "doc" as const, id: id! };
  const isDoc = src.kind === "doc";
  const formatName = src.kind === "plan" ? "Markdown" : "HTML";
  const [policy, setPolicy] = useState<DocPolicy | null>(null);
  const [gated, setGated] = useState(false);
  const [config, setConfig] = useState<DocExportConfig | null>(null);
  const [running, setRunning] = useState<Running | null>(null);
  const busy = running !== null;
  const [sharing, setSharing] = useState(false);
  const [sites, setSites] = useState<SharePointSite[] | null>(null);
  const [search, setSearch] = useState("");
  const [site, setSite] = useState("");
  const [folder, setFolder] = useState("");
  const own = useRef<ExportSession | undefined>(undefined);
  const sess = session ?? (own.current ??= new ExportSession());
  const context = JSON.stringify([repo, src.kind, src.id, "rev" in src ? src.rev : null]);
  const current = useRef(context);
  current.current = context;
  const active = useRef(false);
  const checkCurrent = () => {
    if (current.current !== context) throw new Error("The export source changed. Open Export again.");
  };
  useEffect(() => {
    current.current = context;
    return () => { current.current = ""; };
  }, [context]);

  useEffect(() => {
    let live = true;
    setPolicy(null);
    setGated(false);
    setConfig(null);
    // A client without the routes (or an engine that lacks them) gets downloads only.
    Promise.resolve()
      .then(() => client.docPolicy(repo))
      .then(
        (p) => live && setPolicy(p),
        (e) => live && setGated(e instanceof DaemonError && e.status === 402),
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
  const showDownload = !isDoc || allowed("download");
  const google = allowed("google_docs") && !!config?.google_client_id;
  const microsoft = allowed("sharepoint") && !!config?.microsoft_client_id;
  // Off Pro, a visual or plan still shows where it could go: Download is for everyone.
  const proOnly = gated && !isDoc;

  const act = async (what: Running, run: () => Promise<string>) => {
    if (active.current) return;
    active.current = true;
    setRunning(what);
    try {
      const said = await run();
      if (current.current === context) onSaid(said);
    } catch (e) {
      if (current.current === context) onSaid(!isDoc && what !== "sites" ? `Export failed: ${message(e)}` : message(e));
    } finally {
      active.current = false;
      setRunning(null);
      if (current.current === context && what !== "sites") onComplete?.();
    }
  };

  const download = (format: "md" | "docx" | "zip") =>
    act(format, async () => {
      if (isDoc && pickFolder) {
        const dir = await pickFolder("Export the doc to a folder");
        if (!dir) return "Export cancelled.";
        const got = await client.exportDoc(src.id, format, dir);
        return `Exported to ${got.path ?? got.filename}.`;
      }
      const got = await file("download", format);
      checkCurrent();
      const url = URL.createObjectURL(new Blob([got.bytes], { type: got.mime }));
      const a = document.createElement("a");
      a.href = url;
      a.download = got.filename;
      a.click();
      URL.revokeObjectURL(url);
      return isDoc ? `Exported ${got.filename}.` : `Exported ${formatName} to your download location: ${got.filename}.`;
    });

  const file = async (destination: ExportDestination, format: "md" | "docx" | "zip" = "docx") => {
    if (src.kind === "doc") {
      const got = await client.exportDoc(src.id, format, undefined, destination);
      const name = (title || src.id).replace(/[\\/:*?"<>|]+/g, "-");
      const mime = format === "md" ? "text/markdown" : format === "zip" ? "application/zip" : "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
      return { ...got, name, mime, bytes: bytesOf(got.content_b64) };
    }
    if (!client.exportVisual) throw new Error("This engine cannot export visuals yet.");
    const got = await client.exportVisual(src.id, src.rev, src.kind === "visual" ? src.html : "", destination, src.kind === "plan" ? src.markdown : undefined);
    return { ...got, name: got.filename.replace(/\.[^.]+$/, ""), bytes: bytesOf(got.content_b64) };
  };

  const toGoogle = () =>
    act("google", async () => {
      const got = await file("google_docs");
      checkCurrent();
      if (!sess.signedIn("google")) await sess.signIn("google", config!.google_client_id!, config!.google_client_secret);
      checkCurrent();
      const up = await sess.upload(got.name, got.mime, got.bytes, true);
      if (!isDoc) return up.url ? `Exported ${formatName} to Google Drive: ${up.url}` : `Exported ${formatName} to Google Drive.`;
      return up.url ? `Opened as a Google Doc: ${up.url}` : "Saved as a Google Doc in your Drive.";
    });

  const findSites = (e?: FormEvent) => {
    e?.preventDefault();
    return act("sites", async () => {
      if (!sess.signedIn("microsoft")) await sess.signIn("microsoft", config!.microsoft_client_id!);
      const found = await sess.findSites(search);
      checkCurrent();
      setSites(found);
      setSite(found[0]?.id ?? "");
      return found.length ? "Pick the site and folder." : "No site matches.";
    });
  };

  const toSharePoint = () =>
    act("sharepoint", async () => {
      const got = await file("sharepoint");
      checkCurrent();
      const up = await sess.uploadToSharePoint(site, folder.trim(), isDoc ? `${got.name}.docx` : got.filename, got.bytes, got.mime);
      if (!isDoc) return up.url ? `Exported ${formatName} to SharePoint: ${up.url}` : `Exported ${formatName} to SharePoint.`;
      return up.url ? `Saved to SharePoint: ${up.url}` : "Saved to SharePoint.";
    });

  const formats = [
    { f: "md" as const, name: "Markdown", hint: "The doc as a .md file", icon: <FileIcon /> },
    { f: "docx" as const, name: "Word", hint: "A .docx, ready to share", icon: <FileIcon /> },
    { f: "zip" as const, name: "Zip with appendices", hint: "Word, with every appendix", icon: <ArchiveIcon /> },
  ];

  return (
    <div className={exportPanel} role="group" aria-label="Export as" data-testid="export-menu">
      <p className={`${label} px-2.5 pb-1 pt-1.5 text-[10.5px]`} aria-hidden>
        Export a copy
      </p>
      {showDownload && !isDoc && (
        <Item name="Download" hint={src.kind === "plan" ? "A Markdown file" : "A self-contained .html file"} icon={<DownloadIcon />} disabled={busy} running={running === "md"} onClick={() => void download("md")} />
      )}
      {showDownload && isDoc && formats.map((x) => <Item key={x.f} name={x.name} hint={x.hint} icon={x.icon} disabled={busy} running={running === x.f} onClick={() => void download(x.f)} />)}
      {(google || microsoft || proOnly) && <div className="mx-2.5 my-1 h-px bg-line" role="separator" />}
      {(google || proOnly) && (
        <Item name="Google Drive" hint={proOnly ? "Save to Drive on brindle Pro" : "Opens as a Google Doc"} icon={<DriveIcon />} pro={proOnly} disabled={busy || proOnly} running={running === "google"} onClick={() => void toGoogle()} />
      )}
      {(microsoft || proOnly) && (
        <Item
          name="SharePoint"
          hint={proOnly ? "Save to a site on brindle Pro" : "Into a site's document library"}
          icon={<SitesIcon />}
          pro={proOnly}
          disabled={busy || proOnly}
          expanded={proOnly ? undefined : sharing}
          onClick={() => setSharing(!sharing)}
        />
      )}
      {microsoft && sharing && (
        <div className="mx-1 mb-1 flex flex-col gap-2 rounded-md bg-surface-2 px-2.5 py-2" aria-label="SharePoint site and folder" role="group">
          <form className="flex items-center gap-1.5" onSubmit={(e) => void findSites(e)}>
            <input type="text" enterKeyHint="search" autoFocus className={`${field} min-w-0 flex-1`} aria-label="Find a SharePoint site" placeholder="Site name" value={search} onChange={(e) => setSearch(e.target.value)} />
            <button type="submit" className={`${button} text-xs`} disabled={busy}>
              {running === "sites" ? "Finding…" : "Find"}
            </button>
          </form>
          {sites !== null && sites.length > 0 && (
            <>
              <select className={field} aria-label="SharePoint site" value={site} onChange={(e) => setSite(e.target.value)}>
                {sites.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              <div className="flex items-center gap-1.5">
                <input type="text" className={`${field} min-w-0 flex-1`} aria-label="Folder" placeholder="Folder (blank: the top)" value={folder} onChange={(e) => setFolder(e.target.value)} />
                <button type="button" className={`${button} text-xs`} disabled={busy || !site} onClick={() => void toSharePoint()}>
                  {running === "sharepoint" ? "Saving…" : "Save here"}
                </button>
              </div>
            </>
          )}
        </div>
      )}
      {status && (
        <p role="status" className="mt-1 break-words border-t border-line px-2.5 pb-1 pt-2 text-xs text-ink-2">
          {status}
        </p>
      )}
    </div>
  );
}

/* One destination: its icon in a tile, its name, and what it makes; a Pro pill where it's gated. */
function Item({ name, hint, icon, pro, disabled, running, expanded, onClick }: { name: string; hint: string; icon: ReactNode; pro?: boolean; disabled?: boolean; running?: boolean; expanded?: boolean; onClick(): void }) {
  const hintId = useId();
  return (
    <button
      type="button"
      aria-label={name}
      aria-describedby={hintId}
      aria-expanded={expanded}
      aria-busy={running || undefined}
      title={pro ? `${name} needs brindle Pro` : undefined}
      disabled={disabled}
      className="group flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors duration-100 hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-branch disabled:cursor-not-allowed disabled:hover:bg-transparent aria-expanded:bg-surface-2"
      onClick={onClick}
    >
      <span className={`flex size-8 shrink-0 items-center justify-center rounded-md border border-line bg-canvas text-ink-2 transition-colors duration-100 group-hover:border-line-strong group-hover:text-ink group-disabled:group-hover:border-line group-disabled:group-hover:text-ink-2 ${pro ? "opacity-55" : ""}`}>{icon}</span>
      <span className={`flex min-w-0 flex-1 flex-col ${pro ? "opacity-70" : ""}`}>
        <span className="text-[13px] font-medium leading-5 text-ink">{name}</span>
        <span id={hintId} className="truncate text-xs leading-4 text-ink-3">
          {running ? "Working…" : hint}
        </span>
      </span>
      {pro && <span className={`${pill} text-branch`}>Pro</span>}
      {expanded !== undefined && (expanded ? <ChevronDownIcon size={14} className="text-ink-3" /> : <ChevronRightIcon size={14} className="text-ink-3" />)}
    </button>
  );
}
