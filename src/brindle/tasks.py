"""Task coordination between parallel workers.

``assign``/``handoff`` may declare ``files`` (paths/globs a task expects to
touch) and ``depends_on`` (earlier tasks, by agent id or branch name, that
must be merged first). A task with unmet dependencies is recorded here as
'pending' (no worker started yet) and started once ``merge_workspace``
resolves its dependencies, cut from the updated base branch. A task with
declared ``files`` is checked against other active workers' declared and
actually-changed files, so the caller learns about likely collisions without
being blocked by them.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import PurePath

from brindle import agents, conflicts, git, savings
from brindle.db import DB, Agent, Task, Workspace


def new_id() -> str:
    return agents.new_id()


def _dumps(items: list[str] | None) -> str | None:
    return json.dumps(list(items)) if items else None


def _loads(text: str | None) -> list[str]:
    return json.loads(text) if text else []


# -- overlap warnings ---------------------------------------------------------


def _normpath(p: str) -> str:
    """``p`` with a leading './' and redundant separators collapsed, so
    "./src/a.py" compares equal to "src/a.py"."""
    return os.path.normpath(p) if p else p


def _glob_match(a: str, b: str) -> bool:
    """Whether globs/paths ``a`` and ``b`` could refer to the same file(s)."""
    a, b = _normpath(a), _normpath(b)
    if a == b:
        return True
    if fnmatch.fnmatch(b, a) or fnmatch.fnmatch(a, b):
        return True
    if "**" in a or "**" in b:
        try:
            return PurePath(b).match(a) or PurePath(a).match(b)
        except ValueError:
            return False
    return False


def _changed_files(ws: Workspace) -> list[str]:
    """Files ``ws``'s branch has touched relative to its base, cheaply (no
    process beyond a couple of git calls)."""
    return conflicts.changed_files(ws)


def active_tasks(db: DB, repo_root: str) -> list[Task]:
    """Started tasks in ``repo_root`` whose worker hasn't reported yet."""
    out = []
    for t in db.list_tasks(repo_root, state="started"):
        if not t.agent_id:
            continue
        agent = db.get_agent(t.agent_id)
        if agent and agent.result is None:
            out.append(t)
    return out


RUNNING = ("starting", "processing", "waiting", "idle", "unknown")


def running_tasks(db: DB, repo_root: str) -> list[tuple[Task, Agent, Workspace | None]]:
    """Active tasks whose worker is still running (not paused, done or
    exited), with the worker and its workspace."""
    out = []
    for t in active_tasks(db, repo_root):
        agent = db.get_agent(t.agent_id) if t.agent_id else None
        if agent is None or agent.status not in RUNNING:
            continue
        out.append((t, agent, db.get_workspace(agent.workspace_id)))
    return out


def overlap_warning(db: DB, ws: Workspace, files: list[str] | None) -> str | None:
    """A warning if ``files`` overlaps another active task's declared or
    actually-changed files, or None. Still starts the worker regardless: this
    is informational, not a block."""
    if not files:
        return None
    for t, _agent, other_ws in running_tasks(db, ws.repo_root):
        candidates = list(_loads(t.files))
        if other_ws:
            candidates += _changed_files(other_ws)
        for mine in files:
            for theirs in candidates:
                if _glob_match(mine, theirs):
                    branch = other_ws.branch if other_ws else "?"
                    return (f"overlaps with {t.agent_id} ({branch}) on {theirs}; "
                            "consider depends_on or merging first")
    return None


# -- predicted conflicts between running branches -------------------------------


def conflict_forecast(db: DB, ws: Workspace, worker: Agent) -> list[tuple[Agent, Workspace, list[str]]]:
    """Other running branches in ``ws``'s repo that have changed a file
    ``ws``'s branch has changed too (actual diffs, not declared ``files``):
    (their worker, their workspace, the shared files). These are the pairs
    that will conflict when the second of them merges."""
    mine = _changed_files(ws)
    if not mine:
        return []
    found = []
    for _t, other, other_ws in running_tasks(db, ws.repo_root):
        if other.id == worker.id or other_ws is None or other_ws.id == ws.id:
            continue
        if other_ws.kind != "worktree" or other_ws.base_branch != ws.base_branch:
            continue
        shared = conflicts.shared_files(mine, _changed_files(other_ws))
        if shared:
            found.append((other, other_ws, shared))
    return found


def warn_predicted_conflicts(db: DB, worker: Agent) -> str | None:
    """A worker's turn ended: if its branch now shares changed files with
    another running branch, tell its supervisor, once per pair of branches
    (again only when more files join the overlap). Returns the warning sent,
    or None. Never raises: this runs inside the worker's Stop hook."""
    try:
        if worker.mode not in ("assign", "handoff", "handoff_detached") or not worker.parent_id:
            return None
        ws = db.get_workspace(worker.workspace_id)
        if ws is None or ws.kind != "worktree" or db.get_agent(worker.parent_id) is None:
            return None
        lines = []
        for other, other_ws, shared in conflict_forecast(db, ws, worker):
            told = db.conflict_notice(ws.id, other_ws.id)
            if told is not None and set(shared) <= set(told):
                continue
            db.set_conflict_notice(ws.id, other_ws.id, shared)
            shown = ", ".join(shared[:6]) + (f" and {len(shared) - 6} more" if len(shared) > 6 else "")
            lines.append(f"`{ws.branch}` ({worker.id}) and `{other_ws.branch}` ({other.id}) "
                         f"have both changed {shown}.")
        if not lines:
            return None
        text = ("[brindle] Likely merge conflict: " + " ".join(lines)
                + " Whichever merges second will have to resolve it (brindle asks that "
                "branch's worker when the time comes). To head it off: have one worker "
                "wait for the other's merge, or tell them which one owns those files.")
        from brindle import gates

        gates.tell(db, worker.parent_id, text)
        return text
    except Exception:  # noqa: BLE001 - a forecast must never break a hook
        return None


# -- dependencies ---------------------------------------------------------------


def _dep_branch(db: DB, dep: str) -> str:
    """A dependency identifier's branch: ``dep`` may be an agent id (its
    workspace's branch) or already a branch name."""
    agent = db.get_agent(dep)
    if agent:
        ws = db.get_workspace(agent.workspace_id)
        if ws:
            return ws.branch
    return dep


def _dep_task(db: DB, repo_root: str, dep: str) -> Task | None:
    """The task brindle is tracking for ``dep`` (an agent id or branch name), if
    any -- including one that was cancelled: a cancelled dependency can never
    become merged, so ``unmet_dependencies`` must treat it as a hard failure
    rather than something to keep waiting on. An untracked dependency (e.g. a
    branch never assigned through brindle) falls back to a git ancestry check
    in ``unmet_dependencies``.

    A dependency may live in another repo of the session (brindle.repos): an
    agent id is found wherever its task is, but a bare branch name only
    matches within ``repo_root``, since two repos may have a branch of the
    same name."""
    branch = _dep_branch(db, dep)
    candidates = [t for t in db.list_tasks(repo_root) if t.agent_id == dep or t.branch == branch]
    if not candidates:
        candidates = [t for t in db.list_tasks() if t.agent_id == dep]
    return max(candidates, key=lambda t: t.created_at) if candidates else None


def _same_repo(db: DB, repo_root: str, dep: str) -> bool:
    """Whether ``dep`` (an agent id) belongs to ``repo_root``; a bare branch
    name is taken to."""
    agent = db.get_agent(dep)
    if not agent:
        return True
    ws = db.get_workspace(agent.workspace_id)
    return ws is None or ws.repo_root == repo_root


def unmet_dependencies(db: DB, caller_ws: Workspace, depends_on: list[str] | None) -> list[str]:
    """``depends_on`` entries not yet merged into ``caller_ws``'s branch.

    Raises ``agents.AgentError`` if any dependency's task was cancelled: it
    can never merge, so there's nothing left to wait for.

    A dependency a task hasn't diverged from yet (no commits beyond its base)
    would trivially satisfy a plain git ancestry check even though it hasn't
    been merged, so a dependency brindle is tracking as a task is judged by its
    recorded state (only 'merged' counts) instead; only a dependency brindle
    never started falls back to ancestry."""
    if not depends_on:
        return []
    unmet = []
    for dep in depends_on:
        task = _dep_task(db, caller_ws.repo_root, dep)
        if task is not None:
            if task.state == "cancelled":
                raise agents.AgentError(f"dependency {dep} was cancelled and will never merge")
            if task.state != "merged":
                unmet.append(dep)
        elif not _same_repo(db, caller_ws.repo_root, dep):
            # A worker in another repo that brindle isn't tracking as a task:
            # its branch can't be an ancestor here; judge it by its merge record.
            agent = db.get_agent(dep)
            ws = db.get_workspace(agent.workspace_id) if agent else None
            if ws is None or not db.merged_sha(ws.id):
                unmet.append(dep)
        elif not git.ok(["merge-base", "--is-ancestor", _dep_branch(db, dep), "HEAD"], caller_ws.path):
            unmet.append(dep)
    return unmet


def _dep_matches(dep: str, ws: Workspace, worker_id: str | None) -> bool:
    return dep == ws.branch or (worker_id is not None and dep == worker_id)


def _refers_to(dep: str, t: Task, from_repo: str | None = None) -> bool:
    """Whether a ``depends_on`` entry names task ``t``: its worker's agent id
    (once it has one), from any repo, or its declared branch, only from the
    same repo (``from_repo``: the depending task's; two repos may have a
    branch of the same name)."""
    if t.agent_id is not None and dep == t.agent_id:
        return True
    return dep == t.branch and (from_repo is None or from_repo == t.repo_root)


def _cancel(db: DB, task_id: str, reason: str) -> list[str]:
    """Cancel a still-pending task and tell its caller, then cascade the
    cancellation to any pending task depending on it, recursively: a task
    waiting on one that can never merge can itself never merge. Re-fetches
    and checks state so cancelling the same task twice (reachable via more
    than one dependency path) is a no-op the second time. Returns the ids
    cancelled, the task first."""
    t = db.get_task(task_id)
    if t is None or t.state != "pending":
        return []
    cancelled = [t.id]
    db.update_task(t.id, state="cancelled")
    if t.caller_id:
        try:
            agents.send_message(
                db, t.caller_id, f"Cancelled queued task {t.id}: {reason}", sender_id=None,
            )
        except agents.AgentError:
            pass
    for dependent in db.list_tasks(state="pending"):   # any repo of the session may wait on it
        if any(_refers_to(d, t, dependent.repo_root) for d in _loads(dependent.depends_on)):
            cancelled += _cancel(
                db, dependent.id, f"its dependency {t.id} ({t.branch or t.id}) was cancelled")
    return cancelled


def cancel(db: DB, caller: Agent | None, task_id: str, reason: str = "") -> str:
    """Cancel a pending task on behalf of ``caller`` (its own caller, or the
    root of its session) and say what was cancelled, dependents included.
    Raises ``ValueError`` if the task is unknown, not the caller's, or has
    already started."""
    from brindle import autopilot

    t = db.get_task(task_id)
    if t is None:
        raise ValueError(f"No task {task_id}. list_tasks shows what's queued.")
    mine = caller.id == t.caller_id if caller else t.caller_id is None
    if not mine and caller and t.caller_id:
        mine = autopilot.root_of(db, t.caller_id) == caller.id
    if not mine:
        raise ValueError(f"Task {task_id} isn't yours to cancel: only its caller or its session root can.")
    if t.state != "pending":
        raise ValueError(f"Task {task_id} is {t.state}, not queued: only a pending task can be cancelled.")
    ids = _cancel(db, t.id, reason or "cancelled by its caller")
    text = f"Cancelled task {ids[0]}."
    if len(ids) > 1:
        text += f" Also cancelled its dependents: {', '.join(ids[1:])}."
    return text


# -- queueing and starting -------------------------------------------------------


def enqueue(
    db: DB, caller: Agent | None, caller_ws: Workspace, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None, plan_first: bool | None = None,
    weight: str | None = None,
) -> Task:
    """Record a task that can't start yet: no worker, no workspace, just what
    it takes to start it once its dependencies are merged."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=None,
        caller_id=caller.id if caller else None, caller_ws_id=caller_ws.id,
        profile=profile, task_text=task_text, mode=mode, isolate=int(isolate),
        branch=branch, done_when=done_when, files=_dumps(files),
        depends_on=_dumps(depends_on), state="pending", created_at=time.time(), weight=weight,
    )
    db.add_task(t)
    if plan_first is not None:
        db.update_task(t.id, plan_first=int(plan_first))
    return t


def record_started(
    db: DB, caller_ws: Workspace, worker: Agent, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None, weight: str | None = None,
) -> Task:
    """Record a task that started right away, so its ``files`` can be checked
    for overlap against later tasks."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=worker.id,
        caller_id=worker.parent_id, caller_ws_id=caller_ws.id, profile=profile,
        task_text=task_text, mode=mode, isolate=int(isolate), branch=branch,
        done_when=done_when, files=_dumps(files), depends_on=_dumps(depends_on),
        state="started", created_at=time.time(), started_at=time.time(), weight=weight,
    )
    db.add_task(t)
    return t


def start_queued(db: DB, task: Task) -> Agent:
    """Start a queued task now that its dependencies are met, cutting its
    branch from the caller workspace's current (updated) base."""
    caller_ws = db.get_workspace(task.caller_ws_id)
    if caller_ws is None:
        raise agents.AgentError(f"workspace {task.caller_ws_id} for queued task {task.id} is gone")
    caller = db.get_agent(task.caller_id) if task.caller_id else None
    worker, _wws = agents.delegate(
        db, caller, caller_ws, task.profile, task.task_text, task.mode,
        isolate=bool(task.isolate), branch=task.branch, done_when=task.done_when,
        plan_first=None if task.plan_first is None else bool(task.plan_first),
    )
    db.update_task(task.id, agent_id=worker.id, state="started", started_at=time.time())
    savings.attach_agent(db, task.id, worker.id)
    return worker


def on_merged(db: DB, ws: Workspace) -> None:
    """``ws``'s branch was just merged into its base: mark its own task
    'merged' (so dependents judge it correctly, and it stops counting as
    started), then start any pending task that was only waiting on it and now
    has every dependency merged, and tell its caller. A pending task may be
    in another repo of the session (brindle.repos); a branch-name dependency
    only matches within ``ws``'s own repo."""
    worker = agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    if worker:
        for t in db.list_tasks(ws.repo_root, state="started"):
            if t.agent_id == worker.id:
                db.update_task(t.id, state="merged")
    for t in db.list_tasks(state="pending"):
        deps = _loads(t.depends_on)
        worker_id = worker.id if worker else None
        if not any(_dep_matches(d, ws, worker_id) and (d == worker_id or t.repo_root == ws.repo_root)
                   for d in deps):
            continue
        caller_ws = db.get_workspace(t.caller_ws_id)
        if not caller_ws:
            continue
        try:
            if unmet_dependencies(db, caller_ws, deps):
                continue
        except agents.AgentError as e:
            _cancel(db, t.id, str(e))
            continue
        try:
            new_worker = start_queued(db, t)
        except agents.AgentError:
            continue
        if t.caller_id:
            try:
                text = f"Started {new_worker.id} (was waiting on {dep_ref})."
                new_ws = db.get_workspace(new_worker.workspace_id)
                warning = agents.add_dirs_warning(new_worker, new_ws) if new_ws else None
                if warning:
                    text += f"\nWarning: {warning}"
                agents.send_message(db, t.caller_id, text, sender_id=None)
            except agents.AgentError:
                pass


def on_removed_unmerged(db: DB, ws: Workspace) -> None:
    """``ws`` was removed while its branch still had commits not in its base:
    cancel any pending task depending on it -- and, recursively, any pending
    task that in turn depends on those -- and tell each one's caller."""
    worker = agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    worker_id = worker.id if worker else None
    for t in db.list_tasks(state="pending"):
        if any(_dep_matches(d, ws, worker_id) and (d == worker_id or t.repo_root == ws.repo_root)
               for d in _loads(t.depends_on)):
            _cancel(db, t.id, f"it was waiting on {dep_ref}, which was removed unmerged.")


def list_text(db: DB, repo_root: str) -> str:
    """Pending and cancelled tasks: started ones already show in list_agents."""
    lines = []
    for t in db.list_tasks(repo_root):
        if t.state not in ("pending", "cancelled"):
            continue
        deps = _loads(t.depends_on)
        parts = [t.id, t.state, t.profile, f"branch={t.branch or '(auto)'}"]
        if deps:
            parts.append(f"depends_on={','.join(deps)}")
        if t.files:
            parts.append(f"files={','.join(_loads(t.files))}")
        lines.append(" ".join(parts))
    return "\n".join(lines) or "No pending or cancelled tasks."
