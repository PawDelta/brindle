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

The reads go through :class:`Providers`: signed account policies, managed
device policies and org plan rules share the same context.
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
    * ``status()``: the verified signed status for enforced accounts, otherwise
      the legacy saved status, as ``brindle.pro.status.Saved``; on a managed
      device, combined with the device's own verified status (the stricter side).
    * ``managed()``: the managed-device binding (brindle.pro.managed), None
      when the device isn't managed. Default: ``managed.load()``.
    * ``plan_rules()``: the org's plan rules for ``plan_policy.needs_plan``
      (``OrgPlanRules`` or a policy dict), None for none. Default: the policy's rules.
    """
    policy: Callable[[], object] | None = None
    status: Callable[[], object] | None = None
    managed: Callable[[], object] | None = None
    plan_rules: Callable[[], object] | None = None


def _once(read: Callable[[], object]) -> tuple[object, Exception | None]:
    try:
        return read(), None
    except Exception as e:  # noqa: BLE001 - kept, and raised again on each use
        return None, e


class OrgContext:
    """The org's state for one check. Build it with :meth:`resolve`."""

    def __init__(self, *, repo_root: str | None, home: Path, home_given: bool, now: float,
                 providers: Providers, cached_only: bool = False) -> None:
        self.repo_root = repo_root
        self.home = home
        self.now = now
        self.providers = providers
        self.cached_only = cached_only
        self._home_given = home_given
        self._offline = False
        self._cached: dict[str, object] = {}
        self._license_reads: dict = {}
        self._verified: dict = {}
        self._policy_sources: list = []
        self._source: str | None = None
        self._unavailable: str | None = None
        self._client = self._store = None

    @classmethod
    def resolve(cls, *, home=None, now: float | None = None, providers: Providers | None = None,
                repo_root: str | None = None, cached_only: bool = False) -> OrgContext:
        """The org context for ``repo_root`` (the checkout whose policy
        applies; None: the first request :meth:`org_decision` is asked
        about). ``home`` is brindle's home the default reads use (the
        entitlement, the status, the cached policy; ``brindle_home()`` when
        None). ``now`` is the time the check is made at (the current time
        when None). Each provider is called the first time an answer needs
        it, and never again for this context. ``cached_only`` reads verified
        local state without network requests, for audit recording."""
        import time

        from brindle.pro._files import pro_home

        return cls(repo_root=repo_root, home=Path(home) if home is not None else pro_home(),
                   home_given=home is not None, now=time.time() if now is None else now,
                   providers=providers or Providers(), cached_only=cached_only)

    @contextmanager
    def _reads(self) -> Iterator[None]:
        """Read at this context's home, every entitlement lookup checked at
        this context's time and answered from one read (license.reading with
        this context's memo)."""
        from brindle.pro import license
        from brindle.pro._files import at_home

        with (at_home(self.home) if self._home_given else nullcontext(), license.reading(self._license_reads),
              license.at_time(self.now)):
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
            return _once((lambda: license.current(refresh=False)) if self.cached_only else license.current)

    @property
    def entitlement(self):
        """The verified entitlement, or None (none, or it couldn't be checked)."""
        return self._entitlement[0]

    def entitled(self, feature: str) -> bool:
        """Whether account or device governance includes ``feature``."""
        from brindle.pro import license, managed

        if feature in ("team", "org_budgets", "managed_models", "managed_rollout", "audit") \
                and isinstance(self.managed, managed.Managed):
            return True
        if self.cached_only:
            return self.entitlement is not None and feature in self.entitlement.features
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
        with self._reads():
            return _once(self.providers.status or self._default_status)

    def _default_status(self):
        """The member's status combined with the managed device's own, so a
        pause or control from either org applies, signed in or not."""
        from brindle.pro import status

        saved = self._member_status()
        device = self._device_status
        if device is None:
            return saved
        return replace(saved, status=status.stricter(saved.status, device, self.now))

    @cached_property
    def _device_status(self):
        """The managed device's verified org status, got with its device token
        (``GET /managed/{org_id}/status``) or else its last verified copy, which
        is kept; None off a managed device or when neither can be had."""
        import json

        from brindle.pro import managed, status
        from brindle.pro._files import private_dir, read_private, write_private

        device = self.managed
        if not isinstance(device, managed.Managed):
            return None
        path = private_dir(home=self.home) / "device-status.json"
        try:
            raw = read_private(path, status.MAX_FILE)
            cached = json.loads(raw) if raw else None
        except (OSError, ValueError):
            cached = None
        answer = managed.device_status(device, fetch=lambda d: self._fetch("status", d.org_id, d),
                                       cached=cached, now=self.now)
        if answer is None:
            return None
        if answer is not cached:
            write_private(path, json.dumps(answer).encode())
        return status.parse({k: v for k, v in answer.items() if k != "token"}, self.now)

    def _member_status(self):
        from brindle import airgap
        from brindle.pro import managed, policy_cache, status

        ent = self.__dict__.get("_policy_entitlement", self.entitlement)
        if airgap.enabled() and managed.airgap_allowed(self.managed):
            return status.Saved()
        if ent is not None and (policy_cache.enforced(ent) or isinstance(self.managed, managed.Managed)):
            copy = self._signed("status", ent)
            if isinstance(copy, policy_cache.Unavailable):
                self._unavailable = copy.reason
                return status.Saved(org_id=ent.org_id)
            return status.Saved(org_id=ent.org_id, status=status.parse(copy.body, self.now))
        if self.managed is not None:
            return status.Saved()
        return status._load_saved() if self.cached_only else status.load()

    def _signed(self, kind, ent):
        from brindle.pro import policy_cache, signed

        binding = policy_cache.Binding(ent.sub, ent.org_id, ent.role, ent.policy_role)
        key = (kind, binding)
        if key not in self._verified:
            try:
                self._verified[key] = policy_cache.get_verified(
                    kind, binding=binding, fetch=lambda k: self._fetch(k, ent.org_id),
                    home=self.home, now=self.now)
            except (OSError, signed.SignedError) as e:
                self._verified[key] = policy_cache.Unavailable(f"The signed {kind} cache can't be used: {e}")
        return self._verified[key]

    def _fetch(self, kind, org_id, device=None):
        from brindle import airgap
        from brindle.pro import auth, credentials, managed, team_policy

        if self.cached_only:
            raise team_policy.PolicyUnavailable("This context reads only locally stored governance documents")
        store = self._store or credentials.default_store()
        client = self._client or auth.Client((store.load() or {}).get("base_url"))
        airgap.guard(client.base, "org governance request")
        if device is not None:
            path, headers = managed.device_request(device, kind)
            code, body = client.call("GET", path, None, headers=headers)
            if code in (401, 403):
                raise managed.DeviceTokenRefused(f"HTTP {code}")
        else:
            code, body = auth.authed(client, store, "GET", f"/orgs/{org_id}/{kind}")
        if code != 200:
            raise team_policy.PolicyUnavailable(f"HTTP {code}")
        return body

    @property
    def status(self):
        """The org status (``brindle.pro.status.Saved``); raises what reading it raised."""
        saved, error = self._status
        if error is not None:
            raise error
        return saved

    @cached_property
    def managed(self):
        from brindle.pro import managed

        return self.providers.managed() if self.providers.managed else managed.load(now=self.now)

    @cached_property
    def plan_rules(self):
        from brindle.plan_policy import OrgPlanRules, OrgRulesUnavailable

        if self.providers.plan_rules:
            rules = self.providers.plan_rules()
            return rules if isinstance(rules, OrgPlanRules) or rules is None else OrgPlanRules.from_policy(rules)
        if why := self.blocked_reason():
            raise OrgRulesUnavailable(why)
        p = self._enforced()
        return OrgPlanRules.from_policy(p) if p is not None else None

    def _enforced(self):
        """The ``OrgPolicy`` in force, or None (no team org, a denial, unreadable)."""
        from brindle.policy import Decision

        p = self.policy
        return None if p is None or isinstance(p, Decision) else p

    # -- the default policy read ---------------------------------------------------------------------

    def _team_entitlement(self, store=None, client=None, entitlement=None):
        from brindle.pro import license, policy_cache, team_policy

        if entitlement or store or client:
            with self._reads():
                ent, error = _once(entitlement or (lambda: license.current(store=store, client=client)))
        else:
            ent, error = self._entitlement
        if isinstance(error, license.LicenseError):
            return None
        if error is not None:
            log.warning("brindle Pro: couldn't check the entitlement; no team policy applies",
                        exc_info=error)
            return None
        return ent if ent is not None and (team_policy.FEATURE in ent.features or policy_cache.enforced(ent)) else None

    def read_policy(self, repo_root: str | None, *, store=None, client=None, entitlement=None):
        """``ProPolicy.policy``: the member's effective policy narrowed by a
        throttle in force, ``allow()`` with no team entitlement, or a denial
        when a policy is required but none can be had (:func:`unavailable`).
        ``store``, ``client`` and ``entitlement`` are the plugin's own, if it
        was given any; otherwise this context's entitlement is used."""
        from brindle import airgap
        from brindle.policy import Decision, allow, deny
        from brindle.pro import managed, policy_cache, team_policy

        self._client, self._store = client, store
        if isinstance(self.managed, managed.Invalid):
            self._unavailable = self.managed.reason
            return deny(self._unavailable)

        ent = self._team_entitlement(store, client, entitlement)
        self._policy_entitlement = ent
        device = self._device_policy
        if isinstance(device, Decision):
            return device
        if device is not None and (ent is None or ent.org_id == device.org_id):
            return self.narrow(device.enforced)
        if ent is None:
            return allow()
        try:
            with self._reads():
                if airgap.enabled() and (device is None or managed.airgap_allowed(self.managed)):
                    p = team_policy.with_role_overrides(team_policy.load_offline(repo_root, ent.org_id),
                                                        ent.role, ent.policy_role)
                elif policy_cache.enforced(ent) or device is not None:
                    copy = self._signed("policy", ent)
                    if isinstance(copy, policy_cache.Unavailable):
                        self._unavailable = copy.reason
                        return deny(copy.reason)
                    p = team_policy.with_role_overrides(
                        team_policy.parse_policy(ent.org_id, copy.body, self.now), ent.role, ent.policy_role)
                elif self.cached_only:
                    p = self.cached_policy(ent.org_id)
                    if p is None or p.cached_for != (ent.role, ent.policy_role):
                        raise team_policy.PolicyUnavailable("No policy is saved for this member's role")
                else:
                    p = team_policy.current_policy(ent, client, store, repo_root, cached=self.cached_policy)
        except team_policy.PolicyUnavailable as e:
            return unavailable(ent.org_id, e)
        if not airgap.enabled():
            self._cached[ent.org_id] = p      # what the cache now holds
        self._policy_sources.append(p)
        if device is not None:
            p = managed.stricter(device, p, now=self.now)
        # A throttle from the org's remote control narrows the member's own policy.
        return self.narrow(p.enforced)

    @cached_property
    def _device_policy(self):
        import json

        from brindle import airgap
        from brindle.policy import deny
        from brindle.pro import managed, team_policy
        from brindle.pro._files import private_dir, read_private, write_private

        device = self.managed
        if not isinstance(device, managed.Managed):
            return None
        if airgap.enabled() and managed.airgap_allowed(device):
            try:
                p = team_policy.load_offline(self.repo_root, device.org_id)
            except team_policy.PolicyUnavailable as e:
                self._unavailable = str(e)
                return deny(str(e))
            self._source = "offline"
        else:
            path = private_dir(home=self.home) / "device-policy.json"
            try:
                raw = read_private(path, team_policy.MAX_CACHE)
                cached = json.loads(raw) if raw else None
            except (OSError, ValueError):
                cached = None
            p = managed.device_policy(device, fetch=lambda d: self._fetch("policy", d.org_id, d),
                                      cached=cached, now=self.now)
            if p is None:
                self._unavailable = ("This managed device has no valid signed policy. Connect to the network "
                                     "to fetch one with its device token, or ask your admin for a new device file.")
                return deny(self._unavailable)
            self._source = "snapshot" if device.snapshot and p.raw_body == device.snapshot.raw_body else "device"
            write_private(path, json.dumps(p.raw_body).encode())
        self._policy_sources.append(p)
        return p

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
        if self._unavailable:
            return Decision("deny", self._unavailable)
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
        """The signed policies' sinks and the managed file's sink, once each."""
        from brindle.pro import managed

        p = self.policy
        sinks = []
        sources = self._policy_sources or ([p] if self._enforced() is not None else [])
        for p in sources:
            body = p.raw_body or {}
            body = body.get("body", body)
            policy = body.get("policy", body)
            sink = policy.get("audit_sink")
            if isinstance(sink, dict) and sink not in sinks:
                sinks.append(sink)
        if isinstance(self.managed, managed.Managed) and self.managed.audit_sink not in (None, *sinks):
            sinks.append(self.managed.audit_sink)
        return sinks

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
            if self._verified or self.managed is not None:
                return None
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
        from brindle.policy import Decision
        from brindle.pro import status

        p = self._enforced() if policy is _RESOLVED else policy
        if self._unavailable:
            return self._unavailable
        if policy is _RESOLVED:
            raw, error = self._policy
            if error is not None:
                return str(error)
            if isinstance(raw, Decision) and not raw.allowed:
                return raw.reason
        try:
            if self._status[1] is not None:
                raise self._status[1]
            if p is not None and getattr(p, "paused", False):
                return paused_message(getattr(p, "paused_reason", None))
            c = self.shutdown_control()
            if c:
                return status.shutdown_reason(c)
            why = self.pause_reason()
            return self._unavailable or why
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
        """Where the org policy in force came from: the managed binding's
        ``source`` on a managed device ("device" for a copy got with the
        device token, "snapshot" for the managed file's own), else "account";
        "offline" for air-gap mode's local policy. None without an org policy."""
        p = self._enforced()
        if p is None:
            from brindle.pro import managed, policy_cache

            if isinstance(self.managed, (managed.Managed, managed.Invalid)):
                return self._source or "device"
            return "account" if policy_cache.enforced(self.entitlement) else None
        if self._source:
            return self._source
        if isinstance(self.managed, dict) and self.managed.get("source"):
            return self.managed["source"]
        return "offline" if self._offline else "account"

    @property
    def org_id(self) -> str | None:
        return getattr(self.managed, "org_id", None) or getattr(self.entitlement, "org_id", None)

    @property
    def org_name(self) -> str | None:
        p = self._enforced()
        grants = p.permission_grants if p is not None else None
        return (grants.org_name if grants else None) or self.org_id

    @property
    def verified(self) -> bool:
        """Whether the policy in force came from a verified signed document."""
        from brindle.pro import policy_cache

        if self._enforced() is None:
            return False
        return (self._source in ("device", "snapshot") or any(
            isinstance(copy, policy_cache.VerifiedCopy) and copy.kind == "policy"
            for copy in self._verified.values()) or any(
            "token" in (p.raw_body or {}) for p in self._policy_sources))


def _tighter(a, b):
    return b if a is None else a if b is None else min(a, b)


# -- the fail-closed rules ---------------------------------------------------------------------------


def unavailable(org_id: str, error) -> object:
    """The denial when a team policy is required for ``org_id`` but none can
    be had (``error`` says why): nothing may start until one is."""
    from brindle import airgap
    from brindle.policy import deny

    if airgap.enabled():
        return deny(f"air-gap mode: no usable offline policy for org {org_id} "
                    f"({error}); put the org's policy (the schema `brindle account org policy` "
                    f"shows, as JSON) at .brindle/{airgap.POLICY_FILE}")
    return deny(f"brindle Pro team policy for org {org_id} has never been fetched "
                f"({error}); connect to the network and run `brindle account org policy`")


def paused_message(why: str | None) -> str:
    return f"paused by your org{': ' + why if why else ''} (ask an org admin to resume you)"


__all__ = ["OrgContext", "Providers", "active", "paused_message", "unavailable"]
