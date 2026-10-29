"""Managed devices (brindle Enterprise): a machine an org admin enrolled.

An admin issues a managed file (``POST /orgs/{id}/managed-devices``) and an
MDM installs it as ``managed.json`` in :func:`system_dir`. The device carries
its policy: the file is ``{"managed": <jws>, "policy_body": ..., "device_id"}``
where ``managed`` is a signed managed document (``brindle-managed+jwt``)
whose claims are ``org_id``, ``device_id``, a ``device_token``,
``allow_airgap``, an optional ``audit_sink`` and ``policy_token``: the signed
policy document covering ``policy_body``, bound to the device's ``org_id``
and ``sub`` (``device:<device_id>``). :func:`verify_file` checks all of that
as the server's ``verify_document`` does (the shared signing vectors
``managed_with_policy_*``); :func:`load` reads the installed file.

The device token is read-only and revocable. It authenticates exactly two
routes, ``GET /managed/{org_id}/policy`` and ``GET /managed/{org_id}/status``,
with ``Authorization: Bearer <device token>`` (:func:`device_request`); both
answer with the same signed shape as a member's policy and status, bound to
the device. Nothing here needs a person's sign-in and nothing here asks for
one: the device rules apply whoever is signed in, or nobody.

:func:`device_policy` picks the newest valid copy of the device's policy
among a fresh fetch (injected: no network is touched here), the cached copy
and the file's snapshot; a fetch the server refuses, for a revoked device
token or anything else, falls back to the stored copies. :func:`stricter`
merges the device policy with a signed-in member's org policy so every rule
takes the stricter side. :func:`airgap_allowed` and
:func:`local_policy_allowed` are the device's say over air-gap mode and the
offline policy file: an unsigned ``.brindle/policy.json`` is refused on a
managed device.

Device tokens are never logged or put in exception messages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from brindle.pro import signed
from brindle.pro.team_policy import (
    AgentSetup, GrantPolicy, OrgPolicy, PolicyUnavailable, parse_policy,
)

log = logging.getLogger(__name__)

FILE = "managed.json"
MAX_FILE_BYTES = 256 * 1024
DEVICE_SUB_PREFIX = "device:"
DEVICE_ROLE = "device"
POLICY_PATH = "/managed/{org_id}/policy"
STATUS_PATH = "/managed/{org_id}/status"
SINK_KINDS = ("syslog", "https")
_CLAIMS = ("org_id", "device_id", "device_token", "allow_airgap", "policy_token")


class ManagedError(Exception):
    """The managed file is not acceptable. The message never holds a token."""


class DeviceTokenRefused(Exception):
    """The server refused the device token (revoked, another org's, or the
    org's plan lapsed). Raised by an injected fetch; :func:`device_policy`
    falls back to the stored copies."""


@dataclass(frozen=True)
class Invalid:
    """A managed file is installed but can't be used: the device is managed,
    and ``reason`` says what's wrong with its file."""
    reason: str


@dataclass(frozen=True)
class Managed:
    """A verified managed file."""
    org_id: str
    device_id: str
    device_token: str = field(repr=False)
    allow_airgap: bool
    audit_sink: dict | None
    # The signed policy the file carries, parsed; its ``raw_body`` holds the
    # answer with its token, which :func:`device_policy` re-verifies at its own
    # clock. None when the snapshot's policy token has lapsed (see verify_file):
    # ``snapshot_expired`` says so, and the device must fetch a fresh copy.
    snapshot: OrgPolicy | None = field(repr=False)
    snapshot_expired: bool
    expires_at: int                   # the managed document's exp

    @property
    def sub(self) -> str:
        return DEVICE_SUB_PREFIX + self.device_id


# -- the installed file ----------------------------------------------------------------------------


def system_dir() -> Path:
    """Where an MDM installs the managed file: ``/Library/Application
    Support/brindle/`` on macOS, ``/etc/brindle/`` on Linux,
    ``%ProgramData%\\brindle\\`` on Windows."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/brindle")
    if os.name == "nt":
        return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "brindle"
    return Path("/etc/brindle")


def load(dir: str | os.PathLike | None = None, *, now: float | None = None) -> Managed | Invalid | None:
    """The managed file in ``dir`` (default :func:`system_dir`), verified at
    ``now``. None when there is none: the device is not managed. A present
    but unreadable, malformed, tampered or expired file gives
    :class:`Invalid` with the reason, never None: the device stays managed."""
    path = Path(dir) if dir is not None else system_dir()
    path = path / FILE
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as e:
        return Invalid(f"cannot read {path}: {e.strerror or e}")
    try:
        return verify_file(data, now=now)
    except (ManagedError, signed.SignedError, PolicyUnavailable) as e:
        return Invalid(f"{path}: {e}")


def verify_file(data: bytes, *, now: float | None = None) -> Managed:
    """The :class:`Managed` device a managed file describes. Raises
    :class:`ManagedError` for a malformed file and :class:`signed.SignedError`
    for a bad document: the managed document must verify (a pinned, unrevoked
    key; its ``exp``), and its ``policy_token`` must verify as a policy
    document over ``policy_body``, bound to the managed document's ``org_id``
    and ``sub``: the server's rule for a managed document, with its verdicts
    (a tampered ``policy_body`` is a claim mismatch). Both are checked at
    ``now``.

    One exception to that rule: a policy snapshot whose only fault is that
    its token has lapsed leaves the file valid, with ``snapshot`` None and
    ``snapshot_expired`` True, while the managed document itself is current:
    the file is installed once and lives longer than its snapshot, and the
    device fetches a fresh copy with its device token instead. Any other
    fault in the snapshot (signature, binding, digest) is the file's fault."""
    now = time.time() if now is None else now
    if len(data) > MAX_FILE_BYTES:
        raise ManagedError("managed file is too large")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ManagedError("managed file is not valid JSON") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("managed"), str):
        raise ManagedError("managed file has no managed document")
    body = doc.get("policy_body")
    if not isinstance(body, dict) or "token" in body:
        raise ManagedError("managed file has no policy_body")
    claims = signed.verify(doc["managed"], "managed", {}, now=now)
    missing = [c for c in _CLAIMS if c not in claims]
    if missing:
        raise signed.ClaimMismatch(f"managed document is missing claims: {', '.join(missing)}")
    org_id, device_id = claims["org_id"], claims["device_id"]
    if not (isinstance(org_id, str) and org_id and isinstance(device_id, str) and device_id
            and isinstance(claims["device_token"], str) and claims["device_token"]
            and isinstance(claims["allow_airgap"], bool)):
        raise signed.ClaimMismatch("managed document has malformed claims")
    if claims.get("sub") != DEVICE_SUB_PREFIX + device_id:
        raise signed.ClaimMismatch("managed document is bound to another device")
    if doc.get("device_id") not in (None, device_id):
        raise signed.ClaimMismatch("managed file names another device")
    sink = claims.get("audit_sink")
    if sink is not None and not (isinstance(sink, dict) and sink.get("kind") in SINK_KINDS
                                 and isinstance(sink.get("url"), str) and sink["url"]):
        raise signed.ClaimMismatch("managed document has a malformed audit_sink")
    sub = claims["sub"]
    token = claims["policy_token"]
    try:
        verify_policy_answer(token, body, org_id=org_id, sub=sub, now=now)
    except signed.Expired:
        # Sound in every other respect (signature, timing but for the elapsed
        # exp, binding, digest, and a body that parses as a policy): the file
        # stays valid without a usable snapshot. Any other fault is the file's.
        _verify_answer(token, "policy", body, org_id=org_id, sub=sub, now=now, lapsed_ok=True)
        parse_policy(org_id, {**body, "token": token}, now)
        snapshot, expired = None, True
    else:
        snapshot = parse_policy(org_id, {**body, "token": token}, float(claims["iat"]))
        expired = False
    return Managed(org_id=org_id, device_id=device_id, device_token=claims["device_token"],
                   allow_airgap=claims["allow_airgap"], audit_sink=dict(sink) if sink else None,
                   snapshot=snapshot, snapshot_expired=expired, expires_at=claims["exp"])


# -- signed answers --------------------------------------------------------------------------------


def document_sha256(body: dict) -> str:
    """The digest a signed answer's ``document_sha256`` claim covers: the
    canonical JSON of the answer without its ``token``, as the server
    computes it."""
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def verify_policy_answer(token: str, body: dict, *, org_id: str, sub: str,
                         now: float | None = None) -> dict:
    """The claims of ``token``, a signed policy document that covers ``body``
    (the answer without its token) and is bound to ``org_id`` and ``sub``.
    Raises :class:`signed.SignedError`; a body the token doesn't cover is a
    :class:`signed.ClaimMismatch`."""
    return _verify_answer(token, "policy", body, org_id=org_id, sub=sub, now=now)


def verify_status_answer(token: str, body: dict, *, org_id: str, sub: str,
                         now: float | None = None) -> dict:
    """As :func:`verify_policy_answer`, for ``GET /managed/{org_id}/status``."""
    return _verify_answer(token, "status", body, org_id=org_id, sub=sub, now=now)


def _verify_answer(token, doc_type: str, body: dict, *, org_id: str, sub: str, now,
                   lapsed_ok: bool = False) -> dict:
    expect = {"org_id": org_id, "sub": sub}
    if lapsed_ok:
        # Everything :func:`signed.verify` checks except that ``exp`` has
        # elapsed at ``now``: a token not yet valid is still refused.
        claims = signed.verify_signed(token, typ=signed.header_typ(doc_type), token_use=doc_type,
                                      what=doc_type)
        iat, exp = claims.get("iat"), claims.get("exp")
        if not (signed._is_int(iat) and signed._is_int(exp)):
            raise signed.ClaimMismatch(f"{doc_type} has no valid iat or exp")
        if iat > now + signed.LEEWAY:
            raise signed.Expired(f"{doc_type} is not valid yet")
        for name, value in expect.items():
            if claims.get(name) != value:
                raise signed.ClaimMismatch(f"{doc_type} claim {name!r} does not match")
    else:
        claims = signed.verify(token, doc_type, expect, now=now)
    if claims.get("doc_type") != doc_type:
        raise signed.ClaimMismatch(f"{doc_type} document does not say it covers a {doc_type}")
    covered = (document_sha256(claims["document"]) if "document" in claims
               else claims.get("document_sha256"))
    if covered != document_sha256(body):
        raise signed.ClaimMismatch(f"{doc_type} document does not cover this body")
    return claims


def device_request(managed: Managed, what: str = "policy") -> tuple[str, dict]:
    """The path and headers of the device's ``what`` ("policy" or "status")
    request, for whatever transport the caller injects."""
    path = {"policy": POLICY_PATH, "status": STATUS_PATH}[what]
    return path.format(org_id=managed.org_id), {"Authorization": f"Bearer {managed.device_token}"}


# -- the device policy -----------------------------------------------------------------------------


def _answer(copy) -> dict | None:
    """A stored or fetched copy as the signed answer dict it came from."""
    if isinstance(copy, OrgPolicy):
        copy = copy.raw_body
    return copy if isinstance(copy, dict) and isinstance(copy.get("token"), str) else None


def _valid_copy(managed: Managed, copy, now: float, what: str) -> tuple[tuple, OrgPolicy] | None:
    """``copy`` as ``(order key, policy)`` when it is a signed policy answer
    bound to this device that verifies at ``now``, else None."""
    answer = _answer(copy)
    if answer is None:
        return None
    body = {k: v for k, v in answer.items() if k != "token"}
    try:
        claims = verify_policy_answer(answer["token"], body, org_id=managed.org_id, sub=managed.sub,
                                      now=now)
        policy = parse_policy(managed.org_id, answer, body.get("fetched_at") or now)
    except (signed.SignedError, PolicyUnavailable) as e:
        log.info("ignoring the %s device policy (%s)", what, e)
        return None
    return (policy.version, claims["iat"]), policy


def device_policy(managed: Managed, *, fetch, cached=None, now: float | None = None) -> OrgPolicy | None:
    """The device's policy: the newest valid copy (highest version, then the
    most recently signed) among what ``fetch(managed)`` answers, ``cached``
    (the last answer kept, a dict or the :class:`OrgPolicy` parsed from it)
    and the file's snapshot. Each copy must be a signed policy answer bound
    to this device that verifies now; a fetch that raises, a refused device
    token (:class:`DeviceTokenRefused`) included, or answers something that
    doesn't verify leaves the stored copies to choose from. None when no
    copy is valid: the caller fails closed. The answer's ``raw_body`` is
    what to cache."""
    now = time.time() if now is None else now
    try:
        fetched = fetch(managed)
    except DeviceTokenRefused as e:
        log.warning("the server refused the device token (%s); using a stored policy", e)
        fetched = None
    except Exception as e:  # noqa: BLE001 - any failed fetch leaves the stored copies
        log.info("device policy fetch failed (%s); using a stored policy", e)
        fetched = None
    copies = [c for c in (_valid_copy(managed, fetched, now, "fetched"),
                          _valid_copy(managed, cached, now, "cached"),
                          _valid_copy(managed, managed.snapshot, now, "snapshot")) if c]
    if not copies:
        return None
    return max(copies, key=lambda c: c[0])[1]


# -- merging with a member's policy -----------------------------------------------------------------


def _both(a, b, pick):
    if a is None:
        return b
    if b is None:
        return a
    return pick(a, b)


def _intersect(a: tuple | None, b: tuple | None) -> tuple | None:
    """Allowed lists: None allows everything, so the other side rules."""
    return _both(a, b, lambda x, y: tuple(i for i in x if i in set(y)))


def _union(a: tuple, b: tuple) -> tuple:
    return tuple(dict.fromkeys((*a, *b)))


def _newest_version(a: str | None, b: str | None) -> str | None:
    from brindle.pro.rollout import version_tuple

    return _both(a, b, lambda x, y: x if (version_tuple(x) or ()) >= (version_tuple(y) or ()) else y)


# A side without a ``permission_grants`` block grants nothing and denies nothing.
_NO_GRANTS = GrantPolicy(allow=False, kinds=(), overridable=(), max_minutes=0, max_uses=0,
                         lifetimes=(), scopes=())


def _stricter_grants(a: GrantPolicy | None, b: GrantPolicy | None) -> GrantPolicy | None:
    """Each rule's stricter side: what both allow, and every deny and control
    of either. A side with no block grants nothing, so nothing is allowed,
    but the other side's denies and controls still stand; None only when
    nothing remains to restrict."""
    if a is None and b is None:
        return None
    one_sided = a is None or b is None
    a, b = a or _NO_GRANTS, b or _NO_GRANTS
    merged = GrantPolicy(allow=a.allow and b.allow, kinds=_intersect(a.kinds, b.kinds),
                         overridable=_intersect(a.overridable, b.overridable),
                         max_minutes=min(a.max_minutes, b.max_minutes), max_uses=min(a.max_uses, b.max_uses),
                         deny=_union(a.deny, b.deny), allow_persistent=a.allow_persistent and b.allow_persistent,
                         lifetimes=_intersect(a.lifetimes, b.lifetimes), scopes=_intersect(a.scopes, b.scopes),
                         controls=_union(a.controls, b.controls), org_name=a.org_name or b.org_name)
    if one_sided and not merged.deny and not merged.controls:
        return None
    return merged


def _denies_personal_keys(p: OrgPolicy) -> bool:
    """``p``'s effective personal-key rule, by managed_models' precedence:
    ``agent_setup`` when it sets one, else the policy's top-level field."""
    s = p.agent_setup
    return p.deny_personal_keys if s is None or s.deny_personal_keys is None else s.deny_personal_keys


def _stricter_setup(a: AgentSetup | None, b: AgentSetup | None, deny_personal_keys: bool) -> AgentSetup | None:
    """Two routings can't be combined: ``a``'s routes stand, with the
    stricter flags of both and the merged personal-key rule, which is read
    from here first."""
    if a is None and b is None:
        return None
    if a is None or b is None:
        return replace(a or b, deny_personal_keys=deny_personal_keys)
    return replace(a, enforce=a.enforce or b.enforce, deny_personal_keys=deny_personal_keys,
                   exclusive=_both(a.exclusive, b.exclusive, lambda x, y: x or y))


def _larger_spend(x: OrgPolicy, y: OrgPolicy, now: float) -> dict:
    """The seat spend to carry: the larger of the two sides' spends for the
    current UTC month, so a budget guard counts at least what either side
    has seen this month. A stale or undated amount never displaces a
    current one; only when neither side has a current one is the larger of
    them carried (the guard discards it as last month's)."""
    month = time.strftime("%Y-%m", time.gmtime(now))
    sides = [p for p in (x, y) if p.spend_seat_usd is not None]
    current = [p for p in sides if p.spend_month == month]
    sides = current or sides
    if not sides:
        return {"spend_seat_usd": None, "spend_month": None}
    top = max(sides, key=lambda p: p.spend_seat_usd)
    return {"spend_seat_usd": top.spend_seat_usd, "spend_month": top.spend_month}


_PLAN_ORDER = {"never": 0, "auto": 1, "always": 2}


def _stricter_plan(a: str, b: str) -> str:
    return max(a, b, key=lambda m: _PLAN_ORDER.get(m, 0))


def stricter(a: OrgPolicy, b: OrgPolicy, *, now: float | None = None) -> OrgPolicy:
    """One policy from two (each one's effective policy, overrides applied)
    where every rule takes the stricter side: allowed lists intersect (a
    missing list allows everything, so the other side's rules), budgets and
    the worker limit take the lower value, protected paths, plan paths,
    required items and permission denies and controls accumulate, a plan
    needs an approver from each side's list (``plan_approver_groups``),
    flags (human review, paused, kill switch, two-person plans, no personal
    keys by either side's effective rule) are on if either side has them,
    a reviewer may approve a low-risk plan only when both sides let them,
    planning is "always" over "auto" over "never", the version floor is the
    higher one, permission grants shrink to what both allow (a side with no
    grants block allows none, and the other side's denies still stand), and
    visibility is what both show. A managed provider or agent routing can't
    be combined, so ``a``'s stands (the device's, when ``a`` is the device
    policy). The seat spend carried is the larger of the two for the
    current month (at ``now``), so a budget guard never counts less than
    either side has seen. The result is its own effective policy and
    carries ``a``'s identity and sources."""
    now = time.time() if now is None else now
    x, y = a.enforced, b.enforced
    paused = x.paused or y.paused
    deny_keys = _denies_personal_keys(x) or _denies_personal_keys(y)
    merged = OrgPolicy(
        org_id=a.org_id, version=max(a.version, b.version), fetched_at=min(a.fetched_at, b.fetched_at),
        allowed_providers=_intersect(x.allowed_providers, y.allowed_providers),
        allowed_models=_intersect(x.allowed_models, y.allowed_models),
        allowed_profiles=_intersect(x.allowed_profiles, y.allowed_profiles),
        require_human_review=x.require_human_review or y.require_human_review,
        max_parallel_workers=_both(x.max_parallel_workers, y.max_parallel_workers, min),
        budget_seat_month_usd=_both(x.budget_seat_month_usd, y.budget_seat_month_usd, min),
        budget_goal_usd=_both(x.budget_goal_usd, y.budget_goal_usd, min),
        budget_task_usd=_both(x.budget_task_usd, y.budget_task_usd, min),
        protected_paths=_union(x.protected_paths, y.protected_paths),
        paused=paused,
        paused_reason=(x.paused_reason if x.paused else y.paused_reason) if paused else None,
        budget_sources=dict(x.budget_sources), member_overrides=dict(x.member_overrides),
        **_larger_spend(x, y, now),
        visibility={k: bool(x.visibility.get(k, True)) and bool(y.visibility.get(k, True))
                    for k in {*x.visibility, *y.visibility}},
        min_version=_newest_version(x.min_version, y.min_version),
        required_profiles=_union(x.required_profiles, y.required_profiles),
        required_rule_packs=_union(x.required_rule_packs, y.required_rule_packs),
        kill_switch=x.kill_switch or y.kill_switch,
        provider_config=x.provider_config or y.provider_config,
        deny_personal_keys=deny_keys,
        agent_setup=_stricter_setup(x.agent_setup, y.agent_setup, deny_keys),
        permission_grants=_stricter_grants(x.permission_grants, y.permission_grants),
        plan=_stricter_plan(x.plan, y.plan), plan_paths=_union(x.plan_paths, y.plan_paths),
        # Approval is enforced through the groups: one eligible person from
        # each org's list, so neither org widens the other's approvers.
        # ``plan_approvers`` stays ``a``'s list, for display only.
        plan_approvers=x.plan_approvers,
        plan_approver_groups=tuple(g for g in (*x.plan_approver_groups, *y.plan_approver_groups) if g),
        plan_two_person=x.plan_two_person or y.plan_two_person,
        plan_reviewer_may_approve_low_risk=(x.plan_reviewer_may_approve_low_risk
                                            and y.plan_reviewer_may_approve_low_risk),
        role=a.role, policy_role=a.policy_role)
    return merged


# -- the device's say over local switches ---------------------------------------------------------


def airgap_allowed(managed: Managed | Invalid | None) -> bool:
    """Whether this device may run in air-gap mode: an unmanaged device may;
    a managed one only when its file says ``allow_airgap`` (an unusable file
    says no)."""
    return managed is None or (isinstance(managed, Managed) and managed.allow_airgap)


def local_policy_allowed(managed: Managed | Invalid | None, signed: bool) -> bool:
    """Whether the offline policy file (``.brindle/policy.json``) may stand in
    for the org policy: on an unmanaged device always; on a managed device
    only a signed one (an offline-policy document the org exported), never
    one anyone could have written."""
    return managed is None or signed
