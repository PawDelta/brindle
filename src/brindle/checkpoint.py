"""The last-moment checkpoint: a provider about to cut off has its workers
save their work while they still can.

When provider_health says a provider is ``closing`` (its window resets more
than 20 minutes away and it is about to run out), every worker at work on
it is told, mid-turn, to commit its work in progress, leave a short
handover note with the ``handover_note`` tool (done / left / traps), and
carry on. The note is kept for whoever picks the task up next.

What a worker's pane shows feeds provider_health first (``observe``): the
CLI's approaching-limit notice, and a 429 it is waiting out mid-turn, which
no hook reports.

Delivery depends on the CLI: Claude Code gets the instruction as context in
its next PostToolUse hook reply (``hook_context``); Codex and agy hold input
typed mid-turn until their current step ends, so the cull sweep queues it
and types it in, after anything queued before it (``sweep``). One that
can't go in yet stays pending and is tried again on later sweeps, never
asked a second time meanwhile. OpenCode gets nothing. Each worker is asked
at most MAX_ASKS times per closing spell, ASK_AGAIN_SECONDS apart.

Every function that reads the clock takes ``now``, so tests use a fake one.
"""

from __future__ import annotations

import json
import logging
import time

from brindle import provider_health
from brindle.db import DB, Agent

log = logging.getLogger(__name__)

MARK = "checkpoint"            # agent_marks: {"until", "asks", "at", "pending"} of the last ask
NOTE_MARK = "handover-note"    # agent_marks: {"note", "at"}, the worker's latest handover note
SEEN_MARK = "pane-limit-seen"  # agent_marks: the limit lines last read off the worker's pane
MAX_ASKS = 2
ASK_AGAIN_SECONDS = 10 * 60
CLAIM_LOST_SECONDS = 60       # a claimed ask with no message queued this long was lost
NOTE_LINES = 10
SCREEN_LINES = 15              # the bottom of a pane, where a CLI shows what it is waiting on
WORKER_MODES = ("handoff", "handoff_detached", "assign")
TYPED = ("codex", "antigravity")   # CLIs that keep input typed mid-turn
WATCHED = ("claude", *TYPED)


def instruction(health: provider_health.Health) -> str:
    reset = time.strftime("%-I:%M%p", time.localtime(health.until)).lower() if health.until else "later"
    return (f"[brindle] {health.reason}: it may cut off before it resets at {reset}. Commit your "
            "work in progress now, then call the brindle `handover_note` tool with at most ten "
            "lines: what's done, what's left, and any traps. Then carry on with your task.")


def _parse(value: str | None) -> dict | None:
    try:
        data = json.loads(value) if value else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _at_work(agent: Agent) -> bool:
    return agent.mode in WORKER_MODES and agent.result is None and agent.status == "processing"


def _claim(db: DB, agent: Agent, now: float, pending: bool) -> tuple[provider_health.Health, str] | None:
    """The closing state and the mark recording a new ask of ``agent``, if
    one is due: its provider (and model) is closing, nothing asked is still
    waiting to go in, and it hasn't been asked MAX_ASKS times this spell nor
    within ASK_AGAIN_SECONDS. Recorded atomically, so of two hooks or sweeps
    at once only one asks."""
    if not _at_work(agent):
        return None
    from brindle import limit_report

    health = provider_health.closing(agent.provider,
                                     limit_report.model_of(agent, db.get_workspace(agent.workspace_id)) or None,
                                     now=now)
    if health is None:
        return None
    old = db.mark(agent.id, MARK)
    prev = _parse(old) or {}
    if prev.get("pending"):
        return None   # the last ask hasn't gone in yet
    same_spell = now < (provider_health._num(prev.get("until")) or 0)
    asks = int(prev.get("asks") or 0) + 1 if same_spell else 1
    if same_spell and (asks > MAX_ASKS or now - (provider_health._num(prev.get("at")) or 0) < ASK_AGAIN_SECONDS):
        return None
    new = json.dumps({"until": health.until, "asks": asks, "at": now, "pending": pending})
    won = db.add_mark(agent.id, MARK, new) if old is None else db.replace_mark(agent.id, MARK, old, new)
    return (health, new) if won else None


def claim(db: DB, agent: Agent, now: float | None = None) -> provider_health.Health | None:
    """Whether ``agent`` is to be asked for a checkpoint now, delivered at
    once (see ``_claim``). Returns the closing state, or None."""
    got = _claim(db, agent, time.time() if now is None else now, pending=False)
    return got[0] if got else None


def hook_context(db: DB, agent: Agent, now: float | None = None) -> dict | None:
    """Claude Code's PostToolUse reply carrying the instruction, or None."""
    if agent.provider != "claude":
        return None
    try:
        health = claim(db, agent, now)
    except Exception:  # noqa: BLE001 - a hook must never fail over this
        log.exception("brindle: couldn't check %s for a checkpoint", agent.id)
        return None
    if health is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": instruction(health)}}


def observe(db: DB, agent: Agent, now: float | None = None) -> None:
    """Feed provider_health what ``agent``'s pane shows of its limit: the
    CLI's approaching-limit notice, or a 429 it is waiting out. Each line
    counts when it appears, not again for as long as it stays on screen."""
    from brindle import agents, tmux

    if agent.headless or not agent.tmux_window:
        return
    try:
        screen = tmux.capture(agent.tmux_window, lines=SCREEN_LINES, server=agents.server_of(agent))
    except tmux.TmuxError:
        return
    lines = [line.strip() for line in screen.splitlines()[-SCREEN_LINES:]]
    notices = [line for line in lines if provider_health.is_notice(line)]
    throttles = [line for line in lines if provider_health.is_throttled(line)]
    seen = json.dumps(notices + throttles) if notices or throttles else None
    if seen == db.mark(agent.id, SEEN_MARK):
        return
    db.set_mark(agent.id, SEEN_MARK, seen)
    if notices:
        provider_health.record_notice(agent.provider, now=now)
    if throttles:
        provider_health.record_throttle(agent.provider, now=now)


def _deliver(db: DB, agent: Agent, mark: str, now: float) -> None:
    """Type in the ask ``mark`` holds pending, after anything queued before
    it; once it is in, it counts from now."""
    from brindle import agents

    data = _parse(mark) or {}
    pending = data.get("pending")
    if not isinstance(pending, int) or isinstance(pending, bool):
        # Claimed, but its sweep died before queueing it: free the ask after a while.
        if now - (provider_health._num(data.get("at")) or 0) > CLAIM_LOST_SECONDS:
            db.replace_mark(agent.id, MARK, mark, json.dumps({**data, "pending": None,
                                                              "asks": max(0, int(data.get("asks") or 1) - 1)}))
        return
    if db.message_settled(pending) or agents.deliver_through(db, agent.id, pending):
        db.replace_mark(agent.id, MARK, mark, json.dumps({**data, "pending": None, "at": now}))


def _ask_typed(db: DB, agent: Agent, now: float) -> str | None:
    pending = db.mark(agent.id, MARK)
    if (_parse(pending) or {}).get("pending"):
        _deliver(db, agent, pending, now)
        return None
    from brindle import agents

    got = _claim(db, agent, now, pending=True)
    if got is None:
        return None
    health, mark = got
    message_id = agents.queue_urgent(db, agent, instruction(health))
    data = json.loads(mark)
    with_id = json.dumps({**data, "pending": message_id})
    if db.replace_mark(agent.id, MARK, mark, with_id):
        _deliver(db, agent, with_id, now)
    return f"asked {agent.id} to checkpoint: {health.reason}"


def sweep(db: DB, now: float | None = None) -> list[str]:
    """Read every working worker's pane for its limit, then ask the Codex
    and agy ones due a checkpoint (Claude's ask from their own hook)."""
    now = time.time() if now is None else now
    done = []
    workers = [a for a in db.list_agents() if a.provider in WATCHED and _at_work(a)]
    for agent in workers:
        try:
            observe(db, agent, now)
        except Exception:  # noqa: BLE001 - one worker never stops the sweep
            log.exception("brindle: couldn't read %s's pane for its limit", agent.id)
    for agent in workers:
        if agent.provider not in TYPED:
            continue
        try:
            line = _ask_typed(db, agent, now)
            if line:
                done.append(line)
        except Exception:  # noqa: BLE001
            log.exception("brindle: couldn't ask %s to checkpoint", agent.id)
    return done


def record_note(db: DB, agent: Agent, note: str, now: float | None = None) -> str:
    """Keep ``agent``'s handover note (its first NOTE_LINES lines)."""
    lines = [line.rstrip() for line in note.strip().splitlines()]
    if not any(lines):
        return "The note is empty: write what's done, what's left and any traps."
    kept = "\n".join(lines[:NOTE_LINES])
    db.set_mark(agent.id, NOTE_MARK, json.dumps({"note": kept, "at": time.time() if now is None else now}))
    cut = f" (cut to its first {NOTE_LINES} lines)" if len(lines) > NOTE_LINES else ""
    return f"Handover note kept{cut}. Carry on with your task."


def note(db: DB, agent_id: str) -> str | None:
    """``agent_id``'s latest handover note, or None."""
    data = _parse(db.mark(agent_id, NOTE_MARK))
    return data.get("note") if data else None
