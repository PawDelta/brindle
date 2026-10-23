/* The one recovery handler behind every Continue / Restart of a session: the
   state card's button and the bar's go through it alike (POST
   /v1/sessions/{root}/continue). It keeps what the attempt said (a note, or
   its failure), the engine's 409 {preflight} items when the provider needs
   setting up first (shown as native setup UI by ProviderSurface, which
   calls `resume` again once they are settled), and `attempt`, bumped on
   every success so the surface opens a fresh socket to the pane: whatever
   the last one said (no pane, closed) is forgotten, and the chat that came
   back streams. */

import { useCallback, useState } from "react";
import { DaemonError, type DaemonClient, type PreflightDTO } from "../../api/client";
import { failure, type Notice } from "./notice";

export interface Recovery {
  /* Continue (or restart) the session's chat. */
  resume(): Promise<void>;
  busy: boolean;
  notice: Notice;
  /* The provider's open preflight items, when the engine refused the launch over them. */
  preflight: PreflightDTO | null;
  setPreflight(next: PreflightDTO | null): void;
  /* Counts the successful continues: the surface remounts its socket on each. */
  attempt: number;
}

/* `onResumed`: called after each successful continue (the sidebar's Resume opens the session with it). */
export function useContinue(client: DaemonClient, root: string, reload: () => Promise<void>, onResumed?: () => void): Recovery {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [preflight, setPreflight] = useState<PreflightDTO | null>(null);
  const [attempt, setAttempt] = useState(0);

  const resume = useCallback(async () => {
    setBusy(true);
    setNotice(null);
    try {
      const got = await client.continueSession(root);
      setPreflight(null);
      setNotice({ text: got.notes.map((n) => n.text).join(" ") || `Session ${got.state}.`, failed: false });
      setAttempt((n) => n + 1);
      onResumed?.();
    } catch (e) {
      if (e instanceof DaemonError && e.preflight) setPreflight(e.preflight);
      else setNotice(failure(e));
    } finally {
      setBusy(false);
      await reload().catch(() => undefined);
    }
  }, [client, root, reload, onResumed]);

  return { resume, busy, notice, preflight, setPreflight, attempt };
}
