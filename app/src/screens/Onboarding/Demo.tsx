/* Watch brindle work on a practice repo first (`brindle demo`: POST /demo makes
   the CLI's own small repo of brindle's, two failing test files and a
   two-milestone goal, and starts a session there; two workers fix them side by
   side and each branch is reviewed and merged). Offered from New session:
   the first run's hero with no project yet (screens/New/FirstRun), the
   PracticeRow under the composer after; never in the first launch's flow. It runs on the CLI this Mac is signed
   in to (the engine picks it). A start the engine's preflight holds shows its
   items as New session's does, against the practice repo the engine keeps for
   it; cancelling then has that repo removed. */

import { useRef, useState } from "react";
import { DaemonError, type DaemonClient, type PreflightDTO } from "../../api/client";
import { plain } from "../../api/wording";
import { PlayIcon } from "../../components/icons";
import { Preflight } from "../../components/Preflight";
import { alert, button } from "../../components/ui";
import { useSessions } from "../../store/sessions";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* Start the demo; started, the session opens once the repo list and the snapshot have it
   (as New session's start: a root neither knows yet reads as paused). */
export function useDemo(client: DaemonClient, onStarted: (root: string, repo: string) => void, refresh?: () => Promise<void>) {
  const { reload } = useSessions();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /* A start the engine's preflight held: its items, and the practice repo kept for that launch. */
  const [held, setHeld] = useState<{ items: PreflightDTO; repo?: string } | null>(null);
  /* A start under way: every launch control waits for it, even a second click before the next render. */
  const pending = useRef(false);
  const start = async () => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      const got = await client.startDemo();
      setHeld(null);
      await Promise.all([reload().catch(() => undefined), refresh?.().catch(() => undefined)]);
      onStarted(got.id, got.repo);
    } catch (e) {
      if (e instanceof DaemonError && e.preflight) setHeld({ items: e.preflight, repo: e.repo });
      else setError(message(e));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };
  /* Not going on with a held start: its practice repo goes. */
  const cancel = () => {
    if (held) void client.cancelDemo().catch(() => undefined);
    setHeld(null);
  };
  const problems = (
    <>
      {error && (
        <p className={alert} role="alert">
          The demo didn't start: {error}
        </p>
      )}
      {held && <Preflight preflight={held.items} onRefresh={(items) => setHeld({ ...held, items })} onLaunch={() => void start()} launchLabel="Start the demo" repo={held.repo} launching={busy} />}
    </>
  );
  return { start, cancel, busy, problems };
}

/* New session's way to the demo once there are projects: a row at the foot, under the composer
   (with no project yet, the first run's hero offers it instead: screens/New/FirstRun). */
export function PracticeRow({ client, refresh, onStarted }: { client: DaemonClient; refresh?: () => Promise<void>; onStarted(root: string, repo: string): void }) {
  const demo = useDemo(client, onStarted, refresh);
  return (
    <section aria-label="Practice run" className="flex flex-col gap-s1">
      <div className="flex items-center gap-3 border-t border-line px-s2 py-3.5">
        <PlayIcon className="shrink-0 text-pass" />
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium">Practice run</div>
          <div className="text-xs text-ink-3">Watch two developers fix a sample repo, a few minutes.</div>
        </div>
        <button type="button" className={button} disabled={demo.busy} onClick={() => void demo.start()}>
          {demo.busy ? "Starting…" : "Watch"}
        </button>
      </div>
      {demo.problems}
    </section>
  );
}
