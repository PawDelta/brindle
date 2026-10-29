"""Offline verification of brindle Pro entitlements, and the helpers other
modules use to gate features (``features()``, ``has()``, ``require()``).

The backend has two token kinds signed by the same Ed25519 key: short-lived
access tokens (``typ: at+jwt``, opaque to this client, only sent as Bearer)
and entitlements from ``GET /entitlement`` (``typ: brindle-entitlement+jwt``,
``token_use: entitlement``), which are what this module verifies. The
header names a ``kid``, which must be one of the keys pinned in
:mod:`brindle.pro.keys`. Claims::

    iss, aud="brindle-pro", sub, org_id, plan, status, features[], seats,
    iat, exp, kid, jti, token_use="entitlement"

``iss`` must equal the base URL the entitlement was fetched from. The
signature, header, ``kid`` and revocation checks are
:func:`brindle.pro.signed.verify_signed`'s; every entitlement fetched from the
backend is preceded by a refresh of the revocation list
(:func:`brindle.pro.signed.refresh_revocations`, in ``auth``), and a failure
there never stops the fetch. A cached entitlement is checked against the
stored revocations (whichever process stored them) before it is returned.

Everything fails closed: any problem -- a malformed token, an unknown key, a
bad signature, the wrong ``alg``/``typ``/``aud``/``iss``/``token_use``, an
expired token outside its grace, unreadable credentials -- means no
entitlement and no features.

Offline grace: an expired entitlement is still honoured (flagged
``in_grace``) until ``exp + grace`` (default 1 hour), long enough to ride
out a brief outage; ``grace`` is clamped to ``MAX_GRACE`` (14 days) however it
is configured. The bound comes from a signed claim, so nothing a user can
edit on disk extends it.

Offline license (brindle Enterprise): ``brindle account license install <file>``
stores an entitlement issued for offline use (``$BRINDLE_HOME/pro/license.jwt``,
0600) after verifying it against the pinned keys; see :func:`install`. In
air-gap mode (:mod:`brindle.airgap`) :func:`current` uses it and never
refreshes anything; outside air-gap mode it is used when there is no login.
Its issuer isn't checked against a backend URL, only its signature, type,
audience and validity.

Only pinned keys are trusted, in every mode: nothing in the environment can
make the client trust a key it didn't ship with. To test against a local
backend, pin its key in ``keys.py`` in your own checkout and don't commit it.

Tokens are never logged or put in exception messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import signature

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from brindle.pro import signed
from brindle.pro.keys import PINNED_KEYS  # noqa: F401 - re-exported
from brindle.pro.signed import (  # noqa: F401 - re-exported
    ALG, AUDIENCE, MAX_TOKEN_BYTES, _b64decode, _is_int, _json_object, _test_keys,
    verify_signed,
)

log = logging.getLogger(__name__)

TYP = signed.header_typ("entitlement")
TOKEN_USE = "entitlement"
DEFAULT_GRACE = 3600
MAX_GRACE = 14 * 86400        # hard cap on grace past the signed exp
LOGIN_AGAIN = "log in to brindle Pro again: run `brindle account login`"
NO_SUBSCRIPTION = "no active brindle Pro subscription"
PAID_STATUSES = frozenset({"active", "trialing", "past_due"})
LEEWAY = signed.LEEWAY        # clock skew tolerated on exp / iat
REFRESH_WHEN_LEFT = 0.25      # refresh once less than this share of lifetime remains
MAX_AGE = 600                 # seconds: the sweep refetches an older entitlement (plan and policy changes)
_REQUIRED = ("iss", "aud", "sub", "org_id", "plan", "status", "features", "seats", "iat", "exp",
             "kid", "jti", "token_use")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


LicenseError = signed.SignedError    # no valid entitlement; the message never contains a token
Expired = signed.Expired             # an entitlement past its expiry and its offline grace


class NotEntitled(LicenseError):
    """A valid entitlement that doesn't include the requested feature."""


@dataclass(frozen=True)
class Entitlement:
    sub: str
    org_id: str
    plan: str
    status: str
    features: frozenset[str]
    seats: int
    iat: int
    exp: int
    kid: str
    in_grace: bool = False
    grace_until: int | None = None
    role: str | None = None          # the caller's role in org_id (owner|admin|member)
    policy_version: int = 0          # the org policy version current when issued
    policy_role: str | None = None   # the caller's policy role in org_id, when one is set
    policy_signed: bool = False      # org policy and status must be signed (policy_cache.enforced)


# -- keys (see brindle.pro.signed) ------------------------------------------------------------


def jwk_thumbprint(x: str) -> str:
    """RFC 7638 thumbprint of an Ed25519 OKP key (the backend's kid)."""
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x},
                           separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()


def _trusted_key(kid: str) -> Ed25519PublicKey:
    return signed._trusted_key(kid)


@contextmanager
def _test_signing_key(kid: str, public_key: Ed25519PublicKey):
    """Test-only: :func:`brindle.pro.signed._test_signing_key`, forgetting
    the cached entitlement afterwards."""
    try:
        with signed._test_signing_key(kid, public_key):
            yield
    finally:
        clear_cache()


# -- verification ----------------------------------------------------------------------


def verify(token: str, *, issuer: str | None, now: float | None = None,
           grace: float = DEFAULT_GRACE) -> Entitlement:
    """Verify ``token`` offline and return its entitlement, or raise
    :class:`LicenseError`. ``issuer`` is the base URL it must come from
    (``None`` skips that check); ``grace=0`` disables the offline grace."""
    now = time.time() if now is None else now
    claims = verify_signed(token, typ=TYP, token_use=TOKEN_USE)
    missing = [c for c in _REQUIRED if c not in claims]
    if missing:
        raise LicenseError(f"entitlement is missing claims: {', '.join(missing)}")
    kid = claims["kid"]
    if issuer is not None and claims["iss"] != issuer.rstrip("/"):
        raise LicenseError("entitlement is from a different issuer")
    feats = claims["features"]
    if not (all(isinstance(claims[c], str) and claims[c] for c in ("sub", "org_id", "plan", "status"))
            and isinstance(feats, list) and all(isinstance(f, str) for f in feats)
            and _is_int(claims["seats"]) and claims["seats"] >= 0
            and _is_int(claims["iat"]) and _is_int(claims["exp"])
            and claims["exp"] > claims["iat"]):
        raise LicenseError("entitlement claims are malformed")
    role, policy_version = claims.get("role"), claims.get("policy_version", 0)
    policy_role = claims.get("policy_role")
    if not ((role is None or (isinstance(role, str) and role)) and _is_int(policy_version)
            and policy_version >= 0
            and (policy_role is None or (isinstance(policy_role, str) and policy_role))):
        raise LicenseError("entitlement claims are malformed")
    iat, exp = claims["iat"], claims["exp"]
    if iat > now + LEEWAY:
        raise LicenseError("entitlement is not valid yet")
    in_grace, grace_until = False, None
    if now > exp + LEEWAY:
        grace = min(max(0.0, float(grace)), MAX_GRACE)
        grace_until = int(exp + grace)
        if now > grace_until:
            raise Expired("entitlement has expired")
        in_grace = True
    return Entitlement(
        sub=claims["sub"], org_id=claims["org_id"], plan=claims["plan"], status=claims["status"],
        features=frozenset(feats), seats=claims["seats"], iat=iat, exp=exp, kid=kid,
        in_grace=in_grace, grace_until=grace_until, role=role, policy_version=policy_version,
        policy_role=policy_role, policy_signed=claims.get("policy_signed") is True)


def paid(token: str | None) -> bool:
    """Whether ``token`` (expired or not) was signed for a paid, active plan."""
    try:
        c = verify_signed(token or "", typ=TYP, token_use=TOKEN_USE)
    except LicenseError:
        return False
    return c.get("plan") != "free" and c.get("status") in PAID_STATUSES


def needs_refresh(ent: Entitlement, now: float | None = None, max_age: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (ent.in_grace or (ent.exp - now) < REFRESH_WHEN_LEFT * (ent.exp - ent.iat)
            or (max_age is not None and now - ent.iat > max_age))


# -- the offline license --------------------------------------------------------------------

LICENSE_FILE = "license.jwt"
MAX_LICENSE_FILE = 64 * 1024


def license_path(create: bool = True):
    from brindle.pro._files import private_dir

    d = private_dir(create)
    return None if d is None else d / LICENSE_FILE


def parse_license_file(data: bytes | str) -> str:
    """The entitlement token in a license file: the token itself, or a JSON
    object holding it under ``entitlement``, ``license`` or ``token``."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise LicenseError("the license file is not text") from e
    text = data.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise LicenseError("the license file is not valid JSON") from e
        tok = next((obj.get(k) for k in ("entitlement", "license", "token")
                    if isinstance(obj, dict) and isinstance(obj.get(k), str)), None)
        if not tok:
            raise LicenseError("the license file holds no entitlement")
        text = tok.strip()
    if not text or any(c.isspace() for c in text):
        raise LicenseError("malformed entitlement")
    return text


def install(data: bytes | str, *, now: float | None = None) -> Entitlement:
    """Verify the offline license in ``data`` (a license file's contents)
    against the pinned keys, store it, and return its entitlement. Raises
    :class:`LicenseError` and stores nothing if it isn't valid right now."""
    from brindle.pro._files import write_private

    token = parse_license_file(data)
    ent = verify(token, issuer=None, now=now, grace=0)
    write_private(license_path(), token.encode("ascii"))
    clear_cache()
    return ent


def uninstall() -> bool:
    """Remove the offline license; True if there was one."""
    try:
        os.unlink(license_path())
    except FileNotFoundError:
        return False
    clear_cache()
    return True


def installed(*, now: float | None = None, grace: float = DEFAULT_GRACE) -> Entitlement | None:
    """The installed offline license's entitlement, None when none is
    installed. Raises :class:`LicenseError` for one that is unreadable,
    malformed, forged or expired past its grace."""
    from brindle.pro._files import read_private
    from brindle.pro.credentials import CredentialError

    try:
        path = license_path(create=False)
        raw = read_private(path, MAX_LICENSE_FILE) if path is not None else None
    except CredentialError as e:
        raise LicenseError(str(e)) from e
    if raw is None:
        return None
    return verify(parse_license_file(raw), issuer=None, now=now, grace=grace)


# -- the current entitlement ---------------------------------------------------------------

_cache: tuple[float, Entitlement] | None = None
CACHE_SECONDS = 300


_clear_hooks: list = []   # callables run by clear_cache: caches that depend on the entitlement


DISK_CACHE_FILE = "entitlement-cache.json"
DISK_CACHE_SECONDS = 120   # how long another process may reuse a keystore read
DISK_CACHE_GEN = "entitlement-cache.gen"   # bumped by every clear_cache, under the lock


def _disk_cache_path(create: bool = True):
    from brindle.pro._files import private_dir

    d = private_dir(create)
    return None if d is None else d / DISK_CACHE_FILE


@contextmanager
def _disk_cache_lock():
    """The cross-process lock that orders cache writes against
    invalidations; yields the private directory."""
    import fcntl

    from brindle.pro._files import private_dir

    d = private_dir()
    fd = os.open(d / f"{DISK_CACHE_FILE}.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield d
    finally:
        os.close(fd)   # releases the lock


def _disk_cache_generation(d=None) -> int | None:
    """How many times the cache has been invalidated, or None if unreadable
    (then no write is accepted)."""
    from brindle.pro._files import private_dir, read_private

    try:
        d = d or private_dir(create=False)
        raw = read_private(d / DISK_CACHE_GEN, 32) if d is not None else None
        return int(raw) if raw else 0
    except Exception:  # noqa: BLE001
        return None


def _disk_cache_read(now: float, max_age: float | None) -> Entitlement | str | None:
    """What an earlier process stored, still within its bounded time: the
    entitlement (re-verified, never trusted from the file), the message of
    the LicenseError it hit (a str), or None for no usable cache."""
    from brindle.pro._files import read_private

    try:
        path = _disk_cache_path(create=False)
        raw = read_private(path, MAX_LICENSE_FILE) if path is not None else None
        data = json.loads(raw) if raw else None
        if not isinstance(data, dict) or not isinstance(data.get("until"), (int, float)) \
                or data["until"] <= now or data["until"] - DISK_CACHE_SECONDS > now:
            return None
        if isinstance(data.get("error"), str):
            return data["error"]
        ent = verify(data["token"], issuer=data.get("issuer"), now=now)
        if max_age is not None and now - ent.iat > max_age:
            return None
        return ent if not needs_refresh(ent, now) else None
    except Exception:  # noqa: BLE001 - a cache is never worth failing over
        return None


def _disk_cache_write(now: float, gen: int | None, *, token: str | None = None,
                      issuer: str | None = None, error: str | None = None) -> None:
    """Atomically store the result for DISK_CACHE_SECONDS: 0600, via a unique
    temp file in the same directory. Dropped when the cache was invalidated
    since ``gen`` was read (before the credentials were): a logout or account
    switch meanwhile must not be undone by old credentials."""
    import tempfile

    from brindle.pro._files import pro_home

    try:
        if not pro_home().is_dir():
            return   # no home yet: a lookup that caches a result mustn't create it
        with _disk_cache_lock() as d:
            if gen is None or _disk_cache_generation(d) != gen:
                return   # invalidated while this process read the credentials
            body = {"until": now + DISK_CACHE_SECONDS}
            body.update({"error": error} if error is not None else {"token": token, "issuer": issuer})
            fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{DISK_CACHE_FILE}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(body, fh)
                os.replace(tmp, d / DISK_CACHE_FILE)
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
    except Exception:  # noqa: BLE001
        log.debug("couldn't write the entitlement cache", exc_info=True)


_request_reads: ContextVar[dict | None] = ContextVar("license_request_reads", default=None)
_clock: ContextVar[float | None] = ContextVar("license_clock", default=None)


@contextmanager
def at_time(now: float):
    """Check the entitlement at ``now`` (one org check's time) wherever
    :func:`current` isn't given a time, :func:`has` included."""
    token = _clock.set(now)
    try:
        yield
    finally:
        _clock.reset(token)


@contextmanager
def reading(memo: dict | None = None):
    """Reuse completed licensing evaluations for one routing request (or,
    with ``memo``, for every read that passes the same dict: one org check).

    Credential reads inside an evaluation remain fresh, especially under
    the refresh lock. The next request starts with an empty view.
    """
    token = _request_reads.set({} if memo is None else memo)
    try:
        yield
    finally:
        _request_reads.reset(token)


def _unrevoked(ent: Entitlement) -> Entitlement:
    """``ent``, from a cache, unless another process (or this one) has since
    revoked its key: then every cache is cleared and it is refused."""
    if ent.kid in signed.revoked_kids():
        clear_cache()
        raise signed.Revoked("entitlement is signed by a revoked key")
    return ent


def request_cached(fn):
    sig = signature(fn)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        memo = _request_reads.get()
        if memo is None:
            return fn(*args, **kwargs)
        from brindle.pro._files import pro_home

        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        key = (fn, pro_home(), tuple(bound.arguments.items()))
        if key not in memo:
            try:
                memo[key] = fn(*args, **kwargs)
            except LicenseError as e:
                memo[key] = e
        result = memo[key]
        if isinstance(result, LicenseError):
            raise result
        return _unrevoked(result) if isinstance(result, Entitlement) else result

    return wrapped


def clear_cache() -> None:
    """Forget the cached entitlement, here and on disk, and bump the
    generation so a read already in flight can't store what it saw."""
    global _cache
    _cache = None
    from brindle.pro._files import pro_home, write_private

    try:
        if pro_home().is_dir():   # no home, so nothing on disk to invalidate
            with _disk_cache_lock() as d:
                write_private(d / DISK_CACHE_GEN, str((_disk_cache_generation(d) or 0) + 1).encode())
                (d / DISK_CACHE_FILE).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - no usable private directory: nothing is cached there
        log.debug("couldn't invalidate the entitlement cache", exc_info=True)
    memo = _request_reads.get()
    if memo is not None:
        memo.clear()
    for hook in _clear_hooks:
        hook()


@request_cached
def current(*, refresh: bool = True, now: float | None = None, store=None, client=None,
            max_age: float | None = None, _first_read: bool = True) -> Entitlement:
    """The entitlement from the stored credentials, fetching a new one (and
    rotating the refresh token if needed) when it is expired or near expiry
    (``refresh``), or was issued more than ``max_age`` seconds ago. Offline,
    a stored entitlement is used within its grace. Raises
    :class:`LicenseError` when there is none.

    What was read while the cache was invalidated (a logout, login or account
    switch meanwhile) is never cached or returned: the credentials are read
    once more, and a second such change fails closed."""
    global _cache
    from brindle.pro._files import home_set

    now = _clock.get() if now is None else now
    now = time.time() if now is None else now
    default = store is None and client is None and not home_set()   # the in-process cache is the default home's
    if default and _cache and _cache[0] > now and (max_age is None or now - _cache[1].iat <= max_age):
        return _unrevoked(_cache[1])
    from brindle import airgap
    from brindle.pro import auth, credentials

    offline = airgap.enabled()
    if offline:
        refresh = False                 # air-gap mode: nothing is ever fetched
        ent = installed(now=now)
        if ent is not None:
            if default:
                _cache = (min(now + CACHE_SECONDS, ent.grace_until or ent.exp), ent)
            return ent
    gen = _disk_cache_generation() if default else None   # before the cache and credentials: see _disk_cache_write
    if default:
        hit = _disk_cache_read(now, max_age)
        if hit is not None and _disk_cache_generation() != gen:
            return _read_again(refresh, now, max_age, _first_read)
        if isinstance(hit, str):
            raise LicenseError(hit)
        if hit is not None:
            _cache = (min(now + CACHE_SECONDS, hit.grace_until or hit.exp), hit)
            return hit
    try:
        store = store or credentials.default_store()
        creds = store.load()
    except credentials.CredentialError as e:
        raise LicenseError(str(e)) from e
    if not creds or not (creds.get("entitlement") or creds.get("refresh_token")):
        if offline:
            raise LicenseError("air-gap mode: no offline license installed "
                               "(run `brindle account license install <file>`)")
        ent = installed(now=now)
        if ent is not None:
            if default:
                if _disk_cache_generation() != gen:
                    return _read_again(refresh, now, max_age, _first_read)
                _cache = (min(now + CACHE_SECONDS, ent.grace_until or ent.exp), ent)
            return ent
        msg = "not logged in to brindle Pro (run `brindle account login`)"
        if default:
            _disk_cache_write(now, gen, error=msg)
        raise LicenseError(msg)
    try:
        issuer = client.base if client is not None else auth.base_url(creds.get("base_url"))
    except auth.AuthError as e:
        raise LicenseError("stored brindle Pro URL is not allowed") from e
    ent = None
    try:
        ent = verify(creds.get("entitlement") or "", issuer=issuer, now=now)
    except LicenseError:
        if not refresh:
            raise
    if default and ent is not None and not offline and not needs_refresh(ent, now, max_age):
        _disk_cache_write(now, gen, token=creds["entitlement"], issuer=issuer)
    if refresh and (ent is None or needs_refresh(ent, now, max_age)) and creds.get("refresh_token"):
        try:
            ent = auth.refresh(client or auth.Client(issuer), store, creds, now=now)
        except auth.AuthError as e:
            if e.revoked:
                raise LicenseError(_lapsed(creds)) from e
            log.info("entitlement refresh failed (%s); using the stored entitlement", e.code)
            ent = None    # checked again below, against revocations the attempt may have fetched
    if ent is None:
        try:
            ent = verify(creds.get("entitlement") or "", issuer=issuer, now=now)
        except Expired as e:
            if refresh and creds.get("refresh_token"):
                raise LicenseError(_lapsed(creds)) from e   # couldn't renew it
            raise
    if default:
        if _disk_cache_generation() != gen:   # also after this call's own renewal (it clears the cache)
            return _read_again(refresh, now, max_age, _first_read)
        _cache = (min(now + CACHE_SECONDS, ent.grace_until or ent.exp), ent)
    return ent


def _read_again(refresh: bool, now: float, max_age: float | None, first: bool) -> Entitlement:
    """:func:`current` once more, for a read the cache's invalidation made stale."""
    if not first:
        raise LicenseError("the brindle Pro login changed while it was being read; run the command again")
    return current(refresh=refresh, now=now, max_age=max_age, _first_read=False)


def _lapsed(creds: dict) -> str:
    """Why there's no entitlement after a failed renewal: a paid subscriber is
    asked to log in; anyone else is just told there's no subscription."""
    return LOGIN_AGAIN if paid(creds.get("entitlement")) else NO_SUBSCRIPTION


ENDED_NOTICE = ("Your brindle Pro subscription has ended. brindle keeps working on the free plan; "
                "`brindle account upgrade` resubscribes.")


def ended_notice(store=None) -> str | None:
    """The notice that a paid subscription ended, while a renewal that saw it
    end hasn't been announced yet (see :func:`ended_notice_told`)."""
    from brindle.pro import credentials

    try:
        creds = (store or credentials.default_store()).load() or {}
    except Exception:  # noqa: BLE001 - a notice is never worth failing over
        return None
    return ENDED_NOTICE if creds.get("subscription_ended") else None


def ended_notice_told(store=None) -> None:
    """Never announce this ending again."""
    from brindle.pro import auth, credentials

    store = store or credentials.default_store()
    with auth.refresh_lock():
        creds = store.load() or {}
        if creds.pop("subscription_ended", None) is not None:
            store.save(creds)


def features() -> frozenset[str]:
    """The entitled features, or an empty set when there is no valid
    entitlement for any reason (fail closed)."""
    try:
        return current().features
    except Exception:  # noqa: BLE001 - fail closed on anything
        return frozenset()


def has(feature: str) -> bool:
    return feature in features()


def require(feature: str, *, store=None, client=None) -> Entitlement:
    """The entitlement if it includes ``feature``; raises otherwise.
    ``store`` and ``client`` are as for :func:`current`."""
    try:
        ent = current(store=store, client=client)
    except LicenseError:
        raise
    except Exception as e:  # noqa: BLE001 - fail closed on anything
        raise LicenseError("could not check the brindle Pro entitlement") from e
    if feature not in ent.features:
        raise NotEntitled(f"your brindle Pro plan ({ent.plan}) does not include {feature!r}")
    return ent
