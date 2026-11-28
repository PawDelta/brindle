"""Who may release a plan's hold: the approval sources.

While a session's plan is unapproved its own work stays queued
(brindle.plans.holding), whether or not anyone could approve it right now: an
app that is closed, restarting or not answering holds the work too. Only an
approval releases it, and this module says whose approval counts. The hold
logic never asks which source approved; brindle.plans.decide asks
:func:`may_release` before it lets a verdict through.

Today the one source is the person in the app, through the daemon's guarded
route (``APP``). A remote or cloud approver would be one more entry in
``SOURCES`` that vouches for its own kind of release, with no change to the
hold itself."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

APP = "app"     # the person, in the app, through the daemon (which refuses agents' processes)


@dataclass(frozen=True)
class Release:
    """A verdict asking to release plan ``plan_id``'s hold, and the channel it came ``via``
    (``APP``, or None for anyone no source vouches for, such as an agent)."""
    plan_id: str
    via: str | None


class ApprovalSource(Protocol):
    name: str

    def vouches(self, release: Release) -> bool:
        """Whether this source stands behind ``release``."""
        ...


class PersonInApp:
    name = APP

    def vouches(self, release: Release) -> bool:
        return release.via == APP


SOURCES: tuple[ApprovalSource, ...] = (PersonInApp(),)


def may_release(release: Release) -> bool:
    """Whether any approval source vouches for ``release``."""
    return any(source.vouches(release) for source in SOURCES)
