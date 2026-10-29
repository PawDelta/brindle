"""Paused sessions: listing them, and keeping only a few.

A session is an interactive agent (usually a supervisor) plus every agent it
started. Closing its chat pauses it: processes stop, and the work (branches,
worktrees, CLI conversations) stays on disk for `brindle continue`. Paused
sessions use no memory, but their worktrees use disk, so each repo keeps its
KEEP most recent paused sessions for at most MAX_AGE_DAYS.

Cleaning up never merges or commits anything. Branches always stay. A worker's
worktree is removed only when it has no uncommitted changes.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from brindle import agents, bus, git, history, knowledge, pool, scratch, tmux, workspaces
from brindle.db import DB, Agent, Workspace

# Free-tier retention. Read from the environment so a paid add-on (or a person
# who wants more) can raise them without changing this module.
KEEP = int(os.environ.get("BRINDLE_KEEP_SESSIONS", "3"))
MAX_AGE_DAYS = float(os.environ.get("BRINDLE_SESSION_DAYS", "7"))


@dataclass
class Session:
    root: Agent
    workspace: Workspace          # where the root (the chat) runs
    members: list[Agent]          # root + everything it started
    branches: list[str]           # worker branches, for display

    @property
    def paused_at(self) -> float:
        return self.root.status_since or self.root.created_at


def paused(db: DB, repo_root: str) -> list[Session]:
    """Paused sessions whose chat ran in ``repo_root``, newest first. A
    "failed" chat whose pane is still running isn't one: that status also
    marks a live chat (a brief whose delivery wasn't confirmed, an auth error
    on one turn), and dropping it (see enforce) would close it mid-session."""
    out = []
    panes = owners = None
    for ws in db.find_workspaces(repo_root):
        for a in db.list_agents(ws.id):
            if a.mode == "interactive" and a.status == "failed":
                if panes is None:
                    panes = tmux.list_panes()
                    owners = agents.pane_owners(db, panes)
                # On another server, its own server's panes say whose its pane is.
                mine = owners if agents.same_server(a, panes) else None
                if agents.is_alive(a, panes) and agents.owns_pane(db, a, mine):
                    continue
            if a.mode == "interactive" and a.status in ("paused", "failed"):
                members = agents.tree(db, a.id)
                branches = []
                for m in members[1:]:
                    mws = db.get_workspace(m.workspace_id)
                    if mws and mws.kind == "worktree" and mws.branch not in branches:
                        branches.append(mws.branch)
                out.append(Session(a, ws, members, branches))
    return sorted(out, key=lambda s: s.paused_at, reverse=True)


@dataclass
class LiveSession:
    root: Agent
    workspace: Workspace          # where the chat runs (the checkout or a brindle/session-N worktree)
    goal: str                     # one line: the autopilot goal, else the start prompt
    workers: int                  # workers of this session that are running

    @property
    def branch(self) -> str:
        return self.workspace.branch or ""

    @property
    def age(self) -> float:
        return time.time() - self.root.created_at


def goal_line(db: DB, root: Agent) -> str:
    """The session's goal on one line, trimmed for a list."""
    ap = db.get_autopilot(root.id)
    text = (ap.goal if ap and ap.goal else root.task) or ""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= 60 else line[:59] + "…"


def live(db: DB, repo_root: str) -> list[LiveSession]:
    """Live interactive sessions in every workspace of ``repo_root`` (the
    checkout and its ``brindle/session-N`` worktrees), newest first. An agent
    whose recorded pane id a newer agent has since taken (tmux reuses ids)
    isn't live: that pane is the newer agent's. One tmux call answers for
    every agent, not one per agent (the app's first load waited on those)."""
    panes = tmux.list_panes()
    owners = agents.pane_owners(db, panes)
    out = []
    for ws in db.find_workspaces(repo_root):
        for a in db.list_agents(ws.id):
            if (a.mode == "interactive" and a.status not in ("paused", "done")
                    and agents.is_alive(a, panes) and agents.owns_pane(db, a, owners)):
                running = [m for m in agents.tree(db, a.id)[1:]
                           if m.mode != "interactive" and m.status not in ("paused", "done", "failed")
                           and agents.is_alive(m, panes)]
                out.append(LiveSession(a, ws, goal_line(db, a), len(running)))
    return sorted(out, key=lambda s: s.root.created_at, reverse=True)


HANDOVER = (
    "[brindle handover] You are taking over supervising this work from {old}, which ran on branch "
    "{branch}. The goal and milestones, the workers below and any queued tasks are now yours; "
    "call get_progress and list_agents first.{note}{workers}"
)


def handover_prompt(db: DB, old_root: Agent, note: str | None) -> str:
    """What the new supervisor is told on start: the handoff note and the workers it inherits."""
    ws = db.get_workspace(old_root.workspace_id)
    lines = []
    for w in db.children(old_root.id):
        wws = db.get_workspace(w.workspace_id)
        lines.append(f"- {w.id} ({w.profile}, {w.status}) on {wws.branch if wws else '?'}"
                     f", based on {wws.base_branch if wws else '?'}")
    return HANDOVER.format(
        old=old_root.id, branch=ws.branch if ws else "?",
        note=f"\n\nHandoff note:\n{note.strip()}" if note and note.strip() else "",
        workers="\n\nWorkers you inherit:\n" + "\n".join(lines) if lines else "",
    )


def handover(db: DB, old_root_id: str, dest: Workspace, note: str | None = None, *,
             pause_old: bool = True) -> Agent:
    """Hand a supervisor session to a new supervisor running in ``dest``.

    Carries over the goal and milestones (with their last results), the
    workers (their ``parent_id``), queued and started tasks, and a handoff note
    that becomes the new supervisor's first message. The old supervisor's
    goal driver is blocked and, with ``pause_old``, the old session paused
    (not when the old supervisor itself is calling: it ends its own turn)."""
    from brindle import autopilot as pilot

    old = db.get_agent(old_root_id)
    if old is None:
        raise agents.AgentError(f"no session {old_root_id}")
    ap = db.get_autopilot(old_root_id)
    prompt = handover_prompt(db, old, note)
    # Always a supervisor, even from a session started as something else.
    new = agents.spawn(db, dest, agents.SUPERVISOR, prompt=prompt, provider_name=old.provider,
                       watch_pane=True, background_setup=True, autopilot=True,
                       claim_goals=False)
    if ap and ap.goal:
        old_ms = db.milestones(old_root_id)
        pilot.set_goal(db, new.id, ap.goal,
                       [(m.title, m.check_cmd, m.detail, m.profile, m.repo) for m in old_ms], ap.detail)
        for prev, m in zip(old_ms, db.milestones(new.id)):
            if prev.status != "pending":
                db.record_check(m.id, prev.status == "passed", prev.output or "", prev.checked_sha,
                                passed_sha=prev.passed_sha, state=prev.state, state_why=prev.state_why,
                                ever_passed=prev.ever_passed)
            if ap.state == "done":
                # Copying a historical result is not a fresh check.
                with db.tx() as c:
                    c.execute("UPDATE milestones SET checked_at=? WHERE id=?", (prev.checked_at, m.id))
        db.update_autopilot(new.id, goal_sha=ap.goal_sha, audit_history=ap.audit_history)   # the audit reviews the whole goal's work
        if ap.state == "done":
            # Handover changes the supervisor, not the completed goal's verdict.
            db.update_autopilot(new.id, state="done", audit_sha=ap.audit_sha, audit_ok=ap.audit_ok)
        if ap.state != "done":
            pilot.remove_goals_file(db, old_root_id)  # active work lives in the new session's file
        pilot.sync_goals_file(db, new.id)
    if ap and ap.state != "done":
        db.update_autopilot(old_root_id, state="blocked", note=f"Handed over to session {new.id}", question=None)
    with db.tx() as c:
        # Preserve only messages this supervisor sent while it still owned
        # these descendants. A sender-wide alias would also trust unrelated
        # messages it sends after handover, or messages to workers it never
        # supervised. Keep the original attribution and chronological order.
        c.execute(
            "WITH RECURSIVE descendants(id) AS ("
            "SELECT id FROM agents WHERE parent_id=? UNION "
            "SELECT a.id FROM agents a JOIN descendants d ON a.parent_id=d.id) "
            "UPDATE inbox SET supervisor_context=1 WHERE sender_id=? "
            "AND agent_id IN (SELECT id FROM descendants)",
            (old_root_id, old_root_id),
        )
        c.execute("UPDATE agents SET parent_id=? WHERE parent_id=?", (new.id, old_root_id))
        # A queued task for the session's own repo is cut from the new
        # supervisor's checkout; one for an attached repo keeps its own.
        c.execute("UPDATE tasks SET caller_id=?, caller_ws_id=? WHERE caller_id=? "
                  "AND state IN ('pending', 'started') AND repo_root=?",
                  (new.id, dest.id, old_root_id, dest.repo_root))
        c.execute("UPDATE tasks SET caller_id=? WHERE caller_id=? AND state IN ('pending', 'started')",
                  (new.id, old_root_id))
        # The attached repos (brindle.repos) move across with the session.
        c.execute("INSERT OR IGNORE INTO session_repos (root_id, alias, repo_root, added_at) "
                  "SELECT ?, alias, repo_root, added_at FROM session_repos WHERE root_id=?",
                  (new.id, old_root_id))
        c.execute("DELETE FROM session_repos WHERE root_id=?", (old_root_id,))
        # So does an outside job it waits on (wait_on), or the notice of one
        # that just ended: the old session no longer drives, so nobody would
        # be told. In this transaction, so the wait is finished once, by one side.
        if c.execute("UPDATE autopilot SET external_wait=(SELECT external_wait FROM autopilot "
                     "WHERE root_id=?) WHERE root_id=? AND external_wait IS NULL",
                     (old_root_id, new.id)).rowcount:
            c.execute("UPDATE autopilot SET external_wait=NULL WHERE root_id=?", (old_root_id,))
        c.execute("UPDATE inbox SET agent_id=? WHERE agent_id=? AND delivered_at IS NULL "
                  "AND instr(body, ?) > 0", (new.id, old_root_id, pilot.WAIT_ENDED_HEAD))
    _repoint_orphaned_tasks(db, new.id)
    bus.publish_agent(db, "session.handover", new.id, from_root=old_root_id)
    if pause_old:
        agents.pause(db, old_root_id, stop_local_models=False)
    return new


def _repoint_orphaned_tasks(db: DB, root_id: str) -> None:
    """A queued task whose caller workspace is gone (an attached repo's
    checkout record was removed, say) would fail to start: point it at its
    repo's root checkout instead. A repo that is gone from disk is left
    alone; starting the task reports that then."""
    from brindle import repos

    for t in db.list_tasks(state="pending"):
        if t.caller_id != root_id or db.get_workspace(t.caller_ws_id) is not None:
            continue
        try:
            ws = repos.target_workspace(db, t.repo_root)
        except git.GitError:
            continue
        db.update_task(t.id, caller_ws_id=ws.id)


def _forget(db: DB, s: Session) -> None:
    """Drop a session: its records, and any of its worktrees that are clean and
    not used by anything else. Branches and dirty worktrees stay."""
    worktrees = {}
    panes = tmux.list_panes()
    owners = agents.pane_owners(db, panes)
    for a in s.members:
        # Only a window that is still really this agent's: pausing already
        # closed its windows, and its recorded pane id may since have been
        # given to a newer session's pane (see agents.owns_pane) -- which,
        # on a freshly started tmux server, is usually the very chat being
        # launched right now.
        # And only on this tmux server: another server's pane id would name
        # some other pane here (see agents.same_server).
        if (a.tmux_window and agents.same_server(a, panes) and agents.is_alive(a, panes)
                and agents.owns_pane(db, a, owners)):
            from brindle.sidebar_follow import rescue_sidebar_from_window

            rescue_sidebar_from_window(db, a)
            tmux.kill_window(a.tmux_window)
        ws = db.get_workspace(a.workspace_id)
        if ws and ws.kind == "worktree":
            worktrees[ws.id] = ws
    member_ids = {a.id for a in s.members}
    for ws in worktrees.values():
        if any(a.id not in member_ids for a in db.list_agents(ws.id)):
            continue  # someone else still works here
        if os.path.isdir(ws.path) and git.dirty_files(ws.path):
            continue  # uncommitted work: keep it, quietly
        try:
            workspaces.remove(db, ws, force=False, delete_branch=False)
        except (workspaces.WorkspaceError, git.GitError):
            pass
    if not history.archive(db, None, [a for a in s.members if db.get_agent(a.id)],
                           "session dropped"):
        return  # kept for a later try
    from brindle import autopilot as pilot
    pilot.remove_goals_file(db, s.root.id)   # the session's own goal file goes with it
    knowledge.note(db, s.root.id, knowledge.END)
    for a in s.members:
        agents._note_reviewer_end(db, a)
        db.delete_agent(a.id)
        agents.forget_files(a)


def enforce(db: DB, repo_root: str, now: float | None = None) -> int:
    """Keep the KEEP newest paused sessions in ``repo_root`` that are younger
    than MAX_AGE_DAYS. Returns how many were dropped."""
    now = now or time.time()
    dropped = 0
    for i, s in enumerate(paused(db, repo_root)):
        too_old = now - s.paused_at > MAX_AGE_DAYS * 86400
        if i >= KEEP or too_old:
            _forget(db, s)
            dropped += 1
    if dropped:
        # _forget deletes agent rows; a dropped agent's usage mark (if its
        # history rows are gone too, e.g. never reported) is now dead weight.
        db.prune_usage_marks()
    try:
        # Sweeping and trimming happen in the detached fill process, so a
        # large trim's rmtree never blocks whoever's calling enforce (e.g.
        # `brindle start`).
        pool.fill_in_background(repo_root)
    except (git.GitError, ValueError, OSError):
        pass
    return dropped


def prune_scratch(db: DB, now: float | None = None) -> int:
    """Delete scratch sessions older than MAX_AGE_DAYS whose work was
    transferred (or that never had any), with no agent still running there."""
    from brindle import pipeline

    now = now or time.time()
    removed = 0
    for ws in scratch.sessions(db):
        if now - ws.created_at <= MAX_AGE_DAYS * 86400:
            continue
        if any(agents.is_alive(a) for a in db.list_agents(ws.id)):
            continue
        has_work = scratch.commit_count(ws) > 0 or bool(git.dirty_files(ws.path))
        if has_work and not scratch.transferred_to(ws.path):
            continue  # untransferred work stays
        for s in paused(db, ws.repo_root):
            _forget(db, s)
        # Done sessions aren't paused ones: their records go with the workspace.
        if not history.archive(db, ws, db.list_agents(ws.id), "scratch session dropped"):
            continue
        tmux.kill_session(ws.tmux_session)
        pipeline.on_workspace_cleanup(db, ws)
        db.delete_workspace(ws.id)
        shutil.rmtree(ws.path, ignore_errors=True)
        removed += 1
    return removed


def disk_usage(path: str | Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total
