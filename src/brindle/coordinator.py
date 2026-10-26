"""The coordinator: an opt-in agent above a repo's supervisors (brindle Pro).

It is one more client of the knowledge store (brindle.knowledge). The sweep
(brindle.cull) wakes one per repo, on the ``coordinator`` profile, when
something it watches has happened there since it last looked: a new or
changed intent, a cross-session clash, a decision, or a task queued longer
than :data:`QUEUED_TOO_LONG`. Off unless ``coordinator`` is on in the repo's
config.

It may message supervisors, hold, release or reorder queued tasks from any
session and propose decisions. It may never cancel or pause running workers,
discard branches, merge, accept decisions or edit code: what it may call is
:data:`TOOLS`, checked in brindle.mcp_server before any tool runs, not in its
prompt. For running work it can only ask the owning supervisor.

Every action is a ``coordinator`` event in ``kb_events`` with its reason and
what it changed, the affected supervisors are told, and ``brindle coordinator
undo <id>`` puts the queue back. ``brindle coordinator stop`` is the kill
switch.
"""

from __future__ import annotations

import json
import logging
import time

from brindle import agents, autopilot, bus, gates, knowledge, repos, tasks
from brindle.config import load_repo_config, set_local
from brindle.db import DB, Agent, Task, Workspace

log = logging.getLogger(__name__)

PROFILE = "coordinator"
MODE = "coordinator"      # the agent's mode: no session, no branch, never a worker
FEATURE = "coordinator"   # the brindle Pro entitlement
ACTIONS = ("advise", "hold_task", "release_task", "reorder_queue", "propose_decision")
# Everything the coordinator may call: its actions, what it reads, and
# report_result to finish.
TOOLS = frozenset({*ACTIONS, "list_tasks", "who_is_working", "decisions", "read_messages",
                   "report_result"})
WATCHED = (knowledge.INTENT, knowledge.CLASH, knowledge.DECISION)
QUEUED_TOO_LONG = 20 * 60
# A coordinator idle this long without reporting is closed, so the next
# event can wake a new one.
IDLE_AFTER = 10 * 60
# One still at work this long after it started, in any status (stuck on
# endless API retries, say), is closed and what it was woken for is given to
# the next one.
LIFETIME = 15 * 60
# How many of its earlier actions and reports a coordinator's brief recalls.
RECALL = 8
# What undo can put back; advice can't be unsent.
UNDOABLE = ("hold_task", "release_task", "reorder_queue", "propose_decision")
PRO_MESSAGE = ("The coordinator is a brindle Pro feature: `brindle account login` with a Pro "
               "plan, then set \"coordinator\": true in .brindle/config.json.")


class CoordinatorError(ValueError):
    pass


def is_coordinator(agent: Agent | None) -> bool:
    """Whether ``agent`` is a coordinator :func:`wake` started: only those
    may act. By mode alone, never by profile, so a worker a supervisor
    started on the coordinator profile gets no say over other sessions."""
    return agent is not None and agent.mode == MODE and agent.parent_id is None


def refusal(agent: Agent | None, tool: str) -> str | None:
    """Why ``agent`` may not call ``tool``, or None: the coordinator (and
    anything on its profile) only :data:`TOOLS`, everyone else none of its
    :data:`ACTIONS`."""
    if is_coordinator(agent) or (agent is not None and agent.profile == PROFILE):
        if tool not in TOOLS:
            return (f"The coordinator may not call {tool}: only {', '.join(sorted(TOOLS))}. "
                    "For running work, advise its supervisor.")
    elif tool in ACTIONS:
        return f"{tool} is the coordinator's tool; supervisors use cancel_task and depends_on."
    return None


def entitled() -> bool:
    from brindle.pro import license

    try:
        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 - fail closed
        return False


def require() -> None:
    """Raise ``CoordinatorError`` unless the plan includes the coordinator."""
    from brindle.pro import license

    try:
        license.require(FEATURE)
    except Exception as e:  # noqa: BLE001 - fail closed on anything
        raise CoordinatorError(PRO_MESSAGE) from e


def enabled(repo_root: str) -> bool:
    return load_repo_config(repo_root).coordinator is True


# -- actions ----------------------------------------------------------------------


def _home(db: DB, coord: Agent) -> Workspace:
    ws = db.get_workspace(coord.workspace_id)
    if ws is None:
        raise CoordinatorError("the coordinator's workspace is gone")
    return ws


def _task(db: DB, coord: Agent, task_id: str) -> Task:
    t = db.get_task(task_id)
    if t is None or t.repo_root != _home(db, coord).repo_root:
        raise CoordinatorError(f"No task {task_id} in this repo. list_tasks shows what's queued.")
    return t


def _reason(reason: str) -> str:
    text = knowledge._one_line(reason or "", 500)
    if not text:
        raise CoordinatorError("Give a reason: the supervisors are shown it.")
    return text


def _owner(db: DB, t: Task) -> str | None:
    """The session root whose task ``t`` is."""
    return autopilot.root_of(db, t.caller_id) if t.caller_id else None


def _record(db: DB, coord: Agent, action: str, reason: str, **payload) -> str:
    event_id = knowledge.append(db, knowledge.repo_key(_home(db, coord).repo_root),
                                knowledge.COORDINATOR,
                                {"action": action, "reason": reason, **payload}, actor=coord.id)
    bus.publish_agent(db, "coordinator.action", coord.id, action=action, event_id=event_id,
                      task_id=payload.get("task"))
    return event_id


def _tell(db: DB, roots, text: str) -> list[str]:
    told = []
    for root in dict.fromkeys(r for r in roots if r):
        if db.get_agent(root):
            gates.tell(db, root, text)
            told.append(root)
    return told


def _told(told: list[str]) -> str:
    return f" Told session(s) {', '.join(told)}." if told else ""


def _notice(what: str, reason: str, event_id: str) -> str:
    # The reason is the coordinator's own text: quoted, so it can't read as brindle's.
    return (f"[brindle] The coordinator {what}. Its reason: {json.dumps(reason, ensure_ascii=False)}. "
            f"To undo it: brindle coordinator undo {event_id[:8]}")


def hold_task(db: DB, coord: Agent, task_id: str, reason: str) -> str:
    """Hold a pending task so it doesn't start when its dependencies merge."""
    reason, t = _reason(reason), _task(db, coord, task_id)
    if not tasks.move(db, t.id, "pending", "held"):
        raise CoordinatorError(f"Task {t.id} is {db.get_task(t.id).state}: only a pending task can be "
                               "held. For running work, advise its supervisor.")
    eid = _record(db, coord, "hold_task", reason, task=t.id, before="pending", after="held")
    told = _tell(db, [_owner(db, t)], _notice(f"held your queued task {t.id}", reason, eid))
    return f"Held task {t.id} (event {eid[:8]}).{_told(told)}"


def release_task(db: DB, coord: Agent, task_id: str, reason: str) -> str:
    """Release a held task: it starts once its dependencies are merged."""
    reason, t = _reason(reason), _task(db, coord, task_id)
    if not tasks.move(db, t.id, "held", "pending"):
        raise CoordinatorError(f"Task {t.id} is {db.get_task(t.id).state}, not held.")
    eid = _record(db, coord, "release_task", reason, task=t.id, before="held", after="pending")
    told = _tell(db, [_owner(db, t)], _notice(f"released your queued task {t.id}", reason, eid))
    return f"Released task {t.id} (event {eid[:8]}).{_told(told)}"


def reorder_queue(db: DB, coord: Agent, task_id: str, after: list[str], reason: str) -> str:
    """Make a queued task also wait for ``after`` (agent ids or branches),
    such as another session's work on the same files."""
    reason, t = _reason(reason), _task(db, coord, task_id)
    old = tasks._loads(t.depends_on)
    added = [d for d in dict.fromkeys(after or []) if d and d not in old]
    if not added:
        raise CoordinatorError("Name at least one agent id or branch the task doesn't wait for yet.")
    unknown = [d for d in added if not tasks.known_dependency(db, t.repo_root, d)]
    if unknown:
        raise CoordinatorError(f"No agent or branch {', '.join(unknown)} in this repo.")
    caller_ws = db.get_workspace(t.caller_ws_id)
    if caller_ws is not None:
        try:
            tasks.unmet_dependencies(db, caller_ws, added, caller_id=t.caller_id,
                                     dependent_task_id=t.id)
        except agents.AgentError as e:
            raise CoordinatorError(str(e)) from e
    new = old + added
    if not tasks.set_dependencies(db, t.id, new, expect=old):
        raise CoordinatorError(f"Task {t.id} has started or changed since: list_tasks again.")
    eid = _record(db, coord, "reorder_queue", reason, task=t.id, before=old, after=new)
    told = _tell(db, [_owner(db, t)],
                 _notice(f"made your queued task {t.id} wait for {', '.join(added)}", reason, eid))
    return f"Task {t.id} now waits for {', '.join(new)} (event {eid[:8]}).{_told(told)}"


def advise(db: DB, coord: Agent, session: str, message: str) -> str:
    """Send a supervisor of this repo a message (the only way to ask about
    running work)."""
    root = db.get_agent(session)
    ws = db.get_workspace(root.workspace_id) if root else None
    if (root is None or root.parent_id or root.mode != "interactive" or ws is None
            or ws.repo_root != _home(db, coord).repo_root):
        raise CoordinatorError(f"No session {session} in this repo. who_is_working lists them.")
    text = _reason(message)
    eid = _record(db, coord, "advise", text, to=root.id)
    gates.tell(db, root.id, f"[brindle] The coordinator advises: {json.dumps(text, ensure_ascii=False)}")
    return f"Sent to session {root.id} (event {eid[:8]})."


def propose_decision(db: DB, coord: Agent, topic: str, decision: str, rationale: str,
                     paths: list[str] | None = None) -> str:
    """Propose a decision; a supervisor accepts it by recording it."""
    rationale = _reason(rationale)
    ws = _home(db, coord)
    d = knowledge.propose_decision(db, knowledge.repo_key(ws.repo_root), topic, decision,
                                   rationale, paths, actor=coord.id)
    eid = _record(db, coord, "propose_decision", rationale, decision=d["id"])
    told = _tell(db, knowledge.sessions_touching(db, ws.repo_root, d["scope"]),
                 _notice(f"proposes a decision, {knowledge._line(d)}; record_decision accepts it",
                         rationale, eid))
    return f"Proposed decision {d['id'][:8]} (event {eid[:8]}).{_told(told)}"


def finish(db: DB, coord: Agent, result: str) -> str:
    """The coordinator's report: it is done until the next wake. Kept as a
    ``report`` event, which the next wake's brief recalls, and in history,
    since the agent itself is closed."""
    from brindle import history

    ws = _home(db, coord)
    knowledge.append(db, knowledge.repo_key(ws.repo_root), knowledge.COORDINATOR,
                     {"action": "report", "text": knowledge._one_line(result or "", 2000)},
                     actor=coord.id)
    history.record_safely(db, ws.repo_root, "coordinator", agent=coord, with_usage=True,
                          branch=ws.branch, task=coord.task, result=result)
    db.set_result(coord.id, result)
    db.set_status(coord.id, "done")
    agents.close_later(coord.id)
    return "Recorded. The sweep wakes a coordinator again on the next event."


# -- undo -----------------------------------------------------------------------


def _event(db: DB, repo_key: str, ref: str):
    rows = db.conn.execute("SELECT * FROM kb_events WHERE repo_key = ? AND kind = ?"
                           " AND substr(id, 1, ?) = ?",
                           (repo_key, knowledge.COORDINATOR, len(ref), ref)).fetchall()
    return rows[0] if len(rows) == 1 else None


def undo(db: DB, repo_key: str, ref: str) -> str:
    """Reverse the coordinator action whose event id is ``ref`` (or starts
    with it): the task goes back to the state or dependencies it had, or the
    proposed decision is withdrawn. Refused once the task has moved on (it
    started, or was changed again) or the action was already undone."""
    ev = _event(db, repo_key, ref) if ref else None
    payload = json.loads(ev["payload"]) if ev else {}
    action = payload.get("action")
    if action not in UNDOABLE:
        raise CoordinatorError(f"No coordinator action {ref} to undo in this repo.")
    for row in db.conn.execute("SELECT payload FROM kb_events WHERE repo_key = ? AND kind = ?",
                               (repo_key, knowledge.COORDINATOR)):
        if json.loads(row["payload"]).get("undoes") == ev["id"]:
            raise CoordinatorError(f"Event {ev['id'][:8]} was already undone.")
    told: list[str] = []
    if action == "propose_decision":
        d = knowledge.get_decision(db, payload["decision"], repo_key)
        if d is None or d["status"] != "proposed":
            raise CoordinatorError("That proposal is no longer open.")
        knowledge.withdraw_proposal(db, d)
        what = f"withdrew proposed decision {d['id'][:8]}"
    else:
        t = db.get_task(payload["task"])
        if action == "reorder_queue":
            ok = t is not None and tasks.set_dependencies(db, t.id, payload["before"],
                                                          expect=payload["after"])
        else:
            ok = t is not None and tasks.move(db, t.id, payload["after"], payload["before"])
        if not ok:
            state = t.state if t else "gone"
            raise CoordinatorError(f"Task {payload['task']} has moved on since ({state}): nothing undone.")
        what = f"put task {t.id} back as it was before the coordinator's {action}"
        told = _tell(db, [_owner(db, t)], f"[brindle] A person {what} (event {ev['id'][:8]}).")
    knowledge.append(db, repo_key, knowledge.COORDINATOR, {"action": "undo", "undoes": ev["id"]})
    bus.publish(db, "coordinator.action", action="undo", undoes=ev["id"],
                task_id=payload.get("task"))
    return f"Undid {ev['id'][:8]}: {what}.{_told(told)}"


# -- waking and stopping -----------------------------------------------------------


def _roots_by_key(db: DB) -> dict[str, list[str]]:
    """Repo roots brindle has workspaces in with the coordinator on, by
    repo key: two clones of one repo share one coordinator."""
    out: dict[str, list[str]] = {}
    for root in sorted({ws.repo_root for ws in db.find_workspaces() if ws.repo_root}):
        try:
            if enabled(root):
                out.setdefault(knowledge.repo_key(root), []).append(root)
        except Exception:  # noqa: BLE001 - one unreadable repo mustn't stop the rest
            log.exception("brindle: couldn't read the coordinator setting of %s", root)
    return out


def running(db: DB, roots: list[str], now: float | None = None) -> list[Agent]:
    """Coordinators still at work in ``roots``. One idle past IDLE_AFTER
    without reporting, or alive past LIFETIME in any status, is closed and
    not counted; for the latter, what it was woken for is given back so the
    next sweep wakes a fresh one."""
    now = time.time() if now is None else now
    found = []
    for root in roots:
        for ws in db.find_workspaces(root):
            for a in db.list_agents(ws.id):
                if (a.mode != MODE or a.status in ("paused", "done")
                        or a.dismissed_at is not None):
                    continue
                hung = now - a.created_at > LIFETIME
                if hung or (a.status == "idle"
                            and now - (a.status_since or a.created_at) > IDLE_AFTER):
                    try:
                        agents.close(db, a.id)
                    except agents.AgentError:
                        pass
                    if hung:
                        _give_back(db, knowledge.repo_key(ws.repo_root), expired=a.id)
                    continue
                found.append(a)
    return found


def _last_wake(c, repo_key: str) -> tuple[int, dict]:
    """The last wake event's ``(rowid, payload)``, or ``(0, {})``."""
    for row in c.execute("SELECT rowid, payload FROM kb_events WHERE repo_key = ? AND kind = ?"
                         " ORDER BY rowid DESC", (repo_key, knowledge.COORDINATOR)):
        p = json.loads(row["payload"])
        if p.get("action") == "wake":
            return row["rowid"], p
    return 0, {}


def _mark(c, repo_key: str) -> tuple[int, int, list[str]]:
    """The last wake's ``(rowid, events seen through, tasks flagged)``."""
    rowid, p = _last_wake(c, repo_key)
    return rowid, p.get("through", 0), p.get("tasks", [])


def _give_back(db: DB, repo_key: str, **why) -> None:
    """Move the mark back to where it was before the last wake, so the next
    sweep wakes a coordinator again for what that wake claimed."""
    _, p = _last_wake(db.conn, repo_key)
    if "before" in p:
        through, flagged = p["before"]
        knowledge.append(db, repo_key, knowledge.COORDINATOR,
                         {"action": "wake", "through": through, "tasks": flagged, **why})


def unseen(db: DB, repo_key: str, roots: list[str], now: float) -> tuple[list, list[Task], int, list[str]]:
    """What a coordinator for ``repo_key`` hasn't been woken for: watched
    events since the last wake (proposals and withdrawals aside), tasks
    newly queued past QUEUED_TOO_LONG, the last event's rowid, and every
    task queued that long (flagged now or before)."""
    _, through, flagged = _mark(db.conn, repo_key)
    rows = db.conn.execute("SELECT rowid, * FROM kb_events WHERE repo_key = ? AND rowid > ?"
                           " ORDER BY rowid", (repo_key, through)).fetchall()
    last = rows[-1]["rowid"] if rows else through
    events = [r for r in rows if r["kind"] in WATCHED and not (
        r["kind"] == knowledge.DECISION and json.loads(r["payload"]).get("status") != "accepted")]
    old = [t for root in roots for t in db.list_tasks(root, state="pending")
           if now - t.created_at >= QUEUED_TOO_LONG]
    return events, [t for t in old if t.id not in flagged], last, [t.id for t in old]


def _q(text: str) -> str:
    return json.dumps(knowledge._one_line(text or "", 200), ensure_ascii=False)


def recent(db: DB, repo_key: str, limit: int) -> list:
    """The last ``limit`` coordinator events of ``repo_key`` worth recalling,
    newest first: actions, reports, undos, stops and wakes that failed or
    expired (not the plain wake marks)."""
    out = []
    for row in db.conn.execute("SELECT * FROM kb_events WHERE repo_key = ? AND kind = ?"
                               " ORDER BY rowid DESC", (repo_key, knowledge.COORDINATOR)):
        p = json.loads(row["payload"])
        if p.get("action") == "wake" and not (p.get("expired") or p.get("failed")):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def describe(event) -> str:
    """One line for a coordinator event, its own text quoted."""
    p = json.loads(event["payload"])
    action = p.get("action", "")
    if action == "report":
        return f"report: {_q(p.get('text', ''))}"
    if action == "undo":
        return f"a person undid {str(p.get('undoes', ''))[:8]}"
    if action == "stop":
        return "a person stopped the coordinator"
    if action == "wake":
        return (f"coordinator {p['expired']} ran past {LIFETIME // 60} min and was closed"
                if p.get("expired") else f"couldn't start: {_q(p.get('failed', ''))}")
    target = p.get("task") or p.get("to") or str(p.get("decision", ""))[:8]
    return f"{action} {target}: {_q(p.get('reason', ''))}"


def brief(events, stale: list[Task], recalled=()) -> str:
    lines = []
    for e in events:
        p = json.loads(e["payload"])
        if e["kind"] == knowledge.INTENT:
            lines.append(f"- session {e['session']} announced its intent: {_q(p.get('summary', ''))}"
                         f" on {', '.join(p.get('paths', [])) or 'no paths'}")
        elif e["kind"] == knowledge.CLASH:
            lines.append(f"- session {e['session']}'s new task clashed: {_q(p.get('warning', ''))}"
                         f" ({p.get('outcome', '')})")
        else:
            lines.append(f"- a decision was recorded: {knowledge._line(p | {'rationale': ''})}")
    for t in stale:
        deps = ", ".join(tasks._loads(t.depends_on)) or "nothing"
        lines.append(f"- task {t.id} has been queued {int((time.time() - t.created_at) // 60)} min,"
                     f" waiting on {deps}")
    memory = "".join(f"\n- {describe(e)}" for e in reversed(recalled))
    return ("You were woken by what happened in this repo since the last coordinator looked:\n"
            + "\n".join(lines[:40])
            + (f"\n\nWhat earlier coordinators here did, oldest first:{memory}" if memory else "")
            + "\n\nLook at list_tasks and who_is_working, act only where it helps, then call "
              "report_result with what you did.")


def wake(db: DB, now: float | None = None) -> list[str]:
    """Start a coordinator in each repo with the coordinator on that has
    unseen watched events and none running. The events are claimed in one
    transaction first, so two sweeps at once start one. Returns what it did."""
    now = time.time() if now is None else now
    by_key = _roots_by_key(db)
    if not by_key or not entitled():
        return []
    done = []
    for key, roots in by_key.items():
        if running(db, roots, now):
            continue
        events, stale, last, flagged = unseen(db, key, roots, now)
        if not events and not stale:
            continue
        mark = _mark(db.conn, key)
        with db.tx() as c:
            if _mark(c, key)[0] != mark[0]:
                continue    # another sweep claimed these
            knowledge._write(c, key, knowledge.COORDINATOR,
                             {"action": "wake", "through": last, "tasks": flagged,
                              "before": [mark[1], mark[2]]})
        try:
            agent = agents.spawn(db, repos.target_workspace(db, roots[0]), PROFILE,
                                 prompt=brief(events, stale, recent(db, key, RECALL)),
                                 mode=MODE, background_setup=True)
        except Exception as e:  # noqa: BLE001 - give the events back for the next sweep
            _give_back(db, key, failed=str(e)[:300])
            done.append(f"couldn't start the coordinator for {key}: {e}")
            continue
        done.append(f"woke coordinator {agent.id} for {key} "
                    f"({len(events)} event(s), {len(stale)} long-queued task(s))")
    return done


def stop(db: DB, repo_root: str) -> list[str]:
    """The kill switch: turn the coordinator off in this checkout's local
    config and close any running for the repo. Returns the ids closed."""
    set_local(repo_root, "coordinator", False)
    key = knowledge.repo_key(repo_root)
    roots = sorted({ws.repo_root for ws in db.find_workspaces() if ws.repo_root
                    and (ws.repo_root == repo_root or knowledge.repo_key(ws.repo_root) == key)})
    closed = []
    for a in running(db, roots or [repo_root]):
        try:
            agents.close(db, a.id)
            closed.append(a.id)
        except agents.AgentError:
            pass
    knowledge.append(db, key, knowledge.COORDINATOR, {"action": "stop", "closed": closed})
    return closed
