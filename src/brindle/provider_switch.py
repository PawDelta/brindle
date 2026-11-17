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
dismissed / failed). Pressing Switch claims the row with one UPDATE from
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
from dataclasses import dataclass, replace
from pathlib import Path

from brindle import identity_lock, quota
from brindle.db import DB, Agent
from brindle.service.dto import InboxActionDTO, InboxItemDTO

# Providers that can run an interactive supervisor, in the order a tie in
# headroom is broken. Native (local model) is left out: see the design doc.
SUPERVISOR_PROVIDERS = ("claude", "codex", "antigravity", "opencode")

# Inbox card kinds (InboxItemDTO.kind); request_id carries the offer id.
KIND_OFFER = "provider_switch"
KIND_BACK = "provider_switch_back"

# What the handoff note quotes from brindle's records.
RECENT_MESSAGES = 10         # inbox rows to or from the old supervisor
TRANSCRIPT_TAIL_ENTRIES = 40  # text turns from the old supervisor's transcript (agents.transcript_file)

# A plan window this full counts as out of quota even before the CLI refuses.
FULL_PERCENT = 100.0


@dataclass
class Offer:
    """A ``provider_switches`` row."""
    id: str
    root_id: str
    kind: str                    # switch | back
    from_provider: str
    to_provider: str
    state: str                   # open | switching | done | dismissed | failed
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


def limited_until(provider: str, now: float) -> float | None:
    """When ``provider``'s limit lifts, or None while it is in quota.
    ``quota.get(provider).limited_until`` after ``now``; else the fullest
    plan window (``quota.fullest``) at FULL_PERCENT, until its ``resets_at``.
    An API-key Claude (``quota._key_billed``) is never limited."""
    if provider not in SUPERVISOR_PROVIDERS or quota._key_billed(provider):
        return None
    q = quota.get(provider)
    if q is None:
        return None
    if q.limited_until is not None and q.limited_until > now:
        return q.limited_until
    window = quota.fullest(replace(q, windows=[w for w in q.windows
                                              if w.resets_at is None or w.resets_at > now]))
    if window and window.used >= FULL_PERCENT and window.resets_at is not None:
        return window.resets_at
    return None


def usable(provider: str, repo_root: str | None) -> bool:
    """Installed, turned on and signed in: ``preflight.check`` has nothing
    that would stop a launch on ``provider`` in ``repo_root``."""
    from brindle import preflight

    return provider in SUPERVISOR_PROVIDERS and not preflight.check(provider, repo_root)


def _targets(current: str, repo_root: str | None, now: float, *, allow_limited: bool = False) -> list[str]:
    """Eligible targets, ordered by headroom with a stable provider tie break."""
    from brindle import agents, airgap, autopilot, connections, policy
    from brindle.config import load_repo_config
    from brindle.pro import managed_models
    from brindle.profiles import load_profile
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
        profile = load_profile(agents.SUPERVISOR, repo_root)
        if repo_root:
            profile = agents._with_supervisor_choice(profile, provider, repo_root)
        profile = replace(profile, provider=provider)
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


def out_of_quota(db: DB, root_id: str, now: float) -> float | None:
    """When the supervisor ``root_id``'s provider is available again, or
    None while it is in quota, or when ``root_id`` isn't an active session
    root (a worker, or a paused or done session)."""
    root = db.get_agent(root_id)
    if (root is None or root.parent_id is not None or root.mode != "interactive"
            or root.status in ("paused", "done") or root.dismissed_at is not None
            or identity_lock.locked_by(root.id)):
        return None
    return limited_until(root.provider, now)


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
                  "AND from_provider=? AND limited_until=?)",
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


def sweep(db: DB, now: float) -> list[Offer]:
    """New offers for active roots, or carry them out with automatic consent."""
    from brindle import agents

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
            if offer.kind == "back" and limited_until(offer.to_provider, now) is not None:
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


def inbox_items(db: DB, repo_root: str | None, *, now: float | None = None) -> list[InboxItemDTO]:
    """Open offers as Inbox cards: KIND_OFFER or KIND_BACK, ``request_id``
    the offer id, ``detail`` "<from> -> <to>", ``action`` POST
    /v1/sessions/{root}/provider-switch. Built on every Inbox read, so it
    reads only the database, quota.json and the targets the last sweep
    found (``check_cards``): never preflight, profiles or policy."""
    from brindle import sessions

    now = time.time() if now is None else now
    checked = _card_targets.get(db.path, {})
    items = []
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
                or root.provider != offer.to_provider or identity_lock.locked_by(root.id)
                or (repo_root is not None and ws.repo_root != repo_root)):
            continue
        ap = db.get_autopilot(root.id)
        if ap and sessions.handed_away(ap):
            continue
        reset = time.strftime("%H:%M", time.localtime(offer.limited_until)) if offer.limited_until else "unknown"
        items.append(InboxItemDTO(kind=KIND_BACK,
                                  reason=f"Switched to {offer.to_provider} · {offer.from_provider} was out of quota, back at {reset}",
                                  detail=f"{NOTICE_PREFIX}{offer.from_provider} → {offer.to_provider}"
                                         + (f" · {reset}" if offer.limited_until else ""),
                                  agent_id=root.id, workspace_id=ws.id,
                                  root_id=root.id, request_id=offer.id,
                                  action=InboxActionDTO(method="POST",
                                      path=f"/v1/sessions/{root.id}/provider-switch")))
    for row in db.conn.execute("SELECT * FROM provider_switches WHERE state IN ('open', 'failed') ORDER BY created_at, id"):
        offer = Offer(**dict(row))
        root = db.get_agent(offer.root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        if (root is None or ws is None or (repo_root is not None and ws.repo_root != repo_root)
                or identity_lock.locked_by(root.id)):
            continue
        if offer.state != "failed" and offer.kind == "switch" and (root.provider != offer.from_provider
                                       or out_of_quota(db, root.id, now) is None):
            continue
        if offer.state != "failed" and offer.to_provider not in checked.get(
                (offer.from_provider, ws.repo_root, offer.kind == "back"), ()):
            continue
        detail = f"{offer.from_provider} → {offer.to_provider}"
        items.append(InboxItemDTO(kind=KIND_BACK if offer.kind == "back" else KIND_OFFER,
                                  reason=("Provider switch failed. Check the session before continuing."
                                          if offer.state == "failed" else "Switch provider or choose Not now"),
                                  detail=detail,
                                  agent_id=root.id, workspace_id=ws.id, root_id=root.id,
                                  request_id=offer.id,
                                  action=InboxActionDTO(method="POST",
                                      path=f"/v1/sessions/{root.id}/provider-switch")))
    return items
