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

DESIGN STUB: signatures and data layout only; see docs/provider-switch-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from brindle.db import DB, Agent
from brindle.service.dto import InboxItemDTO

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
    raise NotImplementedError


def usable(provider: str, repo_root: str | None) -> bool:
    """Installed, turned on and signed in: ``preflight.check`` has nothing
    that would stop a launch on ``provider`` in ``repo_root``."""
    raise NotImplementedError


def pick_target(current: str, repo_root: str | None, now: float) -> str | None:
    """The provider to move a supervisor on ``current`` to: usable, in quota,
    most headroom first (ties in SUPERVISOR_PROVIDERS order). None when no
    other provider qualifies."""
    raise NotImplementedError


def out_of_quota(db: DB, root_id: str, now: float) -> float | None:
    """When the supervisor ``root_id``'s provider is available again, or
    None while it is in quota, or when ``root_id`` isn't an active session
    root (a worker, or a paused or done session)."""
    raise NotImplementedError


# -- offers ----------------------------------------------------------------------


def get(db: DB, offer_id: str) -> Offer | None:
    raise NotImplementedError


def open_offer(db: DB, root_id: str) -> Offer | None:
    """The session's offer that is open or switching, if any."""
    raise NotImplementedError


def maybe_offer(db: DB, root_id: str, now: float) -> Offer | None:
    """Offer to switch ``root_id`` when its provider is out of quota and
    ``pick_target`` finds another. Idempotent: an offer already open for the
    session is returned as is. None (and no row) when there is nothing to offer."""
    raise NotImplementedError


def maybe_offer_back(db: DB, now: float) -> list[Offer]:
    """For each done switch whose ``from_provider`` is in quota again and
    whose new supervisor is still the session's, a back offer (once: an
    open or dismissed back offer for that switch is not repeated)."""
    raise NotImplementedError


def dismiss(db: DB, offer_id: str, now: float) -> Offer:
    """Not now: the offer closes and isn't made again for this limit."""
    raise NotImplementedError


def sweep(db: DB, now: float) -> list[Offer]:
    """The daemon's periodic pass (alongside its other sweeps): offers for
    every live supervisor out of quota, then back offers. New offers made."""
    raise NotImplementedError


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


def inbox_items(db: DB, repo_root: str | None) -> list[InboxItemDTO]:
    """Open offers as Inbox cards: KIND_OFFER or KIND_BACK, ``request_id``
    the offer id, ``detail`` "<from> -> <to>", ``action`` POST
    /v1/sessions/{root}/provider-switch. Added to ``service.inbox.items``
    once implemented."""
    raise NotImplementedError
