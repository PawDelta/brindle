/* The session's Repo tab: the session checkout's tracked files down the
   left (GET /sessions/{root}/tree, read again each time the tab is shown),
   the file clicked in an editor beside them (GET .../file), saved with PUT
   .../file against the blob it was read at. As in Changes, nothing read
   again replaces an unsaved edit: a file that moved on says so in one line
   with Copy my edit / Discard / Reload, and the engine's 409 busy shows in
   the save bar. Shell opens the person's own shell in the session checkout
   (POST /terminals/shell) in a drawer at the foot, shown as the Terminal's
   shells are; it closes with its × or when the view goes. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DaemonError, type DaemonClient, type FileContent, type SessionTree } from "../../api/client";
import { plain } from "../../api/wording";
import { ShellView, tilde, type ShellTab } from "../../components/TerminalPanel";
import { button, label, mono } from "../../components/ui";
import { COMMIT, CONFLICT, EditBox, FileTree, refusal } from "./SessionDiffEditor";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* An unsaved edit of a file: its text, the blob it started from, and whether a save found the file moved on. */
interface Edit {
  text: string;
  base: string;
  conflict: boolean;
}

export function RepoView({ client, root, repo, cwd, active }: {
  client: DaemonClient;
  root: string;
  repo: string;
  /* The session checkout's folder, where Shell starts; null while the snapshot doesn't have it. */
  cwd: string | null;
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

  // Each file opened, as read (or why it couldn't be), and the edits under way, kept apart so a read never takes an edit.
  const [opened, setOpened] = useState<string | null>(null);
  const [pins, setPins] = useState<Record<string, FileContent | string>>({});
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const read = useCallback(
    async (path: string) => {
      try {
        const got = await client.sessionFile(root, path);
        setPins((p) => ({ ...p, [path]: got }));
        // Read at another blob than an edit started from: that edit is in conflict.
        setEdits((all) => (all[path] && all[path].base !== got.base ? { ...all, [path]: { ...all[path], conflict: true } } : all));
      } catch (e) {
        setPins((p) => ({ ...p, [path]: message(e) }));
      }
    },
    [client, root],
  );
  const drop = (path: string) =>
    setEdits((all) => {
      const { [path]: _, ...rest } = all;
      return rest;
    });
  const pin = opened ? pins[opened] : undefined;

  // The drawer's shell: one at a time; Shell while one starts or runs shows that one.
  const [shell, setShell] = useState<ShellTab | null>(null);
  const [drawer, setDrawer] = useState(false);
  const [shellError, setShellError] = useState<string | null>(null);
  const starting = useRef(false);
  const running = useRef<string | null>(null);
  running.current = shell?.terminal ?? null;
  const openShell = async () => {
    setDrawer(true);
    if (!cwd || shell || starting.current) return;
    starting.current = true;
    setShellError(null);
    try {
      const got = await client.openShell(cwd);
      const name = repo.split("/").filter(Boolean).pop() ?? repo;
      setShell({ repo, name, cwd, why: "started in the session checkout", terminal: got.terminal, at: { server: got.server ?? null, target: got.target }, shell: got.shell || "shell" });
    } catch (e) {
      setShellError(`Couldn't start a shell: ${message(e)}`);
    } finally {
      starting.current = false;
    }
  };
  const closeShell = async () => {
    if (!shell) return setDrawer(false);
    setShellError(null);
    try {
      await client.closeTerminal(shell.terminal);
    } catch (e) {
      // Gone already: nothing is left to close.
      if (!(e instanceof DaemonError && e.code === "not_found")) return setShellError(`Couldn't close the shell: ${message(e)}`);
    }
    setShell(null);
    setDrawer(false);
  };
  // Leaving the session's view ends its drawer's shell.
  useEffect(
    () => () => {
      if (running.current) void client.closeTerminal(running.current).catch(() => undefined);
    },
    [client],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-s1">
      <div className="flex flex-wrap items-center gap-s2">
        <span className={`${label} flex-1`}>
          {tree ? <>{tree.files.length} {tree.files.length === 1 ? "file" : "files"} on <span className={mono}>{tree.branch}</span></> : "The session checkout"}
        </span>
        <button type="button" className={`${button} text-xs`} aria-pressed={drawer} disabled={!cwd} title={cwd ? `A shell in ${tilde(cwd)}` : "The session checkout isn't known yet"} onClick={() => void openShell()} data-testid="repo-shell">
          Shell
        </button>
      </div>
      {treeError && (
        <p className="text-sm text-sap" role="alert">
          {treeError}
        </p>
      )}
      <div className="flex min-h-0 flex-1 overflow-hidden rounded-lg border border-line bg-surface">
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
                    className={`flex w-full min-w-0 items-baseline gap-s1 px-s2 py-1 text-left ${mono} ${f.path === opened ? "bg-branch-soft text-ink" : "text-ink-2 hover:bg-surface-2"}`}
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
              <RepoFile
                key={opened}
                client={client}
                root={root}
                path={opened}
                pin={pin}
                edit={edits[opened]}
                onEdit={(text) => setEdits((all) => ({ ...all, [opened]: all[opened] ? { ...all[opened], text } : { text, base: pin.base, conflict: false } }))}
                onConflict={() => setEdits((all) => (all[opened] ? { ...all, [opened]: { ...all[opened], conflict: true } } : all))}
                onSaved={(text, blob) => {
                  drop(opened);
                  if (blob) setPins((p) => ({ ...p, [opened]: { path: opened, content: text, base: blob } }));
                  else void read(opened);
                }}
                onDiscard={() => {
                  const stale = edits[opened]?.conflict;
                  drop(opened);
                  if (stale) void read(opened);
                }}
                onReload={() => {
                  drop(opened);
                  void read(opened);
                }}
              />
            )}
          </div>
        )}
      </div>
      {drawer && (
        <section className="flex h-[40%] min-h-[120px] flex-none flex-col overflow-hidden rounded-md border border-line bg-shell-bg" aria-label="Shell" data-testid="repo-shell-drawer">
          <div className="flex h-8 flex-none items-center gap-1.5 border-b border-shell-line bg-shell-bg-2 px-3 text-[12px] text-shell-dim">
            <span className="min-w-0 flex-1 truncate">
              {shell ? (
                <>
                  {shell.shell}, {shell.why} <span className="font-mono text-[11px] text-shell-ink/80">{tilde(shell.cwd)}</span>
                </>
              ) : (
                !shellError && "Starting a shell…"
              )}
            </span>
            <button type="button" className="inline-grid h-[22px] w-[22px] flex-none place-items-center rounded-md text-[14px] text-shell-dim hover:text-shell-ink" title="Close this shell" aria-label="Close this shell" onClick={() => void closeShell()}>
              ×
            </button>
          </div>
          {shellError && (
            <p role="alert" className="flex-none border-b border-shell-line bg-shell-bg-2 px-3 py-1 text-[11.5px] text-shell-bad">
              {shellError}
            </p>
          )}
          {shell && <ShellView key={shell.terminal} tab={shell} onEnded={() => (setShell(null), setDrawer(false))} />}
        </section>
      )}
    </div>
  );
}

/* The open file in its box: the edit under way, else the file as read. Save
   goes against the blob the edit started from; a 409 stale is a conflict,
   any other refusal (busy) shows in the bar. */
function RepoFile({ client, root, path, pin, edit, onEdit, onConflict, onSaved, onDiscard, onReload }: {
  client: DaemonClient;
  root: string;
  path: string;
  pin: FileContent;
  edit: Edit | undefined;
  onEdit(text: string): void;
  onConflict(): void;
  onSaved(text: string, blob: string | null): void;
  onDiscard(): void;
  onReload(): void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    if (!edit || busy || edit.conflict) return;
    setBusy(true);
    setError(null);
    try {
      const got = await client.putSessionFile(root, { path, content: edit.text, base: edit.base });
      onSaved(edit.text, got.blob);
    } catch (e) {
      if (e instanceof DaemonError && e.code === "stale") onConflict();
      else setError(refusal(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <EditBox
      ariaLabel={`Edit ${path}`}
      text={edit?.text ?? pin.content}
      setText={onEdit}
      busy={busy}
      saveable={!!edit && !edit.conflict}
      error={error}
      conflict={edit?.conflict ? CONFLICT : null}
      onSave={() => void save()}
      onDone={() => (setError(null), onDiscard())}
      onCopy={() => void navigator.clipboard?.writeText(edit?.text ?? "").catch(() => undefined)}
      onReload={() => (setError(null), onReload())}
      what={COMMIT}
      testId="repo-editor"
    />
  );
}
