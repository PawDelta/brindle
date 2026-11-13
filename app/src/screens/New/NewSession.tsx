/* The calm empty state, and the New session composer: where the app lands
   with no session to show, and where "New session" goes. With no project
   yet, the first run's hero instead (FirstRun: the practice run first, then
   open a folder or clone). Otherwise one composer: what brindle should work
   on, a toolbar under it with the project chip (its menu: the projects with
   their running and paused counts, then Open a folder… and Clone a
   repository…), the CLI chip (the signed-in CLIs, then Sign in to more…),
   Options (session name and branch, opening inside the composer) and
   Send ⌘↵ (SendButton); the practice run's row at the foot. With a session
   already running (or paused) in the repo, Send starts on a new branch in
   its own worktree at once, without asking; the
   chevron inside Send offers pausing it instead, or opening (resuming) a
   session there; a start the engine refuses
   with 409 {preflight} (the CLI needs a sign-in, a key, a trust or a
   consent first) shows the engine's items as native UI (Preflight) and
   starts again once they are settled. Started, the app opens the new
   session's provider session. A project is added as the first launch adds
   one (components/ProjectPicker, its rows: open a folder, or clone a
   repository): in the first run's hero while there is none, and from the
   project menu after. */

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { DaemonError, LOGIN_PROVIDERS, type DaemonClient, type Note, type PreflightDTO, type Repo, type StartSession } from "../../api/client";
import type { PickFolder } from "../../api/open";
import type { SessionDTO } from "../../api/schema";
import { ChipMenu, SEPARATOR } from "../../components/ChipMenu";
import { BranchIcon, FolderIcon, LinkIcon, PlusIcon, SlidersIcon } from "../../components/icons";
import { LoadingButton } from "../../components/LoadingButton";
import { Preflight } from "../../components/Preflight";
import { ProjectPicker } from "../../components/ProjectPicker";
import { Dot } from "../../components/Sidebar";
import { alert, button, card, field, h2, label, lede, main, quiet, title } from "../../components/ui";
import { isPaused, providerName } from "../../store/overview";
import { useSessions } from "../../store/sessions";
import { PracticeRow } from "../Onboarding/Demo";
import { FirstRun } from "./FirstRun";
import { SendButton, sessionLabel, type SendHow } from "./SendButton";

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
  /* Arrived to add a project (a finished practice run's Start your own): the folder picker, or the clone form. */
  add?: "open" | "clone";
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

export function NewSession({ client, onOpen, initialRepo, pickFolder, refresh, onSignIn = () => undefined, add }: NewSessionProps) {
  const { repos: allRepos, sessions, error: loadError, reload } = useSessions();
  /* The practice repo is for the demo flow only: never a project to start in. */
  const repos = useMemo(() => allRepos && allRepos.filter((r) => !r.demo), [allRepos]);
  const [attempt, setAttempt] = useState(0);
  /* The project to start in: as opened, or the one just added. */
  const [chosen, setChosen] = useState(initialRepo);
  /* The way of adding a project this screen was opened for, until a project is added: the
     composer that mounts once there is one must not open the picker or the clone form again. */
  const [intent, setIntent] = useState(add);
  const added = (repo: Repo) => {
    setIntent(undefined);
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

  return (
    <section className="relative flex min-h-0 flex-1" aria-label="New session">
      <div className="flex min-w-0 flex-1 flex-col overflow-auto px-s4">
        {repos === null ? (
          <p className="mx-auto w-full max-w-[672px] pt-s8 text-sm text-ink-3">Loading…</p>
        ) : repos.length === 0 ? (
          <div data-walkthrough="project-picker">
            <FirstRun client={client} pickFolder={pickFolder} refresh={refresh} onStarted={onOpen} onPicked={added} start={intent} />
          </div>
        ) : (
          <div className="mx-auto flex min-h-full w-full max-w-[672px] flex-col pb-7 pt-s8">
            <h1 className={title}>Start a session</h1>
            <p className={`${lede} mt-1.5`}>Say what you want done. The supervisor plans it and hands out the work.</p>
            <Composer client={client} repos={repos} sessions={sessions} reload={reload} refresh={refresh} onOpen={onOpen} chosen={chosen} onChoose={setChosen} pickFolder={pickFolder} onAdded={added} onSignIn={onSignIn} add={intent} />
            <div className="mt-auto pt-s4">
              <PracticeRow client={client} refresh={refresh} onStarted={onOpen} />
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

/* Running and paused sessions in a repo, as the project menu says them. */
function counts(sessions: SessionDTO[] | undefined) {
  const all = sessions ?? [];
  const running = all.filter((s) => !isPaused(s)).length;
  return { running, paused: all.length - running };
}

/* One composer for every repo: the goal on top, a toolbar under it (the
   project chip, the CLI chip, Options, Start), name and branch behind
   Options inside it. A start that conflicts, or that the engine's
   preflight refuses, asks here what to do. */
function Composer({ client, repos, sessions, reload, refresh, onOpen, chosen, onChoose, pickFolder, onAdded, onSignIn, add }: {
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
  add?: "open" | "clone";
}) {
  const repo = repos.some((r) => r.repo_root === chosen) ? chosen! : (repos[0]?.repo_root ?? "");
  // A project being added from the project menu: how, and which ask (each one opens afresh).
  const [adding, setAdding] = useState<{ how: "open" | "clone"; ask: number } | null>(add ? { how: add, ask: 0 } : null);
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
  // A start the engine refused until the CLI is set up: what they asked for, and the items.
  // `attempt`: the start it answers. A card's re-check that answers after a newer start (on
  // another CLI, say) began is that older start's, and is dropped.
  const [preflight, setPreflight] = useState<{ fields: StartFields; how: SendHow; items: PreflightDTO; attempt: number } | null>(null);
  const attempts = useRef(0);
  const currentRepo = useRef(repo);
  currentRepo.current = repo;
  const [identity, setIdentity] = useState<{ repo: string; fields: StartFields; how: SendHow } | null>(null);
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

  /* A start with a session already here (running or paused) never asks.
     Send's way ("n") asks for a new branch in its own worktree outright; a
     conflict from sessions this screen didn't know of yet is answered at
     once with `how` ("n", or "p": pause it and start here) for the session
     the daemon names as the default. */
  const post = async (fields: StartFields, how: SendHow) => {
    if (how === "n" && (sessions[repo] ?? []).length > 0) return client.startSession({ repo, ...fields, choice: "n" });
    try {
      return await client.startSession({ repo, ...fields });
    } catch (e) {
      const conflict = e instanceof DaemonError ? e.conflict : undefined;
      const target = conflict?.kind === "running" && conflict.choices.some((c) => c.key === how) ? (conflict.sessions[conflict.default] ?? conflict.sessions[0]) : undefined;
      if (!target) throw e;
      return client.startSession({ repo, ...fields, choice: how, session_id: target.id });
    }
  };

  const start = async (fields: StartFields, how: SendHow = "n"): Promise<boolean> => {
    const attempt = ++attempts.current;
    setBusy(true);
    setStarting(true);
    setError(null);
    setNotes([]);
    let opened: string | null = null;
    try {
      const got = await post(fields, how);
      if (currentRepo.current !== repo || attempt !== attempts.current) return false;
      setIdentity(null);
      setPreflight(null);
      if ("opened" in got) opened = got.opened.id;
      else {
        setNotes(got.notes);
        opened = got.id;
      }
    } catch (e) {
      if (currentRepo.current !== repo || attempt !== attempts.current) return false;
      if (e instanceof DaemonError && e.code === "git_identity_missing") {
        setIdentity({ repo, fields, how });
        return false;
      }
      if (e instanceof DaemonError && e.preflight) setPreflight({ fields, how, items: e.preflight, attempt });
      // No card of its own: a card still showing stays usable, as this (now latest) start's,
      // so its re-checks apply again; any asked before this start began are still dropped.
      else setPreflight((cur) => cur && { ...cur, attempt });
      if (!(e instanceof DaemonError && e.preflight)) setError(message(e));
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
      await start(mine.fields, mine.how);
    } catch (e) {
      if (currentRepo.current === mine.repo && attempt === attempts.current) setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const send = async (how: SendHow) => {
    // The same guard as Send's: ⌘↵ in the prompt and the chevron's menu come here too.
    if (busy || !repo || usable === null || needsPick) return;
    const ok = await start(
      {
        autopilot: true,
        prompt: prompt.trim() || undefined,
        // Pause it and start here means this checkout: a Branch given in Options would start elsewhere.
        branch: how === "p" ? undefined : branch.trim() || undefined,
        provider: chosenCli || undefined,
        name: sessionName.trim() || undefined,
      },
      how,
    );
    if (ok) {
      setPrompt("");
      setBranch("");
      setSessionName("");
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send("n");
  };

  // A paused session picked from Send's menu: resumed, then opened.
  const resume = async (session: SessionDTO) => {
    if (busy) return;
    const attempt = ++attempts.current;
    // Still this resume's project and the latest action: another project picked meanwhile keeps the screen.
    const current = () => currentRepo.current === repo && attempt === attempts.current;
    setBusy(true);
    setError(null);
    let resumed = false;
    try {
      await client.continueSession(session.id);
      resumed = true;
    } catch (e) {
      if (current()) setError(message(e));
    } finally {
      setBusy(false);
      await Promise.all([reload().catch((e: unknown) => current() && setError(message(e))), resumed ? refresh?.() : undefined]);
    }
    if (resumed && current()) onOpen(session.id, repo);
  };

  // The sessions running or paused in the project: Send's chevron and the hint beside the chips.
  const inRepo = sessions[repo] ?? [];
  const runningHere = inRepo.filter((s) => !isPaused(s));
  const running = runningHere.length;
  const pausedCount = inRepo.length - running;
  // Where Send goes, and what is already here.
  const hint = `sends on a new branch · ${
    running > 0
      ? running === 1
        ? `${sessionLabel(runningHere[0])} runs here`
        : `${running} sessions run here`
      : pausedCount === 1
        ? `${sessionLabel(inRepo[0])} is paused here`
        : `${pausedCount} paused sessions here`
  }`;
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
  const here = repos.find((r) => r.repo_root === repo);
  // The CLI a start runs on: the one usable, or the person's pick among several.
  const cli = usable?.length === 1 ? usable[0] : chosenCli;
  const missing = usable ? LOGIN_PROVIDERS.filter((p) => !usable.includes(p)) : [];
  const rest = (
    <>
      {lookupFailed && (
        <p role="alert" className={`${alert} flex items-center gap-s2`}>
          <span>Couldn't check which CLIs can run here.</span>
          <button type="button" className={button} onClick={() => setLookups((n) => n + 1)}>
            Try again
          </button>
        </p>
      )}
      {needsPick && (
        <div className="flex flex-col gap-s1">
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
    </>
  );
  return (
    <>
    <form aria-label="Start a session" onSubmit={submit} className={`mt-s3 flex flex-col rounded-md border border-line-strong bg-[color-mix(in_srgb,var(--surface-2)_70%,var(--canvas))]`}>
      <textarea
        className="field-sizing-content max-h-[50vh] min-h-[130px] w-full resize-none rounded-t-md bg-transparent px-4 py-3.5 text-[14.5px] leading-[1.55] text-ink placeholder:text-ink-3 focus:outline-none"
        aria-label="What should brindle work on?"
        placeholder="What should brindle work on? Describe the task…"
        value={prompt}
        rows={3}
        autoFocus
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e);
        }}
      />
      <div className="flex items-start gap-2 border-t border-line px-3 py-2.5" role="toolbar" aria-label="Session settings">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
        <span data-walkthrough="project-picker" className="inline-flex">
          <ChipMenu
            label="Project"
            value={here?.name ?? ""}
            chip={<><FolderIcon />{here?.name}</>}
            heading="Projects"
            items={[
              ...repos.map((r) => {
                const c = counts(sessions[r.repo_root]);
                const said = [c.running > 0 && `${c.running} running`, c.paused > 0 && `${c.paused} paused`].filter(Boolean).join(" · ");
                return { key: r.repo_root, label: r.name, checked: r.repo_root === repo, detail: said || undefined, onSelect: () => onChoose(r.repo_root) };
              }),
              SEPARATOR,
              { key: "open", label: "Open a folder…", icon: <FolderIcon />, onSelect: () => setAdding((a) => ({ how: "open", ask: (a?.ask ?? 0) + 1 })) },
              { key: "clone", label: "Clone a repository…", icon: <LinkIcon />, onSelect: () => setAdding((a) => ({ how: "clone", ask: (a?.ask ?? 0) + 1 })) },
            ]}
          />
        </span>
        <ChipMenu
          label="Supervisor CLI"
          value={usable === null ? "Checking…" : cli ? providerName(cli) : "Pick a CLI"}
          chip={usable === null ? <span className="text-ink-3">Checking…</span> : cli ? <><Dot tone="done" className="!size-1.5" />{providerName(cli)}</> : <span className="text-sap">Pick a CLI</span>}
          tone={needsPick ? "ask" : undefined}
          disabled={usable === null}
          heading="Supervisor runs on"
          width={330}
          items={[
            ...(usable ?? []).map((p) => ({ key: p, label: providerName(p), checked: p === cli, detail: p === provider ? "signed in · last used" : "signed in", onSelect: () => pickCli(p) })),
            ...(missing.length > 0 ? [SEPARATOR, { key: "more", label: "Sign in to more…", icon: <PlusIcon />, detail: missing.map(providerName).join(", "), onSelect: onSignIn }] : []),
          ]}
          footer="Workers and reviewers follow your profiles."
        />
        <button type="button" className={`${quiet} ${options ? "bg-ink/6 text-ink" : ""}`} aria-expanded={options} onClick={() => setOptions((o) => !o)}>
          <SlidersIcon />
          Options <span className="text-[10px] text-ink-3">{options ? "▴" : "▾"}</span>
        </button>
        </div>
        <SendButton
          client={client}
          repo={repo}
          project={here?.name ?? ""}
          sessions={inRepo}
          busy={busy && !identity}
          disabled={busy || !repo || usable === null || needsPick}
          onSend={(how) => void send(how)}
          onOpen={(s) => onOpen(s.id, repo)}
          onResume={(s) => void resume(s)}
        />
      </div>
      {inRepo.length > 0 && (
        <p className="-mt-1 flex min-w-0 items-center gap-1.5 px-4 pb-2.5 text-xs text-ink-3" data-testid="send-hint" title={hint}>
          <BranchIcon size={12} className="shrink-0 text-branch" />
          <span className="truncate">{hint}</span>
        </p>
      )}
      {options && (
        <div className="grid grid-cols-2 gap-x-5 gap-y-1.5 border-t border-line px-4 pb-3.5 pt-3" data-testid="start-options">
          <label className="flex flex-col gap-1.5 text-xs text-ink-2">
            Session name
            <input className={field} placeholder="Named from the goal" value={sessionName} onChange={(e) => setSessionName(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-ink-2">
            Branch
            <input className={`${field} font-mono`} placeholder="brindle picks one" value={branch} onChange={(e) => setBranch(e.target.value)} />
          </label>
          <span className="text-[11.5px] text-ink-3">Shown in the sidebar. Empty: named from the goal.</span>
          <span className="text-[11.5px] text-ink-3">Its own worktree. Empty: brindle picks one.</span>
        </div>
      )}
      <div className="flex flex-col gap-s1 border-t border-line px-4 py-3 empty:hidden">{rest}</div>
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
        onLaunch={() => void start(preflight.fields, preflight.how)}
        onUseInstead={(other) => {
          // Only a CLI the current repo's lookup says can run.
          if (!usable?.includes(other)) return;
          if (many) setProvider(other);
          void start({ ...preflight.fields, provider: other }, preflight.how);
        }}
        usable={usable ?? []}
        launchLabel="Start"
        repo={repo}
        launching={busy}
      />
    )}
    {adding && (
      <section className="mt-s3 flex flex-col gap-s1" aria-label="Add a project">
        <div className="flex items-center justify-between">
          <span className={label}>Add a project</span>
          <button type="button" className={quiet} onClick={() => setAdding(null)}>
            Close
          </button>
        </div>
        <ProjectPicker
          key={adding.ask}
          client={client}
          pickFolder={pickFolder}
          layout="rows"
          start={adding.how}
          onPicked={(picked) => {
            setAdding(null);
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
