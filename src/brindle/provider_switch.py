"""Moving an out-of-quota supervisor to another provider, and back.

When a session's supervisor hits its provider's subscription limit
(brindle.quota: a ``limited_until`` in the future, or its fullest plan window
at 100%), brindle offers in the Inbox to carry the session on with another
provider that is installed, signed in and in quota. Switch starts a new
supervisor on that provider in the *same* workspace and branch, hands it the
session the way ``sessions.handover_to`` does (goal, milestones, workers,
queued tasks), and gives it a handoff note built from brindle's own records,
since the new CLI can't read the old one's conversation. Once the original
provider's limit resets, brindle offers to switch back the same way. With
quota_switch="auto", sweeps carry out these switches and show a notice
with an action to return early instead of asking first.

Records live in the ``provider_switches`` table (see db.py): one row per
offer, ``kind`` switch or back, ``state`` open -> switching -> done (or
dismissed / failed, or withdrawn once its limit is not confirmed: see
``withdraw_unconfirmed``). Pressing Switch claims the row with one UPDATE from
open to switching, so a second press (two windows, a double click) finds it
taken and gets the same supervisor back instead of starting another.

Every function takes ``now`` so tests drive it with a fake clock. Nothing
here calls a provider's backend: quota comes from brindle.quota, install and
sign-in from brindle.preflight.

The handoff and switch-back operations are implemented separately; see
docs/provider-switch-design.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from brindle import identity_lock, quota
from brindle.db import DB, Agent
from brindle.service.dto import InboxActionDTO, InboxChoiceDTO, InboxItemDTO

# Providers that can run an interactive supervisor, in the order a tie in
# headroom is broken. Native (local model) is left out: see the design doc.
SUPERVISOR_PROVIDERS = ("claude", "codex", "antigravity", "opencode")

# Inbox card kinds (InboxItemDTO.kind); request_id carries the offer id.
KIND_OFFER = "provider_switch"
KIND_BACK = "provider_switch_back"

# What the handoff note quotes from brindle's records.
RECENT_MESSAGES = 10         # inbox rows to or from the old supervisor
TRANSCRIPT_TAIL_ENTRIES = 40  # text turns from the old supervisor's transcript (agents.transcript_file)


@dataclass
class Offer:
    """A ``provider_switches`` row."""
    id: str
    root_id: str
    kind: str                    # switch | back
    from_provider: str
    to_provider: str
    state: str                   # open | switching | done | dismissed | failed | withdrawn
    limited_until: float | None
    new_root_id: str | None
    back_of: str | None
    created_at: float
    updated_at: float


# -- detection -------------------------------------------------------------------


def quota_switch() -> str:
    """User consent to automatic quota switching; unknown values keep asking."""
    from brindle import config

    return "auto" if config.user_settings().get("quota_switch") == "auto" else "ask"


def _key_billed(profile) -> bool:
    """Conservatively exclude per-token routes from unattended switches."""
    from brindle import connections, pane_auth

    # OpenCode's stored model credentials may bill without an environment
    # key; there is no subscription billing signal for it here.
    if profile.auth == "api_key" or profile.api_key_env or profile.provider == "opencode":
        return True
    if profile.auth == "subscription":
        return False
    return (quota._key_billed(profile.provider)
            or any(profile.env.get(name) or os.environ.get(name)
                   for name in pane_auth.API_KEYS.get(profile.provider, ()))
            or connections._key(profile.provider)[0] is not None)


def limited_until(provider: str, now: float, model: str | None = None) -> float | None:
    """When ``provider`` (running ``model``) can take a supervisor again, or
    None while it can now: ``provider_health.limit``, the definition routing
    and the reviewer check use too (limited, closing, rate limited or
    erroring, each until it lifts). An API-key Claude
    (``quota._key_billed``) is never out of quota, though it can still be
    rate limited or erroring."""
    if provider not in SUPERVISOR_PROVIDERS:
        return None
    from brindle import provider_health

    health = provider_health.limit(provider, model, now=now)
    return None if health.ok else health.until


def _supervisor_profile(provider: str, repo_root: str | None, name: str | None = None):
    """The profile a supervisor launched on ``provider`` would use: the
    configured supervisor choice, on that provider."""
    from brindle import agents
    from brindle.profiles import load_profile

    profile = load_profile(name or agents.SUPERVISOR, repo_root)
    if repo_root:
        profile = agents._with_supervisor_choice(profile, provider, repo_root)
    return replace(profile, provider=provider)


def _supervisor_model(provider: str, repo_root: str | None, name: str | None = None) -> str | None:
    """The model a supervisor on ``provider`` runs, for one-model caps; None
    when it can't be told."""
    from brindle import autopilot

    try:
        profile = _supervisor_profile(provider, repo_root, name)
    except (KeyError, OSError, ValueError):
        return None
    return autopilot.effective_profile_model(profile, repo_root) or None


def usable(provider: str, repo_root: str | None) -> bool:
    """Installed, turned on and signed in: ``preflight.check`` has nothing
    that would stop a launch on ``provider`` in ``repo_root``."""
    from brindle import preflight

    return provider in SUPERVISOR_PROVIDERS and not preflight.check(provider, repo_root)


def _targets(current: str, repo_root: str | None, now: float, *, allow_limited: bool = False) -> list[str]:
    """Eligible targets, ordered by headroom with a stable provider tie break."""
    from brindle import airgap, autopilot, connections, policy
    from brindle.config import load_repo_config
    from brindle.pro import managed_models
    from brindle.service.routing import RoutingPolicy

    cfg = load_repo_config(repo_root) if repo_root else None
    gate = RoutingPolicy.load(repo_root or "")
    if gate.access.error or (cfg and airgap.enabled(cfg)):
        return []
    targets = []
    for provider in SUPERVISOR_PROVIDERS:
        if (provider == current or (cfg and provider in cfg.exclude_providers)
                or not gate.access.allows(provider)
                or (not allow_limited and limited_until(provider, now) is not None)
                or not usable(provider, repo_root)):
            continue
        # Judge the identity a supervisor launched with this provider would use,
        # including a configured supervisor choice and the org's managed route.
        profile = _supervisor_profile(provider, repo_root)
        if gate.managed is not None:
            if managed_models.refusal(gate.managed, profile):
                continue
            profile = managed_models.apply_profile(gate.managed, profile)
        profile = connections.apply(profile, repo_root)
        if quota_switch() == "auto" and (managed_models.has_org_key(gate.managed, provider)
                                          or _key_billed(profile)):
            continue
        model = autopilot.effective_profile_model(profile, repo_root) or None
        if not gate.access.allows_profile(profile.name, provider, model):
            continue
        if not allow_limited and model and limited_until(provider, now, model) is not None:
            continue    # a cap on the model this supervisor would run
        if cfg is not None:
            info = policy.AssignInfo(repo_root=repo_root, profile=profile.name,
                                     provider=provider, model=model, mode="handoff")
            decision = policy._decide("delegation", cfg, repo_root, lambda p: p.check_assign(info))
            if not decision.allowed:
                continue
        q = None if quota._key_billed(provider) else quota.get(provider)
        window = quota.fullest(replace(q, windows=[w for w in q.windows
                                                   if w.resets_at is None or w.resets_at > now])) if q else None
        headroom = max(0.0, 100.0 - window.used) if window else 100.0
        targets.append((provider, headroom))
    targets.sort(key=lambda item: -item[1])
    return [provider for provider, _ in targets]


# What the Inbox cards show is checked here, off the request path: per
# database (DB.path), (from provider, repo root, allow_limited) -> the
# targets ``_targets`` found then. maybe_offer records its own look and each
# sweep (the daemon's, every PROVIDER_SWITCH_EVERY) replaces the lot for the
# offers still open, so ``inbox_items`` never runs preflight, profiles or
# policy itself: a card is at most one sweep out of date, and pressing Switch
# checks again (``switch``). An offer no look has covered yet shows no card.
_card_targets: dict[str, dict[tuple[str, str | None, bool], list[str]]] = {}


def _checked_targets(db: DB, current: str, repo_root: str | None, now: float, *,
                     allow_limited: bool = False) -> list[str]:
    """``_targets``, remembered for this database's Inbox cards."""
    found = _targets(current, repo_root, now, allow_limited=allow_limited)
    _card_targets.setdefault(db.path, {})[(current, repo_root, allow_limited)] = found
    return found


def _card_key(db: DB, offer: Offer) -> tuple[str, str | None, bool] | None:
    root = db.get_agent(offer.root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    return None if ws is None else (offer.from_provider, ws.repo_root, offer.kind == "back")


def check_cards(db: DB, now: float) -> None:
    """Look again at the targets of every open offer, for ``inbox_items``."""
    checked: dict[tuple[str, str | None, bool], list[str]] = {}
    for row in db.conn.execute("SELECT * FROM provider_switches WHERE state='open'"):
        key = _card_key(db, Offer(**dict(row)))
        if key is not None and key not in checked:
            checked[key] = _targets(key[0], key[1], now, allow_limited=key[2])
    _card_targets[db.path] = checked


def pick_target(current: str, repo_root: str | None, now: float) -> str | None:
    """The provider to move a supervisor on ``current`` to: usable, in quota,
    most headroom first (ties in SUPERVISOR_PROVIDERS order). None when no
    other provider qualifies."""
    if current not in SUPERVISOR_PROVIDERS:
        return None
    return next(iter(_targets(current, repo_root, now)), None)


def confirmed_until(provider: str, now: float, model: str | None = None) -> float | None:
    """When ``provider``'s confirmed limit lifts, or None while there is
    none: a recorded limit, or a full account window (or a full one-model
    cap on ``model``). Only this moves a supervisor away. A forecast
    (``closing``), one 429 (``rate_limited``) or a run of errors
    (``erroring``) is no limit: each lifts on its own, and acting on it once
    offered every supervisor a switch while Claude was at 49%."""
    if provider not in SUPERVISOR_PROVIDERS:
        return None
    from brindle import provider_health

    return provider_health.limited_until(provider, model, now=now)


def out_of_quota(db: DB, root_id: str, now: float) -> float | None:
    """When the supervisor ``root_id``'s provider (on the model it runs) is
    available again after a confirmed limit (``confirmed_until``), or None
    while there is none, or when ``root_id`` isn't an active session root (a
    worker, or a paused or done session)."""
    root = db.get_agent(root_id)
    if (root is None or root.parent_id is not None or root.mode != "interactive"
            or root.status in ("paused", "done") or root.dismissed_at is not None
            or identity_lock.locked_by(root.id)):
        return None
    ws = db.get_workspace(root.workspace_id)
    return confirmed_until(root.provider, now,
                           _supervisor_model(root.provider, ws.repo_root if ws else None, root.profile))


# -- offers ----------------------------------------------------------------------


def get(db: DB, offer_id: str) -> Offer | None:
    row = db.conn.execute("SELECT * FROM provider_switches WHERE id=?", (offer_id,)).fetchone()
    return Offer(**dict(row)) if row else None


def open_offer(db: DB, root_id: str) -> Offer | None:
    """The session's offer that is open or switching, if any."""
    row = db.conn.execute("SELECT * FROM provider_switches WHERE root_id=? "
                          "AND state IN ('open', 'switching')", (root_id,)).fetchone()
    return Offer(**dict(row)) if row else None


def maybe_offer(db: DB, root_id: str, now: float) -> Offer | None:
    """Offer to switch ``root_id`` when its provider is out of quota and
    ``pick_target`` finds another. Idempotent: an offer already open for the
    session is returned as is. None (and no row) when there is nothing to offer."""
    if existing := open_offer(db, root_id):
        return existing
    until = out_of_quota(db, root_id, now)
    if until is None:
        return None
    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id)
    if ws is None:
        return None
    if root.provider not in SUPERVISOR_PROVIDERS:
        return None
    target = next(iter(_checked_targets(db, root.provider, ws.repo_root, now)), None)
    if target is None:
        return None
    offer_id = uuid.uuid4().hex
    with db.tx() as c:
        # Serialize the history check and insert: dismissals and failed starts
        # consume this limit, and concurrent detections share the active offer.
        # Follow handovers so an early switch back cannot repeat this limit.
        c.execute("INSERT OR IGNORE INTO provider_switches "
                  "(id, root_id, kind, from_provider, to_provider, state, limited_until, created_at, updated_at) "
                  "SELECT ?, ?, 'switch', ?, ?, 'open', ?, ?, ? WHERE NOT EXISTS "
                  "(SELECT 1 FROM provider_switches WHERE root_id IN ("
                  "WITH RECURSIVE lineage(id) AS (SELECT ? UNION "
                  "SELECT p.root_id FROM provider_switches p JOIN lineage l ON p.new_root_id=l.id "
                  "WHERE p.state='done') SELECT id FROM lineage) AND kind='switch' "
                  "AND state != 'withdrawn' AND from_provider=? AND limited_until=?)",
                  (offer_id, root_id, root.provider, target, until, now, now,
                   root_id, root.provider, until))
    return open_offer(db, root_id)


def maybe_offer_back(db: DB, now: float, *, switch_id: str | None = None) -> list[Offer]:
    """For each done switch whose ``from_provider`` is in quota again and
    whose new supervisor is still the session's, a back offer (once: an
    open or dismissed back offer for that switch is not repeated).
    An explicit switch_id is the notice's action to return before reset."""
    from brindle import sessions

    offers = []
    rows = db.conn.execute("SELECT * FROM provider_switches WHERE kind='switch' AND state='done' "
                           "AND NOT EXISTS (SELECT 1 FROM provider_switches AS back "
                           "WHERE back.back_of=provider_switches.id) ORDER BY created_at, id").fetchall()
    for row in rows:
        original = Offer(**dict(row))
        if switch_id is not None and original.id != switch_id:
            continue
        root = db.get_agent(original.new_root_id) if original.new_root_id else None
        if (root is None or root.parent_id is not None or root.mode != "interactive"
                or root.status in ("paused", "done") or root.dismissed_at is not None
                or root.provider != original.to_provider or identity_lock.locked_by(root.id)):
            continue
        ap = db.get_autopilot(root.id)
        if (ap and sessions.handed_away(ap)) or open_offer(db, root.id) is not None:
            continue
        ws = db.get_workspace(root.workspace_id)
        if ws is None or original.from_provider not in _targets(
                root.provider, ws.repo_root, now, allow_limited=switch_id is not None):
            continue
        offer_id = uuid.uuid4().hex
        with db.tx() as c:
            # Check history inside the write transaction too: concurrent sweeps
            # share one offer, and every terminal state consumes the back offer.
            inserted = c.execute(
                "INSERT OR IGNORE INTO provider_switches "
                "(id, root_id, kind, from_provider, to_provider, state, limited_until, back_of, "
                "created_at, updated_at) SELECT ?, ?, 'back', ?, ?, 'open', ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM provider_switches WHERE back_of=?)",
                (offer_id, root.id, root.provider, original.from_provider, original.limited_until,
                 original.id, now, now, original.id)).rowcount
        if inserted:
            offers.append(get(db, offer_id))
    return offers


def dismiss(db: DB, offer_id: str, now: float) -> Offer:
    """Not now: the offer closes and isn't made again for this limit."""
    with db.tx() as c:
        c.execute("UPDATE provider_switches SET state='dismissed', updated_at=? "
                  "WHERE id=? AND state IN ('open', 'failed')", (now, offer_id))
    offer = get(db, offer_id)
    if offer is None:
        raise ValueError(f"no provider switch offer {offer_id}")
    return offer


def dismiss_notice(db: DB, offer_id: str) -> None:
    """Hide the completed-switch notice while preserving its recovery history."""
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO inbox_dismissed (key) VALUES (?)",
                  (f"provider-switch-notice:{offer_id}",))


def withdraw_unconfirmed(db: DB, now: float) -> list[str]:
    """Withdraw every open switch offer whose session has no confirmed limit
    (``out_of_quota`` is None): its provider is back in quota, or the limit
    was never real (an offer made on a forecast before ``confirmed_until``),
    or the session stopped. A withdrawn offer consumes nothing: a real limit
    later, even with the same reset, is offered afresh. Another identity's
    session is left as it is. Returns the offers withdrawn."""
    withdrawn = []
    for row in db.conn.execute("SELECT id, root_id FROM provider_switches "
                               "WHERE kind='switch' AND state='open'").fetchall():
        if identity_lock.locked_by(row["root_id"]) or out_of_quota(db, row["root_id"], now) is not None:
            continue
        with db.tx() as c:
            if c.execute("UPDATE provider_switches SET state='withdrawn', updated_at=? "
                         "WHERE id=? AND state='open'", (now, row["id"])).rowcount:
                withdrawn.append(row["id"])
    return withdrawn


def sweep(db: DB, now: float) -> list[Offer]:
    """New offers for active roots, or carry them out with automatic consent.
    Offers whose limit isn't confirmed (any more) are withdrawn first, so the
    first sweep after a start clears what an earlier build offered wrongly."""
    from brindle import agents

    withdraw_unconfirmed(db, now)
    offers = maybe_offer_back(db, now)
    automatic = quota_switch() == "auto"
    for root in db.list_agents():
        existing = open_offer(db, root.id)
        if (automatic and existing is not None and existing.state == "open" and existing.kind == "switch"
                and out_of_quota(db, root.id, now) != existing.limited_until):
            dismiss(db, existing.id, now)
            existing = None
        if existing is not None:
            if automatic and existing.state == "open" and existing not in offers:
                offers.append(existing)
            continue
        if offer := maybe_offer(db, root.id, now):
            offers.append(offer)
    if automatic:
        for offer in offers:
            if offer.kind == "back":
                back_root = db.get_agent(offer.root_id)
                back_ws = db.get_workspace(back_root.workspace_id) if back_root else None
                model = _supervisor_model(offer.to_provider, back_ws.repo_root if back_ws else None)
                if limited_until(offer.to_provider, now, model) is not None:
                    continue
            try:
                switch(db, offer.id, now, automatic=True)
            except agents.AgentError:
                # The failed row consumes this limit and exposes the failure.
                continue
        check_cards(db, now)
        return []
    check_cards(db, now)
    return offers


# -- handoff ---------------------------------------------------------------------


def handoff_note(db: DB, root_id: str, now: float) -> str:
    """What the new supervisor is told, from brindle's records only: the
    goal and each milestone with its last result, the workers it inherits
    (id, profile, status, branch), queued tasks, the last RECENT_MESSAGES
    inbox messages to or from ``root_id``, and the text of the last
    TRANSCRIPT_TAIL_ENTRIES turns of its transcript when brindle knows the
    file (Claude's JSONL: user/assistant text blocks). Passed to
    ``sessions.handover_to`` as its ``note``."""
    from brindle import agents

    root = db.get_agent(root_id)
    if root is None:
        raise agents.AgentError(f"no session {root_id}")
    ap = db.get_autopilot(root_id)
    lines = [f"Session {root_id}, recorded at {now:g}", "", "Goal:",
             ap.goal if ap and ap.goal else root.task or "No goal recorded."]
    if ap and ap.detail:
        lines.append(ap.detail)
    lines.append("\nMilestones and last results:")
    for m in db.milestones(root_id):
        lines.append(f"- {m.title} [{m.status}]\n{m.output or 'No result recorded.'}")
    lines.append("\nWorkers:")
    for worker in agents.tree(db, root_id)[1:]:
        ws = db.get_workspace(worker.workspace_id)
        lines.append(f"- {worker.id}: {worker.profile}, {worker.status}, branch {ws.branch if ws else '?'}")
    lines.append("\nQueued tasks:")
    for task in db.list_tasks(state="pending"):
        if task.caller_id == root_id:
            lines.append(f"- {task.id} ({task.profile}): {task.task_text}")
    lines.append("\nRecent inbox messages:")
    messages = db.conn.execute("SELECT sender_id, agent_id, body FROM inbox "
                               "WHERE agent_id=? OR sender_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                               (root_id, root_id, RECENT_MESSAGES)).fetchall()
    for msg in reversed(messages):
        lines.append(f"- {msg['sender_id'] or 'brindle'} → {msg['agent_id']}: {msg['body']}")
    lines.append("\nRecent conversation:")
    lines.extend(_transcript_tail(db, root))
    return "\n".join(lines)


def _transcript_tail(db: DB, root: Agent) -> list[str]:
    """Only conversation text from the files already used for usage tracking."""
    from brindle import provider_homes, usage
    from brindle.web.chat import _claude_message

    tail: deque[str] = deque(maxlen=TRANSCRIPT_TAIL_ENTRIES)
    omitted = ["Transcript tail omitted: no readable conversation transcript verified for this session."]
    try:
        if root.provider == "codex":
            # Usage tracking may infer a rollout from cwd/time. A handoff must
            # never export another session's text on that weaker association,
            # or look in whichever provider home happens to be current now.
            paths = []
            if root.session_ref and root.config_home is not None:
                home = Path(root.config_home) if root.config_home else provider_homes.source("codex")
                path = usage.find_codex_rollout(root.session_ref, None, root.created_at, home)
                if path:
                    paths = [path]
        else:
            paths = usage.agent_transcripts(db, root)
        # Claude's additional paths belong to subagents, not this conversation.
        if paths:
            with paths[0].open(encoding="utf-8", errors="replace") as transcript:
                if root.provider == "codex":
                    # Verify the header on the same open file used for the
                    # tail: a matching filename alone is not ownership proof.
                    try:
                        header = json.loads(transcript.readline())
                    except ValueError:
                        return omitted
                    meta = header.get("payload") if isinstance(header, dict) else None
                    if (not isinstance(meta, dict) or header.get("type") != "session_meta"
                            or meta.get("id") != root.session_ref):
                        return omitted
                for line in transcript:
                    if not line.endswith("\n"):
                        continue  # the CLI may still be writing this entry
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    message = _claude_message(entry)
                    if isinstance(entry, dict) and root.provider == "codex":
                        payload = entry.get("payload")
                        message = None
                        if (entry.get("type") == "response_item" and isinstance(payload, dict)
                                and payload.get("type") == "message"
                                and payload.get("role") in ("user", "assistant")):
                            content = payload.get("content")
                            text = "\n".join(b["text"] for b in content
                                             if isinstance(b, dict) and b.get("type") in ("input_text", "output_text")
                                             and isinstance(b.get("text"), str)) if isinstance(content, list) else ""
                            if text.strip():
                                message = {"role": payload["role"], "text": text.strip()}
                    elif isinstance(entry, dict) and root.provider == "antigravity":
                        # Usage-only events have no conversation text to carry.
                        role = {"USER_MESSAGE": "user", "PLANNER_RESPONSE": "assistant"}.get(entry.get("type"))
                        if role and isinstance(entry.get("text"), str) and entry["text"].strip():
                            message = {"role": role, "text": entry["text"].strip()}
                    if message:
                        tail.append(f"{message['role']}: {message['text']}")
    except OSError:
        return ["Transcript tail omitted: the recorded transcript could not be read."]
    return list(tail) or omitted


def switch(db: DB, offer_id: str, now: float, *, automatic: bool = False) -> Agent:
    """Carry out a switch or back offer: claim it (open -> switching in one
    UPDATE), then ``sessions.handover_to`` with ``dest`` the old root's own
    workspace, ``provider=offer.to_provider`` and ``handoff_note`` as the
    note, then record ``new_root_id`` and state done. A claim that finds the
    row already switching or done returns that supervisor (waiting for an
    in-flight one) and starts nothing. A failed start marks it failed and
    reopens nothing: the person sees the failure on the card."""
    from brindle import agents, bus, pipeline, sessions

    offer = get(db, offer_id)
    if offer is None:
        raise agents.AgentError(f"no provider switch offer {offer_id}")
    key = hashlib.sha256(f"{db.path}\0{offer.id}".encode()).hexdigest()
    # Keep the lock until the result is durable. Other presses wait here,
    # without holding a database transaction or interrupting the first start.
    with pipeline._file_lock("provider-switch-" + key):
        offer = get(db, offer_id)
        if offer is None:
            raise agents.AgentError(f"no provider switch offer {offer_id}")
        if offer.state == "done":
            new = db.get_agent(offer.new_root_id) if offer.new_root_id else None
            if new is None:
                raise agents.AgentError("the switched supervisor no longer exists")
            return new
        if offer.state == "switching":
            # Acquiring the lock means the previous holder exited without
            # recording its result. Never launch a replacement for that start.
            with db.tx() as c:
                c.execute("UPDATE provider_switches SET state='failed', updated_at=? WHERE id=?",
                          (now, offer_id))
            raise agents.AgentError("the provider switch was interrupted; check the session before continuing")
        if automatic and offer.state == "open" and offer.kind == "switch":
            # Recheck after taking the dispatch lock: an old ask card is not
            # consent to move a recovered session or to use an outdated target.
            until = out_of_quota(db, offer.root_id, now)
            if until is None or until != offer.limited_until:
                dismiss(db, offer.id, now)
                raise agents.AgentError("the quota limit for this offer is no longer current")
            root = db.get_agent(offer.root_id)
            ws = db.get_workspace(root.workspace_id)
            target = pick_target(root.provider, ws.repo_root, now) if ws else None
            if target is None:
                dismiss(db, offer.id, now)
                raise agents.AgentError("no automatic switch target is currently available")
            with db.tx() as c:
                c.execute("UPDATE provider_switches SET to_provider=?, updated_at=? WHERE id=? AND state='open'",
                          (target, now, offer.id))
            offer = replace(offer, to_provider=target)
        with db.tx() as c:
            claimed = c.execute("UPDATE provider_switches SET state='switching', updated_at=? "
                                "WHERE id=? AND state='open'", (now, offer_id)).rowcount
        if not claimed:
            raise agents.AgentError(f"provider switch offer is {offer.state}")
        try:
            root = db.get_agent(offer.root_id)
            if (root is None or root.parent_id is not None or root.mode != "interactive"
                    or root.status in ("paused", "done") or root.dismissed_at is not None
                    or root.provider != offer.from_provider or identity_lock.locked_by(root.id)):
                raise agents.AgentError("the session can no longer switch providers")
            ws = db.get_workspace(root.workspace_id)
            if ws is None or offer.to_provider not in _targets(
                    root.provider, ws.repo_root, now, allow_limited=offer.kind == "back"):
                raise agents.AgentError("the target provider is no longer available for this session")
            new = sessions.handover_to(db, root.id, ws, handoff_note(db, root.id, now),
                                       provider=offer.to_provider, pause_old=True).agent
            with db.tx() as c:
                c.execute("UPDATE provider_switches SET state='done', new_root_id=?, updated_at=? WHERE id=?",
                          (new.id, now, offer_id))
        except BaseException:
            with db.tx() as c:
                c.execute("UPDATE provider_switches SET state='failed', updated_at=? WHERE id=?",
                          (now, offer_id))
            bus.publish_agent(db, "provider_switch.failed", offer.root_id, offer_id=offer_id)
            raise
        return new


def switch_back(db: DB, offer_id: str, now: float) -> Agent:
    """``switch`` for a back offer (kind back): the session returns to the
    provider it started on, in the same workspace."""
    from brindle import agents

    offer = get(db, offer_id)
    if offer is None or offer.kind != "back":
        raise agents.AgentError(f"no provider switch-back offer {offer_id}")
    return switch(db, offer_id, now)


# -- inbox -----------------------------------------------------------------------


NOTICE_PREFIX = "notice: "  # marks a completed-switch notice's detail: "notice: from → to · HH:MM"


SESSIONS_MARK = " sessions: "  # a card for several sessions ends "· <n> sessions: <id>, <id>"
WORKERS_MARK = " workers: "    # workers paused on the same limit: "· <n> workers: <id>, <id>"


@dataclass
class _Alert:
    """One Inbox card: everything waiting on the person about one limit (a
    provider, or one model's cap, until one reset). Its members are the
    same whichever repo's Inbox shows it, and one answer goes to them all."""
    kind: str                                    # limit | back | failed
    offers: list = field(default_factory=list)   # (Offer, root, ws): open switch (limit) or back offers
    notices: list = field(default_factory=list)  # (Offer, new root, ws): switches made on this limit
    workers: list = field(default_factory=list)  # (Handover, worker, ws, its own card): workers asking

    def repos(self) -> set:
        return {m[2].repo_root for m in self.offers + self.notices + self.workers}


def _alerts(db: DB, now: float) -> list[_Alert]:
    """Every card the Inbox shows about quota, in every repo: one per limit,
    keyed by provider and reset (``limited_until``; a worker's handover:
    its provider and ``until_ts``), whatever target each session would move
    to and whether it has switched already. Sessions offered a switch, the
    switches made (their notices) and the workers whose handover waits on
    the person (brindle.handover: a replacement billed per token, or a task
    out of handovers) all share it; a later session or worker on that limit
    joins it. The offers back once it resets share another, keyed by the
    provider back in quota. A failed switch is a card of its own. Reads only
    the database, quota.json and the targets the last sweep found
    (``check_cards``): never preflight, profiles or policy."""
    from brindle import handover, sessions

    checked = _card_targets.get(db.path, {})
    alerts: dict[tuple, _Alert] = {}

    def alert(key: tuple) -> _Alert:
        return alerts.setdefault(key, _Alert(key[0]))

    for row in db.conn.execute(
            "SELECT * FROM provider_switches WHERE kind='switch' AND state='done' "
            "AND NOT EXISTS (SELECT 1 FROM inbox_dismissed WHERE key='provider-switch-notice:' || provider_switches.id) "
            "AND NOT EXISTS (SELECT 1 FROM provider_switches AS back WHERE back.back_of=provider_switches.id) "
            "ORDER BY created_at, id"):
        offer = Offer(**dict(row))
        root = db.get_agent(offer.new_root_id) if offer.new_root_id else None
        ws = db.get_workspace(root.workspace_id) if root else None
        if (root is None or ws is None or root.status in ("paused", "done")
                or root.parent_id is not None or root.mode != "interactive" or root.dismissed_at is not None
                or root.provider != offer.to_provider or identity_lock.locked_by(root.id)):
            continue
        ap = db.get_autopilot(root.id)
        if ap and sessions.handed_away(ap):
            continue
        alert(("limit", offer.from_provider, offer.limited_until)).notices.append((offer, root, ws))
    for row in db.conn.execute("SELECT * FROM provider_switches WHERE state IN ('open', 'failed') ORDER BY created_at, id"):
        offer = Offer(**dict(row))
        root = db.get_agent(offer.root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        if root is None or ws is None or identity_lock.locked_by(root.id):
            continue
        if offer.state != "failed" and offer.kind == "switch" and (root.provider != offer.from_provider
                                       or out_of_quota(db, root.id, now) is None):
            continue
        if offer.state != "failed" and offer.to_provider not in checked.get(
                (offer.from_provider, ws.repo_root, offer.kind == "back"), ()):
            continue
        if offer.state == "failed":
            alert(("failed", offer.id)).offers.append((offer, root, ws))
        elif offer.kind == "back":
            alert(("back", offer.to_provider, offer.limited_until)).offers.append((offer, root, ws))
        else:
            alert(("limit", offer.from_provider, offer.limited_until)).offers.append((offer, root, ws))
    for card in handover.inbox_items(db, None):
        h = handover.get(db, card.request_id)
        worker = db.get_agent(h.from_agent) if h else None
        ws = db.get_workspace(worker.workspace_id) if worker else None
        if h is not None and worker is not None and ws is not None:
            alert(("limit", worker.provider, h.until_ts)).workers.append((h, worker, ws, card))
    return list(alerts.values())


def _alert_of(db: DB, member_id: str, now: float) -> _Alert | None:
    """The alert ``member_id`` (an offer or a handover) belongs to now."""
    for a in _alerts(db, now):
        if member_id in {m[0].id for m in a.offers + a.notices + a.workers}:
            return a
    return None


def group_of(db: DB, offer_id: str, now: float) -> list[Offer]:
    """The offers one answer on ``offer_id``'s card goes to, ``offer_id``'s
    own first: the alert's open offers, or its switches made when
    ``offer_id`` is one (a notice). Just that offer when no card shows it."""
    a = _alert_of(db, offer_id, now)
    members = ([] if a is None else [m[0] for m in a.offers] if offer_id in {m[0].id for m in a.offers}
               else [m[0] for m in a.notices])
    if not members:
        offer = get(db, offer_id)
        return [offer] if offer else []
    return sorted(members, key=lambda o: o.id != offer_id)


def workers_of(db: DB, member_id: str, now: float) -> list:
    """The handovers waiting on the person in ``member_id``'s alert (an
    offer's, or a handover's own first): what a workers' answer goes to."""
    a = _alert_of(db, member_id, now)
    return sorted((m[0] for m in a.workers), key=lambda h: h.id != member_id) if a else []


def _worker_choices(workers: list) -> list[InboxChoiceDTO]:
    """The workers' answers on an alert (brindle.handover.answer, given to
    each): continue on the replacement each was offered, wait, or stop."""
    picks = {h.to_profile for h, *_ in workers if h.to_profile}
    billed = " (billed per token)" if any(_bills(h, ws) for h, _, ws, _ in workers) else ""
    cont = ([InboxChoiceDTO(id="continue", label=(f"Continue on {next(iter(picks))}" if len(picks) == 1
                                                  else "Continue each on its replacement") + billed)]
            if picks else [])
    return cont + [InboxChoiceDTO(id="wait", label="Wait for its provider"), InboxChoiceDTO(id="stop", label="Stop")]


def _bills(h, ws) -> bool:
    """Whether continuing ``h``'s worker on its offered replacement bills per
    token, whatever paused it: an asking handover was paused for exactly
    that; one held after its last handover (brindle.handover.MAX_HOPS) may
    offer a billed replacement too, and Continue starts it with no other
    ask (``handover.per_token``, which counts what it can't tell as billed)."""
    from brindle import handover

    if not h.to_profile:
        return False
    return h.state == handover.ASKING or handover.per_token(h.to_profile, ws.repo_root)


def _worker_entry(h, worker: Agent, ws) -> str:
    """One worker as an alert lists it, with what continuing would mean:
    "w1 → developer-codex (billed per token)", or "w2 (out of handovers;
    could continue on developer-codex; billed per token)". No commas:
    entries are joined by them."""
    from brindle import handover

    cost = " (billed per token)" if _bills(h, ws) else ""
    if h.state == handover.ASKING:
        return f"{worker.id} → {h.to_profile}{cost}"
    return (f"{worker.id} (out of handovers" + (f"; could continue on {h.to_profile}" if h.to_profile else "")
            + ("; billed per token" if cost else "") + ")")


def _to(shown: list) -> str:
    """Where an alert's sessions go: their targets, "+"-joined when they differ."""
    return "+".join(dict.fromkeys(o.to_provider for o, _, _ in shown))


def inbox_items(db: DB, repo_root: str | None, *, now: float | None = None) -> list[InboxItemDTO]:
    """One Inbox card per alert (``_alerts``), shown in every repo one of
    its members is in, with all of its members. A limit with sessions to
    switch is KIND_OFFER (once they have all switched, the notice, KIND_BACK
    with NOTICE_PREFIX); one with only workers waiting is the handover card
    (brindle.handover's own, for one worker). ``request_id`` is the first
    member, ``detail`` "<from> → <to>" (a notice: "notice: <from> → <to> ·
    HH:MM"; <to> "+"-joins the targets when sessions go to different ones),
    then "· <n> sessions: <id> → <target>, ..." when it is for several and
    "· <n> workers: <entry>, ..." (``_worker_entry``: each worker's
    replacement and whether it bills per token); their answers are the
    card's ``choices``. ``action`` POST /v1/sessions/{root}/provider-switch
    on the first session; an answer there goes to the whole card
    (``group_of``, ``workers_of``)."""
    now = time.time() if now is None else now
    items = []
    for a in _alerts(db, now):
        if repo_root is not None and repo_root not in a.repos():
            continue
        workers = (f" · {len(a.workers)}{WORKERS_MARK}" + ", ".join(_worker_entry(h, w, ws) for h, w, ws, _ in a.workers)
                   if a.workers else "")
        choices = _worker_choices(a.workers) if a.workers else None
        shown = a.offers or a.notices
        if not shown:   # workers only: brindle.handover's card, for all of them
            # Its Continue says whether it bills, held or asking alike (``_bills``).
            card = a.workers[0][3].model_copy(update={"choices": choices})
            if len(a.workers) > 1:
                card = card.model_copy(update={
                    "reason": f"{len(a.workers)} workers stopped at their provider's limit; "
                              + card.reason[0].lower() + card.reason[1:],
                    "detail": (card.detail or "") + workers, "choices": choices})
            items.append(card)
            continue
        offer, root, ws = shown[0]
        to = _to(shown)
        # Each session with the provider it moves (or moved) to: they can differ.
        many = (f" · {len(shown)}{SESSIONS_MARK}" + ", ".join(f"{r.id} → {o.to_provider}" for o, r, _ in shown)
                if len(shown) > 1 else "")
        if not a.offers:
            reset = time.strftime("%H:%M", time.localtime(offer.limited_until)) if offer.limited_until else "unknown"
            kind = KIND_BACK
            reason = (f"Switched {'to' if len(shown) == 1 else f'{len(shown)} sessions to'} {to.replace('+', ' and ')} "
                      f"· {offer.from_provider} was out of quota, back at {reset}")
            detail = (f"{NOTICE_PREFIX}{offer.from_provider} → {to}"
                      + (f" · {reset}" if offer.limited_until else "") + many + workers)
        else:
            kind = KIND_BACK if offer.kind == "back" else KIND_OFFER
            switched = f" ({len(a.notices)} switched already)" if a.notices else ""
            reason = ("Provider switch failed. Check the session before continuing." if a.kind == "failed"
                      else "Switch provider or choose Not now"
                      + (f" for {len(shown)} sessions" if len(shown) > 1 else "") + switched)
            detail = f"{offer.from_provider} → {to}" + many + workers
        items.append(InboxItemDTO(kind=kind, reason=reason, detail=detail, choices=choices,
                                  agent_id=root.id, workspace_id=ws.id, root_id=root.id,
                                  request_id=offer.id,
                                  action=InboxActionDTO(method="POST",
                                      path=f"/v1/sessions/{root.id}/provider-switch")))
    return items
