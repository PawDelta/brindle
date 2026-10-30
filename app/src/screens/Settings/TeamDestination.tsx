/* Settings › Team destination: where the organisation's docs are exported to
   (SharePoint or Google Drive), shown to owners and admins only (GET /account's
   `role`; the daemon refuses anyone else on PUT and DELETE too). It shows what
   is connected (GET /analysis-docs/destination), connects one and revokes it
   (DELETE). Both are app credentials, never a person's sign-in (that is only for
   personal exports, Knowledge's Export): Google Drive takes a service account's
   JSON key and a shared drive's id (and a folder); SharePoint an Entra app's
   tenant id, client id and secret (granted Sites.Selected on one site) and that
   site (and a folder). The secret goes to the server once, in the PUT, and is
   dropped here; nothing is stored. Anyone else, or an engine without the routes,
   sees nothing. */

import { useEffect, useState } from "react";
import type { DaemonClient, TeamDestination as Destination, TeamDestinationKind } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, button, card, field, h2, label, notice } from "../../components/ui";

export type TeamDestinationClient = Pick<DaemonClient, "account" | "docDestination" | "setDocDestination" | "clearDocDestination">;

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const KINDS: { kind: TeamDestinationKind; name: string }[] = [
  { kind: "sharepoint", name: "SharePoint" },
  { kind: "google_drive", name: "Google Drive" },
];

const when = (v: number | string | null) => {
  if (v === null || v === "") return null;
  const ms = typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : Date.parse(v);
  return Number.isNaN(ms) ? null : new Date(ms).toLocaleDateString();
};

function Field({ name, value, onChange, secret, area }: { name: string; value: string; onChange(v: string): void; secret?: boolean; area?: boolean }) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className={label}>{name}</span>
      {area ? (
        <textarea className={`${field} font-mono text-xs`} rows={3} value={value} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input type={secret ? "password" : "text"} autoComplete="off" className={field} value={value} onChange={(e) => onChange(e.target.value)} />
      )}
    </label>
  );
}

export function TeamDestination({ client }: { client: TeamDestinationClient }) {
  const [admin, setAdmin] = useState(false);
  const [dest, setDest] = useState<Destination | null>(null);
  const [kind, setKind] = useState<TeamDestinationKind>("sharepoint");
  /* The credentials are held only until the PUT. */
  const [f, setF] = useState({ key: "", driveId: "", folderId: "", tenant: "", clientId: "", secret: "", site: "", folder: "" });
  const set = (k: keyof typeof f) => (v: string) => setF((had) => ({ ...had, [k]: v }));
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    client.account().then(
      (a) => {
        if (!live || (a.role !== "owner" && a.role !== "admin")) return;
        setAdmin(true);
        client.docDestination().then((d) => live && setDest(d), () => undefined);
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [client]);

  if (!admin) return null;

  const run = async (what: () => Promise<string>) => {
    setBusy(true);
    setError(null);
    setSaid(null);
    try {
      setSaid(await what());
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const connectGoogle = () =>
    run(async () => {
      let parsed: { type?: unknown; client_email?: unknown; private_key?: unknown } = {};
      try {
        parsed = JSON.parse(f.key);
      } catch {
        /* checked below */
      }
      if (parsed.type !== "service_account" || !parsed.client_email || !parsed.private_key) throw new Error("Paste or choose the service account's JSON key.");
      if (!f.driveId.trim()) throw new Error("Enter the shared drive's id.");
      try {
        setDest(await client.setDocDestination({ kind: "google_drive", target: { drive_id: f.driveId.trim(), ...(f.folderId.trim() ? { folder_id: f.folderId.trim() } : {}) }, credential: f.key }));
      } finally {
        set("key")("");
      }
      return "Connected to Google Drive.";
    });

  const connectSharePoint = () =>
    run(async () => {
      const [tenant_id, client_id, client_secret, site] = [f.tenant.trim(), f.clientId.trim(), f.secret.trim(), f.site.trim()];
      if (!tenant_id || !client_id || !client_secret) throw new Error("Enter the app's tenant id, client id and client secret.");
      if (!site) throw new Error("Enter the site's URL or id.");
      try {
        setDest(await client.setDocDestination({ kind: "sharepoint", target: { site, ...(f.folder.trim() ? { folder: f.folder.trim() } : {}) }, credential: { tenant_id, client_id, client_secret } }));
      } finally {
        set("secret")("");
      }
      return "Connected to SharePoint.";
    });

  const revoke = () =>
    run(async () => {
      await client.clearDocDestination();
      setDest({ kind: null, target_name: null, connected_at: null });
      return "Disconnected.";
    });

  const shown = KINDS.find((k) => k.kind === dest?.kind);
  return (
    <section id="team-destination" aria-labelledby="team-destination-title" className="flex scroll-mt-s2 flex-col gap-s1" data-testid="team-destination">
      <header className="flex flex-wrap items-baseline gap-x-s2">
        <h2 id="team-destination-title" className={h2}>
          Team destination
        </h2>
        <span className="text-xs text-ink-3">Where your organisation's docs are exported to. Owners and admins only.</span>
      </header>
      <div className={`${card} flex flex-col gap-s1 px-s2 py-s2 text-sm`}>
        {shown ? (
          <div className="flex flex-wrap items-center justify-between gap-s1">
            <span>
              {shown.name}: <strong>{dest?.target_name ?? "connected"}</strong>
              {when(dest?.connected_at ?? null) && <span className="text-ink-3"> · since {when(dest?.connected_at ?? null)}</span>}
            </span>
            <button type="button" className={button} disabled={busy} onClick={() => void revoke()}>
              Revoke
            </button>
          </div>
        ) : (
          <p className="text-ink-2">No destination is connected.</p>
        )}
        <div className="flex flex-col gap-s1" role="group" aria-label="Connect a destination">
          <label className="flex flex-col gap-0.5">
            <span className={label}>Where</span>
            <select className={field} value={kind} onChange={(e) => setKind(e.target.value as TeamDestinationKind)}>
              {KINDS.map((k) => (
                <option key={k.kind} value={k.kind}>
                  {k.name}
                </option>
              ))}
            </select>
          </label>
          {kind === "sharepoint" ? (
            <>
              <p className="text-xs text-ink-3">Use an Entra app granted Sites.Selected on this one site. The secret is sent once and isn't kept on this machine.</p>
              <Field name="Tenant id" value={f.tenant} onChange={set("tenant")} />
              <Field name="Client id" value={f.clientId} onChange={set("clientId")} />
              <Field name="Client secret" value={f.secret} onChange={set("secret")} secret />
              <Field name="Site URL or id" value={f.site} onChange={set("site")} />
              <Field name="Folder (optional)" value={f.folder} onChange={set("folder")} />
              <div>
                <button type="button" className={button} disabled={busy} onClick={() => void connectSharePoint()}>
                  Connect
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="text-xs text-ink-3">Add the service account's email as a Content manager on the shared drive first. The key is sent once and isn't kept on this machine.</p>
              <Field name="Service account JSON key" value={f.key} onChange={set("key")} area />
              <input
                type="file"
                accept="application/json,.json"
                aria-label="Choose the key file"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (file) void file.text().then(set("key"));
                }}
              />
              <Field name="Shared drive id" value={f.driveId} onChange={set("driveId")} />
              <Field name="Folder id (optional)" value={f.folderId} onChange={set("folderId")} />
              <div>
                <button type="button" className={button} disabled={busy} onClick={() => void connectGoogle()}>
                  Connect
                </button>
              </div>
            </>
          )}
        </div>
      </div>
      {said && (
        <p role="status" className={notice}>
          {said}
        </p>
      )}
      {error && (
        <p role="alert" className={alert}>
          Could not do that: {error}
        </p>
      )}
    </section>
  );
}
