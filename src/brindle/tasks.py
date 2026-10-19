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
import re
import time
from dataclasses import dataclass
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


def _issue_numbers(text: str | None) -> set[str]:
    return set(re.findall(r"#(\d+)", text or ""))


def _reported_unmerged(db: DB, repo_root: str) -> list[tuple[Task, Agent, Workspace]]:
    """Started tasks in ``repo_root``, from any session, whose worker has
    reported but whose branch isn't merged into the repo's default branch yet:
    the work still exists and will land later."""
    out = []
    for t in db.list_tasks(repo_root, state="started"):
        agent = db.get_agent(t.agent_id) if t.agent_id else None
        if agent is None or agent.result is None:
            continue
        other_ws = db.get_workspace(agent.workspace_id)
        if other_ws is None or other_ws.kind != "worktree":
            continue
        if git.merged_into_default(repo_root, other_ws.branch):
            continue
        out.append((t, agent, other_ws))
    return out


def _where(other_ws: Workspace | None) -> str:
    if other_ws is None:
        return "?"
    session = f", session {other_ws.base_branch}" if other_ws.base_branch else ""
    return f"{other_ws.branch}{session}"


@dataclass
class Clash:
    """What a new task's ``files`` (or issue number) collides with: the
    ``warning`` text, the worker holding the files (None for an intent, which
    has no worker to wait for) and whether it belongs to another session."""
    warning: str
    holder: str | None = None
    holder_root: str | None = None
    cross_session: bool = False


def _own_roots(db: DB, ws: Workspace, caller_id: str | None) -> set[str]:
    from brindle import autopilot
    if caller_id:
        return {autopilot.root_of(db, caller_id)}
    return {autopilot.root_of(db, a.id) for a in db.list_agents(ws.id)}


def _held(db: DB, own: set[str], warning: str, holder: str | None) -> Clash:
    from brindle import autopilot
    root = autopilot.root_of(db, holder) if holder else None
    return Clash(warning, holder, root, root is not None and root not in own)


def _intent_overlap(db: DB, ws: Workspace, files: list[str], own: set[str]) -> Clash | None:
    """A clash if ``files`` matches a live intent another session announced
    for this repo, naming that session and its goal line."""
    from brindle import knowledge
    for intent in knowledge.intents(db, knowledge.repo_key(ws.repo_root)):
        if intent["status"] != "live" or intent["session_root"] in own:
            continue
        for mine in files:
            for theirs in intent["paths"]:
                if _glob_match(mine, theirs):
                    goal = intent["summary"].strip().splitlines()[0] if intent["summary"].strip() else ""
                    return Clash(f"overlaps with session {intent['session_root']}'s intent on {theirs}"
                                 + (f" ({goal})" if goal else "")
                                 + "; consider depends_on or merging first",
                                 None, intent["session_root"], True)
    return None


def find_clash(db: DB, ws: Workspace, files: list[str] | None, task_text: str | None = None,
               caller_id: str | None = None) -> Clash | None:
    """What ``files`` overlaps: another active task's declared or
    actually-changed files, a task from another session whose worker has
    reported but whose branch isn't merged yet, a task text naming the same
    issue (#NNN) as one of those, or another session's live intent. A task is
    another session's when its root differs from ``caller_id``'s (without
    one: from the roots of ``ws``'s agents)."""
    own = _own_roots(db, ws, caller_id)
    if files:
        for t, _agent, other_ws in running_tasks(db, ws.repo_root):
            candidates = list(_loads(t.files))
            if other_ws:
                candidates += _changed_files(other_ws)
            for mine in files:
                for theirs in candidates:
                    if _glob_match(mine, theirs):
                        branch = other_ws.branch if other_ws else "?"
                        return _held(db, own, f"overlaps with {t.agent_id} ({branch}) on {theirs}; "
                                     "consider depends_on or merging first", t.agent_id)
    if files:
        clash = _intent_overlap(db, ws, files, own)
        if clash:
            return clash
    mine_issues = _issue_numbers(task_text)
    if not files and not mine_issues:
        return None
    for t, _agent, other_ws in _reported_unmerged(db, ws.repo_root):
        where = f"{t.agent_id} ({_where(other_ws)})"
        candidates = _loads(t.files) + _changed_files(other_ws)
        for mine in files or []:
            for theirs in candidates:
                if _glob_match(mine, theirs):
                    return _held(db, own, f"overlaps with {where}, reported but not yet merged, on {theirs}; "
                                 "consider depends_on or merging first", t.agent_id)
        shared = mine_issues & _issue_numbers(t.task_text)
        if shared:
            return _held(db, own, f"names issue #{min(shared, key=int)} like {where}, reported but not yet "
                         "merged; check it isn't already fixed there", t.agent_id)
    return None


def overlap_warning(db: DB, ws: Workspace, files: list[str] | None,
                    task_text: str | None = None) -> str | None:
    """``find_clash``'s warning text, or None."""
    clash = find_clash(db, ws, files, task_text)
    return clash.warning if clash else None


def overlap_mode(setting: str | None, cross_session: bool) -> str:
    """How a clash is handled: the repo's ``overlap`` setting ("block", "warn"
    or "queue") when it has one; unset, "block" within a session and "queue"
    behind another session's task, whose work is never cancelled or replaced."""
    if setting in ("block", "warn", "queue"):
        return setting
    return "queue" if cross_session else "block"


def _goal_line(db: DB, root_id: str) -> str:
    """A session's goal line: its autopilot goal, else its live intent."""
    pilot = db.get_autopilot(root_id)
    text = pilot.goal if pilot and pilot.goal else ""
    if not text:
        row = db.conn.execute("SELECT summary FROM intents WHERE session_root = ? AND status = 'live' "
                              "ORDER BY updated_at DESC", (root_id,)).fetchone()
        text = row["summary"] if row else ""
    return text.strip().splitlines()[0] if text.strip() else ""


def notify_clash(db: DB, caller_root: str, clash: Clash, outcome: str) -> None:
    """Tell both sessions' supervisors about a cross-session clash, each
    naming the other session's goal line. ``outcome`` says what happened to
    the new task (queued, refused, started with a warning)."""
    from brindle import gates
    if not clash.holder_root:
        return
    what = clash.warning.split(";")[0]
    for to, other in ((caller_root, clash.holder_root), (clash.holder_root, caller_root)):
        goal = _goal_line(db, other)
        who = f"session {other}" + (f" (goal: {goal})" if goal else "")
        if to == caller_root:
            text = f"[brindle] Your new task {what}, held by {who}: {outcome}."
        else:
            text = (f"[brindle] A new task from {who} {what}, your session's work: "
                    f"{outcome}. Your work is not changed.")
        if db.get_agent(to):
            gates.tell(db, to, text)


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
        if other_ws.kind != "worktree":
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
    from brindle import identity_lock

    for dep in depends_on:   # a branch from another identity's session can't be pulled in
        agent = db.get_agent(dep)
        found = [db.get_workspace(agent.workspace_id)] if agent else [
            w for w in db.find_workspaces(caller_ws.repo_root) if w.branch == dep]
        for w in found:
            identity_lock.ensure_workspace(db, w)   # a missing workspace (None) is never locked
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


def pending_duplicate(db: DB, caller_id: str | None, repo_root: str, task_text: str) -> Task | None:
    """A task ``caller_id`` already queued in ``repo_root`` with the same brief,
    so a second assign of it leaves the first in place instead of adding one."""
    return next((t for t in db.list_tasks(repo_root, state="pending")
                 if t.caller_id == caller_id and t.task_text == task_text), None)


def _holder_start(db: DB, caller_ws: Workspace, depends_on: list[str]) -> str | None:
    """The commit a queued task's branch must start from: a dependency's
    merge commit that ``caller_ws``'s branch lacks, as when the holder merged
    into another session's branch; None to start from the caller's branch."""
    for dep in depends_on:
        agent = db.get_agent(dep)
        dep_ws = db.get_workspace(agent.workspace_id) if agent else None
        if dep_ws is None or dep_ws.repo_root != caller_ws.repo_root:
            continue
        sha = db.merged_sha(dep_ws.id)
        if not sha or not git.ok(["cat-file", "-e", f"{sha}^{{commit}}"], caller_ws.path):
            continue   # no merge record, or its commit is gone
        if not git.ok(["merge-base", "--is-ancestor", sha, caller_ws.branch], caller_ws.path):
            return sha
    return None


def start_queued(db: DB, task: Task) -> Agent:
    """Start a queued task now that its dependencies are met, cutting its
    branch from the caller workspace's current (updated) base, or from a
    dependency's merge commit that base lacks (``_holder_start``)."""
    from brindle.config import load_repo_config

    caller_ws = db.get_workspace(task.caller_ws_id)
    if caller_ws is None:
        raise agents.AgentError(f"workspace {task.caller_ws_id} for queued task {task.id} is gone")
    caller = db.get_agent(task.caller_id) if task.caller_id else None
    profile = task.profile
    cfg = load_repo_config(caller_ws.repo_root)
    weight_order = {"light": 0, "medium": 1, "heavy": 2, "advanced": 3, "reasoning": 3, "complex": 3}
    ceiling = cfg.max_weight if (cfg.max_weight and cfg.max_weight in weight_order) else None
    if ceiling and profile:
        from brindle.autopilot import profile_weight, choose_profile
        pw = profile_weight(profile, cfg, caller_ws.repo_root)
        if weight_order.get(pw, 0) > weight_order[ceiling]:
            if task.weight:
                effective_weight = ceiling if weight_order.get(task.weight, 0) > weight_order[ceiling] else task.weight
                routed_profile, _learned = choose_profile(
                    db, task.caller_id or "", caller_ws.repo_root,
                    requested=None,
                    weight=effective_weight,
                    task=task.task_text,
                    files=_loads(task.files),
                )
                profile = routed_profile
                db.update_task(task.id, profile=profile, weight=effective_weight)
            else:
                choose_profile(db, task.caller_id or "", caller_ws.repo_root, requested=profile, weight=None)

    worker, _wws = agents.delegate(
        db, caller, caller_ws, profile, task.task_text, task.mode,
        isolate=bool(task.isolate), branch=task.branch, done_when=task.done_when,
        plan_first=None if task.plan_first is None else bool(task.plan_first),
        start=_holder_start(db, caller_ws, _loads(task.depends_on)), files=_loads(task.files),
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
        worker_id = worker.id if worker else None
        if not any(_dep_matches(d, ws, worker_id) and (d == worker_id or t.repo_root == ws.repo_root)
                   for d in _loads(t.depends_on)):
            continue
        _start_if_ready(db, t, dep_ref)


def _start_if_ready(db: DB, t: Task, dep_ref: str) -> bool:
    """Start pending task ``t`` if every dependency is merged, and tell its
    caller. The task is claimed first, so a second call (a sweep racing a
    merge) finds it taken. A dependency that can never merge cancels it; a
    start that fails cancels it too, with the reason sent to its caller
    (``requeue`` makes a new one). Returns whether it started."""
    caller_ws = db.get_workspace(t.caller_ws_id)
    if not caller_ws:
        return False
    try:
        if unmet_dependencies(db, caller_ws, _loads(t.depends_on)):
            return False
    except agents.AgentError as e:
        _cancel(db, t.id, str(e))
        return False
    if not db.claim_task(t.id):
        return False
    try:
        new_worker = start_queued(db, t)
    except agents.AgentError as e:
        db.update_task(t.id, state="pending")
        _cancel(db, t.id, f"it could not start: {e}")
        return False
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
    return True


def sweep(db: DB) -> int:
    """Start every pending task whose dependencies are all merged, whatever
    stranded it (a merge run cut off before ``on_merged``, a branch merged by
    hand). A started task whose workspace has a merge record is first marked
    merged, as ``on_merged`` would have. Safe to repeat: ``_start_if_ready``
    claims each task. Returns how many started."""
    pending = db.list_tasks(state="pending")
    if not pending:
        return 0
    for t in db.list_tasks(state="started"):
        agent = db.get_agent(t.agent_id) if t.agent_id else None
        if agent and db.merged_sha(agent.workspace_id):
            db.update_task(t.id, state="merged")
    started = 0
    for t in pending:
        deps = _loads(t.depends_on)
        started += _start_if_ready(db, t, ", ".join(deps) if deps else "its dependencies")
    return started


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


def requeue(db: DB, caller: Agent | None, caller_ws: Workspace, task_id: str,
            depends_on: list[str] | None = None) -> str:
    """Make a new task from a cancelled one: same brief, done_when, files,
    profile, weight and (unless ``depends_on`` overrides them) dependencies.
    It starts at once when nothing is left to wait on. Raises ``ValueError``
    if the task is unknown or not cancelled, or a dependency can never merge."""
    old = db.get_task(task_id)
    if old is None:
        raise ValueError(f"No task {task_id}. list_tasks shows what's queued.")
    if old.state != "cancelled":
        raise ValueError(f"Task {task_id} is {old.state}, not cancelled: only a cancelled task can be requeued.")
    ws = db.get_workspace(old.caller_ws_id) or caller_ws
    deps = _loads(old.depends_on) if depends_on is None else list(depends_on)
    try:
        unmet = unmet_dependencies(db, ws, deps)
    except agents.AgentError as e:
        raise ValueError(f"{e}. Pass depends_on to replace the dependencies (an empty list for none).") from e
    new = enqueue(
        db, caller, ws, old.profile, old.task_text, old.mode, isolate=bool(old.isolate),
        branch=old.branch, done_when=old.done_when, files=_loads(old.files), depends_on=deps,
        plan_first=None if old.plan_first is None else bool(old.plan_first), weight=old.weight,
    )
    if unmet:
        return f"Requeued task {old.id} as {new.id}, waiting on {', '.join(unmet)}."
    try:
        worker = start_queued(db, new)
    except agents.AgentError as e:
        _cancel(db, new.id, f"it could not start: {e}")
        raise ValueError(f"Nothing left to wait on, but the worker could not start: {e}") from e
    return f"Requeued task {old.id} as {new.id}: nothing to wait on, started worker {worker.id}."


def _matches(glob: str, path: str) -> bool:
    g = _normpath(glob)
    return _glob_match(g, path) or path.startswith(g.rstrip("/") + "/")


def dry_run_text(db: DB, ws: Workspace, profile: str, why: list[str], files: list[str] | None,
                 depends_on: list[str] | None, overlap: str | None,
                 caller_id: str | None = None) -> str:
    """What ``assign``/``handoff`` would do, without doing it: the profile, the
    files each glob matches in ``ws``'s checked-out branch, overlaps with
    active workers, and whether it would queue."""
    lines = ["Dry run: nothing started or queued.", f"profile: {profile}"]
    lines += [f"profile: {w}" for w in why]
    if files:
        tracked = git.paths(["ls-files"], ws.path)
        for glob in files:
            hits = [p for p in tracked if _matches(glob, p)]
            if hits:
                shown = ", ".join(hits[:5]) + (f" and {len(hits) - 5} more" if len(hits) > 5 else "")
                lines.append(f"files: {glob} matches {len(hits)}: {shown}")
            else:
                lines.append(f"Warning: files glob {glob!r} matches nothing in {ws.branch}, "
                             "so overlap detection won't cover it")
    else:
        lines.append("files: none declared, so overlap detection won't cover this task")
    clash = find_clash(db, ws, files, caller_id=caller_id)
    warning = clash.warning if clash else None
    mode = overlap_mode(overlap, clash.cross_session) if clash else "warn"
    if mode == "queue" and not (clash and clash.holder):
        mode = "warn"
    if clash:
        done = {"block": "refused", "warn": "warned", "queue": "queued"}[mode]
        lines.append(f"overlap: this task {warning} ({done} by the repo's overlap setting)")
    else:
        lines.append("overlap: none")
    try:
        unmet = unmet_dependencies(db, ws, depends_on)
    except agents.AgentError as e:
        lines.append(f"queue: would not start: {e}")
    else:
        if unmet:
            lines.append(f"queue: would queue until {', '.join(unmet)} merge"
                         f"{'s' if len(unmet) == 1 else ''}")
        elif mode == "block":
            lines.append("queue: would not start (overlap)")
        elif mode == "queue":
            lines.append(f"queue: would queue until {clash.holder} merges")
        else:
            lines.append("queue: would start now")
    return "\n".join(lines)


def list_text(db: DB, repo_root: str, full: bool = False) -> str:
    """Pending and cancelled tasks: started ones already show in list_agents.
    ``full`` adds each one's brief and done_when."""
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
        line = " ".join(parts)
        if full:
            line += f"\n  task_text: {t.task_text}"
            if t.done_when:
                line += f"\n  done_when: {t.done_when}"
        lines.append(line)
    return "\n".join(lines) or "No pending or cancelled tasks."
