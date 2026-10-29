/* "Review & open PR": one sheet over the supervisor session, per session
   (design/options-v2/V2-stepped-sheet's single calm sheet, with V1's file
   list down the left, and no worker attribution anywhere). It exists only
   once the engine says the session is ready for a PR (review.ts): nothing
   of it shows before.

   One surface, editable straight from the diff: the session branch
   against its PR base (GET /sessions/{root}/diff), the changed files
   down the left, the selected one hunk by hunk. Click a hunk's header
   (or press e) and its new side becomes a box, the file's one draft (one
   per file: its text, the blob it started from, hunk or whole file, a
   hunk's original lines); ⌘S reads the file from the session's checkout
   (GET /sessions/{root}/file, {path, content, base}), splices the hunk's
   lines in and writes the whole back with PUT /sessions/{root}/file
   {path, content, base}, always against the blob the draft started from:
   a file that moved on since is a conflict, never a write (the box keeps
   its text and says so, with Copy my edit / Discard / Reload), and the
   engine's own 409 (busy while the supervisor is mid-turn) shows inline.
   Nothing the engine sends ever replaces the text of an open box. "Full
   file" edits the whole file in a box the same way; "+ New file" writes
   one (no base); each file's ✕ deletes it (DELETE .../file {path, base}).
   Saved, the diff is read again.

   Then "Open PR": the title and body pre-filled from GET .../pr/draft,
   sent with POST .../pr, a job whose job.done result carries the PR's
   URL. brindle never opens or pushes a PR itself: it only prepares one
   (its checks, run once the session is ready) and asks in the Inbox.
   Once the PR is open the sheet says so, with the link, and stays
   editable: edits go to the same branch, which is the PR's, and wait
   there ("N new commits not on the PR") until the person's Push (POST
   .../pr/push, through the same checks). */

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { DaemonClient } from "../../api/client";
import { openExternal, type OpenUrl } from "../../api/open";
import { Sheet } from "../../components/Sheet";
import { button, field, label, main, mono, quiet } from "../../components/ui";
import { useAppState } from "../../store/useStore";
import { BUSY } from "../Branch/DiffView";
import { useJob } from "../Branch/useJob";
import type { SessionDiffState, Readiness } from "./review";
import { SessionDiffEditor } from "./SessionDiffEditor";

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export interface ReviewSheetProps {
  client: DaemonClient;
  root: string;
  readiness: Readiness;
  state: SessionDiffState;
  onClose(): void;
  openUrl?: OpenUrl;
}

/* How many times a diff refused while the engine says ready is read again. */
const RETRIES = 3;

export function ReviewSheet({ client, root, readiness, state, onClose, openUrl = openExternal }: ReviewSheetProps) {
  const { diff, files, error, notReady, reload } = state;
  const supervisor = useAppState((s) => s.workspaces.flatMap((ws) => ws.agents ?? []).find((a) => a.id === root));
  const editable = !!supervisor && !BUSY.has(supervisor.status);
  const why = supervisor ? "supervisor busy" : "supervisor not running";

  const [step, setStep] = useState<"diff" | "pr">("diff");
  const refresh = reload;
  // The engine judging the session again while the sheet is open (an edit made here, a sweep): said quietly,
  // the diff and whatever is being edited left as they are; ready again, a diff it refused is read again.
  const rechecking = readiness.state !== "ready" || (!!diff && !!notReady);
  // A refused diff is read again while the engine says ready: on the ready transition, and when the refusal
  // lands after it (the readiness read missed the brief not-ready moment, or the two answers crossed), a few
  // times with a pause between, never in a loop; a diff that loads resets the count.
  const retries = useRef(0);
  const prevState = useRef(readiness.state);
  useEffect(() => {
    if (diff && !notReady && !error) retries.current = 0;
  }, [diff, notReady, error]);
  useEffect(() => {
    const ready = readiness.state === "ready";
    // Ready again after a re-check (not the first reading at mount, whose diff read is under way already).
    const became = ready && prevState.current === "not_ready";
    prevState.current = readiness.state;
    if (!ready) return;
    if (became) {
      // The diff (and its pins) are read afresh, whatever the last read said.
      retries.current = 0;
      refresh();
      return;
    }
    if (!(notReady || error) || retries.current >= RETRIES) return;
    retries.current += 1;
    const timer = setTimeout(refresh, 400 * retries.current);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readiness.state, notReady, error]);

  const added = files.reduce((n, f) => n + f.added, 0);
  const removed = files.reduce((n, f) => n + f.removed, 0);
  const opened = readiness.pr;
  const push = useJob();

  const title = (
    <>
      <span>Review &amp; open PR</span>
      {diff && (
        <span className={`${mono} font-normal text-ink-2`}>
          {diff.branch} → {diff.base}
        </span>
      )}
    </>
  );
  const subtitle = diff ? (
    <>
      <span>
        {files.length} {files.length === 1 ? "file" : "files"} · <span className="text-pass">+{added}</span> <span className="text-sap">−{removed}</span>
      </span>
      <span>· {rechecking ? "brindle is re-checking the session" : "brindle judges it ready: every milestone passed, nothing running or waiting"}</span>
      <span
        className={`ml-auto inline-flex items-center gap-1 rounded-sm border px-2 py-px text-xs ${editable ? "border-line bg-surface-2 text-ink-2" : "border-dashed border-line-strong text-ink-3"}`}
        title={editable ? "Click a hunk, Full file, + New file or a file's ✕: every edit becomes a commit on the session branch." : `Editing is off: ${why}.`}
        data-testid="review-edit-pill"
      >
        ✎ {editable ? "fully editable · supervisor idle" : `editing off · ${why}`}
      </span>
    </>
  ) : undefined;

  return (
    <Sheet label="Review & open PR" title={title} subtitle={subtitle} onClose={onClose} wide testId="review-sheet" footer={step === "pr" ? undefined : <DiffFooter opened={opened} canOpen={!!diff && !opened} onOpen={() => setStep("pr")} openUrl={openUrl} />}>
      {opened && step === "diff" && (
        <p className="flex flex-wrap items-center gap-s1 rounded-md bg-pass-soft px-s2 py-s1 text-sm text-ink" data-testid="pr-opened">
          <span className="font-medium text-pass">✓ The PR is open.</span>
          <a className="text-pass underline" href={opened.url} onClick={(e) => (e.preventDefault(), void openUrl(opened.url))}>
            {opened.url}
          </a>
          {opened.unpushed > 0 ? (
            <>
              <span className="text-ink-2" data-testid="pr-unpushed">
                {opened.unpushed} new {opened.unpushed === 1 ? "commit" : "commits"} not on the PR
              </span>
              <button type="button" className={button} disabled={push.busy} onClick={() => void push.start("Pushing to the PR", () => client.pushSessionPr(root))}>
                {push.busy ? "Pushing…" : "Push"}
              </button>
            </>
          ) : (
            <span className="text-ink-2">Keep editing here: every edit goes onto the PR's branch, and waits for your Push.</span>
          )}
        </p>
      )}
      {push.job?.status === "failed" && (
        <p className="text-sm text-sap" role="alert">
          {push.job.error}
        </p>
      )}
      {rechecking && (
        <p className="rounded-md bg-surface-2 px-s2 py-1 text-xs text-ink-2" data-testid="review-rechecking" role="status">
          brindle is re-checking the session{notReady?.length ? `: ${notReady[0]}` : readiness.reasons[0] ? `: ${readiness.reasons[0]}` : ""}. Nothing here is lost.
        </p>
      )}
      {step === "pr" ? (
        <PrStep client={client} root={root} diff={diff} ready={readiness.state === "ready"} openUrl={openUrl} onBack={() => setStep("diff")} onClose={onClose} />
      ) : null}
      <div className={step === "pr" ? "hidden" : "flex min-h-0 flex-1 flex-col"}>
        <SessionDiffEditor client={client} root={root} state={state} />
      </div>
    </Sheet>
  );
}

function DiffFooter({ opened, canOpen, onOpen, openUrl }: { opened: { url: string } | null; canOpen: boolean; onOpen(): void; openUrl: OpenUrl }) {
  return (
    <>
      {opened ? (
        <button type="button" className={main} onClick={() => void openUrl(opened.url)}>
          Open the PR ↗
        </button>
      ) : (
        <button type="button" className={main} disabled={!canOpen} onClick={onOpen} data-testid="open-pr-step">
          Open PR…
        </button>
      )}
      <span className="flex-1" />
      <span className="text-xs text-ink-3">
        <kbd className="font-mono">e</kbd> edit hunk · <kbd className="font-mono">o</kbd> full file · <kbd className="font-mono">esc</kbd> close
      </span>
    </>
  );
}

function PrStep({ client, root, diff, ready, openUrl, onBack, onClose }: { client: DaemonClient; root: string; diff: { branch: string; base: string } | null; ready: boolean; openUrl: OpenUrl; onBack(): void; onClose(): void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [asDraft, setAsDraft] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const job = useJob();
  const [url, setUrl] = useState<string | null>(null);
  // The draft, read once; a draft the engine refused (not ready at the time) is asked for again once it is.
  useEffect(() => {
    if (loaded && !loadError) return;
    if (!ready && loaded) return;
    let live = true;
    client.prDraft(root).then(
      (d) => {
        if (!live) return;
        setTitle(d.title);
        setBody(d.body);
        setLoadError(null);
        setLoaded(true);
      },
      (e: unknown) => live && (setLoadError(message(e)), setLoaded(true)),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, root, ready]);
  const open = () => {
    if (job.busy || url) return;
    void job.start("Opening the PR", () => client.openSessionPr(root, { title, body, draft: asDraft }), (result) => {
      const got = result as { url?: string } | undefined;
      if (got?.url) setUrl(got.url);
    });
  };
  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      open();
    }
  };
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-s2" data-testid="pr-step" onKeyDown={keys}>
      <p className="text-sm text-ink-2">One pull request for the whole session{diff ? `, from ${diff.branch} onto ${diff.base}` : ""}. The title and description come from the goal and its milestones; change anything.</p>
      {loadError && <p className="text-sm text-sap">Couldn't draft it: {loadError}{ready ? "" : " (asked for again once brindle judges the session ready)"}</p>}
      <label className="flex flex-col gap-1 text-sm">
        <span className={label}>Title</span>
        <input className={field} aria-label="PR title" value={title} disabled={!loaded || !!url} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="flex min-h-0 flex-1 flex-col gap-1 text-sm">
        <span className={label}>Description</span>
        <textarea className={`${field} min-h-40 flex-1 resize-y font-mono text-[13px]`} aria-label="PR description" value={body} disabled={!loaded || !!url} onChange={(e) => setBody(e.target.value)} />
      </label>
      <label className="flex items-center gap-1 text-sm text-ink-2">
        <input type="checkbox" checked={asDraft} disabled={!!url} onChange={(e) => setAsDraft(e.target.checked)} />
        open as a draft
      </label>
      {job.job?.status === "failed" && (
        <p className="text-sm text-sap" role="alert">
          {job.job.error}
        </p>
      )}
      {url && (
        <p className="flex flex-wrap items-center gap-s1 rounded-md bg-pass-soft px-s2 py-s1 text-sm" data-testid="pr-opened">
          <span className="font-medium text-pass">✓ Opened.</span>
          <a className="text-pass underline" href={url} onClick={(e) => (e.preventDefault(), void openUrl(url))}>
            {url}
          </a>
        </p>
      )}
      <div className="mt-auto flex flex-wrap items-center gap-s1 border-t border-line pt-s2">
        {url ? (
          <>
            <button type="button" className={main} onClick={() => void openUrl(url)}>
              Open it ↗
            </button>
            <button type="button" className={button} onClick={onClose}>
              Close
            </button>
          </>
        ) : (
          <>
            <button type="button" className={main} disabled={!loaded || job.busy || !title.trim()} onClick={open} data-testid="open-pr">
              {job.busy ? "Opening…" : "Open PR"} <kbd className="ml-1 font-mono text-[10px] opacity-80">⌘↵</kbd>
            </button>
            <button type="button" className={quiet} disabled={job.busy} onClick={onBack}>
              ‹ Back
            </button>
          </>
        )}
        <span className="flex-1" />
        {diff && <span className="text-xs text-ink-3">pushes {diff.branch} and opens the PR on its host</span>}
      </div>
    </div>
  );
}
