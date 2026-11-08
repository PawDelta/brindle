"""A worker's question or stall, as an open item its supervisor has to close.

A worker that asks its supervisor something and goes idle waits on that
supervisor alone, and repeating the news doesn't make anyone act (one worker
sat 14 hours under some 80 copies of it). So the first notice opens an item:

- It closes when the supervisor messages the worker, requeues its task or
  removes its workspace, and by itself when the worker reports or works again.
- While it is open the supervisor can't end its turn (``stop_block``), until
  the hour is up and brindle's takeover needs it idle.
- It is never repeated: each later notice says something new (``sweep``).
- An hour on, with the supervisor idle, brindle takes the work over itself
  (``reassign``): a fresh worker from the supervisor's branch, and the stuck
  one retired, its branch kept and its uncommitted changes in the trash. If
  brindle can't, the item is the supervisor's again, and so are its stops.

Reviewers aren't tracked: the pipeline restarts or replaces its own.
"""

from __future__ import annotations

import logging
import time

from brindle import agents, bus, disk, git, identity_lock, pipeline, tasks, tmux, workspaces
from brindle.db import DB, Agent, OpenItem, Task, Workspace
from brindle.providers import get_provider

log = logging.getLogger("brindle")

TRACKED_MODES = ("assign", "handoff_detached")   # a synchronous handoff's caller is already waiting
ESCALATE_AFTER = (1200.0, 2700.0)   # seconds open before the second and the third notice
AUTO_REASSIGN_AFTER = 3600.0
GAVE_UP = len(ESCALATE_AFTER) + 1   # level: brindle couldn't take it over, and said so
CLAIM_EXPIRES = 900.0
ASKED_MARK = "asked-supervisor"     # agent_marks: what it last asked, until answered
DETAIL_LIMIT = 200
WORKING = ("processing", "starting")


def _clip(text: str) -> str:
    return " ".join(text.split())[:DETAIL_LIMIT]


def _stop(worker: Agent) -> float:
    return worker.status_since or worker.created_at


def _on_hold(worker: Agent) -> bool:
    """Waiting by design: its session is paused, or its plan is with whoever approves plans."""
    return worker.status == "paused" or worker.plan_state == "proposed"


def told_of_this_stop(db: DB, worker: Agent) -> bool:
    """Whether an open item already covers the stop ``worker`` is on."""
    item = db.get_open_item(worker.id)
    return item is not None and item.stop_at == _stop(worker)


def track(db: DB, worker: Agent, what: str, now: float | None = None) -> bool:
    """Open ``worker``'s item, once its supervisor has had the first notice.
    ``what``: how it stopped, unless it had asked something. A later stop
    replaces the item of an earlier one, with a fresh clock: it worked in
    between. False if this stop has one."""
    if (worker.mode not in TRACKED_MODES or not worker.parent_id or _on_hold(worker)
            or told_of_this_stop(db, worker)):
        return False
    db.close_open_item(worker.id)
    asked = db.mark(worker.id, ASKED_MARK)
    return db.add_open_item(worker.id, worker.parent_id, "question" if asked else "stalled",
                            _clip(asked or what), time.time() if now is None else now, _stop(worker))


def close(db: DB, worker_id: str) -> None:
    db.close_open_item(worker_id)
    db.set_mark(worker_id, ASKED_MARK, None)


def on_message(db: DB, sender: Agent, to_id: str, body: str) -> None:
    """``sender``'s send_message to ``to_id`` went through: a supervisor's
    closes its worker's item, a worker's to its supervisor is its question."""
    target = db.get_agent(to_id)
    if target is not None and target.parent_id == sender.id:
        close(db, to_id)
    elif sender.parent_id == to_id:
        db.set_mark(sender.id, ASKED_MARK, _clip(agents.mask(body)))


def _working(worker: Agent, panes=None) -> bool:
    return worker.status in WORKING and agents.is_alive(worker, panes)


def _settled(worker: Agent) -> bool:
    """Closed for good. One that died after an earlier report is marked done
    (cull.note_dead_panes), but in a fix round its work still isn't finished."""
    return (worker.dismissed_at is not None or worker.status == "done") and worker.pipeline != "fixing"


def _takeover_due(item: OpenItem, now: float) -> bool:
    return now - item.opened_at >= AUTO_REASSIGN_AFTER and item.level < GAVE_UP


def _live(db: DB, supervisor_id: str | None = None, panes=None) -> list[tuple[OpenItem, Agent]]:
    """The items still waiting, with their workers. Closes those that ended
    some way nobody reported here (a worktree pruned, the worker back at work);
    one whose worker is on hold just waits."""
    live = []
    for item in db.open_items(supervisor_id):
        worker = db.get_agent(item.worker_id)
        if (worker is None or db.get_agent(item.supervisor_id) is None or _settled(worker)
                or _working(worker, panes)):
            close(db, item.worker_id)
        elif not _on_hold(worker):
            live.append((item, worker))
    return live


def _named(db: DB, item: OpenItem, worker: Agent) -> str:
    ws = db.get_workspace(worker.workspace_id)
    where = f" (branch `{ws.branch}`)" if ws else ""
    said = "asked you" if item.kind == "question" else "is stuck"
    return f"Worker {worker.id}{where} {said}: {item.detail}"


def stop_block(db: DB, supervisor: Agent) -> dict | None:
    """The Stop hook's "block" while ``supervisor`` has open items, naming the
    first of them in the one line the chat shows. An item whose takeover is
    due doesn't block: that waits for this supervisor to be idle."""
    now = time.time()
    items = [(i, w) for i, w in _live(db, supervisor.id) if not _takeover_due(i, now)]
    if not items:
        return None
    named = "\n".join(_named(db, i, w) for i, w in items)
    return {"decision": "block", "reason": (
        f"[brindle autopilot] {named}\n\nYou can't end your turn with this open. For each worker: "
        "send_message it what it needs, requeue(<worker id>) to give its task to a fresh worker, "
        "or remove_workspace if the work is abandoned. Don't wait for the user: this is yours to settle.")}


def _escalate(db: DB, item: OpenItem, worker: Agent, age: float) -> bool:
    if not db.set_open_item_level(worker.id, item.level, item.level + 1):
        return False
    minutes = int(age // 60)
    if item.level == 0:
        body = (f"Still open after {minutes} min. {_named(db, item, worker)}. Nothing else will wake it: "
                f"send_message it what it needs, requeue(\"{worker.id}\") or remove_workspace.")
    else:
        body = (f"Last notice, {minutes} min open. {_named(db, item, worker)}. Soon after the hour, once "
                "you are idle, brindle retires it and gives its task to a fresh worker from your current branch.")
    try:
        agents.send_message(db, item.supervisor_id, body, sender_id=worker.id)
    except agents.AgentError:
        db.set_open_item_level(worker.id, item.level + 1, item.level)   # the next sweep sends it
        return False
    return True


def sweep(db: DB, now: float, panes) -> list[str]:
    """One pass of the cull sweep: the next notice of each open item that is
    due one, and brindle's takeover of those past the hour. Runs in every
    brindle process, so each step is claimed in the database first."""
    done = []
    for item, worker in _live(db, panes=panes):
        if not agents.same_server(worker, panes):
            continue
        age = now - item.opened_at
        supervisor = db.get_agent(item.supervisor_id)
        try:
            if (_takeover_due(item, now) and supervisor is not None
                    and supervisor.status == "idle" and agents.is_alive(supervisor, panes)):
                line = reassign(db, item, now)
                if line:
                    done.append(line)
            elif item.level < len(ESCALATE_AFTER) and age >= ESCALATE_AFTER[item.level]:
                if _escalate(db, item, worker, age):
                    done.append(f"reminded {item.supervisor_id} of worker {worker.id}, level {item.level + 1}")
        except Exception:  # noqa: BLE001 - one worker's item must never stop the sweep
            log.exception("brindle: couldn't act on %s's open item", worker.id)
    return done


def for_task(db: DB, ref: str) -> OpenItem | None:
    """The open item of a worker named by its id or its task's."""
    task = db.get_task(ref)
    return db.get_open_item(task.agent_id if task and task.agent_id else ref)


def _came_back(db: DB, item: OpenItem, worker: Agent, ws: Workspace | None) -> bool:
    """Whether ``worker`` shows any sign of life since its item opened. When
    there's no telling, yes: wrongly leaving it costs a sweep, wrongly
    retiring it costs its work."""
    alive = agents.is_alive(worker)
    if _working(worker) or agents.command_running(db, worker) or (alive and agents.transcript_active(worker)):
        return True
    provider = get_provider(worker.provider)
    if alive and not (provider.uses_hooks and provider.announces_turns):
        last = tmux.window_activity(worker.tmux_window, server=agents.server_of(worker))
        if last is None or last > item.opened_at:
            return True
    from brindle import cull

    return cull._last_edit(ws) > item.opened_at


def _gone_or_back(db: DB, item: OpenItem, ws: Workspace | None) -> bool:
    """A fresh look: its item closed, or it shows life (see ``_came_back``)."""
    worker = db.get_agent(item.worker_id)
    return worker is None or db.get_open_item(item.worker_id) is None or _came_back(db, item, worker, ws)


def _why_not(db: DB, worker: Agent, ws: Workspace | None, caller_ws: Workspace | None, brief: str) -> str | None:
    """Why brindle mustn't remove ``ws`` for ``worker``, if so."""
    if ws is None or caller_ws is None or not brief:
        return "brindle can't find its workspace, your workspace in its repo, or its brief"
    if ws.kind != "worktree":
        return "it works in a shared checkout, not a worktree of its own"
    if any(workspaces.live_workers(db, o) for o in db.workspaces_sharing(ws.repo_root, path=ws.path, exclude_id=ws.id)):
        return "another live worker shares its worktree"
    if disk.live_in(db, ws.path, own=ws, stopping=[worker.id]):
        return "someone is attached to it, or a process of another session is in its worktree"
    return None


def _caller_ws(db: DB, supervisor: Agent, ws: Workspace | None, task: Task | None) -> Workspace | None:
    """The workspace its replacement is cut from: the supervisor's, or for a
    worker in a repo the session attached, the one its task was started from."""
    own = db.get_workspace(supervisor.workspace_id)
    if own is not None and ws is not None and own.repo_root == ws.repo_root:
        return own
    return db.get_workspace(task.caller_ws_id) if task else None


def _repoint(db: DB, worker: Agent, ws: Workspace, task: Task | None, new: Task) -> list[tuple[str, list[str], list[str]]]:
    """Queued tasks waiting on the stuck worker now wait on its replacement.
    Returns (task, what it waited on, what it waits on now) for each."""
    moved = []
    for t in tasks.queued(db):
        deps = tasks._loads(t.depends_on)
        now_deps = [f"task:{new.id}" if d in (worker.id, f"workspace:{ws.id}")
                    or (d == ws.branch and t.repo_root == ws.repo_root)
                    or (task is not None and tasks._refers_to(d, task, t.repo_root)) else d for d in deps]
        if t.id != new.id and now_deps != deps and tasks.set_dependencies(db, t.id, now_deps, deps):
            moved.append((t.id, deps, now_deps))
    return moved


def _tell(db: DB, supervisor_id: str, body: str) -> None:
    try:
        agents.send_message(db, supervisor_id, body)
    except agents.AgentError:
        db.enqueue(supervisor_id, body, None)


def _give_up(db: DB, item: OpenItem, worker: Agent, why: str, actor: Agent | None) -> str:
    """brindle can't take this one over: said once, and the item stays open."""
    db.set_open_item_level(worker.id, item.level, GAVE_UP)
    db.release_open_item_retire(worker.id)
    body = f"brindle didn't reassign worker {worker.id}: {why}. It still needs you. {_named(db, item, worker)}"
    if actor is None:
        _tell(db, item.supervisor_id, body)
    return body


def reassign(db: DB, item: OpenItem, now: float | None = None, *, actor: Agent | None = None) -> str | None:
    """Give the stuck worker's task to a fresh worker cut from the supervisor's
    current branch, and retire the stuck one the way remove_workspace would
    (its branch kept, its uncommitted changes in the trash). ``actor``: the
    supervisor who asked for it; otherwise it's the sweep's, and the supervisor
    is told. Returns what happened, or None if another sweep has it or the
    worker came back."""
    now = time.time() if now is None else now
    if not db.claim_open_item_retire(item.worker_id, now, now - CLAIM_EXPIRES):
        return None
    worker, supervisor = db.get_agent(item.worker_id), db.get_agent(item.supervisor_id)
    if worker is None or supervisor is None or db.get_open_item(item.worker_id) is None:
        db.release_open_item_retire(item.worker_id)
        return None
    if identity_lock.locked_by(worker.id):
        db.release_open_item_retire(worker.id)   # another identity's: that one's sweep can
        return None
    ws = db.get_workspace(worker.workspace_id)
    task = next((t for t in db.list_tasks(ws.repo_root) if t.agent_id == worker.id), None) if ws else None
    caller_ws = _caller_ws(db, supervisor, ws, task)
    brief = (task.task_text if task else worker.task) or ""
    why = _why_not(db, worker, ws, caller_ws, brief)
    if why:
        return _give_up(db, item, worker, why, actor)

    if _gone_or_back(db, item, ws):   # before telling anyone it's being removed
        db.release_open_item_retire(worker.id)
        return None

    unmerged_note = (f"\n\n(An earlier worker on this task stopped; its unmerged work is on branch "
                     f"`{ws.branch}`. Check `git log {ws.branch}` and reuse what fits. "
                     f"What it was stuck on: {item.detail})")
    pipeline.note_removed_unmerged(db, ws, actor=actor)
    new = tasks.enqueue(
        db, supervisor, caller_ws, worker.profile, brief + unmerged_note, "assign", isolate=True, branch=None,
        done_when=worker.done_when, files=tasks._loads(task.files) if task else None, depends_on=[],
        plan_first=None if worker.plan_first is None else bool(worker.plan_first), weight=worker.weight,
        analysis_doc=bool(task.analysis_doc) if task else False, unanswered=True,
    )
    moved = _repoint(db, worker, ws, task, new)

    def undo() -> None:
        for task_id, before, after in moved:
            tasks.set_dependencies(db, task_id, before, after)
        tasks.drop_unanswered(db, [new.id])

    # The last look, right before retiring it: the repo's event hooks ran just above.
    if _gone_or_back(db, item, ws):
        undo()
        db.release_open_item_retire(worker.id)
        return None
    try:
        workspaces.remove(db, ws, force=True, delete_branch=False)
    except (workspaces.WorkspaceError, git.GitError, OSError) as e:
        undo()
        return _give_up(db, item, worker, f"removing its worktree failed: {e}", actor)

    close(db, worker.id)
    if task is not None:
        db.update_task(task.id, state="cancelled")
        bus.publish(db, "task.cancelled", repo_root=task.repo_root, task_id=task.id,
                    reason=f"its worker {worker.id} was stuck; replaced by task {new.id}")
    tasks.move(db, new.id, tasks.UNANSWERED, "pending")
    try:
        started = tasks.start_requeued(db, db.get_task(new.id) or new, f"worker {worker.id}'s task")
    except ValueError as e:
        started = f"couldn't start its replacement: {e}"
    minutes = int((now - item.opened_at) // 60)
    body = (f"[brindle] Worker {worker.id} sat {minutes} min on an open item, so brindle took it over: retired it "
            f"(branch `{ws.branch}` kept; any uncommitted changes are in the trash). {started} "
            f"It was: {item.detail}")
    if actor is None:
        _tell(db, item.supervisor_id, body)
    return body
