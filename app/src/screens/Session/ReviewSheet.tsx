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
   URL. When the person's settings let brindle open PRs on its own (the
   engine's auto-PR setting, stubbed until it lands: review.ts autoPrOn)
   and the engine says it did, the sheet says so instead, with the link,
   and stays editable: edits go to the same branch, which is the PR's. */

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { DaemonError, type DaemonClient, type FileContent } from "../../api/client";
import { openExternal, type OpenUrl } from "../../api/open";
import { Sheet } from "../../components/Sheet";
import { button, field, label, mono, primary, quiet } from "../../components/ui";
import { useAppState } from "../../store/useStore";
import { BUSY } from "../Branch/DiffView";
import { hunksOf, newSide, splice, type FileDiff, type Hunk } from "../Branch/parseDiff";
import { useJob } from "../Branch/useJob";
import { hunkMatches, useAutoPr, useSessionDiff, type Readiness } from "./review";

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
const refusal = (e: unknown) => (e instanceof DaemonError && (e.code === "stale" || e.code === "busy") ? `${e.code === "stale" ? "Stale" : "Busy"}: ${e.message}` : message(e));

export interface ReviewSheetProps {
  client: DaemonClient;
  root: string;
  repo: string;
  readiness: Readiness;
  onClose(): void;
  openUrl?: OpenUrl;
}

const STATUS_MARK: Record<FileDiff["status"], string> = { added: "A", deleted: "D", renamed: "R", modified: "M" };

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "bg-branch-soft text-branch";
  if (line.startsWith("+")) return "bg-pass-soft text-pass";
  if (line.startsWith("-")) return "bg-sap-soft text-sap";
  return "text-ink-2";
}

/* What the sheet knows of a file in the checkout: its content and the blob
   an edit is pinned to, read when the file was opened here. */
type Pinned = FileContent | null;

/* An unsaved edit, one per file, owned by the sheet: what is typed, the
   blob the person started editing from (every save is sent against that
   base, so the engine's 409 catches any change meanwhile), whether it is a
   hunk's new side or the whole file, and for a hunk its header, place and
   original lines (the snapshot shown should the hunk move or vanish).
   Nothing the engine sends ever replaces the text of an open draft: a
   file that changed since says so in one line, with Copy my edit,
   Discard and Reload. A draft outlives its file's place in the diff: a
   file that dropped out (reverted, deleted) stays listed for its draft,
   marked so, until the draft is saved or discarded. The one new file
   being written is a draft too (mode "new", under NEW_KEY, its path in
   `header`), kept while another file or the PR step is looked at. */
interface Draft {
  mode: "hunk" | "full" | "new";
  text: string;
  base: string;
  /* The hunk's header; for a new file, its path as typed. */
  header: string;
  newStart: number;
  newCount: number;
  snapshot: string[];
  /* Set when a save was refused for the file having changed. */
  conflict: boolean;
  /* A new file's write under way: the path and text it sent. The box is
     locked meanwhile, and the draft clears only if it still is what was
     sent, so a write that outlives the box never takes newer work. */
  saving?: { path: string; text: string };
  /* What refused a new file's write, kept with the draft. */
  refused?: string;
}

interface Drafts {
  get(path: string): Draft | undefined;
  start(path: string, draft: Draft): void;
  update(path: string, patch: Partial<Draft>): void;
  /* Drops the draft; given `onlyIf`, only one it holds true for. */
  clear(path: string, onlyIf?: (draft: Draft) => boolean): void;
}

export const CONFLICT = "Changed since you started editing";
export const GONE = "not in the diff anymore";

/* The new-file draft's key in the draft map: no path in a diff starts with a slash. */
const NEW_KEY = "/new";
const EMPTY_NEW: Draft = { mode: "new", text: "", base: "", header: "", newStart: 1, newCount: 0, snapshot: [], conflict: false };

/* A file the diff no longer lists, kept in the file list for its draft. */
const orphan = (path: string): FileDiff => ({ path, oldPath: null, status: "modified", added: 0, removed: 0, binary: false, lines: [], blob: null });

/* How many times a diff refused while the engine says ready is read again. */
const RETRIES = 3;

export function ReviewSheet({ client, root, repo, readiness, onClose, openUrl = openExternal }: ReviewSheetProps) {
  const state = useSessionDiff(client, root);
  const { diff, files, error, notReady, reload } = state;
  const autoPr = useAutoPr(client, repo);
  const supervisor = useAppState((s) => s.workspaces.flatMap((ws) => ws.agents ?? []).find((a) => a.id === root));
  const editable = !!supervisor && !BUSY.has(supervisor.status);
  const why = supervisor ? "supervisor busy" : "supervisor not running";

  // What is being typed in a box, kept here, apart from the pins and the diff: a reload (an edit saved, the
  // engine re-checking the session, a file read again) leaves every unsaved edit as it was; a box clears its
  // draft only when its edit is saved or discarded. The new file being written is the draft under NEW_KEY.
  const [selected, setSelected] = useState<string | null>(null);
  const [draftMap, setDraftMap] = useState<Record<string, Draft>>({});
  const drafts: Drafts = useMemo(
    () => ({
      get: (path) => draftMap[path],
      // Starting a draft on a file pins the selection to it: the file stays the one looked at through every
      // fresh diff, however its place in the list changes.
      start: (path, draft) => (setDraftMap((d) => ({ ...d, [path]: draft })), path !== NEW_KEY && setSelected(path)),
      update: (path, patch) => setDraftMap((d) => (d[path] ? { ...d, [path]: { ...d[path], ...patch } } : d)),
      clear: (path, onlyIf) =>
        setDraftMap((d) => {
          if (!d[path] || (onlyIf && !onlyIf(d[path]))) return d;
          const { [path]: _, ...rest } = d;
          return rest;
        }),
    }),
    [draftMap],
  );
  const newDraft = draftMap[NEW_KEY];

  // The files down the left: the diff's, then any the diff no longer lists but a draft is held for (the file
  // reverted, or was deleted, while the draft was open): those stay, marked, with their draft, until it goes.
  const gone = useMemo(() => Object.keys(draftMap).filter((p) => p !== NEW_KEY && !files.some((f) => f.path === p)), [draftMap, files]);
  const entries = useMemo(() => [...files, ...gone.map(orphan)], [files, gone]);
  const file = entries.find((f) => f.path === selected) ?? entries[0] ?? null;
  const [pins, setPins] = useState<Record<string, Pinned>>({});
  const [creating, setCreating] = useState(false);
  const [step, setStep] = useState<"diff" | "pr">("diff");
  const [note, setNote] = useState<string | null>(null);

  // The selected file as the checkout has it, read once it is picked: the blob every edit of it is pinned to.
  useEffect(() => {
    if (!file || file.status === "deleted" || file.binary || file.path in pins) return;
    let live = true;
    client.sessionFile(root, file.path).then(
      (got) => live && setPins((p) => ({ ...p, [file.path]: got })),
      () => live && setPins((p) => ({ ...p, [file.path]: null })),
    );
    return () => {
      live = false;
    };
  }, [client, root, file, pins]);

  // "+ New file…": the one new-file draft, started empty the first time, otherwise opened as it was left.
  const createFile = () => {
    if (!newDraft) drafts.start(NEW_KEY, EMPTY_NEW);
    setCreating(true);
  };

  // A fresh diff: every pin is read again (an edit moved the files on).
  const refresh = () => {
    setPins({});
    reload();
  };
  // A file whose hunks no longer describe it (it moved on between the diff and the file being read) has the
  // diff read again, once per state of the file; should it still not match, the file says so and offers it.
  const autoReloaded = useRef(new Set<string>());
  const onStale = (base: string) => {
    if (autoReloaded.current.has(base)) return;
    autoReloaded.current.add(base);
    refresh();
  };
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
  const opened = readiness.pr && autoPr ? readiness.pr : null;

  const remove = async (f: FileDiff) => {
    const pin = pins[f.path];
    if (!pin) return;
    setNote(null);
    try {
      await client.deleteSessionFile(root, { path: f.path, base: pin.base });
      refresh();
    } catch (e) {
      setNote(refusal(e));
    }
  };

  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    if (step === "pr") return;
    void e;
  };

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
        className={`ml-auto inline-flex items-center gap-1 rounded-full border px-2 py-px text-xs ${editable ? "border-line bg-surface-2 text-ink-2" : "border-dashed border-line-strong text-ink-3"}`}
        title={editable ? "Click a hunk, Full file, + New file or a file's ✕: every edit becomes a commit on the session branch." : `Editing is off: ${why}.`}
        data-testid="review-edit-pill"
      >
        ✎ {editable ? "fully editable · supervisor idle" : `editing off · ${why}`}
      </span>
    </>
  ) : undefined;

  return (
    <Sheet label="Review & open PR" title={title} subtitle={subtitle} onClose={onClose} onKey={keys} wide testId="review-sheet" footer={step === "pr" ? undefined : <DiffFooter opened={opened} canOpen={!!diff && !opened} onOpen={() => setStep("pr")} openUrl={openUrl} />}>
      {opened && (
        <p className="flex flex-wrap items-center gap-s1 rounded-md bg-pass-soft px-s2 py-s1 text-sm text-ink" data-testid="pr-opened">
          <span className="font-medium text-pass">✓ brindle opened the PR.</span>
          <a className="text-branch underline" href={opened.url} onClick={(e) => (e.preventDefault(), void openUrl(opened.url))}>
            {opened.url}
          </a>
          <span className="text-ink-2">Keep editing here: every edit goes onto the PR's branch.</span>
        </p>
      )}
      {rechecking && (
        <p className="rounded-md bg-surface-2 px-s2 py-1 text-xs text-ink-2" data-testid="review-rechecking" role="status">
          brindle is re-checking the session{notReady?.length ? `: ${notReady[0]}` : readiness.reasons[0] ? `: ${readiness.reasons[0]}` : ""}. Nothing here is lost.
        </p>
      )}
      {step === "pr" ? (
        <PrStep client={client} root={root} diff={diff} ready={readiness.state === "ready"} openUrl={openUrl} onBack={() => setStep("diff")} onClose={onClose} />
      ) : error ? (
        <div className="flex items-center gap-s2">
          <p className="text-sap">{error}</p>
          <button type="button" className={button} onClick={reload}>
            Retry
          </button>
        </div>
      ) : notReady && !diff ? (
        <div className="flex flex-col gap-s1" data-testid="review-not-ready">
          <p className="text-sm text-ink-2">The engine doesn't judge this session ready for a PR after all:</p>
          <ul className="list-disc pl-s3 text-sm text-ink">
            {notReady.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      ) : !diff ? (
        <p className="text-ink-3">Loading the session's changes…</p>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-s1">
          <div className={label}>
            {files.length} {files.length === 1 ? "file" : "files"} changed against {diff.base}
          </div>
          {note && (
            <p className="text-sm text-sap" role="alert">
              {note}
            </p>
          )}
          <div className="flex min-h-0 flex-1 overflow-hidden rounded-lg border border-line bg-surface">
            <nav className="flex w-64 shrink-0 flex-col overflow-auto border-r border-line py-1" aria-label="Changed files">
              <button type="button" className="mx-1 mb-1 flex items-center gap-1 rounded-md border-b border-line px-s1 py-1 text-left text-xs text-branch hover:bg-surface-2 disabled:opacity-50" disabled={!editable && !newDraft} onClick={createFile} data-testid="new-file">
                + New file…
              </button>
              <ul>
                {newDraft && (
                  <li className="flex items-center">
                    <button
                      type="button"
                      onClick={() => setCreating(true)}
                      aria-current={creating ? "true" : undefined}
                      className={`flex min-w-0 flex-1 items-baseline gap-s1 px-s2 py-1 text-left ${mono} ${creating ? "bg-branch-soft text-ink" : "text-ink-2 hover:bg-surface-2"}`}
                      data-testid="new-file-draft"
                    >
                      <span className="w-3 text-ink-3">A</span>
                      <span className="min-w-0 flex-1 truncate" title={newDraft.header || "new file"}>
                        {newDraft.header || <span className="text-ink-3">new file</span>}
                      </span>
                      <span className="font-sans text-xs text-ink-3">unsaved</span>
                    </button>
                  </li>
                )}
                {entries.map((f) => (
                  <li key={f.path} className="group flex items-center">
                    <button
                      type="button"
                      onClick={() => (setSelected(f.path), setCreating(false))}
                      aria-current={f.path === file?.path && !creating ? "true" : undefined}
                      className={`flex min-w-0 flex-1 items-baseline gap-s1 px-s2 py-1 text-left ${mono} ${f.path === file?.path && !creating ? "bg-branch-soft text-ink" : "text-ink-2 hover:bg-surface-2"}`}
                      data-testid={gone.includes(f.path) ? "draft-orphan" : undefined}
                    >
                      <span className="w-3 text-ink-3">{gone.includes(f.path) ? "·" : STATUS_MARK[f.status]}</span>
                      <span className="flex min-w-0 flex-1" title={f.path}>
                        {f.path.includes("/") && <span className="min-w-0 truncate text-ink-3">{f.path.slice(0, f.path.lastIndexOf("/") + 1)}</span>}
                        <span className="shrink-0">{f.path.slice(f.path.lastIndexOf("/") + 1)}</span>
                      </span>
                      {gone.includes(f.path) ? (
                        <span className="shrink-0 font-sans text-xs text-ink-3">{GONE}</span>
                      ) : (
                        <>
                          <span className="text-pass">+{f.added}</span>
                          <span className="text-sap">-{f.removed}</span>
                        </>
                      )}
                    </button>
                    {f.status !== "deleted" && !gone.includes(f.path) && (
                      <button type="button" className="mr-1 shrink-0 rounded-sm px-1 text-xs text-ink-3 opacity-0 hover:text-fail focus:opacity-100 group-hover:opacity-100 disabled:opacity-0" title={`Delete ${f.path} from the branch`} aria-label={`Delete ${f.path}`} disabled={!editable || !pins[f.path]} onClick={() => void remove(f)}>
                        ✕
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </nav>
            {creating && newDraft ? (
              <NewFile client={client} root={root} draft={newDraft} drafts={drafts} onSaved={(path) => (setCreating(false), setSelected(path), refresh())} onDone={() => setCreating(false)} />
            ) : file ? (
              <FileView key={file.path} file={file} gone={gone.includes(file.path)} pin={pins[file.path]} editable={editable} why={why} client={client} root={root} drafts={drafts} onSaved={refresh} onStale={onStale} />
            ) : (
              <p className="p-s2 text-ink-3">No changes against {diff.base}.</p>
            )}
          </div>
        </div>
      )}
    </Sheet>
  );
}

function DiffFooter({ opened, canOpen, onOpen, openUrl }: { opened: { url: string } | null; canOpen: boolean; onOpen(): void; openUrl: OpenUrl }) {
  return (
    <>
      {opened ? (
        <button type="button" className={primary} onClick={() => void openUrl(opened.url)}>
          Open the PR ↗
        </button>
      ) : (
        <button type="button" className={primary} disabled={!canOpen} onClick={onOpen} data-testid="open-pr-step">
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

/* The selected file: its hunks, one opening for editing on a click of its
   header or `e` (j / k move), or, with Full file, the whole file in a box.
   The file's one draft (if any) is the box: at its hunk while the hunk is
   where it was, else above the hunks with its original lines, so a hunk
   that moved, merged or vanished never loses what was typed. */
function FileView({ file, gone, pin, editable, why, client, root, drafts, onSaved, onStale }: {
  file: FileDiff;
  /* The diff no longer lists the file; it is here for its draft alone. */
  gone: boolean;
  pin: Pinned | undefined;
  editable: boolean;
  why: string;
  client: DaemonClient;
  root: string;
  drafts: Drafts;
  onSaved(): void;
  /* The file as read no longer matches the hunks shown: read the diff again. */
  onStale(base: string): void;
}) {
  const hunks = useMemo(() => hunksOf(file), [file]);
  const [cursor, setCursor] = useState(0);
  useEffect(() => setCursor(0), [file.path]);
  const draft = drafts.get(file.path);
  // Every hunk shown must still describe the file as the checkout has it; else no new edit, and the diff is read again.
  const mismatched = useMemo(() => (pin && !file.binary && file.status !== "deleted" ? hunks.some((h) => !hunkMatches(pin.content, file, h)) : false), [pin, file, hunks]);
  useEffect(() => {
    if (mismatched && pin) onStale(pin.base);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mismatched, pin?.base]);
  const canStart = editable && !file.binary && file.status !== "deleted" && !!pin && !mismatched && !draft;
  const startHunk = (hunk: Hunk) => {
    if (!pin) return;
    const snapshot = newSide(file, hunk);
    drafts.start(file.path, { mode: "hunk", text: snapshot.join("\n"), base: pin.base, header: hunk.header, newStart: hunk.newStart, newCount: hunk.newCount, snapshot, conflict: false });
  };
  const startFull = () => {
    if (!pin) return;
    drafts.start(file.path, { mode: "full", text: pin.content, base: pin.base, header: "", newStart: 1, newCount: 0, snapshot: [], conflict: false });
  };
  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget || hunks.length === 0) return;
    if (e.key === "j") setCursor((c) => Math.min(c + 1, hunks.length - 1));
    else if (e.key === "k") setCursor((c) => Math.max(c - 1, 0));
    else if (e.key === "e" && canStart) startHunk(hunks[cursor]);
    else if (e.key === "o" && canStart) startFull();
    else return;
    e.preventDefault();
  };
  // The draft's hunk, where the diff still has it (same header, same place); else the draft shows above.
  const at = draft?.mode === "hunk" ? hunks.findIndex((h) => h.header === draft.header) : -1;
  const editor = draft ? <DraftEditor client={client} root={root} path={file.path} draft={draft} pin={pin} drafts={drafts} onSaved={onSaved} /> : null;
  const off = `Editing is off: ${file.status === "deleted" ? "the file is deleted" : file.binary ? "a binary file" : mismatched ? "the file changed since the diff was read" : draft ? "finish or discard the edit under way first" : why}`;
  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-auto outline-none" data-testid="file-diff" tabIndex={0} onKeyDown={keys}>
      <div className={`sticky top-0 z-10 flex items-center gap-s1 border-b border-line bg-canvas-2 px-s2 py-1 ${mono}`}>
        <span className="min-w-0 flex-1 truncate">{file.oldPath ? `${file.oldPath} → ${file.path}` : file.path}</span>
        {gone && (
          <span className="shrink-0 font-sans text-xs text-ink-3" data-testid="file-gone">
            {GONE} · your edit is kept until you save or discard it
          </span>
        )}
        {hunks.length > 0 && draft?.mode !== "full" && (
          <span className="shrink-0 font-sans text-xs text-ink-3">
            {hunks.length} {hunks.length === 1 ? "hunk" : "hunks"}
          </span>
        )}
        <button
          type="button"
          role="switch"
          aria-checked={draft?.mode === "full"}
          aria-label="Full file"
          className={`shrink-0 rounded-md border px-s1 py-px font-sans text-xs ${draft?.mode === "full" ? "border-branch bg-branch-soft text-branch" : "border-line text-ink-2 hover:bg-surface-2"} disabled:opacity-50`}
          disabled={!canStart && draft?.mode !== "full"}
          title={canStart ? "Edit the whole file in a box" : draft?.mode === "full" ? "The whole file is being edited: save or discard it" : off}
          onClick={() => canStart && startFull()}
        >
          Full file
        </button>
      </div>
      {mismatched && !draft && (
        <div className="flex flex-wrap items-center gap-s1 border-b border-line bg-sap-soft px-s2 py-1 font-sans text-xs text-sap" role="alert" data-testid="file-stale">
          <span>{file.path} changed since the diff was read; the hunks shown no longer describe it, so nothing can be edited from them.</span>
          <button type="button" className={`${button} px-s1 py-0.5 text-xs`} onClick={onSaved}>
            Reload the diff
          </button>
        </div>
      )}
      {draft?.mode === "full" ? (
        editor
      ) : (
        <>
          {draft?.mode === "hunk" && at < 0 && (
            <section className="border-b border-line" data-testid="draft-moved" aria-label={`Your edit of ${draft.header}`}>
              <div className={`${mono} flex items-center gap-s1 bg-branch px-s2 leading-5 text-on-branch`}>
                <span className="min-w-0 flex-1 truncate">{draft.header}</span>
                <span className="shrink-0 font-sans text-xs opacity-80">your edit · the hunk has moved or is gone; these were its lines</span>
              </div>
              <pre className={`${mono} max-h-40 overflow-auto bg-canvas-2 leading-5 text-ink-3`} data-testid="draft-snapshot">
                {draft.snapshot.map((line, i) => (
                  <div key={i} className="px-s2 whitespace-pre">
                    {line || " "}
                  </div>
                ))}
              </pre>
              {editor}
            </section>
          )}
          {file.binary ? (
            <p className="p-s2 text-ink-3">Binary file.</p>
          ) : hunks.length === 0 ? (
            <pre className={`${mono} leading-5`}>
              {file.lines.map((line, i) => (
                <div key={i} className={`px-s2 whitespace-pre ${lineClass(line)}`}>
                  {line || " "}
                </div>
              ))}
            </pre>
          ) : (
            hunks.map((hunk, h) => (
              <section key={hunk.start} className={h === cursor ? "ring-1 ring-inset ring-branch/40" : ""} data-testid="hunk" data-editing={at === h} aria-label={hunk.header}>
                <button
                  type="button"
                  className={`${mono} flex w-full items-center gap-s1 px-s2 text-left leading-5 ${at === h ? "bg-branch text-on-branch" : "bg-branch-soft text-branch"} ${canStart ? "cursor-pointer" : "cursor-default"}`}
                  disabled={!canStart && at !== h}
                  title={canStart ? "Edit this hunk's new side" : at === h ? "Being edited: ⌘S saves, Discard drops it" : off}
                  onClick={() => {
                    setCursor(h);
                    if (canStart) startHunk(hunk);
                  }}
                >
                  <span className="min-w-0 flex-1 truncate">{hunk.header}</span>
                  {(canStart || at === h) && <span className="shrink-0 font-sans text-xs opacity-70">{at === h ? "editing the new side · ⌘S save · esc discard" : "click or e to edit"}</span>}
                </button>
                {at === h ? (
                  editor
                ) : (
                  <pre className={`${mono} leading-5`}>
                    {file.lines.slice(hunk.start + 1, hunk.end).map((line, i) => (
                      <div key={i} className={`px-s2 whitespace-pre ${lineClass(line)}`}>
                        {line || " "}
                      </div>
                    ))}
                  </pre>
                )}
              </section>
            ))
          )}
        </>
      )}
    </div>
  );
}

/* A box and its save bar: ⌘S saves, Escape discards; the engine's refusal shows in the bar. */
function EditBox({ ariaLabel, text, setText, busy, saveable = true, error, conflict = null, onSave, onDone, onCopy, onReload, what, testId = "hunk-editor" }: {
  ariaLabel: string;
  text: string;
  setText(v: string): void;
  busy: boolean;
  /* False while a save can't go: the text stays, Save waits. */
  saveable?: boolean;
  error: string | null;
  /* The file changed since the edit started: said in one line, with Copy my edit / Discard / Reload. */
  conflict?: string | null;
  onSave(): void;
  onDone(): void;
  onCopy?(): void;
  onReload?(): void;
  what: string;
  testId?: string;
}) {
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    box.current?.focus();
  }, []);
  const keys = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      e.stopPropagation();
      onSave();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onDone();
    }
  };
  return (
    <div className="flex min-h-0 flex-col" data-testid={testId}>
      <textarea ref={box} className={`${mono} w-full resize-y border-l-2 border-branch bg-canvas px-s2 py-1 leading-5 text-ink outline-none`} aria-label={ariaLabel} rows={Math.min(40, Math.max(3, text.split("\n").length))} spellCheck={false} value={text} disabled={busy} onChange={(e) => setText(e.target.value)} onKeyDown={keys} />
      <div className="flex flex-wrap items-center gap-s1 border-y border-line bg-surface-2 px-s2 py-1 font-sans text-xs text-ink-2" data-testid="save-bar">
        {conflict ? (
          <span className="font-medium text-sap" role="alert" data-testid="draft-conflict">
            {conflict}
          </span>
        ) : (
          <span>{what}</span>
        )}
        {error && (
          <span className="text-sap" role="alert">
            {error}
          </span>
        )}
        <span className="flex-1" />
        {conflict ? (
          <>
            <button type="button" className={`${button} px-s1 py-0.5 text-xs`} onClick={onCopy}>
              Copy my edit
            </button>
            <button type="button" className={`${quiet} text-xs`} onClick={onDone}>
              Discard
            </button>
            <button type="button" className={`${quiet} text-xs`} onClick={onReload}>
              Reload
            </button>
          </>
        ) : (
          <>
            <button type="button" className={`${primary} px-s1 py-0.5 text-xs`} disabled={busy || !saveable} onClick={onSave}>
              Save <kbd className="ml-1 font-mono text-[10px] opacity-80">⌘S</kbd>
            </button>
            <button type="button" className={`${quiet} text-xs`} disabled={busy} onClick={onDone}>
              Discard <kbd className="ml-1 font-mono text-[10px] opacity-80">esc</kbd>
            </button>
          </>
        )}
      </div>
    </div>
  );
}

const COMMIT = "Your edit becomes a commit on the session branch.";

/* Whether `lines` are what `content` has from line `start` (1-based). */
const linesAt = (content: string, start: number, lines: string[]): boolean => {
  const all = (content.endsWith("\n") ? content.slice(0, -1) : content).split("\n");
  return lines.every((line, i) => all[start - 1 + i] === line);
};

/* The file's draft in its box. A save goes against the blob the draft
   started from: the file as it then stands is read first, and one that
   moved on (another blob, or the hunk's lines no longer where they were)
   is a conflict, not a write; the engine refuses the rest with its 409.
   In conflict the box keeps its text and offers Copy my edit, Discard and
   Reload. A file read again meanwhile (the pin missing, or another blob)
   never changes the text. */
function DraftEditor({ client, root, path, draft, pin, drafts, onSaved }: { client: DaemonClient; root: string; path: string; draft: Draft; pin: Pinned | undefined; drafts: Drafts; onSaved(): void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The file read again at another blob, or not readable at all any more (gone from the checkout): a conflict.
  const conflict = draft.conflict || (pin !== undefined && (pin === null || pin.base !== draft.base));
  const hunk: Hunk = { header: draft.header, start: 0, end: 0, newStart: draft.newStart, newCount: draft.newCount };
  const save = async () => {
    if (busy || conflict) return;
    setBusy(true);
    setError(null);
    try {
      if (draft.mode === "hunk") {
        const got = await client.sessionFile(root, path);
        if (got.base !== draft.base || !linesAt(got.content, draft.newStart, draft.snapshot)) throw new DaemonError("stale", CONFLICT, 409);
        await client.putSessionFile(root, { path, content: splice(got.content, hunk, draft.text.split("\n")), base: draft.base });
      } else {
        await client.putSessionFile(root, { path, content: draft.text, base: draft.base });
      }
      drafts.clear(path);
      onSaved();
    } catch (e) {
      if (e instanceof DaemonError && e.code === "stale") drafts.update(path, { conflict: true });
      else setError(refusal(e));
    } finally {
      setBusy(false);
    }
  };
  const copy = () => {
    void navigator.clipboard?.writeText(draft.text).catch(() => undefined);
  };
  const what = draft.mode === "hunk" ? COMMIT : `The whole of ${path}; ${COMMIT.toLowerCase()}`;
  return (
    <EditBox
      ariaLabel={draft.mode === "hunk" ? `Edit ${path} ${draft.header}` : `Edit ${path}`}
      text={draft.text}
      setText={(t) => drafts.update(path, { text: t })}
      busy={busy}
      saveable={!conflict}
      error={error}
      conflict={conflict ? CONFLICT : null}
      onSave={() => void save()}
      onDone={() => drafts.clear(path)}
      onCopy={copy}
      onReload={onSaved}
      what={what}
      testId={draft.mode === "hunk" ? "hunk-editor" : "file-editor"}
    />
  );
}

/* A file to add to the branch: its path, then its content, the sheet's
   new-file draft. It clears only when the file is written or the person
   discards it: looking at another file, or the PR step, keeps it. */
function NewFile({ client, root, draft, drafts, onSaved, onDone }: { client: DaemonClient; root: string; draft: Draft; drafts: Drafts; onSaved(path: string): void; onDone(): void }) {
  const path = draft.header;
  // The write under way is the draft's, not this box's: the box locks for it wherever the person goes meanwhile.
  const busy = !!draft.saving;
  const save = async () => {
    if (busy) return;
    if (!path.trim()) {
      drafts.update(NEW_KEY, { refused: "Name the file first." });
      return;
    }
    const sent = { path: path.trim(), text: draft.text };
    drafts.update(NEW_KEY, { saving: sent, refused: undefined });
    try {
      await client.putSessionFile(root, { path: sent.path, content: sent.text });
      // Written: the draft goes only if it still is what was sent; newer work (none can be typed while locked,
      // but a draft started over after a discard could be) stays.
      drafts.clear(NEW_KEY, (d) => d.header.trim() === sent.path && d.text === sent.text);
      drafts.update(NEW_KEY, { saving: undefined });
      onSaved(sent.path);
    } catch (e) {
      drafts.update(NEW_KEY, { saving: undefined, refused: refusal(e) });
    }
  };
  const discard = () => {
    if (busy) return;
    drafts.clear(NEW_KEY);
    onDone();
  };
  return (
    <div className="flex min-w-0 flex-1 flex-col" data-testid="new-file-editor">
      <label className={`flex items-center gap-s1 border-b border-line bg-canvas-2 px-s2 py-1 text-xs text-ink-2`}>
        New file
        <input className={`${field} ${mono} flex-1 py-0.5`} aria-label="New file path" placeholder="path/in/the/repo.ext" value={path} disabled={busy} onChange={(e) => drafts.update(NEW_KEY, { header: e.target.value })} autoFocus />
      </label>
      <EditBox ariaLabel="New file content" text={draft.text} setText={(t) => drafts.update(NEW_KEY, { text: t })} busy={busy} error={draft.refused ?? null} onSave={() => void save()} onDone={discard} what={busy ? "Writing the file…" : "A new file on the session branch."} testId="file-editor" />
    </div>
  );
}

/* Open PR: the title and body as brindle drafts them, to edit, then the one
   button; the job's end brings the PR's URL. */
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
          <a className="text-branch underline" href={url} onClick={(e) => (e.preventDefault(), void openUrl(url))}>
            {url}
          </a>
        </p>
      )}
      <div className="mt-auto flex flex-wrap items-center gap-s1 border-t border-line pt-s2">
        {url ? (
          <>
            <button type="button" className={primary} onClick={() => void openUrl(url)}>
              Open it ↗
            </button>
            <button type="button" className={button} onClick={onClose}>
              Close
            </button>
          </>
        ) : (
          <>
            <button type="button" className={primary} disabled={!loaded || job.busy || !title.trim()} onClick={open} data-testid="open-pr">
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
