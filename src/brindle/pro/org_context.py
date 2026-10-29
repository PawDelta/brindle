"""One org context per check: the entitlement, the Team org's policy, its
status (pause and remote controls), the cached policy and the managed
binding, each read at most once and asked from one place.

``OrgContext.resolve()`` builds it; callers then ask it what they need:

* :meth:`OrgContext.limits` / :meth:`OrgContext.org_decision`: what bounds a
  saved approval and what the org's controls Block or Ask
  (brindle.permission_grants);
* :meth:`OrgContext.blocked_reason`: why no new worker may start (a pause or a
  remote shutdown, brindle.pro.status), :meth:`OrgContext.narrow` (a throttle);
* :meth:`OrgContext.needs_plan`: whether a task must get its plan approved
  (brindle.plan_policy), with the org's plan rules;
* :meth:`OrgContext.entitled`, :meth:`OrgContext.audit_sinks` and
  :meth:`OrgContext.source`.

Every rule that fails closed when the org's state can't be had lives here: a
policy that is required but can't be read (:func:`unavailable`), a policy
that can't be read at all (no saved approval applies), a member the org
paused or shut down (:meth:`OrgContext.blocked_reason`).

The reads go through :class:`Providers`, so later sources (a signed policy
cache, managed devices, org plan rules) plug in without touching callers.
While a context reads its policy it is :func:`active`, so
``ProPolicy.policy`` (the default policy read) reuses its entitlement,
status and cached policy instead of reading them again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path

log = logging.getLogger(__name__)

_active: ContextVar[OrgContext | None] = ContextVar("brindle_org_context", default=None)
_RESOLVED = object()      # blocked_reason's default: the context's own policy


def active() -> OrgContext | None:
    """The org context whose reads are under way in this thread or task, if any."""
    return _active.get()


@dataclass(frozen=True)
class Providers:
    """Where an :class:`OrgContext` reads the org's state. Each is called with
    no arguments, at most once per context; None means today's read:

    * ``policy()``: the policy to enforce, as ``ProPolicy.policy`` returns it
      (an ``OrgPolicy``, ``allow()`` without a team org, or a denial). May
      raise: the context then fails closed. Default:
      ``team_policy.ProPolicy(repo_root).policy()``, which reads through this
      context.
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


def _once(read: Callable[[], object]) -> tuple[object, Exception | None]:
    try:
        return read(), None
    except Exception as e:  # noqa: BLE001 - kept, and raised again on each use
        return None, e


class OrgContext:
    """The org's state for one check. Build it with :meth:`resolve`."""

    def __init__(self, *, repo_root: str | None, home: Path, home_given: bool, now: float,
                 providers: Providers) -> None:
        self.repo_root = repo_root
        self.home = home
        self.now = now
        self.providers = providers
        self._home_given = home_given
        self._offline = False
        self._cached: dict[str, object] = {}
        self._license_reads: dict = {}

    @classmethod
    def resolve(cls, *, home=None, now: float | None = None, providers: Providers | None = None,
                repo_root: str | None = None) -> OrgContext:
        """The org context for ``repo_root`` (the checkout whose policy
        applies; None: the first request :meth:`org_decision` is asked
        about). ``home`` is brindle's home the default reads use (the
        entitlement, the status, the cached policy; ``brindle_home()`` when
        None). ``now`` is the time the check is made at (the current time
        when None). Each provider is called the first time an answer needs
        it, and never again for this context."""
        import time

        from brindle.pro._files import pro_home

        return cls(repo_root=repo_root, home=Path(home) if home is not None else pro_home(),
                   home_given=home is not None, now=time.time() if now is None else now,
                   providers=providers or Providers())

    @contextmanager
    def _reads(self) -> Iterator[None]:
        """Read at this context's home, every entitlement lookup answered from
        one read (license.reading with this context's memo)."""
        from brindle.pro import license
        from brindle.pro._files import at_home

        with at_home(self.home) if self._home_given else nullcontext(), license.reading(self._license_reads):
            yield

    @contextmanager
    def activated(self) -> Iterator[OrgContext]:
        """Make this the :func:`active` context (with its reads) for the code inside."""
        token = _active.set(self)
        try:
            with self._reads():
                yield self
        finally:
            _active.reset(token)

    # -- the reads, once each ------------------------------------------------------------------------

    @cached_property
    def _entitlement(self):
        from brindle.pro import license

        with self._reads():
            return _once(license.current)

    @property
    def entitlement(self):
        """The verified entitlement, or None (none, or it couldn't be checked)."""
        return self._entitlement[0]

    def entitled(self, feature: str) -> bool:
        """Whether the entitlement includes ``feature`` (``license.has``: fails closed)."""
        from brindle.pro import license

        with self._reads():
            return license.has(feature)

    def cached_policy(self, org_id: str):
        """The cached copy of ``org_id``'s policy (``team_policy.load_cached``), or None."""
        if org_id not in self._cached:
            from brindle.pro import team_policy

            with self._reads():
                self._cached[org_id] = team_policy.load_cached(org_id)
        return self._cached[org_id]

    @cached_property
    def _policy(self):
        """``(policy, error)``: what the policy provider gave, or the error it raised."""
        from brindle import airgap

        self._offline = airgap.enabled()
        read = self.providers.policy or self._default_policy
        with self.activated():
            return _once(read)

    def _default_policy(self):
        from brindle.pro import team_policy

        return team_policy.ProPolicy(self.repo_root or "").policy()

    @property
    def policy(self):
        """The policy to enforce (an ``OrgPolicy``), ``allow()`` without a
        team org, a denial, or None when it couldn't be read."""
        return self._policy[0]

    @cached_property
    def _status(self):
        from brindle.pro import status

        with self._reads():
            return _once(self.providers.status or status.load)

    @property
    def status(self):
        """The org status (``brindle.pro.status.Saved``); raises what reading it raised."""
        saved, error = self._status
        if error is not None:
            raise error
        return saved

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

    # -- the default policy read ---------------------------------------------------------------------

    def _team_entitlement(self, store=None, client=None, entitlement=None):
        from brindle.pro import license, team_policy

        if entitlement or store or client:
            ent, error = _once(entitlement or (lambda: license.current(store=store, client=client)))
        else:
            ent, error = self._entitlement
        if isinstance(error, license.LicenseError):
            return None
        if error is not None:
            log.warning("brindle Pro: couldn't check the entitlement; no team policy applies",
                        exc_info=error)
            return None
        return ent if team_policy.FEATURE in ent.features else None

    def read_policy(self, repo_root: str | None, *, store=None, client=None, entitlement=None):
        """``ProPolicy.policy``: the member's effective policy narrowed by a
        throttle in force, ``allow()`` with no team entitlement, or a denial
        when a policy is required but none can be had (:func:`unavailable`).
        ``store``, ``client`` and ``entitlement`` are the plugin's own, if it
        was given any; otherwise this context's entitlement is used."""
        from brindle.policy import allow
        from brindle.pro import team_policy

        ent = self._team_entitlement(store, client, entitlement)
        if ent is None:
            return allow()
        try:
            with self._reads():
                p = team_policy.current_policy(ent, client, store, repo_root, cached=self.cached_policy)
        except team_policy.PolicyUnavailable as e:
            return unavailable(ent.org_id, e)
        from brindle import airgap

        if not airgap.enabled():
            self._cached[ent.org_id] = p      # what the cache now holds
        # A throttle from the org's remote control narrows the member's own policy.
        return self.narrow(p.enforced)

    # -- the questions -------------------------------------------------------------------------------

    def limits(self):
        """The Team org's limits on saved approvals (``permission_grants.OrgLimits``),
        or None without a Team org. A policy that can't be read, a member the
        org paused or shut down, or a policy without ``permission_grants``: no
        saved approval applies."""
        from brindle.permission_grants import OrgLimits, org_rule
        from brindle.policy import Decision

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
        gives them, with this context's org denies). A context resolved
        without a checkout reads its policy for the request's."""
        from brindle import permission_grants as grants
        from brindle.permissions import Decision

        if self.repo_root is None and "_policy" not in self.__dict__:
            self.repo_root = request.repo_root or None
        lim = self.limits()
        if lim is None or not lim.controls:
            return None
        if rules is None:
            rules = self._rules(lim, self.repo_root or request.repo_root or None)
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

    @staticmethod
    def _rules(lim, repo_root: str | None) -> list:
        """``permissions.all_rules`` for ``repo_root``, its org denies taken from ``lim``."""
        from brindle import permissions

        store = permissions.load_store()
        return [*lim.deny, *(permissions.repo_rules(repo_root) if repo_root else []),
                *store.rules, *store.blocking_rules]

    def needs_plan(self, task, *, config) -> bool:
        """``plan_policy.needs_plan`` for ``task``, with the org's plan rules."""
        from brindle import plan_policy

        return plan_policy.needs_plan(task, config=config, org_rules=self.plan_rules)

    def audit_sinks(self) -> list[dict]:
        """Where the org wants audit events sent: none yet."""
        return []

    def shutdown_control(self) -> dict | None:
        """A remote shutdown in force, for an org with ``managed_rollout``."""
        from brindle.pro import rollout, status

        c = status.control_in_force(self.status.status, self.now)
        return c if c and c["action"] == "shutdown" and self.entitled(rollout.FEATURE) else None

    def pause_reason(self) -> str | None:
        """Why the member is paused, from the org status or, when it doesn't
        say so, the cached effective policy (which follows the policy
        version); None when neither does."""
        saved = self.status
        why = saved.status.paused_reason
        if not saved.status.paused:
            try:
                cached = self.cached_policy(saved.org_id) if saved.org_id else None
            except Exception:  # noqa: BLE001
                cached = None
            if cached is None or not cached.enforced.paused:
                return None
            why = cached.enforced.paused_reason
        return paused_message(why)

    def blocked_reason(self, policy=_RESOLVED) -> str | None:
        """Why no new worker or reviewer may start: the member is paused (by
        ``policy``, default the policy in force here, or by the org status)
        or their org shut brindle down remotely. None when nothing blocks,
        and when the status file can't be read at all."""
        from brindle.pro import status

        p = self._enforced() if policy is _RESOLVED else policy
        try:
            if self._status[1] is not None:
                raise self._status[1]
            if p is not None and getattr(p, "paused", False):
                return paused_message(getattr(p, "paused_reason", None))
            c = self.shutdown_control()
            if c:
                return status.shutdown_reason(c)
            return self.pause_reason()
        except Exception:  # noqa: BLE001 - a broken status file must not stop all work
            log.warning("brindle: couldn't read the org status", exc_info=True)
            return None

    def narrow(self, p):
        """``p`` (an enforced ``OrgPolicy``) narrowed by a throttle control in
        force: cheaper models only (the intersection with the policy's), fewer
        parallel workers, a lower budget. Never widens anything."""
        from brindle.pro import rollout, status

        try:
            c = status.control_in_force(self.status.status, self.now)
            t = c.get("throttle") if c and c["action"] == "throttle" else None
            if not t or not self.entitled(rollout.FEATURE):
                return p
            models = p.allowed_models
            if t.get("allowed_models") is not None:
                models = (tuple(t["allowed_models"]) if models is None
                          else tuple(m for m in models if m in t["allowed_models"]))
            return replace(
                p, allowed_models=models,
                max_parallel_workers=_tighter(p.max_parallel_workers, t.get("max_parallel_workers")),
                budget_seat_month_usd=_tighter(p.budget_seat_month_usd, t.get("seat_month_usd")))
        except Exception:  # noqa: BLE001
            log.warning("brindle: couldn't apply the org throttle", exc_info=True)
            return p

    def source(self) -> str | None:
        """"account" when an org policy is in force and came from the
        account, else None (no team org, a denial, or air-gap mode's offline
        policy file)."""
        p = self._enforced()
        return "account" if p is not None and not self._offline else None


def _tighter(a, b):
    return b if a is None else a if b is None else min(a, b)


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


__all__ = ["OrgContext", "Providers", "active", "paused_message", "unavailable"]
