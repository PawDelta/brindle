"""Moving an out-of-quota supervisor to another provider, and back.

When a session's supervisor hits its provider's subscription limit
(brindle.quota: a ``limited_until`` in the future, or its fullest plan window
at 100%), brindle offers in the Inbox to carry the session on with another
provider that is installed, signed in and in quota. Switch starts a new
supervisor on that provider in the *same* workspace and branch, hands it the
session the way ``sessions.handover_to`` does (goal, milestones, workers,
queued tasks), and gives it a handoff note built from brindle's own records,
since the new CLI can't read the old one's conversation. Once the original
provider's limit resets, brindle offers to switch back the same way.

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

import time
import uuid
from dataclasses import dataclass, replace

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


def _targets(current: str, repo_root: str | None, now: float) -> list[str]:
    """Eligible targets, ordered by headroom with a stable provider tie break."""
    from brindle import agents, airgap, autopilot, policy
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
                or not gate.access.allows(provider) or limited_until(provider, now) is not None
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
    target = pick_target(root.provider, ws.repo_root, now)
    if target is None:
        return None
    offer_id = uuid.uuid4().hex
    with db.tx() as c:
        # Serialize the history check and insert: dismissals and failed starts
        # consume this limit, and concurrent detections share the active offer.
        c.execute("INSERT OR IGNORE INTO provider_switches "
                  "(id, root_id, kind, from_provider, to_provider, state, limited_until, created_at, updated_at) "
                  "SELECT ?, ?, 'switch', ?, ?, 'open', ?, ?, ? WHERE NOT EXISTS "
                  "(SELECT 1 FROM provider_switches WHERE root_id=? AND kind='switch' "
                  "AND from_provider=? AND limited_until=?)",
                  (offer_id, root_id, root.provider, target, until, now, now,
                   root_id, root.provider, until))
    return open_offer(db, root_id)


def maybe_offer_back(db: DB, now: float) -> list[Offer]:
    """For each done switch whose ``from_provider`` is in quota again and
    whose new supervisor is still the session's, a back offer (once: an
    open or dismissed back offer for that switch is not repeated)."""
    raise NotImplementedError


def dismiss(db: DB, offer_id: str, now: float) -> Offer:
    """Not now: the offer closes and isn't made again for this limit."""
    with db.tx() as c:
        c.execute("UPDATE provider_switches SET state='dismissed', updated_at=? "
                  "WHERE id=? AND state='open'", (now, offer_id))
    offer = get(db, offer_id)
    if offer is None:
        raise ValueError(f"no provider switch offer {offer_id}")
    return offer


def sweep(db: DB, now: float) -> list[Offer]:
    """New forward offers for active session roots. Switch-back detection
    is a separate pass (``maybe_offer_back``)."""
    offers = []
    for root in db.list_agents():
        if open_offer(db, root.id) is not None:
            continue
        if offer := maybe_offer(db, root.id, now):
            offers.append(offer)
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
    raise NotImplementedError


def switch(db: DB, offer_id: str, now: float) -> Agent:
    """Carry out a switch or back offer: claim it (open -> switching in one
    UPDATE), then ``sessions.handover_to`` with ``dest`` the old root's own
    workspace, ``provider=offer.to_provider`` and ``handoff_note`` as the
    note, then record ``new_root_id`` and state done. A claim that finds the
    row already switching or done returns that supervisor (waiting for an
    in-flight one) and starts nothing. A failed start marks it failed and
    reopens nothing: the person sees the failure on the card."""
    raise NotImplementedError


def switch_back(db: DB, offer_id: str, now: float) -> Agent:
    """``switch`` for a back offer (kind back): the session returns to the
    provider it started on, in the same workspace."""
    raise NotImplementedError


# -- inbox -----------------------------------------------------------------------


def inbox_items(db: DB, repo_root: str | None, *, now: float | None = None) -> list[InboxItemDTO]:
    """Open offers as Inbox cards: KIND_OFFER or KIND_BACK, ``request_id``
    the offer id, ``detail`` "<from> -> <to>", ``action`` POST
    /v1/sessions/{root}/provider-switch."""
    now = time.time() if now is None else now
    items = []
    for row in db.conn.execute("SELECT * FROM provider_switches WHERE state='open' ORDER BY created_at, id"):
        offer = Offer(**dict(row))
        root = db.get_agent(offer.root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        if (root is None or ws is None or (repo_root is not None and ws.repo_root != repo_root)
                or identity_lock.locked_by(root.id)):
            continue
        if offer.kind == "switch" and (root.provider != offer.from_provider
                                       or out_of_quota(db, root.id, now) is None):
            continue
        targets = _targets(offer.from_provider, ws.repo_root, now)
        if offer.to_provider not in targets:
            continue
        detail = f"{offer.from_provider} → {offer.to_provider}"
        alternatives = [p for p in targets if p != offer.to_provider]
        if alternatives:
            detail += f"; alternatives: {', '.join(alternatives)}"
        items.append(InboxItemDTO(kind=KIND_BACK if offer.kind == "back" else KIND_OFFER,
                                  reason="Switch provider or choose Not now",
                                  detail=detail,
                                  agent_id=root.id, workspace_id=ws.id, root_id=root.id,
                                  request_id=offer.id,
                                  action=InboxActionDTO(method="POST",
                                      path=f"/v1/sessions/{root.id}/provider-switch")))
    return items
