"""Permission asks go to the worker's supervisor before the person.

A request brindle's policy leaves to the person (an ask) is held by the
worker's hook as before (brindle.agents._hold_for_app), but for its first
``permission_supervisor_hold_seconds`` it is its supervisor's: brindle sends
the supervisor the request, and the supervisor answers allow or deny, or
escalates it to the person, with the answer_permission tool. The person sees
it only once the supervisor escalates, doesn't answer in time, or can't be
told; and from the start when a person-only rule asks (pushes into the
default or a release branch, ``Rule.person_only``) or the worker has no live
supervisor.

The supervisor's answer reaches the hook the same way the person's does:
through the daemon's hold (brindle.daemon.holds), never the database, which
a worker could write. The daemon tells who is answering from the calling
process's ancestry (daemon.peer.caller_agents), not from anything the caller
says: a worker never answers its own request, and a supervisor only its own
workers'.
"""
from __future__ import annotations

import logging

from brindle.db import DB, Agent

log = logging.getLogger("brindle.permission_routing")

# The presets whose asks are the person's alone, also in a store seeded before
# Rule.person_only existed.
PERSON_ONLY_PRESETS = ("d-push-default", "d-push-release", "d-push-release-sub")
CHOICES = ("allow", "deny", "escalate")
# What a supervisor's answer is released to the hook as, and what the CLI is told.
SUPERVISOR_ANSWERS = {"allow": "supervisor_allow", "deny": "supervisor_deny"}
AS_ANSWER = {"supervisor_allow": "allow_once", "supervisor_deny": "deny"}
REASON_CHARS = 300


class RoutingError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def person_only(decision) -> bool:
    """Whether a rule that asks for this is the person's alone."""
    rules = [r for r in (decision.rule, *decision.asks) if r is not None]
    return any(r.person_only or (r.source == "preset" and r.id in PERSON_ONLY_PRESETS) for r in rules)


def supervisor_for(db: DB, agent: Agent, decision) -> Agent | None:
    """The supervisor that answers ``agent``'s ask before the person: its own
    (``parent_id``), running and not paused. None (the person's alone) for a
    person-only ask, or a worker with no such supervisor."""
    from brindle import agents

    if decision is None or decision.decision != "ask" or person_only(decision):
        return None
    if not agent.parent_id or agent.parent_id == agent.id:
        return None
    supervisor = db.get_agent(agent.parent_id)
    if supervisor is None or agents._closed(supervisor) or not agents.runs_process(supervisor):
        return None
    return supervisor


def in_flight(db: DB, agent_id: str, digest: str) -> bool:
    """Whether the same request of the agent's already waits on its
    supervisor: one answer settles both, so it isn't asked about again."""
    from brindle import permissions

    return any(r["channel"] == "supervisor" and permissions.request_digest(r["request"], r["suggestions"]) == digest
               for r in db.permission_requests_of(agent_id))


def ask_supervisor(db: DB, agent: Agent, supervisor: Agent, request_id: str, req, reason: str,
                   seconds: float) -> bool:
    """Send ``supervisor`` the request now, mid-turn where its CLI takes that
    (its own inbox), whole rather than as a "new messages" notice. False when
    it couldn't be sent: the person has the request from then on."""
    from brindle import agents, tmux

    why = reason.removesuffix("; asking the user") or "no rule allows it"
    body = (f"[brindle] Worker {agent.id} ({agent.profile}) asks to use {req.summary()} ({why}). "
            f"It's yours to answer before the person: answer_permission(agent_id=\"{agent.id}\", "
            f"request_id=\"{request_id}\", answer=\"allow\" | \"deny\" | \"escalate\", reason=\"...\") "
            f"within {round(seconds)}s. Escalate what the person should decide; after {round(seconds)}s "
            "the person is asked.")
    try:
        agents._send_message(db, supervisor, body, None)
    except (agents.AgentError, tmux.TmuxError, OSError):
        log.info("brindle: couldn't ask supervisor %s about %s's request", supervisor.id, agent.id)
        return False
    return True


def answer(db: DB, callers: set[str], worker_id: str, request_id: str, choice: str, reason: str = "") -> dict:
    """The daemon's side of a supervisor's answer: ``choice`` (allow, deny or
    escalate) to the request ``request_id`` worker ``worker_id``'s hook
    holds, from a process of the agents ``callers``. Refused unless the
    worker's own supervisor is among them and the worker isn't, and while
    the request is still the supervisor's (not person-only, escalated, or
    past its time). Every identical request of the worker's waiting on the
    supervisor is answered with it."""
    from brindle.daemon.holds import HOLDS

    if choice not in CHOICES:
        raise RoutingError("invalid", f"answer must be one of {', '.join(CHOICES)}", 400)
    worker = db.get_agent(worker_id)
    if worker is None:
        raise RoutingError("not_found", f"no agent {worker_id}", 404)
    if worker.id in callers:
        raise RoutingError("forbidden", "a worker can't answer its own permission request", 403)
    if not worker.parent_id or worker.parent_id not in callers:
        raise RoutingError("forbidden", f"only {worker.id}'s own supervisor answers its permission requests", 403)
    hold = HOLDS.held(worker.id, request_id)
    if hold is None or not hold.supervised(HOLDS.clock()):
        raise RoutingError("conflict", f"{worker.id}'s request {request_id} isn't waiting on you: it is the "
                                       "person's (person-only, escalated, or your time ran out), or no longer held")
    same = [request_id, *(h.request_id for h in HOLDS.of_agent(worker.id)
                          if h.request_id != request_id and h.digest == hold.digest)]
    if choice == "escalate":
        note = " ".join(reason.split())[:REASON_CHARS]
        done = [r for r in same if HOLDS.escalate(worker.id, r, note)]
        for r in done:
            db.update_permission_request(worker.id, r, channel="hook")
    else:
        done = []
        for r in same:
            if HOLDS.claim(worker.id, r, hold.digest, SUPERVISOR_ANSWERS[choice], by_supervisor=True):
                if HOLDS.release(worker.id, r):
                    done.append(r)
                else:
                    HOLDS.unclaim(worker.id, r)
    if request_id not in done:
        raise RoutingError("conflict", f"{worker.id}'s request {request_id} was answered or left you meanwhile")
    return {"answer": choice, "requests": done}


def answer_via_daemon(worker_id: str, request_id: str, choice: str, reason: str = "") -> str:
    """The answer_permission tool: ``answer`` above, in the daemon, which
    checks who this process belongs to."""
    from brindle.daemon import holds

    got = holds.post(f"{holds.PREFIX}/{request_id}/supervise",
                     {"agent_id": worker_id, "answer": choice, "reason": reason}, holds.CALL_SECONDS)
    if got is None:
        return "Not answered: brindle's daemon isn't running, so the person answers it."
    status, data = got
    if status != 200:
        return f"Not answered: {(data.get('error') or {}).get('message', 'refused')}"
    n = len(data.get("requests") or [])
    also = f" (and {n - 1} identical request{'s' if n > 2 else ''})" if n > 1 else ""
    if choice == "escalate":
        return f"Escalated {worker_id}'s request{also} to the person."
    return f"Answered {choice} to {worker_id}'s request{also}."
