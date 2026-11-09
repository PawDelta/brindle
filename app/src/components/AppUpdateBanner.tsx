/* The app's own update, offered (src-tauri/src/updates.rs: the shell looks at
   its feed as it launches and every 6 h while it is open; nothing installs
   unasked). Updates are offered in the Inbox and Settings › About. Only a build that updates by itself
   (`automatic`: not an rc or a staff build) offers one. */

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import type { OpenUrl } from "../api/open";
import { releaseNotesUrl, type AppUpdateStatus, type AppUpdates, type ReleaseProgress } from "../api/tauri";
import { plain } from "../api/wording";
import { useAppState } from "../store/useStore";

/* Where Update now is: not started, downloading (the share done, when the
   feed says how big), installing, restarting into it, or failed. */
export type UpdatePhase =
  | { kind: "idle" }
  | { kind: "downloading"; pct: number | null }
  | { kind: "installing" }
  | { kind: "restarting" }
  | { kind: "failed"; message: string };

interface Install {
  updates: AppUpdates;
  phase: UpdatePhase;
  start: () => void;
  later: () => void;
}

const InstallContext = createContext<Install | null>(null);

/* Update now's state, shared by update surfaces. */
export function AppUpdateProvider({ updates, children }: { updates?: AppUpdates; children: ReactNode }) {
  const [phase, setPhase] = useState<UpdatePhase>({ kind: "idle" });
  // One Update now at a time, whichever button started it (the shell refuses a second install too).
  const running = useRef(false);
  const start = useCallback(() => {
    if (!updates || running.current) return;
    running.current = true;
    setPhase({ kind: "downloading", pct: null });
    const progress = (p: ReleaseProgress) => {
      if (p.kind === "downloading") setPhase({ kind: "downloading", pct: p.total ? Math.min(100, Math.round((100 * p.downloaded) / p.total)) : null });
      else if (p.kind === "installing") setPhase({ kind: "installing" });
    };
    updates
      .updateNow(progress)
      .then(
        // Installed: the shell restarts the app into it. Nothing newer by now: nothing to do.
        (version) => setPhase(version ? { kind: "restarting" } : { kind: "idle" }),
        (e: unknown) => setPhase({ kind: "failed", message: plain(e instanceof Error ? e.message : String(e)) }),
      )
      .finally(() => {
        running.current = false;
      });
  }, [updates]);
  // Later: the offer set aside, and a failed Update now with it, when no update is under way.
  const later = useCallback(() => {
    if (!updates || running.current) return;
    void updates.later().then(() => {
      if (!running.current) setPhase({ kind: "idle" });
    });
  }, [updates]);
  const install = updates ? { updates, phase, start, later } : null;
  return <InstallContext.Provider value={install}>{children}</InstallContext.Provider>;
}

/* The update on offer, with Update now, when there is one (and a shell to ask). */
function useOffer(): { status: AppUpdateStatus; version: string; install: Install } | null {
  const status = useAppState((s) => s.appUpdate);
  const install = useContext(InstallContext);
  if (!install || !status?.automatic || !status.available) return null;
  return { status, version: status.available.version, install };
}

/* The notes' first line of prose: headings passed over, a bullet's mark dropped. */
function firstLine(notes: string | null): string | null {
  for (const line of (notes ?? "").split("\n")) {
    if (line.trimStart().startsWith("#")) continue;
    const text = line.replace(/^[\s>*\-•]+/, "").trim();
    if (text) return text;
  }
  return null;
}

const KEEPS = "Your sessions and agents keep running while brindle restarts.";

/* `rollback`: the feed was pointed back at this older release, which the headline says. */
function copy(phase: UpdatePhase, version: string, notes: string | null, rollback: boolean): [string, string] {
  const available = `brindle ${version} is available${rollback ? " (rollback)" : ""}`;
  switch (phase.kind) {
    case "downloading":
      return [`Downloading brindle ${version}…${phase.pct === null ? "" : ` ${phase.pct}%`}`, "You can keep working. brindle restarts when the update is installed."];
    case "installing":
      return [`Installing brindle ${version}…`, "Almost done. Your sessions keep running."];
    case "restarting":
      return [`Restarting into brindle ${version}…`, "The app reopens in a moment, right where you left it."];
    case "failed":
      return [available, `The update didn't install: ${phase.message}`];
    case "idle":
      return [available, [firstLine(notes), KEEPS].filter(Boolean).join(" ")];
  }
}

const BUTTON = "inline-flex min-h-8 shrink-0 items-center whitespace-nowrap rounded-md px-s2 text-[13px] disabled:opacity-60";

/* The strip: the offer, or Update now under way (a thin bar as it downloads).
   Set aside ("Later"), it shows only while an Update now runs or failed. */
export function AppUpdateBanner({ openUrl }: { openUrl: OpenUrl }) {
  const offer = useOffer();
  if (!offer) return null;
  const { status, version, install } = offer;
  const { phase } = install;
  if (status.later && phase.kind === "idle") return null;
  const [headline, detail] = copy(phase, version, status.available?.notes ?? null, status.available?.rollback ?? false);
  const busy = phase.kind === "downloading" || phase.kind === "installing" || phase.kind === "restarting";
  const bar = phase.kind === "downloading" && phase.pct !== null ? phase.pct : 100;
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-s2 border-b border-pass/30 bg-pass-soft px-s3 py-s1 text-sm" data-testid="app-update-banner" data-phase={phase.kind}>
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden className="shrink-0 text-pass">
        <circle cx="9" cy="9" r="7.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M9 5v6M6.5 8.5L9 11l2.5-2.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <strong className="font-semibold text-ink">{headline}</strong>
        <span className="truncate text-[13px] text-ink-2" title={detail}>{detail}</span>
        {busy && (
          <div className="mt-1 h-1 w-80 max-w-full overflow-hidden rounded-sm bg-pass/20" aria-hidden>
            <div className="h-1 rounded-sm bg-pass" style={{ width: `${bar}%` }} data-testid="app-update-bar" />
          </div>
        )}
      </div>
      {!busy && (
        <div className="flex shrink-0 flex-wrap items-center gap-s1">
          <button type="button" className="px-1 text-[13px] text-pass hover:underline" onClick={() => void openUrl(releaseNotesUrl(version))}>
            What's new
          </button>
          <button type="button" className={`${BUTTON} border border-line-strong text-ink-2 hover:bg-surface-2`} onClick={install.later} data-testid="app-update-later">
            Later
          </button>
          <button type="button" className={`${BUTTON} bg-pass font-semibold text-canvas hover:opacity-90`} onClick={install.start} data-testid="app-update-now">
            {phase.kind === "failed" ? "Update failed · Try again" : "Update now"}
          </button>
        </div>
      )}
    </div>
  );
}
