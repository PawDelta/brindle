"""The machine-wide worker limit, shared fairly across sessions.

Each session is capped by its own ``max_agents``; this caps every session's
workers together at the person's ``machine_max_workers``
(``~/.brindle/config.json``; 0: no limit), by default the machine's core
count, up to twice that while the machine is lightly loaded (``limit``). A
session may use idle capacity, but while other sessions have work queued
the free slots go round-robin: the next start is the oldest queued task of
the session running the fewest workers (``order``), so a session at its fair
share (the limit over the sessions with work, at least 1) waits behind the
others. Only new starts are gated: running workers are never stopped to
rebalance. A start that may not go now waits for room like any other
(``tasks.task_wait``) and starts, in this order, as slots free.
"""

from __future__ import annotations

import time

from brindle.db import DB, Task

WORKER_MODES = ("handoff", "handoff_detached", "assign")


def limit() -> int:
    """The machine-wide worker limit: the person's ``machine_max_workers``
    (0 means none); unset, the machine's core count, or up to twice that
    while under half its cores are busy (``spawn._load``)."""
    from brindle import spawn
    from brindle.config import user_settings

    value = user_settings().get("machine_max_workers")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    cores = spawn._cores()
    return 2 * cores if spawn._load() < cores / 2 else cores


def _session(db: DB, agent_id: str | None) -> str:
    from brindle import autopilot

    return autopilot.root_of(db, agent_id) if agent_id else ""


def running(db: DB) -> dict[str, int]:
    """Workers at work per session root: running process-launching workers,
    plus tasks claimed to start whose worker doesn't exist yet."""
    from brindle import agents

    counts: dict[str, int] = {}
    for a in db.list_agents():
        if a.mode in WORKER_MODES and a.status in ("starting", "processing") and agents.runs_process(a):
            s = _session(db, a.id)
            counts[s] = counts.get(s, 0) + 1
    for t in db.list_tasks(state="starting"):
        if not t.agent_id:
            s = _session(db, t.caller_id)
            counts[s] = counts.get(s, 0) + 1
    return counts


def queued(db: DB) -> list[Task]:
    """Tasks ready to start but for room: pending, not held by an unmerged
    dependency (waiting for room, or with none), in a session not paused,
    and starting a process of brindle's."""
    from brindle import agents, tasks

    return [t for t in db.list_tasks(state="pending")
            if (t.waiting is not None or not tasks._loads(t.depends_on))
            and not tasks.session_paused(db, t) and agents.launches_process(t.profile, t.repo_root)]


def order(db: DB, extra: Task | None = None) -> list[str]:
    """The ids of the queued tasks (and ``extra``) in the order they get
    slots: round-robin across sessions, each pick going to the session with
    the fewest workers at work counting earlier picks, oldest task first."""
    work = running(db)
    heads: dict[str, list[Task]] = {}
    for t in [*(t for t in queued(db) if extra is None or t.id != extra.id), *([extra] if extra else [])]:
        heads.setdefault(_session(db, t.caller_id), []).append(t)
    for line in heads.values():
        line.sort(key=lambda t: t.created_at)
    out: list[str] = []
    while heads:
        s = min(heads, key=lambda s: (work.get(s, 0), heads[s][0].created_at))
        out.append(heads[s].pop(0).id)
        work[s] = work.get(s, 0) + 1
        if not heads[s]:
            del heads[s]
    return out


def start_reason(db: DB, caller_id: str | None, repo_root: str, profile: str) -> str | None:
    """``wait_reason`` for a worker started directly, with no queued task
    (a branch's follow-up worker): it queues behind those already waiting."""
    new = Task(id="(new)", repo_root=repo_root, agent_id=None, caller_id=caller_id, caller_ws_id="",
               profile=profile, task_text="", mode="assign", isolate=0, branch=None, done_when=None,
               files=None, depends_on=None, state="pending", created_at=time.time())
    return wait_reason(db, new)


def wait_reason(db: DB, t: Task) -> str | None:
    """Why task ``t`` must wait for its turn (the limit is reached, or other
    sessions' queued work goes first), or None: it may take a slot now."""
    cap = limit()
    if cap <= 0:
        return None
    work = running(db)
    total = sum(work.values())
    picks = order(db, t)
    if picks.index(t.id) < cap - total:
        return None
    mine = _session(db, t.caller_id)
    sessions = {s for s, n in work.items() if n} | {_session(db, q.caller_id) for q in queued(db)} | {mine}
    share = max(1, cap // len(sessions))
    used = f"session {mine or '-'} uses {work.get(mine, 0)} of {cap} (its share is {share})"
    if total >= cap:
        return f"machine worker limit reached, {total} of {cap} running; fair share, {used}"
    return f"fair share, {used}; other sessions' queued work starts first"
