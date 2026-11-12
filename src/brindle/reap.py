"""Reaping: what outlived its work, found and stopped by the periodic sweep.

``cull.sweep`` runs ``run`` every minute. It looks at every ``brindle_*``
tmux session on brindle's server that belongs to this brindle home, and at
the agent rows, and stops:

1. Orphan sessions: no workspace row names the session and no live agent
   runs in it, or its workspace's worktree folder is gone. Their processes
   are stopped, then the session.
2. Finished workers: once a worker's branch is merged into its base (and
   every agent in its workspace has reported or closed), its agents are
   closed and its session killed. A reviewer still running a few minutes
   after its verdict is closed.
3. Empty shells: a session whose panes run only a shell (or a sidebar, or
   nothing), belonging to a workspace whose work is finished.
4. Idle supervisors: a supervisor chat with no input for
   ``idle_supervisor_days`` (repo config, default 3; 0: never) and no
   running workers is paused (``agents.pause``: ``brindle continue`` brings
   it back) and the Inbox says so (service/inbox).
5. Coordinator rows: finished or paused coordinator agents older than a day
   (rows only: they hold no work).
6. Superseded workers: a worker idle for ``superseded_after`` hours (repo
   config, default 6; 0: never) whose branch adds nothing its base lacks is
   stopped and its supervisor told which commit holds its work; one whose
   worktree has uncommitted changes is only reported. A worker whose files
   and task mostly match a branch merged since it started is reported once,
   never stopped.

Never touched: a session with a client attached, one created, typed in or
written to in the last SESSION_GRACE seconds, a session of another brindle
home (BRINDLE_HOME) or on another tmux server, a session holding a live
agent of another workspace or a live supervisor's sidebar, a supervisor
whose pause would close or change an attached session (a worker's shell,
its sidebar's) or one with recent output or a busy pane brindle didn't
start, and a worktree with uncommitted changes whose work
isn't finished (an orphan's folder with uncommitted changes is kept). An
agent is only stopped from a session the listing shows on this server, of
this home, with nobody attached (``_quiet_session``); a supervisor is kept
while its worktree, or an unreported worker's, has uncommitted changes; and
a git status that fails counts as uncommitted work. A session's cleanup
closes only the agents whose panes are in it (``_occupants``). Each step is
planned again, for its one target, from a fresh listing just before it acts:
a client that attached, input that arrived or work that went uncommitted
since the plan stops it. A step whose plan ran git probes (merged,
contained, similar) is checked once more with their answers, so nothing
slow sits between the last safety read and the stop. One reaper runs at a time per brindle home (a
lock); a sweep that finds it held skips its turn.

Everything done is logged in the ``reaped`` table; ``brindle doctor`` lists
recent entries and what a sweep would reap now (``run(dry_run=True)``).
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from brindle import agents, git, procs, tmux
from brindle.config import brindle_home, load_repo_config
from brindle.db import DB, Agent, Workspace

log = logging.getLogger(__name__)

SESSION_GRACE = 600.0          # a session this new may not have its rows yet
REVIEW_LINGER = 120.0          # a reviewer's own close (agents.close_later) gets this long
COORDINATOR_KEEP = 24 * 3600.0
LOG_KEEP = 30 * 24 * 3600.0
DOCTOR_WINDOW = 7 * 24 * 3600.0
DAY = 24 * 3600.0
WORKER_MODES = ("assign", "handoff", "handoff_detached")
# How close a worker's task must be to a merged branch's (shared words over the
# shorter text's words), and how many of its files that branch must touch.
SIMILAR_WORDS = 0.5
_STOP = frozenset("the and for with that this from into when then than are was were not but its"
                  " all any can will should must have has had you your our their them they".split())


@dataclass
class Reap:
    kind: str                     # orphan | finished | shell | reviewer | idle_supervisor |
                                  # coordinator | superseded | superseded_dirty | similar
    target: str                   # a session name or an agent id
    detail: str                   # one line, what was (or would be) done
    act: Callable[[], None]
    root_id: str | None = None
    notice: bool = False          # a message to a supervisor, not a stop
    facts: dict | None = None     # its git probes' answers, for the last check before it acts


def _default_home() -> Path:
    return Path.home() / ".brindle"


def _ours(s: tmux.SessionInfo) -> bool:
    """A session of this brindle home: one without BRINDLE_HOME is the
    default home's (workspaces.workspace_env only passes it on when set)."""
    mine = os.path.realpath(brindle_home())
    return os.path.realpath(s.home or _default_home()) == mine


def _dirty(path: str) -> bool:
    """Whether ``path`` may hold uncommitted work: True when it has some, and
    when git couldn't say (a failed or refused status is no proof it's clean).
    Only a folder git confirms is no repository at all is clean."""
    try:
        inside = git.run(["rev-parse", "--is-inside-work-tree"], path, check=False)
        if inside.returncode != 0:
            return "not a git repository" not in (inside.stderr or "")
        return bool(git.dirty_files(path))
    except Exception:  # noqa: BLE001 - unknown is never clean
        return True


def _quiet_session(a: Agent, panes, session_of: dict[str, tmux.SessionInfo]) -> tmux.SessionInfo | None:
    """The session ``a``'s pane is in, if brindle may stop ``a`` there: on
    this tmux server, this brindle home's, nobody attached. None when it
    isn't, or when the listing doesn't show where ``a`` runs."""
    if not agents.same_server(a, panes):
        return None
    s = session_of.get(a.tmux_window)
    if s is None or s.attached or not _ours(s):
        return None
    return s


def _occupants(s: tmux.SessionInfo, agents_: list[Agent], panes,
               session_of: dict[str, tmux.SessionInfo]) -> list[str]:
    """The ids of ``agents_`` brindle may stop as part of ``s``'s cleanup:
    their own pane is in ``s``, on this server (``_quiet_session``)."""
    return [a.id for a in agents_
            if (q := _quiet_session(a, panes, session_of)) is not None and q.name == s.name]


def _finished(a: Agent) -> bool:
    from brindle import view

    return view._finished(a)


def _last_activity(s: tmux.SessionInfo) -> float:
    """The last input a client gave ``s`` or output any of its panes made."""
    return max([s.activity, *(p.activity for p in s.panes)])


def _paused_sessions(db: DB, tree: list[Agent], panes,
                     session_of: dict[str, tmux.SessionInfo]) -> list[tmux.SessionInfo]:
    """The listed sessions ``agents.pause`` of ``tree`` may close or change:
    each member's workspace session, the sessions its panes are in, and the
    supervisor's sidebar's."""
    by_name = {s.name: s for s in session_of.values()}
    found: dict[str, tmux.SessionInfo] = {}
    for m in tree:
        ws = db.get_workspace(m.workspace_id)
        if ws is not None and ws.tmux_session in by_name:
            found[ws.tmux_session] = by_name[ws.tmux_session]
        if m.tmux_window and agents.same_server(m, panes) and m.tmux_window in session_of:
            found[session_of[m.tmux_window].name] = session_of[m.tmux_window]
    sidebar = db.get_sidebar_pane(tree[0].id)
    if sidebar in session_of:
        found[session_of[sidebar].name] = session_of[sidebar]
    return list(found.values())


def _kill(s: tmux.SessionInfo, stop_procs: bool) -> Callable[[], None]:
    def act() -> None:
        if stop_procs:
            procs.terminate(procs.descendants([p.pid for p in s.panes if not p.dead]))
        tmux.kill_session(s.name)
    return act


def _close_all(db: DB, ids: list[str], panes) -> None:
    for aid in ids:
        try:
            agents.close(db, aid, panes)
        except agents.AgentError:
            pass


def _sessions(db: DB, now: float, panes, live: set[str], sessions: list[tmux.SessionInfo],
              session_of: dict[str, tmux.SessionInfo], only: str | None = None,
              facts: dict | None = None) -> list[Reap]:
    out: list[Reap] = []
    all_agents = db.list_agents()
    by_pane = {a.tmux_window: a for a in all_agents if a.tmux_window and agents.same_server(a, panes)}
    live_sidebars = {r["pane"] for r in db.conn.execute("SELECT root_id, pane FROM sidebars")
                     if r["root_id"] in live}
    for s in sessions:
        if (only is not None and s.name != only) or s.attached or not _ours(s) \
                or now - s.created < SESSION_GRACE or now - _last_activity(s) < SESSION_GRACE:
            continue
        pane_ids = {p.id for p in s.panes}
        tagged = {panes.tags.get(p, {}).get(agents.AGENT_TAG) for p in pane_ids} - {None, ""}
        in_session = {by_pane[p].id for p in pane_ids if p in by_pane} | tagged
        ws = db.workspace_by_tmux_session(s.name)
        # An agent recorded by window id (an older brindle) can't be placed by pane.
        windowed = ws is not None and any(
            a.id in live and a.tmux_window.startswith("@") and a.workspace_id == ws.id
            for a in all_agents)
        live_here = [a for a in all_agents if a.id in in_session and a.id in live]
        if windowed or pane_ids & live_sidebars:
            continue
        if ws is not None and any(a.workspace_id != ws.id for a in live_here):
            continue    # another workspace's live agent runs here: never this session's to stop
        gone = ws is not None and not os.path.isdir(ws.path)
        if ws is None and not live_here:
            folders = {p.path for p in s.panes if p.path and os.path.isdir(p.path)}
            if any(_dirty(f) for f in folders):
                continue    # someone's uncommitted work, and nothing says it is finished
            why = "no workspace or live agent"
        elif (gone and now - ws.created_at >= SESSION_GRACE
              and not any(a.mode == "interactive" for a in live_here)):
            why = "its worktree is gone"
        else:
            out.extend(_finished_workers(db, s, ws, live, panes, session_of, now, facts))
            continue
        closing = _occupants(s, live_here, panes, session_of)

        def act(s=s, closing=closing) -> None:
            _close_all(db, closing, panes)
            _kill(s, True)()
        out.append(Reap("orphan", s.name, f"killed tmux session {s.name} ({why})", act))
    return out


def _finished_workers(db: DB, s: tmux.SessionInfo, ws: Workspace | None, live: set[str],
                      panes, session_of: dict[str, tmux.SessionInfo], now: float,
                      facts: dict | None = None) -> list[Reap]:
    """Cases 2 and 3: the session of a workspace whose work is finished."""
    if ws is None or not os.path.isdir(ws.path):
        return []
    everyone = db.list_agents(ws.id)
    if not everyone or any(a.mode == "interactive" and a.dismissed_at is None and a.status != "done"
                           for a in everyone):
        return []   # a person's own workspace, or a supervisor's: never by this rule
    if not all(_finished(a) for a in everyone):
        return []   # a worker that hasn't reported (paused or running): its work isn't finished
    if any(a.id in live and a.status != "idle" for a in everyone):
        return []   # mid-turn: a fix round after its report, say
    if any(now - max(a.created_at, a.status_since or 0) < SESSION_GRACE for a in everyone):
        return []
    # Only the workspace's agents running in this session: one moved to another
    # (an attached one, say) or on another tmux server is not this cleanup's.
    running = _occupants(s, [a for a in everyone if a.id in live and a.dismissed_at is None],
                         panes, session_of)
    from brindle import cull

    merged = facts["merged"] if facts is not None else (
        ws.kind == "worktree" and cull.workspaces_merged(ws))
    if merged:
        def act(running=running) -> None:
            _close_all(db, running, panes)
            _kill(s, True)()
        return [Reap("finished", s.name, f"killed tmux session {s.name}: branch {ws.branch} is merged"
                     + (f", closed {len(running)} agent(s)" if running else ""), act,
                     facts={"merged": True})]
    if not running and s.idle():
        return [Reap("shell", s.name, f"killed tmux session {s.name}: only a shell left, "
                     f"its work on {ws.branch} is finished", _kill(s, False), facts={"merged": False})]
    return []


def _reviewers(db: DB, now: float, live: set[str], panes,
               session_of: dict[str, tmux.SessionInfo], only: str | None = None) -> list[Reap]:
    out = []
    for a in _agents(db, only):
        if (a.mode == "review" and a.result is not None and a.dismissed_at is None
                and a.id in live and a.status == "idle"
                and now - max(a.created_at, a.status_since or 0) > REVIEW_LINGER
                and _quiet_session(a, panes, session_of) is not None):
            out.append(Reap("reviewer", a.id, f"closed reviewer {a.id}: its verdict is in",
                            lambda aid=a.id: _close_all(db, [aid], panes)))
    return out


def _config(ws: Workspace | None, cache: dict, key: str) -> float:
    if ws is None:
        return 0.0
    if ws.repo_root not in cache:
        try:
            cache[ws.repo_root] = load_repo_config(ws.repo_root)
        except ValueError:
            cache[ws.repo_root] = None
    cfg = cache[ws.repo_root]
    return float(getattr(cfg, key) or 0) if cfg is not None else 0.0


def _unfinished_dirty(db: DB, tree: list[Agent]) -> bool:
    """Whether a pause of ``tree`` would leave uncommitted work it hasn't
    finished: the supervisor's own worktree, or an unreported worker's."""
    paths = set()
    for m in tree:
        ws = db.get_workspace(m.workspace_id)
        if ws is not None and os.path.isdir(ws.path) and (m is tree[0] or not _finished(m)):
            paths.add(ws.path)
    return any(_dirty(p) for p in paths)


def _idle_supervisors(db: DB, now: float, live: set[str], panes,
                      session_of: dict[str, tmux.SessionInfo], cache: dict, only: str | None = None) -> list[Reap]:
    out = []
    for a in _agents(db, only):
        if (a.mode != "interactive" or a.parent_id is not None or a.dismissed_at is not None
                or a.status in ("paused", "done") or a.id not in live):
            continue
        ws = db.get_workspace(a.workspace_id)
        days = _config(ws, cache, "idle_supervisor_days")
        s = _quiet_session(a, panes, session_of)
        if days <= 0 or s is None:
            continue   # no session to look at means no telling whether someone's attached
        tree = agents.tree(db, a.id)
        # Every session the pause closes or changes, not just the chat's: a
        # worker's shell someone attached to, or typed in, keeps it running.
        touched = _paused_sessions(db, tree, panes, session_of)
        if any(t.attached or not _ours(t) for t in touched):
            continue
        # Closing them stops every process in them: one brindle didn't start (a
        # dev server, a training run in a split pane) keeps the supervisor.
        ids = {m.id for m in tree}
        # A pane is a member's only by its tag (every launch tags it): a recorded
        # id may name someone else's pane since tmux restarted. An untagged one
        # (an older brindle's) keeps the supervisor running.
        own = {p for p, tags in panes.tags.items() if tags.get(agents.AGENT_TAG) in ids}
        if any(not p.idle() and p.id not in own for t in touched for p in t.panes):
            continue
        last = max([a.created_at, a.status_since or 0, _last_activity(s),
                    *(_last_activity(t) for t in touched)])
        if now - last < days * DAY:
            continue
        if any(m.id in live and m.status not in ("paused", "done") and m.dismissed_at is None
               for m in tree[1:]):
            continue
        if any(t.caller_id in ids for t in db.list_tasks(ws.repo_root if ws else None, state="pending")):
            continue    # it is waiting for work it queued
        if _unfinished_dirty(db, tree):
            continue    # uncommitted work nobody finished: left for the person
        idle_days = int((now - last) // DAY)

        def act(aid=a.id) -> None:
            agents.pause(db, aid)
        out.append(Reap("idle_supervisor", a.id,
                        f"stopped supervisor {a.id} after {idle_days} day(s) without input; "
                        f"`brindle continue` resumes it", act, root_id=a.id))
    return out


def _coordinators(db: DB, now: float, live: set[str], only: str | None = None) -> list[Reap]:
    from brindle import coordinator

    out = []
    for a in _agents(db, only):
        if a.mode != coordinator.MODE or a.id in live:
            continue
        if a.status not in ("paused", "done") and a.dismissed_at is None:
            continue
        if now - max(a.created_at, a.status_since or 0, a.dismissed_at or 0) <= COORDINATOR_KEEP:
            continue
        if db.children(a.id):
            continue
        out.append(Reap("coordinator", a.id, f"forgot finished coordinator {a.id}",
                        lambda aid=a.id: db.delete_agent(aid)))
    return out


# -- superseded workers ------------------------------------------------------------


def _rev(ref: str, cwd: str) -> str | None:
    proc = git.run(["rev-parse", "--verify", "-q", ref], cwd, check=False)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _patch_commit(base: str, since: str, patch_of: str, cwd: str) -> str | None:
    """The commit in ``since..base`` whose patch is ``patch_of``'s, if any."""
    def ids(text: str) -> list[tuple[str, str]]:
        out = git.run(["patch-id", "--stable"], cwd, check=False, input=text).stdout
        return [tuple(line.split()[:2]) for line in out.splitlines() if len(line.split()) >= 2]

    mine = ids(git.run(["show", "--format=commit %H", patch_of], cwd, check=False).stdout)
    if not mine:
        return None
    theirs = ids(git.run(["log", "-p", "--no-merges", "--format=commit %H", f"{since}..{base}"],
                         cwd, check=False).stdout)
    return next((c for pid, c in theirs if pid == mine[0][0]), None)


def contained(ws: Workspace) -> str | None:
    """The commit of ``ws``'s base that holds all of its branch, or None if
    the branch adds something its base lacks: its tip is an ancestor of the
    base; or merging it into the base changes nothing (it merged the base
    in, or it was squash-merged); or its whole change, as one patch on the
    merge base, is one the base already has (``git cherry``)."""
    cwd = ws.repo_root
    br, bs = f"refs/heads/{ws.branch}", f"refs/heads/{ws.base_branch}"
    tip, base = _rev(br, cwd), _rev(bs, cwd)
    if not tip or not base:
        return None
    # A branch with no commit since its worktree was made holds no work to supersede.
    own = git.run(["rev-list", "--count", f"--since={int(ws.created_at)}", tip], cwd, check=False)
    if own.stdout.strip() in ("", "0"):
        return None
    if git.ok(["merge-base", "--is-ancestor", tip, base], cwd):
        path = git.run(["rev-list", "--ancestry-path", "--reverse", f"{tip}..{base}"],
                       cwd, check=False).stdout.split()
        return path[0] if path else tip
    mb = git.run(["merge-base", base, tip], cwd, check=False).stdout.strip()
    merged = git.run(["merge-tree", "--write-tree", base, tip], cwd, check=False)
    if merged.returncode == 0 and merged.stdout.split()[:1] == [_rev(f"{base}^{{tree}}", cwd)]:
        return (_patch_commit(base, mb, tip, cwd) if mb else None) or base
    if not mb:
        return None
    probe = git.run([*git.identity_args(cwd), "commit-tree", f"{tip}^{{tree}}", "-p", mb,
                     "-m", "brindle superseded probe"], cwd, check=False).stdout.strip()
    if not probe:
        return None
    cherry = git.run(["cherry", base, probe, mb], cwd, check=False).stdout.split("\n")
    if any(line.startswith("- ") for line in cherry):
        return _patch_commit(base, mb, probe, cwd) or base
    return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower())) - _STOP


def similarity(a: str, b: str) -> float:
    """Shared words over the shorter text's words: 0 to 1."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _files(ws: Workspace) -> set[str]:
    cwd = ws.repo_root
    mb = git.run(["merge-base", f"refs/heads/{ws.base_branch}", f"refs/heads/{ws.branch}"],
                 cwd, check=False).stdout.strip()
    files = set(git.run(["diff", "--name-only", mb, f"refs/heads/{ws.branch}"], cwd,
                        check=False).stdout.split("\n")) if mb else set()
    try:
        files |= set(git.dirty_files(ws.path))
    except git.GitError:
        pass
    return {f for f in files if f}


_MERGE_SUBJECT = re.compile(r"Merge (?:remote-tracking )?branch '([^']+)'")


def similar(db: DB, a: Agent, ws: Workspace) -> tuple[str, int, int] | None:
    """A branch merged into ``ws``'s base since ``a`` started that touched
    most of its files and whose task reads close to its own: ``(branch,
    files in common, its files)``."""
    ours = _files(ws)
    if not ours or not a.task:
        return None
    log_ = git.run(["log", "--first-parent", "-m", "--name-only", f"--since={int(a.created_at)}",
                    "--format=%x1e%s", f"refs/heads/{ws.base_branch}"], ws.repo_root, check=False).stdout
    tasks = {t.branch: t.task_text for t in db.list_tasks(ws.repo_root) if t.branch}
    for record in log_.split("\x1e")[1:]:
        subject, _, names = record.partition("\n")
        m = _MERGE_SUBJECT.search(subject)
        branch = m.group(1) if m else subject.strip()
        if branch == ws.branch:
            continue
        theirs = {n for n in names.split("\n") if n}
        n = len(ours & theirs)
        if n * 2 <= len(ours):
            continue
        if similarity(a.task, tasks.get(branch, subject)) >= SIMILAR_WORDS:
            return branch, n, len(ours)
    return None


def _last_commit(ws: Workspace) -> float:
    out = git.run(["log", "-1", "--format=%ct", f"refs/heads/{ws.branch}"], ws.repo_root,
                  check=False).stdout.strip()
    return float(out) if out.isdigit() else 0.0


def _noted(db: DB, kind: str, target: str) -> bool:
    return db.conn.execute("SELECT 1 FROM reaped WHERE kind=? AND target=?",
                           (kind, target)).fetchone() is not None


def _tell(db: DB, a: Agent, body: str) -> None:
    agents.send_message(db, a.parent_id, body, sender_id=a.id)


def _superseded(db: DB, now: float, live: set[str], panes, session_of: dict[str, tmux.SessionInfo],
                cache: dict, only: str | None = None, facts: dict | None = None) -> list[Reap]:
    from brindle import cull

    out = []
    for a in _agents(db, only):
        if (a.mode not in WORKER_MODES or not a.parent_id or a.dismissed_at is not None
                or a.status in ("paused", "done") or a.id not in live):
            continue
        s = _quiet_session(a, panes, session_of)
        if s is None:
            continue    # someone is looking at it, or it runs where this listing can't judge it
        activity = {p.id: p.activity for p in s.panes}
        ws = db.get_workspace(a.workspace_id)
        if (ws is None or ws.kind != "worktree" or not ws.base_branch
                or not os.path.isdir(ws.path) or not os.path.isdir(ws.repo_root)):
            continue
        hours = _config(ws, cache, "superseded_after")
        if hours <= 0:
            continue
        # Only old workers: a commit, a turn, pane output or an edit within the window keeps it.
        last = max(a.created_at, a.status_since or 0, activity.get(a.tmux_window, 0.0),
                   _last_commit(ws), cull._last_edit(ws))
        if now - last < hours * 3600:
            continue
        try:
            if facts is not None:
                commit, close = facts["commit"], facts["close"]
            else:
                commit = contained(ws)
                close = None if commit else similar(db, a, ws)
            dirty = bool(commit) and _dirty(ws.path)
        except git.GitError:
            continue
        known = {"commit": commit, "close": close}
        base = ws.base_branch
        if commit and not dirty:
            body = (f"Worker {a.id} (branch `{ws.branch}`) was stopped: its branch adds nothing "
                    f"`{base}` lacks; its work is in commit {commit[:10]}. Its worktree and branch "
                    f"are kept.")

            def act(a=a, body=body, commit=commit) -> None:
                if a.result is None:
                    db.set_result(a.id, f"Superseded: its work is already in {base} ({commit[:10]}).")
                _close_all(db, [a.id], panes)
                try:
                    _tell(db, a, body)
                except agents.AgentError:
                    db.enqueue(a.parent_id, body, a.id)
            out.append(Reap("superseded", a.id, f"stopped worker {a.id}: its branch {ws.branch} "
                            f"is already in {base} ({commit[:10]})", act, root_id=a.parent_id,
                            facts=known))
        elif commit and not _noted(db, "superseded_dirty", a.id):
            body = (f"Worker {a.id}'s branch `{ws.branch}` is already in `{base}` (commit "
                    f"{commit[:10]}), but its worktree {ws.path} has uncommitted changes, so it "
                    f"wasn't stopped. Look at them, then stop {a.id} if nothing there is needed.")
            out.append(Reap("superseded_dirty", a.id, f"told {a.parent_id} that worker {a.id}'s "
                            f"branch is already in {base} (uncommitted changes kept it running)",
                            lambda a=a, body=body: _tell(db, a, body), root_id=a.parent_id, notice=True,
                            facts=known))
        elif close and not _noted(db, "similar", a.id):
            branch, n, m = close
            body = (f"Similar work merged in {branch} ({n} of {m} files overlap); "
                    f"consider stopping {a.id}.")
            out.append(Reap("similar", a.id, f"told {a.parent_id} that similar work to worker "
                            f"{a.id}'s merged in {branch}",
                            lambda a=a, body=body: _tell(db, a, body), root_id=a.parent_id, notice=True,
                            facts=known))
    return out


# -- running it ------------------------------------------------------------------


def _agents(db: DB, only: str | None) -> list[Agent]:
    if only is None:
        return db.list_agents()
    a = db.get_agent(only)
    return [a] if a is not None else []


def plan(db: DB, now: float, panes, sessions: list[tmux.SessionInfo],
         only: str | None = None, facts: dict | None = None) -> list[Reap]:
    """What to reap; ``only``: just what targets that session or agent.
    ``facts``: a plan's git probe answers (merged, contained, similar) taken
    as still true, so only the quick safety reads run (``_apply``'s last check)."""
    from brindle import view

    live = view.live_agents(db, panes)
    session_of = {p.id: s for s in sessions for p in s.panes}
    cache: dict = {}
    steps = [lambda: _sessions(db, now, panes, live, sessions, session_of, only, facts),
             lambda: _reviewers(db, now, live, panes, session_of, only),
             lambda: _idle_supervisors(db, now, live, panes, session_of, cache, only),
             lambda: _coordinators(db, now, live, only),
             lambda: _superseded(db, now, live, panes, session_of, cache, only, facts)]
    out: list[Reap] = []
    for step in steps:
        try:
            out.extend(step())
        except Exception:  # noqa: BLE001 - one step must never stop the others
            log.exception("brindle: a reap step failed")
    return out


def run(db: DB, now: float | None = None, *, dry_run: bool = False) -> list[str]:
    """Reap what's left over. Returns what it did (or, ``dry_run``, would do),
    one line each. A session-wide tmux hang raises TmuxTimeout, as the
    sweep's pane list does: no answer is not the same as no sessions."""
    now = time.time() if now is None else now
    if dry_run:
        return [f"would have {r.detail}" for r in plan(db, now, tmux.list_panes(), tmux.session_infos())
                if not r.notice]
    # One reaper at a time per brindle home: every sidebar runs a sweep, and two
    # planning at once would both send a once-only notice, or act on a stale
    # plan. Planned only once the lock is held, so it sees the last run's work;
    # a run that finds it held leaves this round to that one.
    path = brindle_home() / "locks" / "reap.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        return _apply(db, now, plan(db, now, tmux.list_panes(), tmux.session_infos()))


def _again(db: DB, now: float, r: Reap, facts: dict | None = None) -> Reap | None:
    """``r`` as a plan made from a fresh listing and the DB now has it, or
    None: planning took a while, and since then a client may have attached,
    input arrived, a worker resumed or work gone uncommitted. With ``facts``
    its git probes aren't run again: only the quick reads."""
    # Each session's home read again too: another brindle may have taken it since the plan.
    fresh = plan(db, now, tmux.list_panes(), tmux.session_infos(fresh=True), only=r.target, facts=facts)
    return next((f for f in fresh if f.kind == r.kind and f.target == r.target), None)


def _apply(db: DB, now: float, reaps: list[Reap]) -> list[str]:
    done = []
    for planned in reaps:
        # Twice: once to see it's still needed (git probes and all, which take a
        # while), then, with those answers, for the safety state as it is now
        # (attached, ownership, agents, activity, uncommitted work), just before
        # acting, with no slow probe in between.
        r = _again(db, now, planned)
        if r is not None and r.facts is not None:
            r = _again(db, now, r, facts=r.facts)
        if r is None:
            continue    # no longer safe, or no longer needed
        # Recorded first: stopping a supervisor can stop the process running this
        # (a sweep in its sidebar), and the Inbox item must outlive that.
        with db.tx() as c:
            c.execute("INSERT INTO reaped (at, kind, target, root_id, detail) VALUES (?, ?, ?, ?, ?)",
                      (now, r.kind, r.target, r.root_id, r.detail))
        try:
            r.act()
        except Exception as e:  # noqa: BLE001 - the next one still runs
            log.exception("brindle: reaping %s failed", r.target)
            with db.tx() as c:
                c.execute("DELETE FROM reaped WHERE kind=? AND target=? AND at=?", (r.kind, r.target, now))
            done.append(f"couldn't reap {r.target}: {e}")
            continue
        done.append(r.detail)
    with db.tx() as c:
        c.execute("DELETE FROM reaped WHERE at < ?", (now - LOG_KEEP,))
    return done


def recent(db: DB, now: float | None = None, window: float = DOCTOR_WINDOW) -> list[str]:
    """What the sweep reaped in the last ``window`` seconds, newest first."""
    now = time.time() if now is None else now
    return [f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['at']))} {r['detail']}"
            for r in db.conn.execute("SELECT at, detail FROM reaped WHERE at >= ? ORDER BY at DESC",
                                     (now - window,))]


def idle_stopped(db: DB) -> list[tuple[Agent, str]]:
    """Supervisors the sweep stopped for being idle that are still paused
    (not continued, closed or paused again since), with what was done."""
    out = []
    for r in db.conn.execute("SELECT target, at, detail FROM reaped WHERE kind='idle_supervisor'"
                             " ORDER BY at DESC"):
        a = db.get_agent(r["target"])
        if (a is None or a.status != "paused" or a.dismissed_at is not None
                or any(x.id == a.id for x, _ in out)):
            continue
        if (a.status_since or 0) > r["at"] + 60:
            continue    # continued and paused again since: not this stop any more
        out.append((a, r["detail"]))
    return out
