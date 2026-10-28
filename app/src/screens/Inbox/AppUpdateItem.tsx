/* The app's own Inbox item, not the engine's: an update the shell installed
   by itself in the background (src-tauri/src/updates.rs) waits for a restart
   ("brindle X is installed · Restart to update · What's new") until the app
   restarts on it; then, once, "Updated to brindle X" until dismissed. The app
   never restarts by itself. Only a build that updates by itself (a release,
   not an rc or a staff build: `automatic`) has one. */

import { useEffect, useState } from "react";
import type { OpenUrl } from "../../api/open";
import { releaseNotesUrl, type AppUpdateStatus, type AppUpdates } from "../../api/tauri";
import { plain } from "../../api/wording";
import { card } from "../../components/ui";

export interface AppUpdateItem {
  kind: "pending" | "updated";
  version: string;
}

/* The item the status calls for, if any. */
export function appUpdateItem(status: AppUpdateStatus | null): AppUpdateItem | null {
  if (!status?.automatic) return null;
  if (status.pending) return { kind: "pending", version: status.pending };
  if (status.updated) return { kind: "updated", version: status.updated };
  return null;
}

/* The shell's update status, kept current by what it announces; null
   until it answers, and always without a shell (a browser tab). */
export function useAppUpdateStatus(updates?: AppUpdates): [AppUpdateStatus | null, (status: AppUpdateStatus) => void] {
  const [status, setStatus] = useState<AppUpdateStatus | null>(null);
  useEffect(() => {
    if (!updates) return;
    let live = true;
    const stop = updates.watch((s) => {
      if (live) setStatus(s);
    });
    updates.status().then(
      (s) => {
        if (live) setStatus(s);
      },
      () => {},
    );
    return () => {
      live = false;
      stop();
    };
  }, [updates]);
  return [status, setStatus];
}

const ACT = "inline-flex h-6 items-center whitespace-nowrap rounded-md border px-2 text-[11.5px] font-medium disabled:opacity-50";
const PRIMARY = "border-sap bg-sap text-on-sap hover:opacity-90";
const PLAIN = "border-line-strong bg-surface text-ink hover:bg-surface-2";
const LINK = "text-[11.5px] text-branch hover:underline";

export function AppUpdateCard({ item, updates, openUrl }: { item: AppUpdateItem; updates: AppUpdates; openUrl: OpenUrl }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (what: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await what();
    } catch (e) {
      setError(plain(e instanceof Error ? e.message : String(e)));
    } finally {
      setBusy(false);
    }
  };
  const pending = item.kind === "pending";
  return (
    <article className={`${card} relative grid max-w-[960px] gap-y-1 py-[11px] pl-[18px] pr-[14px] before:absolute before:bottom-[10px] before:left-0 before:top-[10px] before:w-[3px] before:rounded-sm before:bg-pass`} aria-label="App update" data-testid="app-update-card" data-kind={item.kind}>
      <div className="font-mono text-[10.5px] uppercase tracking-[0.06em] text-pass">update</div>
      <div className="text-[13.5px] font-semibold leading-[1.35]">{pending ? `brindle ${item.version} is installed` : `Updated to brindle ${item.version}`}</div>
      {pending && <div className="text-[12.5px] leading-[1.45] text-ink-2">It starts the next time brindle opens; restart whenever suits you.</div>}
      {error && (
        <p className="text-xs text-sap" role="alert">
          {error}
        </p>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        {pending ? (
          <button type="button" className={`${ACT} ${PRIMARY}`} disabled={busy} onClick={() => void run(updates.restart)}>
            Restart to update
          </button>
        ) : (
          <button type="button" className={`${ACT} ${PLAIN}`} disabled={busy} onClick={() => void run(updates.dismiss)}>
            Dismiss
          </button>
        )}
        <button type="button" className={LINK} onClick={() => void openUrl(releaseNotesUrl(item.version))}>
          What's new
        </button>
      </div>
    </article>
  );
}
