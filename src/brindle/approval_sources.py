"""Who may release a plan's hold: the approval sources.

While a session's plan is unapproved its own work stays queued
(brindle.plans.holding), whether or not anyone could approve it right now: an
app that is closed, restarting or not answering holds the work too. Only an
approval releases it, and this module says whose approval counts. The hold
logic never asks which source approved; brindle.plans.decide asks
:func:`may_release` before it lets a verdict through.

The caller that received a verdict over a trusted channel says so by handing
decide an :class:`Approval` naming its source: the daemon's guarded route
(which refuses agents' processes) hands it ``PERSON_IN_APP``, and an approval
it held for the Library settles with the source it was made with. A caller
with no such channel, such as an agent's tool, hands None, and nothing
vouches for it. A remote or cloud approver would be one more entry in
``SOURCES``, checking its own ``evidence`` (a signed token, say), with no
change to the hold itself."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

APP = "app"     # the person, in the app, through the daemon's guarded route


@dataclass(frozen=True)
class Approval:
    """A verdict's origin as the trusted caller that received it states it:
    the approval ``source`` and whatever that source checks (``evidence``)."""
    source: str
    evidence: Any = None


PERSON_IN_APP = Approval(APP)   # the app's route needs no evidence: the daemon's Guard already refused agents


@dataclass(frozen=True)
class Release:
    """A verdict asking to release plan ``plan_id``'s hold, with its ``approval`` (None: nobody vouches)."""
    plan_id: str
    approval: Approval | None


class ApprovalSource(Protocol):
    name: str

    def vouches(self, release: Release) -> bool:
        """Whether this source stands behind ``release``."""
        ...


class PersonInApp:
    name = APP

    def vouches(self, release: Release) -> bool:
        return release.approval is not None and release.approval.source == self.name


SOURCES: tuple[ApprovalSource, ...] = (PersonInApp(),)


def may_release(release: Release) -> bool:
    """Whether any approval source vouches for ``release``."""
    return any(source.vouches(release) for source in SOURCES)
