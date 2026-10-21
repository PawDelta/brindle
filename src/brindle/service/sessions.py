"""A repo's supervisor sessions: start, continue, pause, hand over and list
them, plus the workspace and agent actions next to them (ls, close, send,
merge, rm). The CLI and the desktop app both call these.

Nothing here prints or asks. Where the CLI asks the person (which running
session to open, which paused one to resume), the function returns a
``Conflict`` listing the choices, and the caller asks and calls again with
the answer. What the CLI prints along the way comes back as ``Note``s, in
order."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from brindle import agents, git, scratch, tmux, view, workspaces
from brindle import sessions as sessions_mod
from brindle.db import DB, Agent, Workspace
from brindle.service.dto import SessionDTO
from brindle.service.errors import ServiceError

RUNNING_CHOICES = {"o": "open", "n": "new session in its own worktree", "p": "pause it and start fresh"}
PAUSED_CHOICES = {"r": "resume it"}
NO_PAUSED = "no paused sessions here. Run `brindle` to start a fresh one."


@dataclass(frozen=True)
class Note:
    text: str
    tone: str = ""                # "", "ok" or "warn"


@dataclass
class Conflict:
    """The person has to choose before anything happens: ``sessions`` are the
    candidates (LiveSession for "running", sessions.Session for "paused"),
    newest first, and ``choices`` what can be done with the one picked."""
    kind: str
    message: str
    choices: dict[str, str]
    sessions: list
    here: str | None = None       # the caller's workspace id: its session is the default

    @property
    def default(self) -> int:
        return next((i for i, s in enumerate(self.sessions) if s.workspace.id == self.here), 0)

    def to_json(self) -> dict[str, Any]:
        return {"conflict": {
            "kind": self.kind, "message": self.message, "default": self.default,
            "choices": [{"key": k, "label": label} for k, label in self.choices.items()],
            "sessions": [SessionDTO.from_agent(s.root, getattr(s, "goal", None) or None).to_json()
                         for s in self.sessions],
        }}


@dataclass
class Placed:
    workspace: Workspace          # where the new session runs
    notes: list[Note] = field(default_factory=list)


@dataclass
class Opened:
    """Open this running session instead of starting one."""
    workspace: Workspace
    root: Agent


@dataclass
class Started:
    agent: Agent
    workspace: Workspace
    autopilot: bool
    notes: list[Note] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return SessionDTO.from_agent(self.agent).to_json()


@dataclass
class Continued:
    """``state``: "recovered" (a live session's failed agents restarted),
    "running" (nothing paused; ``root`` is still running) or "resumed"."""
    state: str
    root: Agent
    workspace: Workspace
    restarted: int = 0
    session: sessions_mod.Session | None = None
    others: list[sessions_mod.Session] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"state": self.state, "restarted": self.restarted,
                "session": SessionDTO.from_agent(self.root).to_json()}


@dataclass
class Paused:
    session: sessions_mod.Session
    disk: int                     # bytes its members' worktrees use

    def to_json(self) -> dict[str, Any]:
        return {**SessionDTO.from_agent(self.session.root).to_json(),
                "branches": self.session.branches, "disk": self.disk}


@dataclass
class Handed:
    old_id: str
    agent: Agent
    workspace: Workspace


def ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def describe(s: sessions_mod.Session) -> str:
    workers = len(s.members) - 1
    since = ago(time.time() - s.paused_at)
    what = f", {workers} worker(s)" if workers else ""
    branches = f": {', '.join(s.branches[:3])}" + (" …" if len(s.branches) > 3 else "") if s.branches else ""
    return f"{s.root.id}  paused {since} ago{what}{branches}"


def live_here(db: DB, ws: Workspace) -> list[Agent]:
    """The live sessions (interactive agents) in this checkout."""
    return [s.root for s in sessions_mod.live(db, ws.repo_root) if s.workspace.id == ws.id]


def running_here(db: DB, ws: Workspace) -> Agent | None:
    """The live session (its interactive agent) in this checkout, if any."""
    live = live_here(db, ws)
    return live[0] if live else None


def cull_detached(repo_root: str | None = None) -> None:
    """Clean up leftover agent processes and stale workers (and, given
    ``repo_root``, apply its paused-session retention) without making the
    person wait for it (see brindle.cull, brindle.sessions.enforce)."""
    from brindle.providers import brindle_invocation

    repo = ["--repo", repo_root] if repo_root else []
    subprocess.Popen([*brindle_invocation(), "_cull", *repo], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# -- start -------------------------------------------------------------------


def session_worktree(db: DB, ws: Workspace) -> Workspace:
    """A worktree for a second session in this repo: branch brindle/session-N,
    cut from what this checkout has checked out, so the two sessions never
    share files or a branch."""
    n = 2
    while git.ok(["rev-parse", "--verify", "--quiet", f"refs/heads/brindle/session-{n}"], ws.repo_root):
        n += 1
    created = workspaces.create(db, ws.repo_root, f"brindle/session-{n}", base=ws.branch or None,
                             start=git.out(["rev-parse", "HEAD"], ws.path), apply_prefix=False).workspace
    from brindle import codemap
    codemap.seed_session(created.path, created.repo_root)
    return created


def place(db: DB, ws: Workspace, choice: str | None = None, session_id: str | None = None, *,
          live: list | None = None) -> Placed | Opened | Conflict:
    """Where a new session in ``ws``'s checkout runs, given the repo's live
    sessions (in any worktree). With some running and no ``choice``, a
    Conflict: open one ("o"), start in a new worktree ("n"), or pause one and
    start fresh here ("p"). ``live`` is the Conflict's list, to skip asking
    tmux again."""
    live = sessions_mod.live(db, ws.repo_root) if live is None else live
    if not live:
        return Placed(ws)
    if choice is None:
        return Conflict("running", "Sessions running in this repo:", RUNNING_CHOICES, live, here=ws.id)
    picked = next((s for s in live if s.root.id == session_id), None)
    if choice not in RUNNING_CHOICES or picked is None:
        raise ServiceError("invalid", f"no running session {session_id!r} to {RUNNING_CHOICES.get(choice, choice)}")
    if choice == "o":
        return Opened(picked.workspace, picked.root)
    if choice == "n":
        created = session_worktree(db, ws)
        return Placed(created, [Note(f"✓ new session in its own worktree: {created.path} ({created.branch})")])
    notes = []
    if picked.workspace.id != ws.id:
        agents.pause(db, picked.root.id, stop_procs=False, stop_local_models=False)
        notes.append(Note(f"Paused session {picked.root.id}; `brindle continue {picked.root.id}` brings it back."))
    return Placed(ws, notes)


def tidy_branch(db: DB, ws: Workspace) -> tuple[Workspace, list[Note]]:
    """Before a session starts in a checkout: off a branch that's already
    merged (when nothing is uncommitted), and the default branch fast-forwarded."""
    notes: list[Note] = []
    if scratch.is_scratch(ws.path):
        return ws, notes
    curr_b = git.current_branch(ws.path)
    default_b = git.default_branch(ws.repo_root) if curr_b else None
    if curr_b and git.merged_into_default(ws.repo_root, curr_b):
        notes.append(Note(f"! Branch '{curr_b}' is already merged into '{default_b}'.", "warn"))
        try:
            if not git.dirty_files(ws.path):
                git.run(["checkout", default_b], ws.path)
                ws = workspaces.refresh_branch(db, ws)
                curr_b = default_b
                notes.append(Note(f"✓ Switched to '{default_b}'.", "ok"))
        except Exception:
            pass
    elif curr_b != default_b and git.upstream_gone(ws.path, curr_b):
        # (the default branch is left out: a network probe for the common case)
        notes.append(Note(f"! Branch '{curr_b}' was deleted on origin but has unmerged changes.", "warn"))
    try:
        if curr_b and curr_b == default_b:
            if warning := git.fast_forward(ws.path, curr_b):
                notes.append(Note(f"! {warning}", "warn"))
    except git.GitError:
        pass
    return ws, notes


def preflight(agent: str, provider: str | None, repo_root: str) -> list[str]:
    """Why the chat can't start (no tmux, no CLI), if it can't."""
    from brindle import doctor as doctor_mod
    from brindle.profiles import load_profile

    return doctor_mod.preflight(provider or load_profile(agent, repo_root).provider)


def pause(db: DB, ws: Workspace, stop_procs: bool = True) -> list[Note]:
    """At most one live session per checkout: pause any that's still running.
    ``stop_procs=False`` when a detached cull follows (see agents.pause)."""
    live = live_here(db, ws)
    caller = os.environ.get("BRINDLE_AGENT_ID")
    if caller and any(a.id == caller for a in live):
        # Pausing kills the session's process tree, this command with it.
        raise ServiceError("conflict", "this command would stop the session running it; "
                           "run it from a terminal instead", 409)
    notes = []
    for a in live:
        # A session starts here next: keep its local models loaded.
        agents.pause(db, a.id, stop_procs=stop_procs, stop_local_models=False)
        notes.append(Note(f"Paused the session that was still running here ({a.id}); "
                          f"`brindle continue {a.id}` brings it back."))
    # Chats left running in scratch sessions whose work moved into this repo.
    for aid in scratch.pause_transferred_to(db, ws.repo_root):
        notes.append(Note(f"Paused scratch session chat {aid}: its work moved into this repo; "
                          f"`brindle continue {aid}` brings it back."))
    return notes


def launch(db: DB, ws: Workspace, *, provider: str | None = None, prompt: str | None = None,
           watch: bool = True, autopilot: bool | None = None) -> Started:
    """Start the supervisor chat in ``ws`` (autopilot: the repo's default when None)."""
    from brindle.config import load_repo_config

    if autopilot is None:
        autopilot = load_repo_config(ws.repo_root).autopilot
    a = agents.spawn(db, ws, agents.SUPERVISOR, prompt=prompt, provider_name=provider,
                     watch_pane=watch, background_setup=True, autopilot=autopilot)
    return Started(a, ws, bool(autopilot))


def start(db: DB, repo_root: str, *, provider: str | None = None, prompt: str | None = None,
          watch: bool = True, autopilot: bool | None = None, branch: str | None = None,
          worktree: str | None = None, choice: str | None = None,
          session_id: str | None = None) -> Started | Opened | Conflict:
    """A fresh supervisor session in ``repo_root``'s checkout, or (with
    ``branch``/``worktree``) in that worktree. With a session already running
    in the repo, a Conflict until ``choice`` and ``session_id`` say what to do
    with it (see place)."""
    notes: list[Note] = []
    if branch or worktree:
        ws = workspaces.checkout_for(db, repo_root, branch=branch, worktree=worktree)
    else:
        got = place(db, workspaces.adopt_root(db, repo_root), choice, session_id)
        if not isinstance(got, Placed):
            return got
        ws, more = tidy_branch(db, got.workspace)
        notes = got.notes + more
    problems = preflight(agents.SUPERVISOR, provider, ws.repo_root)
    if problems:
        raise ServiceError("unavailable", "; ".join(problems), 503)
    notes += pause(db, ws, stop_procs=False)
    started = launch(db, ws, provider=provider, prompt=prompt, watch=watch, autopilot=autopilot)
    # After the chat's window exists and is recorded: the cull's session
    # retention closes dropped sessions' windows by their stored pane ids,
    # which a freshly started tmux server hands out again from %0.
    cull_detached(ws.repo_root)
    started.notes = notes
    return started


# -- continue ----------------------------------------------------------------


def continue_workspace(db: DB, cwd: str) -> Workspace:
    """The checkout (or, outside git, the scratch session) ``continue`` works in."""
    try:
        git.main_repo_root(cwd)
    except git.GitError:
        ws = scratch.for_origin(db, cwd)
        if ws is None:
            raise ServiceError("not_found", "no scratch session started from this folder to continue; "
                               "run `brindle` to start one", 404)
        return ws
    return workspaces.adopt_root(db, cwd)


def clear_credentials_block(db: DB, root_id: str) -> bool:
    ap = db.get_autopilot(root_id)
    if not ap or ap.state != "blocked":
        return False
    note = (ap.note or "").lower()
    if "credential" not in note and "sign in" not in note and "api key" not in note and "auth" not in note:
        return False
    tree = agents.tree(db, root_id)
    failed = [a for a in tree if a.status == "failed"]
    if failed:
        return False
    db.update_autopilot(root_id, state="running", note=None)
    return True


def continue_(db: DB, ws: Workspace, session_id: str | None = None, *,
              ask: bool = False) -> Continued | Conflict:
    """Pick up a paused session in ``ws``'s repo (``session_id``: one whose id
    starts with it; else the newest, or with ``ask`` and several paused, a
    Conflict listing them). A live session there with failed agents (or the
    one ``session_id`` names) gets those restarted instead."""
    from brindle import keystore, secrets

    live = agents.find_running(db, ws, "supervisor") or next(
        (a for a in reversed(db.list_agents(ws.id)) if a.mode == "interactive" and (agents.is_alive(a) or a.status == "failed")),
        None,
    )
    live_needs_recovery = False
    if live:
        workers = agents.tree(db, live.id)
        failed_workers = [a for a in workers if a.status == "failed"]
        ap = db.get_autopilot(live.id)
        is_blocked = ap and ap.state == "blocked"
        if failed_workers or is_blocked or live.status == "failed":
            live_needs_recovery = True

    recovery_env = {k: os.environ[k] for k in keystore.known_names() if k in os.environ and os.environ[k] and not secrets.is_job_secret(k)}

    if live and (live.id.startswith(session_id) if session_id else live_needs_recovery):
        resumed = agents.resume(db, live.id, recovery_env=recovery_env)
        clear_credentials_block(db, live.id)
        return Continued("recovered", db.get_agent(live.id) or live, ws, len(resumed))

    available = sessions_mod.paused(db, ws.repo_root)
    if not available:
        if live:
            return Continued("running", live, ws)
        raise ServiceError("not_found", NO_PAUSED, 404)
    if session_id:
        matches = [s for s in available if s.root.id.startswith(session_id)]
        if len(matches) != 1:
            raise ServiceError("not_found", f"no single paused session matches {session_id!r}. Available:\n  "
                               + "\n  ".join(describe(s) for s in available), 404)
        chosen = matches[0]
    elif len(available) > 1 and ask:
        return Conflict("paused", "Paused sessions:", PAUSED_CHOICES, available)
    else:
        chosen = available[0]
    others = [s for s in available if s.root.id != chosen.root.id]
    notes = pause(db, ws)
    resumed = agents.resume(db, chosen.root.id, recovery_env=recovery_env)
    clear_credentials_block(db, chosen.root.id)
    return Continued("resumed", db.get_agent(chosen.root.id) or chosen.root, chosen.workspace,
                     len(resumed), chosen, others, notes)


# -- hand over and list --------------------------------------------------------


def session_root(db: DB, cwd: str) -> str:
    """This repo's current session: the one running here, else the newest
    paused one."""
    try:
        ws = workspaces.adopt_root(db, cwd)
    except git.GitError:
        ws = scratch.for_origin(db, cwd) or workspaces.current(db)
        if ws is None:
            raise ServiceError("not_found", "no brindle session here", 404)
    live = agents.find_running(db, ws, "supervisor")
    if live:
        return live.id
    found = sessions_mod.paused(db, ws.repo_root)
    if found:
        return found[0].root.id
    raise ServiceError("not_found", "no brindle session here. Run `brindle` to start one.", 404)


def handover(db: DB, cwd: str, to: str, note: str | None = None) -> Handed:
    """Hand this repo's session to a new supervisor on the branch or worktree ``to``."""
    root_id = session_root(db, cwd)
    dest = workspaces.checkout_for_target(db, cwd, to)
    new = sessions_mod.handover(db, root_id, dest, note)
    cull_detached(dest.repo_root)
    return Handed(root_id, new, dest)


def transfer_to_repo(db: DB, ws: Workspace, target_path: str, branch: str | None = None,
                     keep_running: str | None = None) -> scratch.Transferred:
    """Move a scratch session's work onto a new branch of the repository at
    ``target_path``. ``keep_running``: a chat to leave up so it can deliver the
    reply (the next session started in the target pauses it)."""
    if not scratch.is_scratch(ws.path):
        raise ServiceError("invalid", "This isn't a scratch session; the work is already in a real repository.")
    try:
        return scratch.transfer(db, ws, target_path, branch, keep_running=keep_running)
    except (scratch.ScratchError, git.GitError, workspaces.WorkspaceError) as e:
        raise ServiceError("conflict", str(e), 409) from e


def list_(db: DB, repo_root: str) -> list[Paused]:
    """Paused sessions in ``repo_root``, newest first, with the disk their worktrees use."""
    out = []
    for s in sessions_mod.paused(db, repo_root):
        # Members can share a workspace (a worker and its reviewer): count each once.
        spaces = {m.workspace_id: db.get_workspace(m.workspace_id) for m in s.members[1:]}
        size = sum(sessions_mod.disk_usage(w.path) for w in spaces.values()
                   if w and w.kind == "worktree" and os.path.isdir(w.path))
        out.append(Paused(s, size))
    return out


# -- workspaces and agents -------------------------------------------------------


def ls(db: DB, repo_root: str | None = None) -> list[tuple[Workspace, dict[str, Any]]]:
    """The sessions' workspaces (every repo's with no ``repo_root``), each
    with its view.workspace_entry."""
    panes = tmux.list_panes()
    return [(ws, view.workspace_entry(db, ws, panes=panes))
            for ws in view.session_workspaces(db, repo_root)]


def close(db: DB, agent_id: str) -> tuple[Agent, bool]:
    """Hide an agent from the dashboard for good; True when it had to be stopped first."""
    panes = tmux.list_panes()
    target = agents.get(db, agent_id)
    was_running = agents.is_alive(target, panes) and agents.owns_pane(db, target)
    return agents.close(db, agent_id, panes), was_running


def close_session(db: DB, root_id: str) -> list[tuple[Agent, bool]]:
    """Close a session's supervisor and every agent under it, as ``close`` does
    one (branches and worktrees stay). Each comes back with whether it had to
    be stopped; agents already closed are skipped, so closing twice is a no-op."""
    root = agents.get(db, root_id)
    return [close(db, a.id) for a in agents.tree(db, root.id) if a.dismissed_at is None]


def close_exited(db: DB, repo_root: str | None = None) -> list[Agent]:
    """Close every agent (in ``repo_root``, else every repo) that has stopped or finished."""
    panes = tmux.list_panes()
    alive = view.live_agents(db, panes)
    closed = [a for ws in db.find_workspaces(repo_root) for a in db.list_agents(ws.id)
              if a.dismissed_at is None and (a.id not in alive or a.status == "done")]
    for a in closed:
        agents.close(db, a.id, panes)
    return closed


def send(db: DB, agent_id: str, message: str) -> str:
    """Send the person's message to an agent (queued until it's idle)."""
    return agents.send_message(db, agent_id, message, person=True)


def merge(db: DB, ws: Workspace, squash: bool = False) -> str:
    """Merge the workspace branch into its base branch locally."""
    return workspaces.merge_back(db, ws, squash)


def unpushed_note(ws: Workspace, delete_branch: bool | None = None) -> str | None:
    """Before ``remove``: the branch has commits that are in neither its base nor its remote."""
    if not ws.base_branch or not os.path.isdir(ws.path) or delete_branch:
        return None
    try:
        st = git.status(ws.path, ws.base_branch)
    except git.GitError:
        return None  # e.g. the base branch is gone; the note is only a courtesy
    if st.ahead and st.unpushed != 0:
        return f"note: {ws.branch} has {st.ahead} commit(s) not in {ws.base_branch} and not pushed; the branch is kept."
    return None


def remove(db: DB, ws: Workspace, *, force: bool = False,
           delete_branch: bool | None = None) -> workspaces.Removed:
    """Stop a workspace's agents and remove its worktree (see workspaces.remove)."""
    return workspaces.remove(db, ws, force=force, delete_branch=delete_branch)
