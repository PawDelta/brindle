/* The session's Repo tab: the session branch, and the session checkout's
   tracked files down the left (GET /sessions/{root}/tree, read again each
   time the tab is shown), the file clicked in an editor beside them (GET
   .../file), saved with PUT .../file against the blob it was read at. As in
   Changes, nothing read again replaces an unsaved edit: a file that moved on
   says so in one line with Copy my edit / Discard / Reload, and the engine's
   409 busy shows in the save bar. A save under way is the file's, not its
   box's: it keeps the box locked through looking at other files, and clears
   the edit only if it still is what was sent. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DaemonError, type DaemonClient, type FileContent, type SessionTree } from "../../api/client";
import { plain } from "../../api/wording";
import { label, mono } from "../../components/ui";
import { COMMIT, CONFLICT, EditBox, FileTree, refusal } from "./SessionDiffEditor";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* An unsaved edit of a file: its text, the blob it started from, whether a
   save found the file moved on, the text a save under way sent, and what
   refused the last save. */
interface Edit {
  text: string;
  base: string;
  conflict: boolean;
  saving?: string;
  refused?: string;
}

export function RepoView({ client, root, branch, active }: {
  client: DaemonClient;
  root: string;
  /* The session branch as the view knows it until the tree names it. */
  branch: string;
  /* The tab is shown: its tree is read again. */
  active: boolean;
}) {
  const [tree, setTree] = useState<SessionTree | null>(null);
  const [treeError, setTreeError] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    let live = true;
    client.sessionTree(root).then(
      (got) => live && (setTree(got), setTreeError(null)),
      (e) => live && setTreeError(message(e)),
    );
    return () => {
      live = false;
    };
  }, [client, root, active]);
  const files = useMemo(() => (tree?.files ?? []).map((path) => ({ path })), [tree]);
  const shownBranch = tree?.branch || branch;

  // Each file opened, as read (or why it couldn't be), and the edits under way, kept apart so a read never takes an edit.
  const [opened, setOpened] = useState<string | null>(null);
  const [pins, setPins] = useState<Record<string, FileContent | string>>({});
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const read = useCallback(
    async (path: string) => {
      try {
        const got = await client.sessionFile(root, path);
        setPins((p) => ({ ...p, [path]: got }));
        // Read at another blob than an edit started from: that edit is in conflict. One being saved is the
        // save's to settle (the blob read may be its own write); a file moved on by others fails it as stale.
        setEdits((all) => (all[path] && all[path].saving === undefined && all[path].base !== got.base ? { ...all, [path]: { ...all[path], conflict: true } } : all));
      } catch (e) {
        setPins((p) => ({ ...p, [path]: message(e) }));
      }
    },
    [client, root],
  );
  const patch = (path: string, change: Partial<Edit>) => setEdits((all) => (all[path] ? { ...all, [path]: { ...all[path], ...change } } : all));
  const drop = (path: string) =>
    setEdits((all) => {
      const { [path]: _, ...rest } = all;
      return rest;
    });
  // A save goes against the blob the edit started from; once written, the edit goes only if it still is what was
  // sent. Each save has its number: one that ends after a later one began for its file changes nothing.
  const saves = useRef<Record<string, number>>({});
  const saveCount = useRef(0);
  const save = async (path: string, edit: Edit) => {
    if (edit.saving !== undefined || edit.conflict) return;
    const sent = edit.text;
    const id = ++saveCount.current;
    saves.current[path] = id;
    patch(path, { saving: sent, refused: undefined });
    try {
      const got = await client.putSessionFile(root, { path, content: sent, base: edit.base });
      if (saves.current[path] !== id) return;
      setEdits((all) => {
        const now = all[path];
        if (!now) return all;
        if (now.text === sent) {
          const { [path]: _, ...rest } = all;
          return rest;
        }
        return { ...all, [path]: { ...now, saving: undefined, base: got.blob ?? now.base } };
      });
      if (got.blob) setPins((p) => ({ ...p, [path]: { path, content: sent, base: got.blob! } }));
      else void read(path);
    } catch (e) {
      if (saves.current[path] !== id) return;
      patch(path, e instanceof DaemonError && e.code === "stale" ? { saving: undefined, conflict: true } : { saving: undefined, refused: refusal(e) });
    }
  };
  const pin = opened ? pins[opened] : undefined;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-s1">
      <div className="flex flex-wrap items-center gap-s2">
        <span data-testid="session-branch" title={shownBranch} className={`${mono} min-w-0 max-w-64 truncate text-xs text-ink-2`}>
          {shownBranch}
        </span>
        <span className={`${label} flex-1`}>{tree && `${tree.files.length} ${tree.files.length === 1 ? "file" : "files"}`}</span>
      </div>
      {treeError && (
        <p className="text-sm text-sap" role="alert">
          {treeError}
        </p>
      )}
      <div className="flex min-h-0 flex-1 overflow-hidden border-t border-line">
        <nav className="flex w-40 shrink-0 flex-col overflow-auto border-r border-line py-1 md:w-64" aria-label="Repository files">
          {!tree && !treeError ? (
            <p className="px-s2 text-sm text-ink-3">Loading…</p>
          ) : (
            <ul>
              <FileTree folded files={files} renderFile={(f) => (
                <li key={f.path}>
                  <button
                    type="button"
                    title={f.path}
                    aria-current={f.path === opened ? "true" : undefined}
                    className={`flex w-full min-w-0 items-baseline gap-s1 px-s2 py-1 text-left ${mono} ${f.path === opened ? "bg-surface-2 text-ink" : "text-ink-2 hover:bg-surface-2"}`}
                    onClick={() => {
                      setOpened(f.path);
                      void read(f.path);
                    }}
                  >
                    <span className="min-w-0 flex-1 truncate">{f.path.slice(f.path.lastIndexOf("/") + 1)}</span>
                    {edits[f.path] && <span className="shrink-0 font-sans text-xs text-ink-3">unsaved</span>}
                  </button>
                </li>
              )} />
            </ul>
          )}
        </nav>
        {!opened ? (
          <p className="p-s2 text-ink-3">Pick a file to open it.</p>
        ) : (
          <div className="flex min-w-0 flex-1 flex-col overflow-auto" data-testid="repo-file">
            <div className={`sticky top-0 z-10 flex items-center gap-s1 border-b border-line bg-canvas-2 px-s2 py-1 ${mono}`}>
              <span className="min-w-0 flex-1 truncate">{opened}</span>
            </div>
            {pin === undefined ? (
              <p className="p-s2 text-ink-3">Loading…</p>
            ) : typeof pin === "string" ? (
              <p className="p-s2 text-sap">Couldn't open {opened}: {pin}</p>
            ) : (
              <EditBox
                key={opened}
                ariaLabel={`Edit ${opened}`}
                text={edits[opened]?.text ?? pin.content}
                setText={(text) => setEdits((all) => ({ ...all, [opened]: all[opened] ? { ...all[opened], text } : { text, base: pin.base, conflict: false } }))}
                busy={edits[opened]?.saving !== undefined}
                saveable={!!edits[opened] && !edits[opened].conflict}
                error={edits[opened]?.refused ?? null}
                conflict={edits[opened]?.conflict ? CONFLICT : null}
                onSave={() => edits[opened] && void save(opened, edits[opened])}
                onDone={() => {
                  // In conflict the file as read is stale too: read it again.
                  const stale = edits[opened]?.conflict;
                  if (edits[opened]?.saving !== undefined) return;
                  drop(opened);
                  if (stale) void read(opened);
                }}
                onCopy={() => void navigator.clipboard?.writeText(edits[opened]?.text ?? "").catch(() => undefined)}
                onReload={() => {
                  // A save under way keeps its edit until it ends.
                  if (edits[opened]?.saving !== undefined) return;
                  drop(opened);
                  void read(opened);
                }}
                what={COMMIT}
                testId="repo-editor"
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
