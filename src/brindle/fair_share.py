"""The machine-wide worker limit, shared fairly across sessions.

Each session is capped by its own ``max_agents``; this caps every session's
workers together at the person's ``machine_max_workers``
(``~/.brindle/config.json``; 0: no limit), by default the machine's core
count, up to twice that while the machine is lightly loaded (``limit``). A
session may use idle capacity, but while other sessions have work queued
the free slots go round-robin: the next start is the oldest queued task of
the session running the fewest workers, ties going to the session whose last
turn was longest ago (``order``), so a session at its fair share (the limit
over the sessions with work, at least 1) waits behind the others. Only new
starts are gated: running workers are never stopped to rebalance. A start
that may not go now waits for room like any other (``tasks.task_wait``)
and starts, in this order, as slots free; the decision is made again under
``machine_lock`` as the slot is claimed.
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from collections.abc import Iterator

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
    plus tasks claimed to start whose worker doesn't link to them yet. One
    snapshot: a claimed task becoming its worker (``tasks.start_queued``: the
    worker's row, then the task's link to it) is then counted once or, in
    between, twice, never missed by reading the two before and after it."""
    from brindle import agents

    with db.snapshot():
        workers = [a for a in db.list_agents()
                   if a.mode in WORKER_MODES and a.status in ("starting", "processing")]
        claims = [t for t in db.list_tasks(state="starting") if not t.agent_id]
    counts: dict[str, int] = {}
    for agent_id in [a.id for a in workers if agents.runs_process(a)] + [t.caller_id for t in claims]:
        s = _session(db, agent_id)
        counts[s] = counts.get(s, 0) + 1
    return counts


@contextlib.contextmanager
def machine_lock() -> Iterator[None]:
    """One start decision at a time across every brindle process and repo:
    the fair-share check and the claim of the slot it allows happen together
    (``tasks._claim_fair``, ``agents.delegate``), so two sessions can't both
    take the last slot on stale counts."""
    from brindle.config import brindle_home

    locks = brindle_home() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / "fair-share.lock", "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _runnable(db: DB, t: Task) -> bool:
    """Whether pending task ``t`` could start but for room: every dependency
    merged now (a dependency added since it began waiting for room holds it;
    one brindle doesn't track as a task is judged by git ancestry, as its
    start judges it, so unknown readiness never takes a turn), its session
    not paused, and it starts a process of brindle's."""
    from brindle import agents, git, tasks

    deps = tasks._loads(t.depends_on)
    if deps:
        ws = db.get_workspace(t.caller_ws_id)
        try:
            if ws is None or tasks.unmet_dependencies(db, ws, deps, caller_id=t.caller_id,
                                                      dependent_task_id=t.id, lock_identities=False):
                return False
        except agents.AgentError:   # a cancelled dependency: it never starts
            return False
        except (git.GitError, OSError):   # readiness unknown (its worktree gone, git busy): no turn, others go on
            return False
    return not tasks.held(db, t) and agents.launches_process(t.profile, t.repo_root)


def queued(db: DB) -> list[Task]:
    """Pending tasks ready to start but for room (``_runnable``)."""
    return [t for t in db.list_tasks(state="pending") if _runnable(db, t)]


def _last_starts(db: DB) -> dict[str, float]:
    """When each session last had a task admitted (its ``started_at``): the
    round-robin's turn history, so a session that just had a turn waits for
    the others' even once its worker has finished."""
    last: dict[str, float] = {}
    for caller_id, at in db.conn.execute(
            "SELECT caller_id, MAX(started_at) FROM tasks WHERE started_at IS NOT NULL "
            "AND caller_id IS NOT NULL GROUP BY caller_id"):
        s = _session(db, caller_id)
        last[s] = max(last.get(s, at), at)
    return last


def order(db: DB, extra: Task | None = None) -> list[str]:
    """The ids of the queued tasks (and ``extra``) in the order they get
    slots: round-robin across sessions, each pick going to the session with
    the fewest workers at work counting earlier picks, then to the one whose
    last turn was longest ago, then its oldest task."""
    work = running(db)
    last = _last_starts(db)
    heads: dict[str, list[Task]] = {}
    for t in [*(t for t in queued(db) if extra is None or t.id != extra.id), *([extra] if extra else [])]:
        heads.setdefault(_session(db, t.caller_id), []).append(t)
    for line in heads.values():
        line.sort(key=lambda t: t.created_at)
    out: list[str] = []
    turn = max([time.time(), *last.values()])
    while heads:
        s = min(heads, key=lambda s: (work.get(s, 0), last.get(s, float("-inf")), heads[s][0].created_at))
        out.append(heads[s].pop(0).id)
        work[s] = work.get(s, 0) + 1
        turn += 1
        last[s] = turn
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
