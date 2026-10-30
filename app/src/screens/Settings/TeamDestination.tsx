/* Settings › Team destination: where the organisation's docs are exported to
   (SharePoint or Google Drive), shown to owners and admins only (GET /account's
   `role`; the daemon refuses anyone else on PUT and DELETE too). It shows what
   is connected (GET /analysis-docs/destination), connects one (the admin signs
   in with the provider in the system browser, as in Knowledge's Export, and the
   credential goes to the daemon in the PUT, then is forgotten here) and revokes
   it (DELETE). Anyone else, or an engine without the routes, sees nothing. */

import { useEffect, useRef, useState } from "react";
import type { DaemonClient, DocExportConfig, TeamDestination as Destination, TeamDestinationKind } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, button, card, field, h2, label, notice } from "../../components/ui";
import { ExportSession, type ExportProvider } from "../Knowledge/ExportSession";

export type TeamDestinationClient = Pick<DaemonClient, "account" | "docDestination" | "docExportConfig" | "setDocDestination" | "clearDocDestination">;

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const KINDS: { kind: TeamDestinationKind; name: string; provider: ExportProvider; target: string }[] = [
  { kind: "sharepoint", name: "SharePoint", provider: "microsoft", target: "Site id, then the folder: site-id/Docs/Analysis" },
  { kind: "google_drive", name: "Google Drive", provider: "google", target: "Drive folder id" },
];

const when = (v: number | string | null) => {
  if (v === null || v === "") return null;
  const ms = typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : Date.parse(v);
  return Number.isNaN(ms) ? null : new Date(ms).toLocaleDateString();
};

export function TeamDestination({ client, session }: { client: TeamDestinationClient; session?: ExportSession }) {
  const [admin, setAdmin] = useState(false);
  const [dest, setDest] = useState<Destination | null>(null);
  const [config, setConfig] = useState<DocExportConfig | null>(null);
  const [kind, setKind] = useState<TeamDestinationKind>("sharepoint");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const own = useRef<ExportSession | undefined>(undefined);
  const sess = session ?? (own.current ??= new ExportSession());

  useEffect(() => {
    let live = true;
    client.account().then(
      (a) => {
        if (!live || (a.role !== "owner" && a.role !== "admin")) return;
        setAdmin(true);
        client.docDestination().then((d) => live && setDest(d), () => undefined);
        client.docExportConfig().then((c) => live && setConfig(c), () => undefined);
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [client]);

  if (!admin) return null;

  const choice = KINDS.find((k) => k.kind === kind)!;
  const clientId = choice.provider === "google" ? config?.google_client_id : config?.microsoft_client_id;
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

  const connect = () =>
    run(async () => {
      try {
        await sess.signIn(choice.provider, clientId!);
        const credential = sess.credential(choice.provider)!;
        setDest(await client.setDocDestination({ kind, target: target.trim(), credential }));
      } finally {
        sess.signOut();
      }
      return `Connected to ${choice.name}.`;
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
        <div className="flex flex-wrap items-end gap-s1" role="group" aria-label="Connect a destination">
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
          <label className="flex min-w-56 flex-1 flex-col gap-0.5">
            <span className={label}>Target</span>
            <input type="text" className={field} placeholder={choice.target} value={target} onChange={(e) => setTarget(e.target.value)} />
          </label>
          <button type="button" className={button} disabled={busy || !clientId || !target.trim()} onClick={() => void connect()}>
            Sign in and connect
          </button>
        </div>
        {config && !clientId && <p className="text-xs text-ink-3">{choice.name} sign-in isn't set up for this build.</p>}
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
