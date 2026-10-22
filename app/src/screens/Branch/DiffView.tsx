/* A branch's diff against its base, as the review sheet shows it: the
   changed files down the left, the selected one's unified diff on the
   right, hunk by hunk, and the engine's untracked-files summary above.
   `useDiff` fetches GET /workspaces/{id}/diff and splits it (parseDiff).

   Changed lines are editable in place, while the worker is idle: click a
   hunk's header (or press `e` on the hunk under the cursor; `j` / `k`
   move it) and its new side becomes a box to edit, with a save bar. ⌘S
   writes the edit as a commit on the worker's branch: the file is read
   from the worktree (GET /workspaces/{id}/file), the hunk's lines are
   replaced in it, and the whole goes back with PUT /workspaces/{id}/file
   pinned to the blob the diff showed (`base`); a 409 (the file moved on,
   or the worker got busy) shows in the bar, as does any other refusal.
   Escape discards. Saved, the diff is fetched again. */

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { DaemonError, type Diff } from "../../api/client";
import { useDaemon } from "../../api/context";
import { button, card, label, mono, primary, quiet } from "../../components/ui";
import { hunksOf, newSide, parseDiff, splice, type FileDiff, type Hunk, type Untracked } from "./parseDiff";

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/* Whether the review sheet may edit the branch's files: only while no
   agent in the workspace is mid-turn (routes_actions.BUSY). */
export const BUSY = new Set(["starting", "processing", "waiting"]);

export interface EditAccess {
  workspaceId: string;
  /* Editing is on; otherwise `why` says what stops it ("worker busy"). */
  editable: boolean;
  why?: string;
}

/* The header's pill for it: "✎ editable · worker idle", or dashed when off. */
export function EditPill({ access }: { access: EditAccess }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-px text-xs ${access.editable ? "border-line bg-surface-2 text-ink-2" : "border-dashed border-line-strong text-ink-3"}`}
      title={access.editable ? "Click a hunk, or press e, to edit its new side. ⌘S saves it as your commit on the worker's branch." : `Editing is off: ${access.why ?? "the worker is busy"}.`}
      data-testid="edit-pill"
    >
      ✎ {access.editable ? "editable · worker idle" : `editing off · ${access.why ?? "worker busy"}`}
    </span>
  );
}

const STATUS_MARK: Record<FileDiff["status"], string> = { added: "A", deleted: "D", renamed: "R", modified: "M" };

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "bg-branch-soft text-branch";
  if (line.startsWith("+")) return "bg-pass-soft text-pass";
  if (line.startsWith("-")) return "bg-sap-soft text-sap";
  return "text-ink-2";
}

export interface DiffState {
  diff: Diff | null;
  error: string | null;
  files: FileDiff[];
  untracked: Untracked | null;
  /* The base the diff is against: the engine's word, else the workspace's, else "base". */
  base: string;
  reload(): void;
}

export function useDiff(workspaceId: string, baseBranch?: string | null): DiffState {
  const client = useDaemon();
  const [diff, setDiff] = useState<Diff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    client.diff(workspaceId).then(
      (d) => live && (setDiff(d), setError(null)),
      (e: unknown) => live && setError(message(e)),
    );
    return () => {
      live = false;
    };
  }, [client, workspaceId, attempt]);
  const { files, untracked } = useMemo(() => parseDiff(diff?.diff ?? ""), [diff]);
  return { diff, error, files, untracked, base: diff?.base ?? baseBranch ?? "base", reload: () => setAttempt((n) => n + 1) };
}

function FileList({ files, selected, onSelect }: { files: FileDiff[]; selected: string | null; onSelect: (path: string) => void }) {
  return (
    <ul className="w-64 shrink-0 overflow-auto border-r border-line py-1" aria-label="Changed files">
      {files.map((f) => (
        <li key={f.path}>
          <button
            type="button"
            onClick={() => onSelect(f.path)}
            aria-current={f.path === selected ? "true" : undefined}
            className={`flex w-full items-baseline gap-s1 px-s2 py-1 text-left ${mono} ${
              f.path === selected ? "bg-branch-soft text-ink" : "text-ink-2 hover:bg-surface-2"
            }`}
          >
            <span className="w-3 text-ink-3">{STATUS_MARK[f.status]}</span>
            {/* The file's name in full; its directory gives way first. */}
            <span className="flex min-w-0 flex-1" title={f.path}>
              {f.path.includes("/") && <span className="min-w-0 truncate text-ink-3">{f.path.slice(0, f.path.lastIndexOf("/") + 1)}</span>}
              <span className="shrink-0">{f.path.slice(f.path.lastIndexOf("/") + 1)}</span>
            </span>
            <span className="text-pass">+{f.added}</span>
            <span className="text-sap">-{f.removed}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/* One hunk being edited: its new side in a box, and the save bar. */
function HunkEditor({ file, hunk, access, onSaved, onDone }: { file: FileDiff; hunk: Hunk; access: EditAccess; onSaved(): void; onDone(): void }) {
  const client = useDaemon();
  const [text, setText] = useState(() => newSide(file, hunk).join("\n"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    box.current?.focus();
  }, []);

  const save = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const got = await client.readFile(access.workspaceId, file.path);
      // The file as the diff showed it: the edit is pinned to that blob (the diff's `index` line,
      // else the one the read answered) and the engine refuses it should the file move on again.
      if (file.blob && !got.base.startsWith(file.blob)) throw new DaemonError("stale", `${file.path} has changed since the diff was shown; reload it`, 409);
      await client.editFile(access.workspaceId, { path: file.path, content: splice(got.content, hunk, text.split("\n")), base: file.blob ?? got.base });
      onSaved();
      onDone();
    } catch (e) {
      setError(e instanceof DaemonError && (e.code === "stale" || e.code === "busy") ? `${e.code === "stale" ? "Stale" : "Busy"}: ${e.message}` : message(e));
    } finally {
      setBusy(false);
    }
  };
  const keys = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      e.stopPropagation();
      void save();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onDone();
    }
  };
  const rows = Math.max(3, text.split("\n").length);
  return (
    <div className="flex flex-col" data-testid="hunk-editor">
      <textarea
        ref={box}
        className={`${mono} w-full resize-y border-l-2 border-branch bg-canvas px-s2 py-1 leading-5 text-ink outline-none`}
        aria-label={`Edit ${file.path} ${hunk.header}`}
        rows={rows}
        spellCheck={false}
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={keys}
      />
      <div className="flex flex-wrap items-center gap-s1 border-y border-line bg-surface-2 px-s2 py-1 font-sans text-xs text-ink-2" data-testid="save-bar">
        <span>Your edit becomes a commit on the worker's branch.</span>
        {error && (
          <span className="text-sap" role="alert">
            {error}
          </span>
        )}
        <span className="flex-1" />
        <button type="button" className={`${primary} px-s1 py-0.5 text-xs`} disabled={busy} onClick={() => void save()}>
          Save <kbd className="ml-1 font-mono text-[10px] opacity-80">⌘S</kbd>
        </button>
        <button type="button" className={`${quiet} text-xs`} disabled={busy} onClick={onDone}>
          Discard <kbd className="ml-1 font-mono text-[10px] opacity-80">esc</kbd>
        </button>
      </div>
    </div>
  );
}

/* The selected file, hunk by hunk; with `access`, a hunk opens for editing
   on a click of its header or `e`, and `j` / `k` move between hunks. */
function FileView({ file, access, onSaved }: { file: FileDiff; access?: EditAccess; onSaved(): void }) {
  const hunks = useMemo(() => hunksOf(file), [file]);
  const [cursor, setCursor] = useState(0);
  const [editing, setEditing] = useState<number | null>(null);
  const host = useRef<HTMLDivElement>(null);
  // Another file, or a fresh diff: nothing is being edited.
  useEffect(() => {
    setEditing(null);
    setCursor(0);
  }, [file]);
  const canEdit = !!access?.editable && !file.binary && !!file.blob && file.status !== "deleted";
  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget || hunks.length === 0) return;
    if (e.key === "j") setCursor((c) => Math.min(c + 1, hunks.length - 1));
    else if (e.key === "k") setCursor((c) => Math.max(c - 1, 0));
    else if (e.key === "e" && canEdit) setEditing(cursor);
    else return;
    e.preventDefault();
  };
  return (
    <div ref={host} className="min-w-0 flex-1 overflow-auto outline-none" data-testid="file-diff" tabIndex={0} onKeyDown={keys}>
      <div className={`sticky top-0 z-10 flex items-center gap-s1 border-b border-line bg-canvas-2 px-s2 py-1 ${mono}`}>
        <span className="min-w-0 flex-1 truncate">{file.oldPath ? `${file.oldPath} → ${file.path}` : file.path}</span>
        {hunks.length > 0 && (
          <span className="shrink-0 font-sans text-xs text-ink-3">
            {hunks.length} {hunks.length === 1 ? "hunk" : "hunks"} · <kbd className="font-mono">j</kbd> <kbd className="font-mono">k</kbd> hunk{canEdit && (
              <>
                {" "}
                · <kbd className="font-mono">e</kbd> edit
              </>
            )}
          </span>
        )}
      </div>
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
          <section key={hunk.start} className={h === cursor ? "ring-1 ring-inset ring-branch/40" : ""} data-testid="hunk" data-editing={editing === h} aria-label={hunk.header}>
            <button
              type="button"
              className={`${mono} flex w-full items-center gap-s1 px-s2 text-left leading-5 ${editing === h ? "bg-branch text-on-branch" : "bg-branch-soft text-branch"} ${canEdit ? "cursor-pointer" : "cursor-default"}`}
              disabled={!canEdit}
              title={canEdit ? "Edit this hunk's new side" : access ? `Editing is off: ${access.why ?? "the worker is busy"}` : undefined}
              onClick={() => {
                setCursor(h);
                if (canEdit) setEditing(editing === h ? null : h);
              }}
            >
              <span className="min-w-0 flex-1 truncate">{hunk.header}</span>
              {canEdit && <span className="shrink-0 font-sans text-xs opacity-70">{editing === h ? "editing the new side · ⌘S save · esc discard" : "click or e to edit"}</span>}
            </button>
            {editing === h && access ? (
              <HunkEditor key={`${file.path}:${hunk.start}`} file={file} hunk={hunk} access={access} onSaved={onSaved} onDone={() => setEditing(null)} />
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
    </div>
  );
}

/* Files git doesn't track yet: in the branch's work, but not in its patch. */
function UntrackedList({ untracked }: { untracked: Untracked }) {
  return (
    <section className="text-sm" aria-label="Untracked files">
      <h3 className="font-medium text-ink-2">Untracked files ({untracked.count})</h3>
      <ul className={`${mono} text-ink-3`}>
        {untracked.entries.map((entry) => (
          <li key={entry}>{entry}</li>
        ))}
      </ul>
    </section>
  );
}

/* The diff as fetched: loading, failed (with a retry), empty, or the files.
   With `access`, the hunks of the selected file can be edited in place. */
export function DiffView({ state, access }: { state: DiffState; access?: EditAccess }) {
  const { diff, error, files, untracked, base, reload } = state;
  const [selected, setSelected] = useState<string | null>(null);
  const file = files.find((f) => f.path === selected) ?? files[0] ?? null;
  if (error) {
    return (
      <div className="flex items-center gap-s2">
        <p className="text-sap">{error}</p>
        <button type="button" className={button} onClick={reload}>
          Retry
        </button>
      </div>
    );
  }
  if (!diff) return <p className="text-ink-3">Loading diff…</p>;
  if (diff.diff.trim() === "") return <p className="text-ink-3">No changes against {base}.</p>;
  return (
    <>
      {untracked && <UntrackedList untracked={untracked} />}
      {files.length > 0 ? (
        <div className="flex min-h-0 flex-1 flex-col gap-s1">
          <div className={label}>
            {files.length} {files.length === 1 ? "file" : "files"} changed against {base}
          </div>
          <div className={`${card} flex min-h-0 flex-1 overflow-hidden`}>
            <FileList files={files} selected={file?.path ?? null} onSelect={setSelected} />
            {file && <FileView file={file} access={access} onSaved={reload} />}
          </div>
        </div>
      ) : (
        !untracked && (
          // Not a patch this view can split: shown as the engine sent it.
          <pre className={`${card} min-h-0 flex-1 overflow-auto p-s2 ${mono}`}>{diff.diff}</pre>
        )
      )}
    </>
  );
}
