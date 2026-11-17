"""A turn that ended on a provider limit, told to its supervisor at once.

When a worker's turn ends on a provider limit (its usage window is full,
it is signed out, or the provider keeps failing turns: provider_health's
``limited``, ``signed_out`` and ``erroring``), the stop-failure hook
(``agents.handle_hook``) tells its supervisor in the same call, in or
outside autopilot, with what it needs to decide: the worker, its model,
branch and workspace, the commits since it started and its uncommitted
files, its last words, what's left, when the limit lifts and what brindle
does next.

A 429 while the window is only partly used is no limit: nothing is paused,
and the turn runs again in place once the rate limit lifts
(``schedule_rerun``, carried out by ``rerun_due`` from the cull sweep).

Every function that reads the clock takes ``now``, so tests use a fake one.
"""

from __future__ import annotations

import logging
import os
import time

from brindle import git, provider_health
from brindle.db import DB, Agent, Workspace

log = logging.getLogger(__name__)

RERUN_MARK = "limit-rerun"      # agent_marks: when its rate-limited turn runs again
RERUNNING = "sending"           # the mark while one sweep types the rerun in
LISTED = 8                      # commits and files named; the rest are counted
REPORTED = (provider_health.LIMITED, provider_health.SIGNED_OUT, provider_health.ERRORING)

RERUN_PROMPT = ("[brindle] Your last turn stopped on a provider rate limit (429) before it "
                "finished, and that limit has lifted. Carry on from where you stopped.")


def when(ts: float | None) -> str:
    """``ts`` as the report names it, in local time."""
    if ts is None:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def model_of(agent: Agent, ws: Workspace | None) -> str:
    """The model ``agent``'s profile runs, or "" when it's the CLI's default."""
    from brindle.autopilot import effective_profile_model

    try:
        return effective_profile_model(agent.profile, ws.repo_root if ws else None) or ""
    except Exception:  # noqa: BLE001 - a report is never lost over a profile
        return ""


def _work(agent: Agent, ws: Workspace | None) -> tuple[list[str], list[str]]:
    """The commits on ``agent``'s branch since it started, newest first, and
    its uncommitted files. Never raises: what can't be read is left empty."""
    if ws is None or not os.path.isdir(ws.path):
        return [], []
    from brindle import rewind

    commits: list[str] = []
    try:
        start = rewind.baseline(ws, agent.id)
        commits = [ln for ln in git.out(["log", "--format=%h %s", f"{start}..HEAD"], ws.path).splitlines()
                   if ln.strip()]
    except Exception:  # noqa: BLE001
        log.debug("brindle: couldn't list %s's commits", agent.id, exc_info=True)
    try:
        dirty = git.dirty_files(ws.path)
    except Exception:  # noqa: BLE001
        dirty = []
    return commits, dirty


def _listed(items: list[str]) -> str:
    shown = "; ".join(items[:LISTED])
    return shown + (f"; and {len(items) - LISTED} more" if len(items) > LISTED else "")


def _left(agent: Agent) -> str:
    if agent.done_when:
        return f"its finish line: {agent.done_when.strip()}"
    task = (agent.task or "").strip()
    if not task:
        return "not recorded"
    line = task.splitlines()[0]
    return f"its task, starting: {line[:200]}" + ("..." if len(line) > 200 or "\n" in task else "")


def body(db: DB, agent: Agent, health: provider_health.Health, next_step: str) -> str:
    """The supervisor's notice that ``agent`` stopped on ``health``."""
    from brindle import rewind

    ws = db.get_workspace(agent.workspace_id)
    commits, dirty = _work(agent, ws)
    what = {provider_health.LIMITED: "its usage limit",
            provider_health.SIGNED_OUT: "an authentication failure (signed out)",
            provider_health.ERRORING: "repeated provider errors"}.get(health.state, health.state)
    lifts = (when(health.until) if health.until is not None
             else f"once {agent.provider} is signed in again" if health.state == provider_health.SIGNED_OUT
             else "unknown")
    words = rewind.turn_words(agent)
    where = f"branch `{ws.branch}`, workspace {ws.id}" if ws else "no workspace"
    lines = [
        f"[brindle] Worker {agent.id} stopped on {what} ({health.reason or agent.provider}) "
        f"before reporting.",
        f"- Worker: {agent.id} ({agent.profile}, {agent.provider}), model "
        f"{model_of(agent, ws) or 'its CLI default'}, {where}",
        f"- Commits since it started: {len(commits)}" + (f" ({_listed(commits)})" if commits else ""),
        f"- Uncommitted files: {len(dirty)}" + (f" ({_listed(dirty)})" if dirty else ""),
        "- Its last words: " + (" ".join(words.split()) if words else "none recorded"),
        f"- What's left: {_left(agent)}",
        f"- The limit lifts: {lifts}",
        f"- What brindle does next: {next_step}",
    ]
    return "\n".join(lines)


def tell(db: DB, agent: Agent, health: provider_health.Health, next_step: str) -> bool:
    """Tell ``agent``'s supervisor it stopped on ``health``, once per stop
    (the stop's API-error mark, so neither the sweep's API-error notice nor
    agy's own stop notice repeats it). ``agent`` is as read at that stop.
    Returns whether the supervisor was told."""
    from brindle import agents, open_items, tmux

    if (agent.mode not in agents.FORWARDING_MODES or not agent.parent_id or agent.result is not None
            or db.get_agent(agent.parent_id) is None):
        return False
    stop = agents.api_error_stop(agent)
    won, previous = db.claim_mark(agent.id, agents.API_ERROR_MARK, stop)
    if not won:
        return False
    text = body(db, agent, health, next_step)
    try:
        agents.send_message(db, agent.parent_id, text, sender_id=agent.id)
    except (agents.AgentError, tmux.TmuxError):
        try:
            db.enqueue(agent.parent_id, text, agent.id)
        except Exception:  # noqa: BLE001
            db.set_mark(agent.id, agents.API_ERROR_MARK, previous)
            return False
    if not agent.unreported_noted:
        # The idle reminder would only repeat this, less exactly.
        db.claim_unreported(agent.id, time.time(), None, reopened_at=agent.reopened_at,
                            status_since=agent.status_since or agent.created_at)
    open_items.track(db, agent, f"stopped on a provider limit: {health.reason or health.state}")
    return True


def told(db: DB, agent: Agent) -> bool:
    """Whether this stop of ``agent``'s was reported (``tell``) or will run
    again (``schedule_rerun``) already."""
    from brindle import agents

    return db.mark(agent.id, agents.API_ERROR_MARK) == agents.api_error_stop(agent)


def on_stop_failure(db: DB, agent: Agent, *, rate_limited: bool, now: float | None = None) -> None:
    """``agent``'s turn just ended on a provider error (``rate_limited``: a
    429), its health already recorded. At the limit (a full window) or with
    the provider ``erroring``: autopilot pauses or blocks as it must and the
    supervisor is told now. A 429 short of the limit pauses nothing: the
    turn runs again in place once the rate limit lifts."""
    from brindle import autopilot as pilot

    ws = db.get_workspace(agent.workspace_id)
    try:
        health = provider_health.limit(agent.provider, model_of(agent, ws) or None, now=now)
    except OSError:
        log.exception("brindle: couldn't read %s's provider health", agent.id)
        return
    if health.state == provider_health.LIMITED:
        nxt = pilot.limit_reached(db, agent, health.until)
        tell(db, agent, health, nxt or (
            "nothing on its own outside autopilot: the worker stays idle at its prompt. "
            "send_message it once the limit lifts, assign the task to another profile, "
            "or remove_workspace if the work is abandoned"))
    elif health.state == provider_health.ERRORING:
        tell(db, agent, health, (
            f"brindle routes new work away from {agent.provider} until {when(health.until)}; "
            "the worker stays idle at its prompt and won't go on by itself: send_message it then, "
            "assign the task to another profile, or remove_workspace if the work is abandoned"))
    elif rate_limited:
        # Its own supervisor (or the person) sends a supervisor its next turn
        # outside autopilot; a worker, or an autopilot supervisor, goes on by itself.
        if agent.mode != "interactive" or db.get_autopilot(pilot.root_of(db, agent.id)):
            at = health.until if health.state == provider_health.RATE_LIMITED and health.until else (
                (time.time() if now is None else now) + provider_health.RATE_LIMIT_SECONDS)
            schedule_rerun(db, agent, at)


# -- a 429 that isn't the limit: the turn runs again in place ----------------------


def schedule_rerun(db: DB, agent: Agent, at: float) -> None:
    """Run ``agent``'s turn again once its rate limit lifts at ``at``. Its
    stop needs no API-error notice: it goes on by itself."""
    from brindle import agents

    db.set_mark(agent.id, agents.API_ERROR_MARK, agents.api_error_stop(agent))
    db.set_mark(agent.id, RERUN_MARK, repr(float(at)))


def rerun_due(db: DB, now: float | None = None) -> list[str]:
    """Type the turn back into every agent whose rate limit has lifted.
    One that started working again meanwhile (its supervisor or the person
    sent it something) keeps that work: its rerun is dropped."""
    from brindle import agents, tmux

    now = time.time() if now is None else now
    done = []
    for row in db.marks_of_kind(RERUN_MARK):
        agent_id, value = row["agent_id"], row["value"]
        try:
            at = float(value)
        except ValueError:
            continue    # another sweep is typing it in
        agent = db.get_agent(agent_id)
        if (agent is None or agent.status != "idle" or agent.dismissed_at is not None
                or agent.result is not None):
            if db.mark(agent_id, RERUN_MARK) == value:
                db.set_mark(agent_id, RERUN_MARK, None)
            continue
        if now < at:
            continue
        health = provider_health.limit(agent.provider, now=now)
        if health.state == provider_health.RATE_LIMITED:
            continue    # another 429 since: wait for that one too
        if not db.replace_mark(agent_id, RERUN_MARK, value, RERUNNING):
            continue
        try:
            agents.send_message(db, agent_id, RERUN_PROMPT)
        except (agents.AgentError, tmux.TmuxError):
            db.replace_mark(agent_id, RERUN_MARK, RERUNNING, value)   # the next sweep tries again
            continue
        db.set_mark(agent_id, RERUN_MARK, None)
        done.append(f"ran {agent_id}'s rate-limited turn again")
    return done
