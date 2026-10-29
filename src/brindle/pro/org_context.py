"""One org context per check: the Team org's policy, its status (pause and
remote controls) and the managed binding, read at most once and asked from
one place.

``OrgContext.resolve()`` builds it; callers then ask it what they need:

* :meth:`OrgContext.limits` / :meth:`OrgContext.org_decision`: what bounds a
  saved approval and what the org's controls Block or Ask
  (brindle.permission_grants);
* :meth:`OrgContext.blocked_reason`: why no new worker may start (a pause or a
  remote shutdown, brindle.pro.status);
* :meth:`OrgContext.needs_plan`: whether a task must get its plan approved
  (brindle.plan_policy), with the org's plan rules;
* :meth:`OrgContext.audit_sinks` and :meth:`OrgContext.source`.

Every rule that fails closed when the org's state can't be had lives here: a
policy that is required but can't be read (:func:`unavailable`), a policy
that can't be read at all (no saved approval applies), a member the org
paused or shut down (:func:`blocked`, :func:`pause_reason`).

The reads go through :class:`Providers`, so later sources (a signed policy
cache, managed devices, org plan rules) plug in without touching callers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Providers:
    """Where an :class:`OrgContext` reads the org's state. Each is called with
    no arguments, at most once per context; None means today's read:

    * ``policy()``: the policy to enforce, as ``ProPolicy.policy`` returns it
      (an ``OrgPolicy``, ``allow()`` without a team org, or a denial). May
      raise: the context then fails closed. Default:
      ``team_policy.ProPolicy(repo_root).policy()``.
    * ``status()``: the org status as ``brindle.pro.status.load()`` returns it.
    * ``managed()``: the managed-device binding (brindle.pro.managed), None
      when the device isn't managed. Default: None.
    * ``plan_rules()``: the org's plan rules for ``plan_policy.needs_plan``
      (``{"paths": [...]}``), None for none. Default: None.
    """
    policy: Callable[[], object] | None = None
    status: Callable[[], object] | None = None
    managed: Callable[[], dict | None] | None = None
    plan_rules: Callable[[], dict | None] | None = None


def _default_policy(repo_root: str | None):
    from brindle.pro import team_policy

    return team_policy.ProPolicy(repo_root or "").policy()


def _default_status():
    from brindle.pro import status

    return status.load()


class OrgContext:
    """The org's state for one check. Build it with :meth:`resolve`."""

    def __init__(self, *, repo_root: str | None, home: Path, now: float, providers: Providers) -> None:
        self.repo_root = repo_root
        self.home = home
        self.now = now
        self.providers = providers
        self._offline = False

    @classmethod
    def resolve(cls, *, home=None, now: float | None = None, providers: Providers | None = None,
                repo_root: str | None = None) -> OrgContext:
        """The org context for ``repo_root`` (the checkout whose policy
        applies; None for none). ``home`` is brindle's home
        (``brindle_home()`` when None), ``now`` the time the check is made
        at (the current time when None). Each provider is called the first
        time an answer needs it, and never again for this context."""
        from brindle.config import brindle_home

        return cls(repo_root=repo_root, home=Path(home) if home is not None else brindle_home(),
                   now=time.time() if now is None else now, providers=providers or Providers())

    # -- the reads, once each ------------------------------------------------------------------------

    @cached_property
    def _policy(self):
        """``(policy, error)``: what the policy provider gave, or the error it raised."""
        from brindle import airgap

        self._offline = airgap.enabled()
        read = self.providers.policy or (lambda: _default_policy(self.repo_root))
        try:
            return read(), None
        except Exception as e:  # noqa: BLE001 - fail closed: see limits()
            return None, e

    @property
    def policy(self):
        """The policy to enforce (an ``OrgPolicy``), ``allow()`` without a
        team org, a denial, or None when it couldn't be read."""
        return self._policy[0]

    @cached_property
    def status(self):
        return (self.providers.status or _default_status)()

    @cached_property
    def managed(self) -> dict | None:
        return self.providers.managed() if self.providers.managed else None

    @cached_property
    def plan_rules(self) -> dict | None:
        return self.providers.plan_rules() if self.providers.plan_rules else None

    def _enforced(self):
        """The ``OrgPolicy`` in force, or None (no team org, a denial, unreadable)."""
        from brindle.policy import Decision

        p = self.policy
        return None if p is None or isinstance(p, Decision) else p

    # -- the questions -------------------------------------------------------------------------------

    def limits(self):
        """The Team org's limits on saved approvals here
        (``permission_grants.OrgLimits``), or None without a Team org (or
        without a checkout). A policy that can't be read, a member the org
        paused or shut down, or a policy without ``permission_grants``: no
        saved approval applies."""
        from brindle.permission_grants import OrgLimits, org_rule
        from brindle.policy import Decision

        if not self.repo_root:
            return None
        p, error = self._policy
        if error is not None:
            return OrgLimits(False, why=f"the org policy can't be read ({error})")
        if isinstance(p, Decision):
            return None if p.allowed else OrgLimits(False, why=p.reason)
        g = p.permission_grants
        deny = tuple(org_rule(s) for s in g.deny) if g else ()
        owner = f"org:{p.org_id}"
        controls, org_name = (tuple(g.controls), g.org_name) if g else ((), "")
        why = self.blocked_reason()
        if why:
            return OrgLimits(False, deny=deny, why=f"org {p.org_id}: {why}", owner=owner, controls=controls,
                             org_name=org_name)
        if g is None:
            return OrgLimits(False, why=f"org {p.org_id}'s policy sets no permission_grants", owner=owner)
        return OrgLimits(g.allow, tuple(g.kinds), g.max_minutes, tuple(g.lifetimes), tuple(g.scopes),
                         g.allow_persistent, deny, "" if g.allow else f"org {p.org_id} allows no saved approvals",
                         owner, controls, org_name)

    def org_decision(self, request, rules=None, checks: list[str] | None = None):
        """What the org's controls say about ``request`` (a
        ``permissions.Request``): the first Block that covers it, else the
        first Ask, as a ``permissions.Decision`` whose rule is the org's, or
        None (no Team org, no controls, or none covers it). ``rules``: the
        rules in force (default: every rule, as ``permissions.all_rules``
        gives them, with this context's org denies)."""
        from dataclasses import replace

        from brindle import permission_grants as grants
        from brindle.permissions import Decision

        lim = self.limits()
        if lim is None or not lim.controls:
            return None
        if rules is None:
            rules = self._rules(lim)
        asked = None
        for c in lim.controls:
            found = grants._control_matches(c, request, rules, checks)
            if found is None:
                continue
            rule = found.rule
            if found.decision == "deny":
                return Decision("deny", grants.org_reason(lim, c, "deny"), rule)
            if asked is None:
                asked = Decision("ask", grants.org_reason(lim, c, "ask"),
                                 replace(rule, decision="ask", id="", limits=grants.org_ask_limits(lim),
                                         person_only=True))
        return asked

    def _rules(self, lim) -> list:
        """``permissions.all_rules`` for this checkout, its org denies taken from ``lim``."""
        from brindle import permissions

        store = permissions.load_store()
        return [*lim.deny, *(permissions.repo_rules(self.repo_root) if self.repo_root else []),
                *store.rules, *store.blocking_rules]

    def needs_plan(self, task, *, config) -> bool:
        """``plan_policy.needs_plan`` for ``task``, with the org's plan rules."""
        from brindle import plan_policy

        return plan_policy.needs_plan(task, config=config, org_rules=self.plan_rules)

    def audit_sinks(self) -> list[dict]:
        """Where the org wants audit events sent: none yet."""
        return []

    def blocked_reason(self) -> str | None:
        """Why no new worker or reviewer may start (:func:`blocked`), with the
        policy in force and the org status, or None."""
        return blocked(self._enforced(), lambda: self.status, self.now)

    def source(self) -> str | None:
        """"account" when an org policy is in force and came from the
        account, else None (no team org, a denial, or air-gap mode's offline
        policy file)."""
        p = self._enforced()
        return "account" if p is not None and not self._offline else None


# -- the fail-closed rules ---------------------------------------------------------------------------


def unavailable(org_id: str, error) -> object:
    """The denial when a team policy is required for ``org_id`` but none can
    be had (``error`` says why): nothing may start until one is."""
    from brindle import airgap
    from brindle.policy import deny

    if airgap.enabled():
        return deny(f"air-gap mode: no usable offline team policy for org {org_id} "
                    f"({error}); put the org's policy (the schema `brindle account org policy` "
                    f"shows, as JSON) at .brindle/{airgap.POLICY_FILE}")
    return deny(f"brindle Pro team policy for org {org_id} has never been fetched "
                f"({error}); connect to the network and run `brindle account org policy`")


def paused_message(why: str | None) -> str:
    return f"paused by your org{': ' + why if why else ''} (ask an org admin to resume you)"


def pause_reason(saved) -> str | None:
    """Why the member is paused, from the org status ``saved`` or, when it
    doesn't say so, the cached effective policy (which follows the policy
    version); None when neither does."""
    why = saved.status.paused_reason
    if not saved.status.paused:
        try:
            from brindle.pro import team_policy

            cached = team_policy.load_cached(saved.org_id) if saved.org_id else None
        except Exception:  # noqa: BLE001
            cached = None
        if cached is None or not cached.enforced.paused:
            return None
        why = cached.enforced.paused_reason
    return paused_message(why)


def blocked(p, load_status: Callable[[], object], now: float | None = None) -> str | None:
    """Why no new worker or reviewer may start: the member is paused (by
    ``p``, the policy being enforced, or by the org status) or their org shut
    brindle down remotely. None when nothing blocks, and when the status
    file can't be read at all."""
    from brindle.pro import status

    try:
        saved = load_status()
        if p is not None and getattr(p, "paused", False):
            return paused_message(getattr(p, "paused_reason", None))
        c = status.shutdown_control(now, saved)
        if c:
            return status.shutdown_reason(c)
        return pause_reason(saved)
    except Exception:  # noqa: BLE001 - a broken status file must not stop all work
        log.warning("brindle: couldn't read the org status", exc_info=True)
        return None


__all__ = ["OrgContext", "Providers", "blocked", "pause_reason", "paused_message", "unavailable"]
