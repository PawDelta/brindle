/* brindle Pro's own sign-in, as the Account screen runs it, for a "login"
   inbox item (the cloud sign-in expired, the session paused until it is
   renewed): POST /v1/account/login answers a job whose progress carries
   the authorize URL, opened once in the system browser; job.done means
   the daemon stored the tokens. One hook and one status line, shared by
   the item's review sheet and the places it is completable in the row
   (ItemActions: the Inbox page, the sidebar's session context). */

import { useEffect, useRef } from "react";
import { useDaemon } from "../../api/context";
import { openExternal } from "../../api/open";
import { useJob } from "../Branch/useJob";

export function useCloudLogin() {
  const client = useDaemon();
  const login = useJob();
  const start = () => void login.start("Browser sign-in", () => client.accountLogin(false));
  // The sign-in's URL opens once, in the system browser.
  const link = login.job?.status === "running" ? (login.job.progress.find((line) => /^https?:\/\//.test(line)) ?? null) : null;
  const opened = useRef<string | null>(null);
  useEffect(() => {
    if (!link || opened.current === link) return;
    opened.current = link;
    void openExternal(link).catch(() => undefined);
  }, [link]);
  return { job: login.job, busy: login.busy, start };
}

export function CloudLoginStatus({ job, compact = false }: { job: ReturnType<typeof useJob>["job"]; compact?: boolean }) {
  if (!job) return null;
  const link = job.progress.find((line) => /^https?:\/\//.test(line));
  return (
    <div className={`flex flex-col gap-1 rounded-md bg-canvas-2 px-s2 py-s1 ${compact ? "text-xs" : "text-sm"}`} role="status" data-testid="cloud-login">
      {job.status === "starting" && <span>Starting the sign-in…</span>}
      {job.status === "running" && link && <span>Finish signing in in your browser; this follows along.</span>}
      {job.status === "running" && !link && <span>Waiting for the daemon…</span>}
      {job.status === "done" && <span className="text-pass">Signed in again.</span>}
      {job.status === "failed" && <span className="text-sap">Sign-in failed: {job.error}</span>}
    </div>
  );
}
