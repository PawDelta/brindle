"""Mid-task handover: a worker whose provider ran out carries on elsewhere.

When a worker's turn ends at its provider's usage limit (provider_health's
``limited``, reached through ``limit_report.at_limit``), brindle snapshots
its worktree (``rewind.snapshot``) and then:

- If the provider is back within PARK_SECONDS, the worker is parked (paused,
  its session kept) and resumed in place once it is back (``sweep``).
- Otherwise a fresh worker starts in the same worktree and branch, on the
  cascade's best runnable replacement (``cascade.workers``), leaving out
  every provider this task already ran on. Its brief is led by the worker's
  handover note (brindle.checkpoint) when it left one; brindle's own records
  follow, read without a model call: the snapshot, the last transcript
  turns, the commits since the task started and the uncommitted files; then
  the last report, open review findings and the round, the plan and the
  messages queued for it; and "the earlier model ran out; continue, don't
  restart". The task row, the pipeline state and round, the routing
  record's round and escalation counters, the snapshots and the baseline
  carry over to the replacement.
- A replacement billed per token (an API key: ``provider_switch._key_billed``)
  is asked of the person first, with an Inbox card; one on a subscription
  goes automatically.
- At most MAX_HOPS handovers per task: the next stop parks the task with one
  Inbox card (wait / continue on <model> / stop).

Each stop is one ``handovers`` row, claimed by its INSERT, so a second
trigger of the same stop starts nothing. The worker is re-read just before
it is stopped: one that moved on meanwhile is left alone and its row
released. A row a process claimed and never finished (it was restarted) is
taken up by ``sweep`` once CLAIM_EXPIRES has passed: carried on while its
worker is still at that stop, its replacement adopted if one was started,
released otherwise.

A released row hands the stop back to ``limit_report.at_limit``, which
pauses or blocks as it did before handovers existed.

Every function that reads the clock takes ``now``, so tests use a fake one.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass

from brindle import provider_health
from brindle.db import DB, Agent, Workspace

log = logging.getLogger(__name__)

PARK_SECONDS = 20 * 60       # back this soon: park the worker and resume it in place
MAX_HOPS = 2                 # handovers per task; the next stop asks the person
CLAIM_EXPIRES = 900.0        # a claimed row untouched this long was left by a process that died
MODES = ("assign", "handoff_detached")   # a synchronous handoff's caller waits on that one worker
TRANSCRIPT_TURNS = 6         # conversation turns quoted in a brief built from records
TURN_CHARS = 600             # of each
LISTED = 12                  # commits, files and messages named; the rest are counted
KIND = "handover"            # the Inbox card's kind; its request_id is the row's id
CHOICES = ("wait", "continue", "stop")
RAN_OUT = "The earlier model ran out; continue, don't restart."

CLAIMED, PARKED, ASKING, HELD = "claimed", "parked", "asking", "held"
STARTED, RESUMED, STOPPED, RELEASED, FAILED = "started", "resumed", "stopped", "released", "failed"
OPEN = (ASKING, HELD)        # waiting on the person's answer to its card


@dataclass
class Handover:
    """A ``handovers`` row."""
    id: str
    chain: str
    from_agent: str
    stop: str
    at_stop: str
    from_profile: str
    to_profile: str | None
    to_agent: str | None
    launch: str | None
    state: str
    snapshot: int | None
    until_ts: float | None
    claimed_at: float | None
    created_at: float
    updated_at: float
    detail: str | None


def _now(now: float | None) -> float:
    return time.time() if now is None else now


# -- the table -------------------------------------------------------------------------------------


def get(db: DB, handover_id: str) -> Handover | None:
    row = db.conn.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
    return Handover(**dict(row)) if row else None


def of_stop(db: DB, agent_id: str, stop: str) -> Handover | None:
    row = db.conn.execute("SELECT * FROM handovers WHERE from_agent=? AND stop=?", (agent_id, stop)).fetchone()
    return Handover(**dict(row)) if row else None


def _in_state(db: DB, *states: str) -> list[Handover]:
    marks = ",".join("?" for _ in states)
    rows = db.conn.execute(f"SELECT * FROM handovers WHERE state IN ({marks}) ORDER BY created_at, id", states)
    return [Handover(**dict(r)) for r in rows]


def chain_of(db: DB, agent_id: str) -> str:
    """The task ``agent_id`` works on, as handovers know it: its chain when it
    took over from another worker, else its own id."""
    row = db.conn.execute("SELECT chain FROM handovers WHERE to_agent=? AND state=?",
                          (agent_id, STARTED)).fetchone()
    return row["chain"] if row else agent_id


def hops(db: DB, chain: str) -> int:
    """How many times ``chain``'s task was handed over so far."""
    return int(db.conn.execute("SELECT COUNT(*) FROM handovers WHERE chain=? AND state=?",
                               (chain, STARTED)).fetchone()[0])


def ran_on(db: DB, chain: str, current: str) -> list[str]:
    """Every profile ``chain``'s task ran on, ``current`` (the one it is on) last."""
    rows = db.conn.execute("SELECT from_profile FROM handovers WHERE chain=? AND state=? ORDER BY created_at",
                           (chain, STARTED)).fetchall()
    return [p for p in dict.fromkeys(r["from_profile"] for r in rows) if p != current] + [current]


def _claim(db: DB, agent: Agent, stop: str, now: float) -> Handover | None:
    """The row for ``agent``'s ``stop``, if this call is the one that made it."""
    hid = uuid.uuid4().hex[:12]
    with db.tx() as c:
        made = c.execute(
            "INSERT INTO handovers (id, chain, from_agent, stop, at_stop, from_profile, state, claimed_at, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(from_agent, stop) DO NOTHING",
            (hid, chain_of(db, agent.id), agent.id, stop, stop, agent.profile, CLAIMED, now, now, now)).rowcount
    return get(db, hid) if made else None


def _move(db: DB, h: Handover, state: str, now: float, **fields) -> bool:
    """Move ``h`` on to ``state`` (setting ``fields``), only if nobody moved it
    since it was read. Returns whether it did."""
    fields = {"claimed_at": now if state == CLAIMED else None, **fields, "state": state, "updated_at": now}
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.tx() as c:
        return bool(c.execute(f"UPDATE handovers SET {cols} WHERE id=? AND state=? AND claimed_at IS ?",
                              (*fields.values(), h.id, h.state, h.claimed_at)).rowcount)


def _set(db: DB, h: Handover, now: float, **fields) -> Handover:
    """Record ``fields`` on a row this process holds."""
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.tx() as c:
        c.execute(f"UPDATE handovers SET {cols}, updated_at=? WHERE id=?", (*fields.values(), now, h.id))
    return get(db, h.id) or h


# -- the cutoff ------------------------------------------------------------------------------------


def _eligible(agent: Agent | None) -> bool:
    # A worker fixing review findings keeps its earlier report: it is still at work.
    return (agent is not None and agent.mode in MODES and agent.dismissed_at is None
            and (agent.result is None or agent.pipeline == "fixing"))


def _still_at(agent: Agent | None, h: Handover) -> bool:
    """``agent`` is where ``h`` left it: at that stop, idle or paused, and
    nobody gave it another turn or retired it meanwhile."""
    from brindle import agents

    return (_eligible(agent) and agent.status in ("idle", "paused")
            and agents.api_error_stop(agent) == h.at_stop)


def on_cutoff(db: DB, agent: Agent, health: provider_health.Health, now: float | None = None) -> str | None:
    """``agent`` (as read at its stop) stopped at its provider's usage limit
    (``health``). Park it, hand its task over or ask the person, as the
    module says. Returns what brindle does next, for its supervisor; None
    when this isn't a handover's to deal with (the caller pauses or blocks
    as before)."""
    from brindle import agents

    now = _now(now)
    if not _eligible(agent):
        return None
    ws = db.get_workspace(agent.workspace_id)
    if ws is None or ws.kind != "worktree" or not os.path.isdir(ws.path):
        return None
    stop = agents.api_error_stop(agent)
    h = _claim(db, agent, stop, now)
    if h is None:
        # Another trigger of this stop (the hook and a sweep) has it already.
        prior = of_stop(db, agent.id, stop)
        return None if prior is None or prior.state in (RELEASED, FAILED) else (
            prior.detail or "brindle is handing its task over")
    try:
        h = _set(db, h, now, snapshot=_snapshot(db, agent, ws), until_ts=health.until)
        return _next(db, h, ws, now)
    except Exception:  # noqa: BLE001 - the stop is still reported, as before handovers
        log.exception("brindle: couldn't hand over %s's task", agent.id)
        _move(db, get(db, h.id) or h, RELEASED, now, detail="the handover failed")
        return None


def _snapshot(db: DB, agent: Agent, ws: Workspace) -> int | None:
    """The worktree as the worker left it: a new snapshot if anything changed
    since its last one, else that one."""
    from brindle import rewind

    try:
        made = rewind.snapshot(db, agent, ws)
        if made is not None:
            return made
        have = rewind.turns(ws.repo_root, agent.id)
        return have[-1][0] if have else None
    except Exception:  # noqa: BLE001 - the brief still has the worktree itself
        log.exception("brindle: couldn't snapshot %s at its cutoff", agent.id)
        return None


def _next(db: DB, h: Handover, ws: Workspace, now: float) -> str | None:
    """What becomes of ``h``'s stop, in this order: past MAX_HOPS the person
    decides (whenever its provider is back); back within PARK_SECONDS the
    worker is parked; else its task is handed over, or the person asked
    first. Nothing starts in a paused session."""
    from brindle import agents

    agent = db.get_agent(h.from_agent)
    if not _still_at(agent, h):
        _move(db, h, RELEASED, now, detail="the worker moved on before brindle acted")
        return None
    if hops(db, h.chain) >= MAX_HOPS:
        pick = replacement(db, agent, ws, ran_on(db, h.chain, agent.profile))
        options = f"wait / continue on {pick} / stop" if pick else "wait / stop"
        return _pause(db, h, HELD, now, to_profile=pick, detail=(
            f"its task stopped at a provider limit a third time (after {MAX_HOPS} handovers), so brindle "
            f"parked it: the worker is paused with its work, and the person decides in the Inbox ({options})"))
    if (h.until_ts is not None and h.until_ts - now <= PARK_SECONDS) or agents.session_paused(db, agent):
        return _park(db, h, now)
    pick = replacement(db, agent, ws, ran_on(db, h.chain, agent.profile))
    if pick is None:
        _move(db, h, RELEASED, now, detail="no other provider can take its task now")
        return None
    if per_token(pick, ws.repo_root):
        return _pause(db, h, ASKING, now, to_profile=pick, detail=(
            f"the best replacement, {pick}, is billed per token, so brindle asks the person first in the "
            f"Inbox (continue on {pick} / wait / stop); the worker is paused with its work meanwhile"))
    return _start(db, h, ws, pick, now)


def _pause(db: DB, h: Handover, state: str, now: float, **fields) -> str | None:
    """Pause ``h``'s worker (its session kept) and leave ``h`` in ``state``."""
    from brindle import agents

    agent = db.get_agent(h.from_agent)
    if not _still_at(agent, h):
        _move(db, h, RELEASED, now, detail="the worker moved on before brindle acted")
        return None
    if agent.status != "paused":
        agents.pause_worker(db, agent)
    paused = db.get_agent(h.from_agent)
    _move(db, h, state, now, at_stop=agents.api_error_stop(paused), **fields)
    return fields.get("detail")


def _park(db: DB, h: Handover, now: float) -> str | None:
    from brindle import agents, limit_report

    worker = db.get_agent(h.from_agent)
    if worker is not None and agents.session_paused(db, worker):
        return _pause(db, h, PARKED, now, detail=(
            "its session is paused, so brindle parked the worker: it goes on with the session's resume"))
    return _pause(db, h, PARKED, now, detail=(
        f"its provider is back at {limit_report.when(h.until_ts)}, within {PARK_SECONDS // 60} minutes, so "
        "brindle parked the worker and resumes it in place then"))


# -- the replacement -------------------------------------------------------------------------------


def replacement(db: DB, agent: Agent, ws: Workspace, after: list[str]) -> str | None:
    """The cascade's best runnable profile for ``agent``'s task, on none of
    the providers ``after`` (the profiles it ran on) used, as a fresh assign
    of it would be checked (the repo's policy included)."""
    from brindle import budget, cascade, policy as pol
    from brindle.config import load_repo_config

    cfg = load_repo_config(ws.repo_root)
    tier = _tier(agent, cfg, ws)

    def refusal(name: str) -> str | None:
        d = pol.assign_decision(cfg, ws.repo_root, name, agent.task or "", "assign", weight=tier,
                                branch=ws.branch)
        return None if d.allowed else (d.reason or "the repo's policy refused it")

    # The budgets a fresh assign by its supervisor would be held to (task, goal, month, the org's).
    gate = budget.Gate(db, cfg, ws.repo_root, agent.parent_id)
    return cascade.workers(cfg, ws.repo_root, tier, gate=gate, after=after, policy=refusal).first


def _tier(agent: Agent, cfg, ws: Workspace) -> str:
    """The routing tier of ``agent``'s task."""
    from brindle import autopilot, cascade

    tier = agent.weight if agent.weight in cascade.TIERS else cascade._weight(
        autopilot.profile_weight(agent.profile, cfg, ws.repo_root))
    return tier if tier in cascade.TIERS else "medium"


def over_budget(db: DB, agent: Agent, ws: Workspace, pick: str) -> str | None:
    """Why ``pick`` taking ``agent``'s task over doesn't fit the budget now, or None."""
    from brindle import budget
    from brindle.config import load_repo_config

    cfg = load_repo_config(ws.repo_root)
    gate = budget.Gate(db, cfg, ws.repo_root, agent.parent_id)
    return gate.why_not(pick, _tier(agent, cfg, ws)) if gate.active else None


CLOUD_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# A route to a gateway or another backend than the subscription's own.
ROUTE_VARS = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "GOOGLE_GEMINI_BASE_URL")


def _route_env(prof, m) -> dict[str, str] | None:
    """The route and credential layers a pane launched with ``prof`` would get
    (as ``agents.agent_env`` stacks them): the company's Claude env, the
    profile's env lines, the org's managed route (agent_setup or the legacy
    provider_config) and an enforced company identity's route. None when
    Claude Code's own managed settings pick the backend: brindle can't see it."""
    from brindle import company_identity, company_login, providers
    from brindle.pro import managed_models

    if prof.provider == "claude" and providers.claude_org_managed():
        return None
    company = company_login.stored_env() if prof.provider == "claude" else {}
    enforced = (company_identity.enforced_route_env() or {}) if prof.provider == "claude" else {}
    return {**company, **prof.env, **managed_models.agent_env(m, prof.provider), **enforced}


def per_token(profile: str, repo_root: str) -> bool:
    """Whether ``profile``, launched as it would be here, bills per token: an
    org-supplied key, a cloud or gateway route (the org's managed one, the
    company's, the person's saved connection or the profile's own), or an API
    key, judged on the identity a launch would use, as provider_switch judges
    its automatic switches. Anything it can't tell counts as per token."""
    from brindle import connections, pane_auth, provider_switch
    from brindle.pro import managed_models
    from brindle.profiles import load_profile
    from brindle.service.routing import RoutingPolicy

    try:
        prof = load_profile(profile, repo_root)
        gate = RoutingPolicy.load(repo_root)
        if gate.access.error:
            return True
        if gate.managed is not None:
            prof = managed_models.apply_profile(gate.managed, prof)
        if managed_models.has_org_key(gate.managed, prof.provider):
            return True
        prof = connections.apply(prof, repo_root)
        env = _route_env(prof, gate.managed)
        if env is None or prof.auth == "cloud":
            return True
        keys = pane_auth.API_KEYS.get(prof.provider, ())
        if any(env.get(k) or (k in CLOUD_SWITCHES and os.environ.get(k))
               for k in (*CLOUD_SWITCHES, *ROUTE_VARS, *keys)
               if prof.provider == "claude" or k not in CLOUD_SWITCHES):
            return True
        return provider_switch._key_billed(prof)
    except Exception:  # noqa: BLE001 - never spend per token on a guess
        log.exception("brindle: couldn't tell how %s bills; asking first", profile)
        return True


def _start(db: DB, h: Handover, ws: Workspace, pick: str, now: float) -> str | None:
    """Start ``pick`` in ``h``'s worktree with the brief, and retire the worker."""
    from brindle import agents

    old = db.get_agent(h.from_agent)
    if not _still_at(old, h):
        _move(db, h, RELEASED, now, detail="the worker moved on before brindle acted")
        return None
    queued = _queued(db, old.id)
    text = brief(db, old, ws, h, queued)
    # The launch's own identity, recorded before it: a process that dies
    # mid-launch leaves recovery this one agent to adopt, never another.
    h = _set(db, h, now, to_profile=pick,
             launch=json.dumps({"brief": _digest(text), "quoted": [i for i, _ in queued]}))
    old = db.get_agent(h.from_agent)     # once more, right before it is stopped
    if not _still_at(old, h):
        _move(db, h, RELEASED, now, detail="the worker moved on before brindle acted")
        return None
    if agents.session_paused(db, old):
        return _park(db, h, now)
    # Checked again at launch: a pick offered on an Inbox card earlier may no longer fit.
    over = over_budget(db, old, ws, pick)
    if over:
        if agents.is_alive(old):
            agents.pause_worker(db, old)
        elif old.status != "paused":
            db.set_status(old.id, "paused")
        detail = (f"{pick} doesn't fit the budget now ({over}), so brindle didn't start it; the worker is "
                  "paused with its work: assign the task to another profile, or continue the worker once "
                  "its limit lifts")
        _set(db, h, now, state=FAILED, to_profile=pick, claimed_at=None, detail=detail)
        return detail
    if agents.is_alive(old):
        agents._stop(db, old)
    # A handoff nobody waits on is still briefed and weighted as a handoff (see agents.spawn).
    detached = old.mode == "handoff_detached"
    try:
        fresh = agents.spawn(db, ws, pick, prompt=text, parent_id=old.parent_id,
                             mode="handoff" if detached else old.mode, detached=detached,
                             done_when=old.done_when, weight=old.weight, background_setup=True)
    except Exception as e:  # noqa: BLE001 - its supervisor must hear of it
        log.exception("brindle: couldn't start %s for %s", pick, old.id)
        if old.status != "paused":
            db.set_status(old.id, "paused")
        detail = (f"brindle couldn't start {pick} to take its task over ({e}); the worker is paused with its "
                  "work: assign the task to another profile, or continue the worker once its limit lifts")
        _set(db, h, now, state=FAILED, to_profile=pick, claimed_at=None, detail=detail)
        return detail
    h = _set(db, h, now, to_agent=fresh.id)
    return _retire(db, h, old, fresh, ws, now, [i for i, _ in queued])


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _retire(db: DB, h: Handover, old: Agent, fresh: Agent, ws: Workspace, now: float,
            quoted: list[int]) -> str:
    """Carry ``old``'s task over to ``fresh`` and retire ``old``. A session
    paused while ``fresh`` launched gets it paused too."""
    from brindle import agents

    carry_over(db, old, fresh, ws, now, quoted)
    if agents.session_paused(db, fresh) and (current := db.get_agent(fresh.id)) is not None \
            and current.status != "paused":
        agents.pause_worker(db, current)
    db.set_status(old.id, "rewound")     # history, as a rewound worker is (view.past_worker)
    db.update_agent(old.id, dismissed_at=now)
    detail = (f"its provider is out until {_until(h)}, so brindle handed its task to {fresh.id} ({fresh.profile}) "
              f"in the same worktree and branch, briefed to continue (hop {hops(db, h.chain) + 1} of {MAX_HOPS})")
    _set(db, h, now, state=STARTED, claimed_at=None, detail=detail)
    try:
        from brindle import events
        from brindle.config import load_repo_config

        events.emit(load_repo_config(ws.repo_root), "assign", ws, fresh, actor=None)
    except Exception:  # noqa: BLE001
        pass
    return detail


def _until(h: Handover) -> str:
    from brindle import limit_report

    return limit_report.when(h.until_ts)


def carry_over(db: DB, old: Agent, fresh: Agent, ws: Workspace, now: float, quoted: list[int] = ()) -> None:
    """What ``old``'s task had built up becomes ``fresh``'s: the original
    task, the pipeline state and round, the plan, the task row, the routing
    record's counters, the snapshots and the baseline. Of the messages
    waiting for ``old``, those ``fresh``'s brief quoted (``quoted``, their
    ids) are settled; every other one goes to ``fresh`` to be delivered as
    usual. "New messages" notices about them are settled: ``fresh`` gets its own."""
    from brindle import git, open_items, rewind

    db.update_agent(fresh.id, task=old.task, pipeline=old.pipeline, pipeline_rounds=old.pipeline_rounds,
                    reviewing_since=old.reviewing_since, review_sha=old.review_sha,
                    plan_first=old.plan_first, plan_state=old.plan_state, plan_text=old.plan_text)
    ids = list(quoted)
    with db.tx() as c:
        # Learning hears the takeover with the merge (learning.handover): the profile it took over
        # from and the hops so far. Only rows still on ``old`` count, so a repeat adds no hop.
        c.execute("INSERT OR REPLACE INTO task_handovers (task_id, handed_from, handovers) "
                  "SELECT t.id, ?, COALESCE((SELECT h.handovers FROM task_handovers h WHERE h.task_id=t.id), 0) + 1 "
                  "FROM tasks t WHERE t.agent_id=?", (old.profile, old.id))
        c.execute("UPDATE tasks SET agent_id=? WHERE agent_id=?", (fresh.id, old.id))
        c.execute("UPDATE routing_decisions SET agent_id=? WHERE agent_id=?", (fresh.id, old.id))
        if ids:
            marks = ",".join("?" for _ in ids)
            c.execute(f"UPDATE inbox SET delivered_at=? WHERE agent_id=? AND delivered_at IS NULL AND id IN ({marks})",
                      (now, old.id, *ids))
        c.execute(f"UPDATE inbox SET delivered_at=? WHERE agent_id=? AND delivered_at IS NULL AND {NOTICE}",
                  (now, old.id))
        c.execute("UPDATE inbox SET agent_id=? WHERE agent_id=? AND delivered_at IS NULL", (fresh.id, old.id))
    open_items.close(db, old.id)
    rewind.copy_turns(ws.repo_root, old.id, fresh.id, upto=1 << 30)
    base = rewind.baseline_ref(old.id)
    if git.ok(["rev-parse", "--verify", "-q", base], ws.repo_root):
        git.run(["update-ref", rewind.baseline_ref(fresh.id), git.out(["rev-parse", base], ws.repo_root)],
                ws.repo_root)


# -- the brief -------------------------------------------------------------------------------------


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.strip().splitlines())


def _listed(items: list[str]) -> str:
    shown = "; ".join(items[:LISTED])
    return shown + (f"; and {len(items) - LISTED} more" if len(items) > LISTED else "")


# An inbox row that is only a pull-mode "new messages" notice (DB.enqueue(notice=True)):
# not held, but noticed. A held message stays a message whether or not a notice went out.
NOTICE = "COALESCE(held,0)=0 AND noticed_at IS NOT NULL"


def _queued(db: DB, agent_id: str) -> list[tuple[int, str]]:
    """The first LISTED messages waiting for ``agent_id`` (held or not), as
    ``(id, line)``: what a brief quotes."""
    rows = db.conn.execute(f"SELECT id, sender_id, body FROM inbox WHERE agent_id=? AND delivered_at IS NULL "
                           f"AND NOT ({NOTICE}) ORDER BY id LIMIT ?", (agent_id, LISTED)).fetchall()
    return [(r["id"], f"from {r['sender_id'] or 'brindle'}: {' '.join(r['body'].split())}") for r in rows]


def _last_turns(db: DB, agent: Agent) -> list[str]:
    """The last TRANSCRIPT_TURNS turns of ``agent``'s conversation, else its
    last words (the tail of its pane): never raises."""
    from brindle import provider_switch, rewind

    try:
        tail = provider_switch._transcript_tail(db, agent)
        if tail and not tail[0].startswith("Transcript tail omitted"):
            return [rewind._trim(t, TURN_CHARS) for t in tail[-TRANSCRIPT_TURNS:]]
    except Exception:  # noqa: BLE001 - a brief is never lost over a transcript
        log.debug("brindle: no transcript tail for %s", agent.id, exc_info=True)
    said = rewind.turn_words(agent)
    return [f"assistant: {said}"] if said else []


def brief(db: DB, agent: Agent, ws: Workspace, h: Handover, queued: list[tuple[int, str]] | None = None) -> str:
    """The replacement's prompt: the task, then the handover (see the module).
    ``queued``: the waiting messages it quotes (``_queued``)."""
    from brindle import checkpoint, limit_report, rewind

    parts = [(agent.task or "").strip() or "(no task was recorded for the earlier worker)"]
    lines = ["---", f"[brindle] {RAN_OUT} (handover {h.id})",
             f"The earlier worker ({agent.profile} on {agent.provider}) stopped at its provider's usage limit "
             f"partway through this task. You carry on in its worktree, on branch `{ws.branch}`: the files are "
             "as it left them, uncommitted changes included (see `git status` and `git diff`). Build on its "
             "work; don't start over."]
    note = checkpoint.note(db, agent.id)
    if note:
        lines += ["", "Its handover note:", _indent(note)]
    commits, dirty = limit_report._work(agent, ws)
    lines += ["", "From brindle's records" + ("" if note else " (it left no handover note)") + ":",
              f"- Commits since the task started: {len(commits)}" + (f" ({_listed(commits)})" if commits else ""),
              f"- Uncommitted files: {len(dirty)}" + (f" ({_listed(dirty)})" if dirty else "")]
    if h.snapshot is not None:
        lines.append(f"- brindle snapshotted the worktree at the stop (turn {h.snapshot}, "
                     f"`{rewind.refs_for(agent.id)}/{h.snapshot}`)")
    turns = _last_turns(db, agent)
    if turns:
        lines += ["- Its last turns:", *(_indent(t) for t in turns)]
    if agent.result:
        lines += ["", "Its last report:", _indent(agent.result)]
    review = db.last_review(ws.id)
    if review is not None and not review.approved and agent.pipeline == "fixing":
        lines += ["", f"Open review findings (fix round {agent.pipeline_rounds or 1}):",
                  _indent(review.summary or "(the reviewer gave no summary)")]
    if agent.plan_state:
        lines += ["", f"Its plan ({agent.plan_state}):", _indent(agent.plan_text or "(not recorded)")]
    queued = _queued(db, agent.id) if queued is None else queued
    if queued:
        lines += ["", "Messages that were waiting for it (any others come to you as messages):",
                  *(f"- {m}" for _, m in queued)]
    if agent.done_when:
        lines += ["", f"Your finish line is the same: {agent.done_when.strip()}"]
    parts.append("\n".join(lines))
    return "\n\n".join(parts)


# -- the person's answer ---------------------------------------------------------------------------


def answer(db: DB, handover_id: str, choice: str, now: float | None = None) -> str:
    """The person's answer on a handover's Inbox card: wait (resume the worker
    in place once its provider is back), continue (start the offered
    replacement) or stop (leave the worker paused). A second answer finds
    the card taken and changes nothing. Returns what brindle did."""
    now = _now(now)
    if choice not in CHOICES:
        raise ValueError(f"choose one of {', '.join(CHOICES)}")
    h = get(db, handover_id)
    if h is None:
        raise KeyError(handover_id)
    if h.state not in OPEN:
        return h.detail or "already answered"
    if choice == "continue" and not h.to_profile:
        return "no replacement was offered: choose wait or stop"
    if not _move(db, h, CLAIMED, now):
        return (get(db, h.id) or h).detail or "already answered"
    h = get(db, h.id)
    worker = db.get_agent(h.from_agent)
    ws = db.get_workspace(worker.workspace_id) if worker else None
    if choice == "stop" or ws is None:
        _move(db, h, STOPPED, now, detail="the person stopped it: the worker stays paused with its work")
    elif choice == "wait":
        _move(db, h, PARKED, now, detail=f"the person chose to wait: brindle resumes the worker in place "
                                         f"once its provider is back ({_until(h)})")
    elif _start(db, h, ws, h.to_profile, now) is None:
        return "the worker moved on meanwhile: nothing was started"
    h = get(db, h.id)
    worker = db.get_agent(h.from_agent)
    if worker is not None:
        _tell(db, worker, h.detail or "")
    return h.detail or ""


def _tell(db: DB, worker: Agent, what: str) -> None:
    """Tell ``worker``'s supervisor what became of its task."""
    from brindle import agents, tmux

    if not worker.parent_id or not what:
        return
    text = f"[brindle] Worker {worker.id}'s task: {what}."
    try:
        agents.send_message(db, worker.parent_id, text, sender_id="brindle")
    except (agents.AgentError, tmux.TmuxError):
        try:
            db.enqueue(worker.parent_id, text, "brindle")
        except Exception:  # noqa: BLE001
            log.exception("brindle: couldn't tell %s about %s", worker.parent_id, worker.id)


# -- the sweep -------------------------------------------------------------------------------------


def sweep(db: DB, now: float | None = None) -> list[str]:
    """Resume parked workers whose provider is back (or hand their task over
    if it won't be soon), and take up rows a process claimed and never
    finished."""
    now = _now(now)
    done = []
    for h in _in_state(db, PARKED):
        try:
            line = _due(db, h, now)
        except Exception:  # noqa: BLE001 - one worker's never stops the sweep
            log.exception("brindle: couldn't resume the parked %s", h.from_agent)
            line = None
        if line:
            done.append(line)
    for h in _in_state(db, CLAIMED):
        if h.claimed_at is not None and now - h.claimed_at < CLAIM_EXPIRES:
            continue   # its process may still be at work on it
        try:
            line = _recover(db, h, now)
        except Exception:  # noqa: BLE001
            log.exception("brindle: couldn't recover the handover of %s", h.from_agent)
            line = None
        if line:
            done.append(line)
    return done


def _due(db: DB, h: Handover, now: float) -> str | None:
    from brindle import agents, autopilot, limit_report

    if h.until_ts is not None and now < h.until_ts:
        return None
    worker = db.get_agent(h.from_agent)
    if not _still_at(worker, h):
        _move(db, h, RELEASED, now, detail="the worker moved on while parked")
        return None
    if agents.session_paused(db, worker):
        return None   # the session's resume brings the worker back (and this row is released then)
    health = limit_report.health_of(db, worker, now)
    if health is None:
        return None
    if not health.ok and health.until is not None and health.until - now <= PARK_SECONDS:
        _move(db, h, PARKED, now, until_ts=health.until)   # back a little later: keep waiting
        return None
    if not _move(db, h, CLAIMED, now):
        return None
    h = get(db, h.id)
    if not health.ok:
        ws = db.get_workspace(worker.workspace_id)
        done = _next(db, _set(db, h, now, until_ts=health.until), ws, now) if ws else None
        if done:
            _tell(db, worker, done)
        return f"handover of {worker.id}: {done}" if done else None
    root_id = autopilot.root_of(db, worker.id)
    with agents.resume_lock(root_id):
        if agents.session_paused(db, worker):    # paused since it was read: it waits for that resume
            _move(db, h, PARKED, now)
            return None
        agents.resume(db, root_id, only={worker.id})
    resumed = db.get_agent(worker.id)
    if resumed is not None and agents.session_paused(db, resumed) and resumed.status != "paused":
        agents.pause_worker(db, resumed)          # a pause that came in during the resume wins
    _move(db, h, RESUMED, now, detail="its provider is back: brindle resumed the worker in place")
    return f"resumed the parked {worker.id} in place"


def _launch(h: Handover) -> dict:
    try:
        data = json.loads(h.launch) if h.launch else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _replacement_started(db: DB, h: Handover) -> Agent | None:
    """The replacement a process started for ``h`` before it died, if any:
    the agent it recorded, else the one launched with exactly the brief it
    recorded before launching (a brief names its handover). Never another
    worker that merely shares the workspace."""
    if h.to_agent:
        return db.get_agent(h.to_agent)
    digest, old = _launch(h).get("brief"), db.get_agent(h.from_agent)
    if not digest or old is None:
        return None
    return next((a for a in db.list_agents(old.workspace_id)
                 if a.id != old.id and a.task and _digest(a.task) == digest), None)


def _recover(db: DB, h: Handover, now: float) -> str | None:
    """A claimed row its process left: adopt the replacement it started,
    carry on while the worker is still at that stop, release it otherwise."""
    if not _move(db, h, CLAIMED, now):
        return None   # another sweep took it
    h = get(db, h.id)
    old = db.get_agent(h.from_agent)
    ws = db.get_workspace(old.workspace_id) if old else None
    fresh = _replacement_started(db, h)
    if fresh is not None and old is not None and ws is not None:
        if old.dismissed_at is None:
            done = _retire(db, _set(db, h, now, to_agent=fresh.id, to_profile=fresh.profile), old, fresh, ws, now,
                           [i for i in _launch(h).get("quoted", []) if isinstance(i, int)])
        else:
            done = _set(db, h, now, to_agent=fresh.id, state=STARTED, claimed_at=None).detail
        _tell(db, old, done or "")
        return f"finished the handover of {old.id} to {fresh.id}"
    if ws is None or not _still_at(old, h):
        _move(db, h, RELEASED, now, detail="the worker moved on before brindle acted")
        return f"released the handover of {h.from_agent}"
    from brindle import limit_report

    health = limit_report.health_of(db, old, now)
    if health is not None and health.ok:
        if old.status == "paused":
            return _due(db, _set(db, h, now, state=PARKED, until_ts=None, claimed_at=None), now)
        _move(db, h, RELEASED, now, detail="its provider is back")
        return f"released the handover of {h.from_agent}: its provider is back"
    h = _set(db, h, now, until_ts=health.until if health else h.until_ts)
    done = _next(db, h, ws, now)
    if done:
        _tell(db, old, done)
    return f"carried on the handover of {old.id}" if done else None


# -- the Inbox -------------------------------------------------------------------------------------


def inbox_items(db: DB, repo_root: str | None) -> list:
    """Handovers waiting on the person, as Inbox cards: KIND, ``request_id``
    the row's id, ``choices`` the answers (continue on <profile> when one was
    offered, wait, stop), each posted to ``action`` (POST /v1/handovers/{id})
    as {"choice": id}. A card whose worker moved on (its session resumed it)
    isn't shown. Reads only the database."""
    from brindle import autopilot
    from brindle.service.dto import InboxActionDTO, InboxChoiceDTO, InboxItemDTO

    items = []
    for h in _in_state(db, *OPEN):
        worker = db.get_agent(h.from_agent)
        ws = db.get_workspace(worker.workspace_id) if worker else None
        if (not _still_at(worker, h) or ws is None
                or (repo_root is not None and ws.repo_root != repo_root)):
            continue
        choices = ([InboxChoiceDTO(id="continue", label=f"Continue on {h.to_profile}")] if h.to_profile else []) \
            + [InboxChoiceDTO(id="wait", label="Wait for its provider"), InboxChoiceDTO(id="stop", label="Stop")]
        reason = (f"Its provider ran out a third time, after {MAX_HOPS} handovers" if h.state == HELD
                  else f"Its provider ran out; {h.to_profile} could carry on, billed per token")
        items.append(InboxItemDTO(kind=KIND, reason=reason, detail=h.detail, choices=choices,
                                  agent_id=worker.id, workspace_id=ws.id,
                                  root_id=autopilot.root_of(db, worker.id), request_id=h.id,
                                  action=InboxActionDTO(method="POST", path=f"/v1/handovers/{h.id}")))
    return items
