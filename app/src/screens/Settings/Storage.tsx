/* Settings › Storage: brindle's disk use (GET /storage, disk.storage_report)
   by category, free space, and the abandoned worktrees (unmerged, no activity
   for a while) with their size and last activity. Clean removes one, Clean
   all the removable abandoned ones shown when the person said yes (POST
   /storage/clean once per confirmed path: never "every abandoned one", which
   the engine would list afresh), nothing removed before; the engine checks
   each again and archives its branch tip first. Only the newest report read
   is shown (reads on the heavy lane can land out of order). A
   worktree the engine keeps (uncommitted changes, a live session, not
   brindle's) says why and has no Clean; one that changed since the report
   and was kept by the engine says why too. The figure the Inbox nudges on
   (store/storage) is kept current from here. An engine without the route
   leaves the section out. */

import { useCallback, useEffect, useRef, useState } from "react";
import { DaemonError, type DaemonClient, type StorageCleaned, type StorageReport, type StorageWorktree } from "../../api/client";
import { plain } from "../../api/wording";
import { ago } from "../../components/ProviderConnect";
import { alert, button, card, danger, h2, mono, notice, quiet } from "../../components/ui";
import { bytes, noteReclaimable, readStarted } from "../../store/storage";

export type StorageClient = Pick<DaemonClient, "storage" | "cleanStorage">;

const message = (e: unknown) =>
  e instanceof DaemonError && e.code === "busy" ? "brindle is busy, try again in a moment." : plain(e instanceof Error ? e.message : String(e));
const ARCHIVE = "refs/brindle-archive/";

/* What is being confirmed: one worktree's path, or the removable abandoned
   ones shown when Clean all was pressed (exactly these are sent). */
type Asking = { path: string } | { all: string[] } | null;

export function Storage({ client, repo }: { client: StorageClient; repo?: string }) {
  const [report, setReport] = useState<StorageReport | null>(null);
  const [measuring, setMeasuring] = useState(true);
  const [absent, setAbsent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState<Asking>(null);
  const [busy, setBusy] = useState(false);
  const [kept, setKept] = useState<Record<string, string>>({});
  const [done, setDone] = useState<string | null>(null);
  // The newest read's stamp: an older read's answer is dropped.
  const newest = useRef(0);

  const measure = useCallback(async () => {
    const started = readStarted();
    newest.current = started;
    setMeasuring(true);
    try {
      const r = await client.storage(repo);
      noteReclaimable(r.reclaimable, started);
      if (newest.current !== started) return;
      setReport(r);
      setError(null);
    } catch (e) {
      if (newest.current !== started) return;
      if (e instanceof DaemonError && e.status === 404) setAbsent(true);
      else setError(`Couldn't measure brindle's disk use: ${message(e)}`);
    } finally {
      if (newest.current === started) setMeasuring(false);
    }
  }, [client, repo]);

  useEffect(() => {
    void measure();
  }, [measure]);

  if (absent) return null;

  const abandoned = report?.worktrees.filter((t) => t.state === "abandoned") ?? [];
  const removable = abandoned.filter((t) => t.removable);

  const clean = async (paths: string[]) => {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const out: StorageCleaned = { removed: [], kept: [] };
      for (const path of paths) {
        const got = await client.cleanStorage({ path });
        out.removed.push(...got.removed);
        out.kept.push(...got.kept);
      }
      setKept(Object.fromEntries(out.kept.map((k) => [k.path, plain(k.reason)])));
      const freed = out.removed.reduce((n, r) => n + r.size, 0);
      if (out.removed.length) setDone(`Removed ${out.removed.length} worktree${out.removed.length === 1 ? "" : "s"}, ${bytes(freed)} given back. Each branch's last commit is kept under ${ARCHIVE}.`);
      await measure();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
      setAsking(null);
    }
  };

  return (
    <section id="storage" aria-labelledby="storage-title" className="flex scroll-mt-s2 flex-col gap-s1" data-testid="storage">
      <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5">
        <h2 id="storage-title" className={h2}>
          Storage
        </h2>
        <span className="text-xs text-ink-3">
          {report ? `brindle uses ${bytes(report.categories.reduce((n, c) => n + (c.name === "build dirs" ? 0 : c.size), 0))}; ${bytes(report.reclaimable)} could be given back.` : "What brindle keeps on this computer."}
        </span>
        <span className="flex-1" />
        <button type="button" className={quiet} disabled={measuring || busy} onClick={() => void measure()}>
          {measuring ? "Measuring…" : "Measure again"}
        </button>
      </header>
      <div className={`${card} flex flex-col divide-y divide-line`}>
        {!report && measuring && <p className="px-s2 py-s1 text-xs text-ink-3">Measuring every worktree…</p>}
        {report && (
          <>
            <ul className="flex flex-col py-1" aria-label="Disk use by category" data-testid="storage-categories">
              {report.categories.map((c) => (
                <li key={c.name} className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-s3 px-s2 py-0.5">
                  <div className="min-w-0">
                    <span className="text-sm capitalize">{c.name}</span>
                    <span className="ml-s1 truncate text-xs text-ink-3">{c.detail}</span>
                  </div>
                  <span className={`${mono} text-ink-2`}>{bytes(c.size)}</span>
                </li>
              ))}
              {report.free_bytes !== null && (
                <li className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-s3 px-s2 py-0.5 text-ink-3">
                  <span className="text-sm">Free on this disk</span>
                  <span className={mono}>{bytes(report.free_bytes)}</span>
                </li>
              )}
            </ul>
            {report.low_disk && (
              <p role="alert" className={`${alert} m-s2`}>
                {plain(report.low_disk)}: new workers and check runs wait until there is room.
              </p>
            )}
            <div className="flex flex-col gap-1 px-s2 py-s1" data-testid="storage-abandoned">
              <div className="flex flex-wrap items-baseline gap-x-s2">
                <span className="text-sm font-medium">Abandoned worktrees</span>
                <span className="text-xs text-ink-3">{abandoned.length ? "Unmerged, with no activity for a while." : "None: nothing has been left behind."}</span>
                <span className="flex-1" />
                {abandoned.length > 0 && (
                  <button type="button" className={button} disabled={busy || removable.length === 0} onClick={() => setAsking({ all: removable.map((t) => t.path) })}>
                    Clean all
                  </button>
                )}
              </div>
              {asking && "all" in asking && (
                <Confirm
                  text={`Remove ${asking.all.length} abandoned worktree${asking.all.length === 1 ? "" : "s"} (${bytes(abandoned.filter((t) => asking.all.includes(t.path)).reduce((n, t) => n + t.size, 0))}) and their branches? Each branch's last commit is kept under ${ARCHIVE}. The ones marked Kept stay.`}
                  busy={busy}
                  onYes={() => void clean(asking.all)}
                  onNo={() => setAsking(null)}
                />
              )}
              {abandoned.length > 0 && (
                <ul className="flex flex-col divide-y divide-line" aria-label="Abandoned worktrees">
                  {abandoned.map((t) => (
                    <WorktreeRow
                      key={t.path}
                      t={t}
                      kept={kept[t.path]}
                      busy={busy}
                      asking={asking !== null && "path" in asking && asking.path === t.path}
                      onAsk={() => setAsking({ path: t.path })}
                      onYes={() => void clean([t.path])}
                      onNo={() => setAsking(null)}
                    />
                  ))}
                </ul>
              )}
              {Object.entries(kept)
                .filter(([path]) => !abandoned.some((t) => t.path === path))
                .map(([path, why]) => (
                  <p key={path} className="text-xs text-sap">
                    {why}
                  </p>
                ))}
            </div>
          </>
        )}
        {done && <p className={`${notice} m-s2`}>{done}</p>}
        {error && (
          <p role="alert" className={`${alert} m-s2`}>
            {error}
          </p>
        )}
      </div>
    </section>
  );
}

function WorktreeRow({ t, kept, busy, asking, onAsk, onYes, onNo }: { t: StorageWorktree; kept?: string; busy: boolean; asking: boolean; onAsk(): void; onYes(): void; onNo(): void }) {
  const why = kept ?? (t.reason ? `Kept: ${plain(t.reason)}.` : null);
  return (
    <li className="flex flex-col gap-1 py-1" data-testid="storage-worktree" data-path={t.path}>
      <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-s3">
        <div className="min-w-0">
          <div className={`${mono} truncate`}>{t.branch}</div>
          <div className="truncate text-xs text-ink-3" title={t.path}>
            {t.path} · last activity {ago(t.last_activity)}
          </div>
          {why && <div className="text-xs text-sap">{why}</div>}
        </div>
        <span className={`${mono} text-ink-2`}>{bytes(t.size)}</span>
        <button type="button" className={button} disabled={busy || !t.removable} title={t.removable ? undefined : (t.reason ?? undefined)} aria-label={`Clean ${t.branch}`} onClick={onAsk}>
          Clean
        </button>
      </div>
      {asking && <Confirm text={`Remove ${t.branch} (${bytes(t.size)}) and its branch? Its last commit is kept under ${ARCHIVE}${t.branch}.`} busy={busy} onYes={onYes} onNo={onNo} />}
    </li>
  );
}

function Confirm({ text, busy, onYes, onNo }: { text: string; busy: boolean; onYes(): void; onNo(): void }) {
  return (
    <div role="group" aria-label="Confirm clean" className={`${notice} flex flex-wrap items-center gap-s1`}>
      <span className="min-w-0 flex-1 text-sm">{text}</span>
      <button type="button" className={danger} disabled={busy} onClick={onYes}>
        {busy ? "Removing…" : "Remove"}
      </button>
      <button type="button" className={quiet} disabled={busy} onClick={onNo}>
        Cancel
      </button>
    </div>
  );
}
