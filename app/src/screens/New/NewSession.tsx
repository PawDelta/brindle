/* The calm empty state, and the New session composer: where the app lands
   with no session to show, and where "New session" goes. One composer:
   what brindle should work on, in which repo, and Start (branch, CLI and
   session name behind Options). A start with sessions already running in the
   repo answers a conflict, and the person picks what to do (open one, a
   new worktree, or pause it and start fresh); a start the engine refuses
   with 409 {preflight} (the CLI needs a sign-in, a key, a trust or a
   consent first) shows the engine's items as native UI (Preflight) and
   starts again once they are settled. Started, the app opens the new
   session's provider session. A project is added as the first launch adds
   one (components/ProjectPicker: open a folder, or clone a repository):
   in place of the composer while there is none, and behind "Add a
   project…" beside the repo after. Under it, the way to the demo the first
   launch offers (screens/Onboarding/Demo). */

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { DaemonError, LOGIN_PROVIDERS, type DaemonClient, type Note, type PreflightDTO, type Repo, type SessionConflict, type StartSession } from "../../api/client";
import type { PickFolder } from "../../api/open";
import type { SessionDTO } from "../../api/schema";
import { LoadingButton } from "../../components/LoadingButton";
import Logo from "../../components/Logo";
import { Preflight } from "../../components/Preflight";
import { ProjectCombobox } from "../../components/ProjectCombobox";
import { ProjectPicker } from "../../components/ProjectPicker";
import { alert, button, card, field, h2, lede, main, quiet, title } from "../../components/ui";
import { isPaused, providerName } from "../../store/overview";
import { useSessions } from "../../store/sessions";
import { WatchDemo } from "../Onboarding/Demo";
import { ConflictChoice } from "./ConflictChoice";

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
  /* To Settings › Providers, to sign in to a CLI. */
  onSignIn?: () => void;
}

/* The CLI the person picked for their sessions, kept in this app between launches. */
const PICK_KEY = "brindle.supervisorCli";
function readPick(): string {
  try {
    return localStorage.getItem(PICK_KEY) ?? "";
  } catch {
    return "";
  }
}
function rememberPick(p: string) {
  try {
    localStorage.setItem(PICK_KEY, p);
  } catch {
    /* kept for this launch only */
  }
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

type StartFields = Omit<StartSession, "repo" | "choice" | "session_id">;

export function NewSession({ client, onOpen, initialRepo, pickFolder, refresh, onSignIn = () => undefined }: NewSessionProps) {
  const { repos: allRepos, sessions, error: loadError, reload } = useSessions();
  /* The practice repo is for the demo flow only: never a project to start in. */
  const repos = useMemo(() => allRepos && allRepos.filter((r) => !r.demo), [allRepos]);
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
            <div className="flex items-center gap-s1">
              <Logo className="text-2xl" />
            </div>
            {repos !== null && repos.length > 0 && <h1 className={title}>Start a session</h1>}
            <p className="text-sm text-ink-2">
              {repos === null
                ? "Loading…"
                : repos.length === 0
                  ? "No repo is set up yet."
                  : live === 0
                    ? "Nothing is running yet. Describe what brindle should work on below, check the project, and press Start: a supervisor plans the work and hands it to agents. Recent sessions are in the sidebar."
                    : "Start another session below. The ones running are in the sidebar."}
            </p>
          </div>
          {repos === null ? null : repos.length === 0 ? (
            <FirstRepo client={client} pickFolder={pickFolder} onPicked={added} />
          ) : (
            <Composer client={client} repos={repos} sessions={sessions} reload={reload} refresh={refresh} onOpen={onOpen} chosen={chosen} onChoose={setChosen} pickFolder={pickFolder} onAdded={added} onSignIn={onSignIn} />
          )}
          <WatchDemo client={client} refresh={refresh} onStarted={onOpen} />
        </div>
      </div>
    </section>
  );
}

/* Nothing to show yet: a first project, opened or cloned. */
function FirstRepo({ client, pickFolder, onPicked }: { client: DaemonClient; pickFolder?: PickFolder; onPicked(repo: Repo): void }) {
  return (
    <section className={`${card} flex flex-col gap-s2 px-s3 py-s3`} aria-label="Welcome" data-walkthrough="project-picker">
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
function Composer({ client, repos, sessions, reload, refresh, onOpen, chosen, onChoose, pickFolder, onAdded, onSignIn }: {
  client: DaemonClient;
  /* To Settings › Providers, where a CLI is signed in. */
  onSignIn(): void;
  repos: Repo[];
  sessions: Record<string, SessionDTO[]>;
  reload(): Promise<void>;
  refresh?: () => Promise<void>;
  onOpen(root: string, repo: string): void;
  chosen?: string;
  onChoose(repo: string): void;
  pickFolder?: PickFolder;
  onAdded(repo: Repo): void;
}) {
  const repo = repos.some((r) => r.repo_root === chosen) ? chosen! : (repos[0]?.repo_root ?? "");
  const [adding, setAdding] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [options, setOptions] = useState(false);
  const [branch, setBranch] = useState("");
  const [sessionName, setSessionName] = useState("");
  const [provider, setProvider] = useState(readPick);
  // The CLIs a new session in a repo can run on, tied to the repo they were read for.
  const [got, setGot] = useState<{ repo: string; usable: string[] | null } | null>(null);
  const [lookups, setLookups] = useState(0);
  // Null while this repo's list loads, and if it couldn't be read: nothing starts then.
  const usable = got?.repo === repo ? got.usable : null;
  const lookupFailed = got?.repo === repo && got.usable === null;
  const [busy, setBusy] = useState(false);
  // A start under way, from the click until its session opens (or it ends without one):
  // said at once, as a status line, so a slow start never looks like a frozen screen.
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  // A start waiting on the person's choice: what they asked for, and the choices.
  const [pending, setPending] = useState<{ fields: StartFields; conflict: SessionConflict } | null>(null);
  // A start the engine refused until the CLI is set up: what they asked for, and the items.
  // `attempt`: the start it answers. A card's re-check that answers after a newer start (on
  // another CLI, say) began is that older start's, and is dropped.
  const [preflight, setPreflight] = useState<{ fields: StartFields; items: PreflightDTO; attempt: number } | null>(null);
  const attempts = useRef(0);
  const currentRepo = useRef(repo);
  currentRepo.current = repo;
  const [identity, setIdentity] = useState<{ repo: string; fields: StartFields; choice?: { key: string; session: string } } | null>(null);
  const [gitName, setGitName] = useState("");
  const [gitEmail, setGitEmail] = useState("");

  useEffect(() => {
    setIdentity(null);
    setGitName("");
    setGitEmail("");
  }, [repo]);

  useEffect(() => {
    let live = true;
    // A lookup begins: whatever an earlier one answered (this repo's too) no longer holds.
    setGot(null);
    client
      .usableProviders(repo)
      .then((r) => live && setGot({ repo, usable: r.usable }))
      .catch(() => live && setGot({ repo, usable: null }));
    return () => {
      live = false;
    };
  }, [client, repo, lookups]);

  // Several usable: the person's pick (none yet on a first session); one: that one, named; none: setup.
  const many = usable !== null && usable.length > 1;
  const chosenCli = many && usable.includes(provider) ? provider : "";
  const needsPick = many && !chosenCli;
  const pickCli = (p: string) => {
    setProvider(p);
    rememberPick(p);
  };

  const start = async (fields: StartFields, choice?: { key: string; session: string }): Promise<boolean> => {
    const attempt = ++attempts.current;
    setBusy(true);
    setStarting(true);
    setError(null);
    setNotes([]);
    let opened: string | null = null;
    try {
      const got = await client.startSession({ repo, ...fields, choice: choice?.key, session_id: choice?.session });
      if (currentRepo.current !== repo || attempt !== attempts.current) return false;
      setIdentity(null);
      setPending(null);
      setPreflight(null);
      if ("opened" in got) opened = got.opened.id;
      else {
        setNotes(got.notes);
        opened = got.id;
      }
    } catch (e) {
      if (currentRepo.current !== repo || attempt !== attempts.current) return false;
      if (e instanceof DaemonError && e.code === "git_identity_missing") {
        setIdentity({ repo, fields, choice });
        return false;
      }
      if (e instanceof DaemonError && e.preflight) setPreflight({ fields, items: e.preflight, attempt });
      // No card of its own: a card still showing stays usable, as this (now latest) start's,
      // so its re-checks apply again; any asked before this start began are still dropped.
      else setPreflight((cur) => cur && { ...cur, attempt });
      if (e instanceof DaemonError && e.conflict) setPending({ fields, conflict: e.conflict });
      else if (!(e instanceof DaemonError && e.preflight)) setError(message(e));
    } finally {
      setBusy(false);
      // Started, the snapshot too: one read on the new chat's status event can predate its window
      // ("exited", no window), and nothing on the bus says when the window lands.
      await Promise.all([reload().catch((e: unknown) => setError(message(e))), opened ? refresh?.() : undefined]);
      // Only this start's own end: a newer one still under way keeps its line.
      if (attempt === attempts.current) setStarting(false);
    }
    // Opened once the list and the snapshot have it: a root neither knows yet reads as paused.
    if (opened) onOpen(opened, repo);
    return opened !== null;
  };

  const saveIdentity = async (e: FormEvent) => {
    e.preventDefault();
    if (!identity || busy) return;
    const mine = identity;
    const attempt = attempts.current;
    setBusy(true);
    setError(null);
    try {
      await client.setRepoGitIdentity(mine.repo, gitName, gitEmail);
      if (currentRepo.current !== mine.repo || attempt !== attempts.current) return;
      setIdentity(null);
      await start(mine.fields, mine.choice);
    } catch (e) {
      if (currentRepo.current === mine.repo && attempt === attempts.current) setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    // The same guard as Start's: ⌘↵ in the prompt comes here too.
    if (busy || !repo || usable === null || needsPick) return;
    const ok = await start({
      autopilot: true,
      prompt: prompt.trim() || undefined,
      branch: branch.trim() || undefined,
      provider: chosenCli || undefined,
      name: sessionName.trim() || undefined,
    });
    if (ok) {
      setPrompt("");
      setBranch("");
      setSessionName("");
    }
  };

  const running = (sessions[repo] ?? []).filter((s) => !isPaused(s)).length;
  if (usable?.length === 0) {
    return (
      <section className={`${card} flex flex-col gap-s2 px-s2 py-s3`} aria-label="Finish setting up">
        <h1 className={title}>Finish setting up</h1>
        <p className={`${lede} max-w-prose`}>
          brindle runs your sessions on a coding CLI. Sign in to one (Claude Code, Codex, Antigravity or OpenCode) and you can start.
        </p>
        <div>
          <button type="button" className={main} onClick={onSignIn}>
            Sign in to a CLI
          </button>
        </div>
      </section>
    );
  }
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
      <div className="flex items-start gap-s1">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-s1">
        <span className="inline-flex items-center gap-s1" data-walkthrough="project-picker">
          {repos.length > 1 ? (
            <ProjectCombobox repos={repos} value={repo} onChange={onChoose} />
          ) : (
            <span className="text-sm text-ink-2">
              in <span className="font-medium text-ink">{repos[0]?.name}</span>
            </span>
          )}
        </span>
        {usable && usable.length === 1 && (
          <span className="text-sm text-ink-2">
            on <span className="font-medium text-ink">{providerName(usable[0])}</span>
          </span>
        )}
        {many && (
          <>
            <span className="text-sm text-ink-2">on</span>
            <select className={field} aria-label="Supervisor CLI" value={chosenCli} onChange={(e) => pickCli(e.target.value)}>
              <option value="" disabled>
                Pick a CLI
              </option>
              {usable.map((p) => (
                <option key={p} value={p}>
                  {providerName(p)}
                </option>
              ))}
            </select>
          </>
        )}
        {usable && !LOGIN_PROVIDERS.every((p) => usable.includes(p)) && (
          <button type="button" className={quiet} onClick={onSignIn}>
            Sign in to more…
          </button>
        )}
        <button type="button" className={quiet} aria-expanded={adding} onClick={() => setAdding((a) => !a)}>
          Add a project…
        </button>
        {running > 0 && <span className="text-xs text-ink-3">{running === 1 ? "one session already runs here" : `${running} sessions already run here`}</span>}
        <button type="button" className={quiet} aria-expanded={options} onClick={() => setOptions((o) => !o)}>
          Options {options ? "▴" : "▾"}
        </button>
        </div>
        <div className="flex shrink-0 items-center gap-s1">
          <span className="text-xs text-ink-3">⌘↵</span>
          <LoadingButton type="submit" busy={busy && !identity} disabled={busy || !repo || usable === null || needsPick} className={main}>
            Start
          </LoadingButton>
        </div>
      </div>
      {lookupFailed && (
        <p role="alert" className={`${alert} flex items-center gap-s2`}>
          <span>Couldn't check which CLIs can run here.</span>
          <button type="button" className={button} onClick={() => setLookups((n) => n + 1)}>
            Try again
          </button>
        </p>
      )}
      {needsPick && (
        <div className="mt-s1 flex flex-col gap-s1 border-t border-line pt-s2">
          <div className={h2}>Which CLI should run your sessions?</div>
          <p className={lede}>You're signed in to {usable.length === 2 ? "two" : usable.length}. brindle remembers the one you pick; change it here any time.</p>
          <div className="flex gap-s1 pt-1">
            {usable.map((p) => (
              <button key={p} type="button" className={button} onClick={() => pickCli(p)}>
                {providerName(p)}
              </button>
            ))}
          </div>
        </div>
      )}
      {options && (
        <div className="flex flex-wrap items-center gap-s1 border-t border-line pt-s1">
          <input className={`${field} w-48`} placeholder="Session name (optional)" aria-label="Session name" value={sessionName} onChange={(e) => setSessionName(e.target.value)} />
          <input className={`${field} w-48 font-mono`} placeholder="Branch (its own worktree)" aria-label="Branch" value={branch} onChange={(e) => setBranch(e.target.value)} />
        </div>
      )}
      {pending && <ConflictChoice client={client} repo={repo} conflict={pending.conflict} onChoose={(key, session) => start(pending.fields, { key, session })} onCancel={() => setPending(null)} />}
      {starting && <Starting />}
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
    {identity?.repo === repo && (
      <section aria-label="Git identity" className={`${card} px-s2 py-s2`}>
        <p className={lede}>What name and email should Git put on your commits? Saved for this repo.</p>
        <form onSubmit={saveIdentity} className="flex flex-col gap-s1">
          <label>Name<input className={field} aria-label="Name" required value={gitName} onChange={(e) => setGitName(e.target.value)} /></label>
          <label>Email<input className={field} aria-label="Email" type="email" required value={gitEmail} onChange={(e) => setGitEmail(e.target.value)} /></label>
          <div className="flex gap-s1">
            <LoadingButton type="submit" busy={busy} className={button} disabled={busy || !gitName.trim() || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(gitEmail.trim())}>Save and start</LoadingButton>
            <button type="button" className={button} disabled={busy} onClick={() => setIdentity(null)}>Cancel</button>
          </div>
        </form>
      </section>
    )}
    {/* Its own forms (a key's field) can't sit inside the composer's. */}
    {preflight && (
      <Preflight
        preflight={preflight.items}
        onRefresh={(items) => {
          const mine = preflight.attempt;
          // Only while this card's start is still the latest one, and its card still shows.
          if (mine === attempts.current) setPreflight((cur) => (cur?.attempt === mine ? { ...cur, items } : cur));
        }}
        onLaunch={() => void start(preflight.fields)}
        onUseInstead={(other) => {
          // Only a CLI the current repo's lookup says can run.
          if (!usable?.includes(other)) return;
          if (many) setProvider(other);
          void start({ ...preflight.fields, provider: other });
        }}
        usable={usable ?? []}
        launchLabel="Start"
        repo={repo}
        launching={busy}
      />
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

/* Shown from the click on: the start is on its way, and for how long so far. */
function Starting() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const began = Date.now();
    const tick = setInterval(() => setSeconds(Math.floor((Date.now() - began) / 1000)), 1000);
    return () => clearInterval(tick);
  }, []);
  return (
    <p role="status" className="text-sm text-ink-2">
      Starting the session…{seconds >= 2 ? ` ${seconds}s` : ""}
    </p>
  );
}
