/* The first launch's last, optional step: watch brindle work on a practice
   repo first (`brindle demo`: POST /demo makes the CLI's own small repo of
   brindle's, two failing test files and a two-milestone goal, and starts a
   session there; two workers fix them side by side and each branch is
   reviewed and merged). Start the demo opens that session; Skip goes on to
   the app as it would have. Offered once (remembered here as soon as it
   shows), never with the engine air-gapped (its agents are hosted ones), and
   started again any time from New session (WatchDemo). A start the engine's
   preflight refuses shows its items as New session's does. */

import { useEffect, useState } from "react";
import { DaemonError, type DaemonClient, type PreflightDTO } from "../../api/client";
import { plain } from "../../api/wording";
import { Preflight } from "../../components/Preflight";
import { alert, mainTone, quiet } from "../../components/ui";
import { useSessions } from "../../store/sessions";
import { Frame, Spinner } from "./Frame";
import { Starting } from "./Starting";

export const DEMO_OFFERED_KEY = "brindle.demo.offered";

export function readDemoOffered(): boolean {
  try {
    return localStorage.getItem(DEMO_OFFERED_KEY) === "1";
  } catch {
    return false;
  }
}

function markDemoOffered(): void {
  try {
    localStorage.setItem(DEMO_OFFERED_KEY, "1");
  } catch {
    // Storage refused: offered again next time, which is harmless.
  }
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const big = `inline-flex items-center gap-1 rounded-md border px-s2 py-s1 ${mainTone}`;

/* Start the demo; started, the session opens once the repo list and the snapshot have it
   (as New session's start: a root neither knows yet reads as paused). */
function useDemo(client: DaemonClient, onStarted: (root: string, repo: string) => void, refresh?: () => Promise<void>) {
  const { reload } = useSessions();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<PreflightDTO | null>(null);
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const got = await client.startDemo();
      setPreflight(null);
      await Promise.all([reload().catch(() => undefined), refresh?.().catch(() => undefined)]);
      onStarted(got.id, got.repo);
    } catch (e) {
      if (e instanceof DaemonError && e.preflight) setPreflight(e.preflight);
      else setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const problems = (
    <>
      {error && (
        <p className={alert} role="alert">
          The demo didn't start: {error}
        </p>
      )}
      {preflight && <Preflight preflight={preflight} onRefresh={setPreflight} onLaunch={() => void start()} launchLabel="Start the demo" />}
    </>
  );
  return { start, busy, problems };
}

export function DemoOffer({ client, refresh, onStarted, onSkip }: {
  client: DaemonClient;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh?: () => Promise<void>;
  onStarted(root: string, repo: string): void;
  onSkip(): void;
}) {
  const demo = useDemo(client, onStarted, refresh);
  /* Whether the engine is air-gapped, once asked: then there's nothing to offer. */
  const [airgap, setAirgap] = useState<boolean | null>(null);
  useEffect(() => {
    markDemoOffered();
    let live = true;
    client.meta().then(
      (m) => live && setAirgap(m.airgap),
      () => live && setAirgap(false),
    );
    return () => {
      live = false;
    };
  }, [client]);
  useEffect(() => {
    if (airgap) onSkip();
  }, [airgap, onSkip]);

  if (airgap !== false) return <Starting />;
  return (
    <Frame step="demo" narrow kicker="Ready" title="Watch brindle work on a practice repo first?" lede="Takes a few minutes; nothing is created in your own repos.">
      <p className="text-sm text-ink-2">
        Two agents fix a small repo's failing tests side by side, each branch is reviewed and merged, and the goal's checks turn green. It runs in a folder of brindle's own.
      </p>
      <div className="mt-s1 flex flex-wrap items-center gap-s2">
        {demo.busy ? (
          <span className="inline-flex items-center gap-s1 text-sm" role="status">
            <Spinner />
            Starting the demo…
          </span>
        ) : (
          <>
            <button type="button" className={big} onClick={() => void demo.start()}>
              Start the demo
            </button>
            <button type="button" className={quiet} onClick={onSkip}>
              Skip
            </button>
          </>
        )}
      </div>
      {demo.problems}
      <p className="text-xs text-ink-3">You can watch it any time from New session.</p>
    </Frame>
  );
}

/* New session's way back to the demo, after the first launch offered it (or never did). */
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
