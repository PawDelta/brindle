/* The calm empty state, and the New session composer: where the app lands
   with no session to show, and where "New session" goes. One composer:
   what brindle should work on, in which repo, and Start (branch, CLI and
   name behind Options). A start with sessions already running in the
   repo answers a conflict, and the person picks what to do (open one, a
   new worktree, or pause it and start fresh); a start the engine refuses
   with 409 {preflight} (the CLI needs a sign-in, a key, a trust or a
   consent first) shows the engine's items as native UI (Preflight) and
   starts again once they are settled. Started, the app opens the new
   session's provider session. A project is added as the first launch adds
   one (components/ProjectPicker: open a folder, or clone a repository):
   in place of the composer while there is none, and behind "Add a
   project…" beside the repo after. */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { DaemonError, type DaemonClient, type Note, type PreflightDTO, type Repo, type SessionConflict, type StartSession } from "../../api/client";
import type { PickFolder } from "../../api/open";
import type { SessionDTO } from "../../api/schema";
import Logo from "../../components/Logo";
import { Preflight } from "../../components/Preflight";
import { ProjectCombobox } from "../../components/ProjectCombobox";
import { ProjectPicker } from "../../components/ProjectPicker";
import { alert, button, card, field, main, mono, quiet } from "../../components/ui";
import { isPaused } from "../../store/overview";
import { useSessions } from "../../store/sessions";

export interface NewSessionProps {
  client: DaemonClient;
  /* Open a session's view: its root agent id, the repo it belongs to. */
  onOpen(root: string, repo: string): void;
  /* The repo to start in (one just picked); else the first. */
  initialRepo?: string;
  /* The native folder picker, for adding a project (none in a browser tab). */
  pickFolder?: PickFolder;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh?: () => Promise<void>;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

type StartFields = Omit<StartSession, "repo" | "choice" | "session_id">;

export function NewSession({ client, onOpen, initialRepo, pickFolder, refresh }: NewSessionProps) {
  const { repos, sessions, error: loadError, reload } = useSessions();
  const [attempt, setAttempt] = useState(0);
  /* The project to start in: as opened, or the one just added. */
  const [chosen, setChosen] = useState(initialRepo);
  const added = (repo: Repo) => {
    setChosen(repo.repo_root);
    void reload();
  };
  useEffect(() => {
    if (attempt > 0) void reload();
  }, [attempt, reload]);

  if (loadError && repos === null) {
    return (
      <div role="alert" className={`${alert} m-s3 flex items-center gap-s2`}>
        <span>Couldn't load sessions: {loadError}</span>
        <button type="button" className={button} onClick={() => setAttempt((n) => n + 1)}>
          Retry
        </button>
      </div>
    );
  }

  const live = Object.values(sessions).flat().filter((s) => !isPaused(s)).length;
  return (
    <section className="relative flex min-h-0 flex-1" aria-label="New session">
      <div className="flex min-w-0 flex-1 flex-col items-center gap-s3 overflow-auto px-s4 py-s6">
        <div className="flex w-full max-w-2xl flex-col gap-s3">
          <div className="flex flex-col gap-1">
            <Logo className="text-2xl" />
            <p className="text-sm text-ink-2">
              {repos === null ? "Loading…" : repos.length === 0 ? "No repo is set up yet." : live === 0 ? "Nothing is running. Describe what brindle should work on, or pick a recent session in the sidebar." : "Start another session. The ones running are in the sidebar."}
            </p>
          </div>
          {repos === null ? null : repos.length === 0 ? (
            <FirstRepo client={client} pickFolder={pickFolder} onPicked={added} />
          ) : (
            <Composer client={client} repos={repos} sessions={sessions} reload={reload} refresh={refresh} onOpen={onOpen} chosen={chosen} pickFolder={pickFolder} onAdded={added} />
          )}
        </div>
      </div>
    </section>
  );
}

/* Nothing to show yet: a first project, opened or cloned. */
function FirstRepo({ client, pickFolder, onPicked }: { client: DaemonClient; pickFolder?: PickFolder; onPicked(repo: Repo): void }) {
  return (
    <section className={`${card} flex flex-col gap-s2 px-s3 py-s3`} aria-label="Welcome">
      <h1 className="text-xl font-semibold tracking-tight">Welcome to brindle</h1>
      <p className="max-w-prose text-ink-2">
        brindle runs coding agents for you, each on its own branch in its own worktree, with a supervisor that hands out the work and a place here to see it all. No repo is set up yet: pick a project to start in.
      </p>
      <ProjectPicker client={client} pickFolder={pickFolder} onPicked={onPicked} />
    </section>
  );
}

/* One composer for every repo: the prompt, the repo, Start, and the rest
   behind Options. A start that conflicts, or that the engine's preflight
   refuses, asks here what to do. */
function Composer({ client, repos, sessions, reload, refresh, onOpen, chosen, pickFolder, onAdded }: {
  client: DaemonClient;
  repos: Repo[];
  sessions: Record<string, SessionDTO[]>;
  reload(): Promise<void>;
  refresh?: () => Promise<void>;
  onOpen(root: string, repo: string): void;
  chosen?: string;
  pickFolder?: PickFolder;
  onAdded(repo: Repo): void;
}) {
  const [repo, setRepo] = useState(repos.some((r) => r.repo_root === chosen) ? chosen! : (repos[0]?.repo_root ?? ""));
  const [adding, setAdding] = useState(false);
  // A project just added is selected once it is listed.
  const selected = useRef(repos.some((r) => r.repo_root === chosen) ? chosen : undefined);
  useEffect(() => {
    if (chosen && selected.current !== chosen && repos.some((r) => r.repo_root === chosen)) {
      selected.current = chosen;
      setRepo(chosen);
    }
  }, [chosen, repos]);
  const [prompt, setPrompt] = useState("");
  const [options, setOptions] = useState(false);
  const [branch, setBranch] = useState("");
  const [sessionName, setSessionName] = useState("");
  const [provider, setProvider] = useState("");
  const [providers, setProviders] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  // A start waiting on the person's choice: what they asked for, and the choices.
  const [pending, setPending] = useState<{ fields: StartFields; conflict: SessionConflict } | null>(null);
  // A start the engine refused until the CLI is set up: what they asked for, and the items.
  const [preflight, setPreflight] = useState<{ fields: StartFields; items: PreflightDTO } | null>(null);

  useEffect(() => {
    if (!repos.some((r) => r.repo_root === repo)) setRepo(repos[0]?.repo_root ?? "");
  }, [repos, repo]);

  useEffect(() => {
    client
      .profiles()
      .then((profiles) => setProviders([...new Set(profiles.map((p) => p.provider))].sort()))
      .catch(() => setProviders([]));
  }, [client]);

  const start = async (fields: StartFields, choice?: { key: string; session: string }): Promise<boolean> => {
    setBusy(true);
    setError(null);
    setNotes([]);
    let opened: string | null = null;
    try {
      const got = await client.startSession({ repo, ...fields, choice: choice?.key, session_id: choice?.session });
      setPending(null);
      setPreflight(null);
      if ("opened" in got) opened = got.opened.id;
      else {
        setNotes(got.notes);
        opened = got.id;
      }
    } catch (e) {
      if (e instanceof DaemonError && e.conflict) setPending({ fields, conflict: e.conflict });
      else if (e instanceof DaemonError && e.preflight) setPreflight({ fields, items: e.preflight });
      else setError(message(e));
    } finally {
      setBusy(false);
      // Started, the snapshot too: one read on the new chat's status event can predate its window
      // ("exited", no window), and nothing on the bus says when the window lands.
      await Promise.all([reload().catch((e: unknown) => setError(message(e))), opened ? refresh?.() : undefined]);
    }
    // Opened once the list and the snapshot have it: a root neither knows yet reads as paused.
    if (opened) onOpen(opened, repo);
    return opened !== null;
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const ok = await start({
      autopilot: true,
      prompt: prompt.trim() || undefined,
      branch: branch.trim() || undefined,
      provider: provider || undefined,
      name: sessionName.trim() || undefined,
    });
    if (ok) {
      setPrompt("");
      setBranch("");
      setSessionName("");
    }
  };

  const running = (sessions[repo] ?? []).filter((s) => !isPaused(s)).length;
  return (
    <>
    <form aria-label="Start a session" onSubmit={submit} className={`${card} flex flex-col gap-s1 px-s2 py-s2`}>
      <div className="flex items-start gap-s1">
        <textarea
          className={`${field} min-h-24 w-full resize-y`}
          aria-label="What should brindle work on?"
          placeholder="What should brindle work on? Describe the task; the supervisor plans and hands out the work."
          value={prompt}
          rows={3}
          autoFocus
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit(e);
          }}
        />
      </div>
      <div className="flex flex-wrap items-center gap-s1">
        {repos.length > 1 ? (
          <ProjectCombobox repos={repos} value={repo} onChange={setRepo} />
        ) : (
          <span className="text-sm text-ink-2">
            in <span className="font-medium text-ink">{repos[0]?.name}</span>
          </span>
        )}
        <button type="button" className={quiet} aria-expanded={adding} onClick={() => setAdding((a) => !a)}>
          Add a project…
        </button>
        {running > 0 && <span className="text-xs text-ink-3">{running === 1 ? "one session already runs here" : `${running} sessions already run here`}</span>}
        <button type="button" className={quiet} aria-expanded={options} onClick={() => setOptions((o) => !o)}>
          Options {options ? "▴" : "▾"}
        </button>
        <span className="flex-1" />
        <span className="text-xs text-ink-3">⌘↵</span>
        <button type="submit" disabled={busy || !repo} className={main}>
          Start
        </button>
      </div>
      {options && (
        <div className="flex flex-wrap items-center gap-s1 border-t border-line pt-s1">
          <input className={`${field} w-48`} placeholder="Session name (optional)" aria-label="Session name" value={sessionName} onChange={(e) => setSessionName(e.target.value)} />
          <input className={`${field} w-48 font-mono`} placeholder="Branch (its own worktree)" aria-label="Branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
          {providers.length > 0 && (
            <select className={field} aria-label="Supervisor CLI" value={provider} onChange={(e) => setProvider(e.target.value)}>
              <option value="">default CLI</option>
              {providers.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
      {pending && <ConflictChoice conflict={pending.conflict} onChoose={(key, session) => start(pending.fields, { key, session })} onCancel={() => setPending(null)} />}
      {error && (
        <p role="alert" className={alert}>
          {error}
        </p>
      )}
      {notes.length > 0 && (
        <ul className="text-sm">
          {notes.map((n, i) => (
            <li key={i} className={n.tone === "warn" ? "text-sap" : n.tone === "ok" ? "text-pass" : "text-ink-2"}>
              {n.text}
            </li>
          ))}
        </ul>
      )}
    </form>
    {/* Its own forms (a key's field) can't sit inside the composer's. */}
    {preflight && (
      <Preflight preflight={preflight.items} onRefresh={(items) => setPreflight({ fields: preflight.fields, items })} onLaunch={() => void start(preflight.fields)} launchLabel="Start" repo={repo} />
    )}
    {adding && (
      <section className={`${card} flex flex-col gap-s1 px-s2 py-s2`} aria-label="Add a project">
        <ProjectPicker
          client={client}
          pickFolder={pickFolder}
          onPicked={(picked) => {
            setAdding(false);
            onAdded(picked);
          }}
        />
      </section>
    )}
    </>
  );
}

function ConflictChoice({ conflict, onChoose, onCancel }: { conflict: SessionConflict; onChoose(key: string, session: string): Promise<boolean>; onCancel(): void }) {
  const [picked, setPicked] = useState(conflict.sessions[conflict.default]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const choose = async (key: string) => {
    setBusy(true);
    await onChoose(key, picked);
    setBusy(false);
  };
  return (
    <div role="group" aria-label="Session already running" className="flex flex-col gap-s1 rounded-md border border-branch bg-branch-soft p-s2 text-sm">
      <p className="font-medium">{conflict.message}</p>
      {conflict.sessions.map((s) => (
        <label key={s.id} className="flex items-center gap-s1">
          <input type="radio" name="conflict-session" checked={picked === s.id} onChange={() => setPicked(s.id)} />
          <span className={mono}>{s.id}</span>
          <span className="text-ink-2">{s.name || s.goal || s.status}</span>
        </label>
      ))}
      <div className="flex flex-wrap gap-s1">
        {conflict.choices.map((c) => (
          <button key={c.key} type="button" disabled={busy || !picked} className={button} onClick={() => choose(c.key)}>
            {c.label[0].toUpperCase() + c.label.slice(1)}
          </button>
        ))}
        <button type="button" className={quiet} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
