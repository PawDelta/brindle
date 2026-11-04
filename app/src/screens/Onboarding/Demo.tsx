/* Watch brindle work on a practice repo first (`brindle demo`: POST /demo makes
   the CLI's own small repo of brindle's, two failing test files and a
   two-milestone goal, and starts a session there; two workers fix them side by
   side and each branch is reviewed and merged). Offered from New session
   (WatchDemo), never on the first launch. It runs on the CLI this Mac is signed
   in to (the engine picks it). A start the engine's preflight holds shows its
   items as New session's does, against the practice repo the engine keeps for
   it; cancelling then has that repo removed. */

import { useRef, useState } from "react";
import { DaemonError, type DaemonClient, type PreflightDTO } from "../../api/client";
import { plain } from "../../api/wording";
import { Preflight } from "../../components/Preflight";
import { alert, quiet } from "../../components/ui";
import { useSessions } from "../../store/sessions";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* Start the demo; started, the session opens once the repo list and the snapshot have it
   (as New session's start: a root neither knows yet reads as paused). */
function useDemo(client: DaemonClient, onStarted: (root: string, repo: string) => void, refresh?: () => Promise<void>) {
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

/* New session's way to the demo. */
export function WatchDemo({ client, refresh, onStarted }: { client: DaemonClient; refresh?: () => Promise<void>; onStarted(root: string, repo: string): void }) {
  const demo = useDemo(client, onStarted, refresh);
  return (
    <div className="flex flex-col items-start gap-s1">
      <button type="button" className={quiet} disabled={demo.busy} onClick={() => void demo.start()}>
        {demo.busy ? "Starting the demo…" : "Watch brindle work on a practice repo"}
      </button>
      {demo.problems}
    </div>
  );
}
