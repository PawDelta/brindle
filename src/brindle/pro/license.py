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

``iss`` must equal the base URL the entitlement was fetched from.

Everything fails closed: any problem -- a malformed token, an unknown key, a
bad signature, the wrong ``alg``/``typ``/``aud``/``iss``/``token_use``, an
expired token outside its grace, unreadable credentials -- means no
entitlement and no features.

Offline grace: an entitlement is fetched after every successful refresh, so
its signed ``iat`` is the time of the last successful refresh. An expired
entitlement is still honoured (flagged ``in_grace``) until ``iat + grace``
(default 7 days); ``grace`` is clamped to ``MAX_GRACE`` (14 days) however it
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
import binascii
import hashlib
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from brindle.pro.keys import PINNED_KEYS

log = logging.getLogger(__name__)

AUDIENCE = "brindle-pro"
ALG = "EdDSA"
TYP = "brindle-entitlement+jwt"
TOKEN_USE = "entitlement"
DEFAULT_GRACE = 7 * 86400
MAX_GRACE = 14 * 86400        # hard cap on grace past the last refresh (signed iat)
LEEWAY = 60                   # clock skew tolerated on exp / iat
MAX_TOKEN_BYTES = 8192
REFRESH_WHEN_LEFT = 0.25      # refresh once less than this share of lifetime remains
_REQUIRED = ("iss", "aud", "sub", "org_id", "plan", "status", "features", "seats", "iat", "exp",
             "kid", "jti", "token_use")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LicenseError(Exception):
    """No valid entitlement. The message never contains a token."""


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


# -- keys ----------------------------------------------------------------------------

_test_keys: dict[str, Ed25519PublicKey] = {}
_keys_lock = threading.Lock()


def jwk_thumbprint(x: str) -> str:
    """RFC 7638 thumbprint of an Ed25519 OKP key (the backend's kid)."""
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x},
                           separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()


def _trusted_key(kid: str) -> Ed25519PublicKey:
    raw = PINNED_KEYS.get(kid)
    if raw is not None:
        try:
            return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))
        except ValueError as e:
            raise LicenseError("pinned key is malformed") from e
    if kid in _test_keys:
        return _test_keys[kid]
    raise LicenseError("entitlement signed by an unknown key")


@contextmanager
def _test_signing_key(kid: str, public_key: Ed25519PublicKey):
    """Test-only: trust ``public_key`` as ``kid`` for the duration. Refuses to
    run outside pytest and refuses to shadow a pinned kid. Not configurable
    from the environment, files or the network."""
    if "pytest" not in sys.modules:
        raise RuntimeError("test signing keys are only available under pytest")
    if kid in PINNED_KEYS:
        raise RuntimeError("a test key may not shadow a pinned kid")
    with _keys_lock:
        _test_keys[kid] = public_key
    try:
        yield
    finally:
        with _keys_lock:
            _test_keys.pop(kid, None)
        clear_cache()


# -- verification ----------------------------------------------------------------------


def _b64decode(part: str, what: str = "entitlement") -> bytes:
    if not part or any(c not in _B64URL for c in part) or len(part) % 4 == 1:
        raise LicenseError(f"malformed {what}")
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as e:
        raise LicenseError(f"malformed {what}") from e


_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _json_object(raw: bytes, what: str = "entitlement") -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise LicenseError(f"malformed {what}") from e
    if not isinstance(obj, dict):
        raise LicenseError(f"malformed {what}")
    return obj


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def verify_signed(token: str, *, typ: str, token_use: str, what: str = "entitlement",
                  max_bytes: int = MAX_TOKEN_BYTES) -> dict:
    """The claims of ``token`` once its signature checks out against a pinned
    key and its header says ``typ``, its ``token_use`` claim says
    ``token_use``, its ``kid`` claim matches the header and its audience is
    brindle Pro. Nothing else is checked (no ``exp``, ``iat`` or issuer):
    callers do that for their own token kind. ``what`` names the token kind
    in errors. Raises :class:`LicenseError`."""
    if not isinstance(token, str) or len(token) > max_bytes:
        raise LicenseError(f"malformed {what}")
    parts = token.split(".")
    if len(parts) != 3:
        raise LicenseError(f"malformed {what}")
    header = _json_object(_b64decode(parts[0], what), what)
    if header.get("alg") != ALG:
        raise LicenseError(f"{what} uses an unsupported algorithm")
    if header.get("typ") != typ:
        raise LicenseError(f"token is not an {what}" if what[0] in "aeiou" else f"token is not a {what}")
    if "crit" in header:
        raise LicenseError(f"{what} has unsupported critical headers")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise LicenseError(f"{what} names no key")
    key = _trusted_key(kid)
    sig = _b64decode(parts[2], what)
    try:
        key.verify(sig, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except InvalidSignature as e:
        raise LicenseError(f"{what} signature is invalid") from e
    # Only now is the payload trusted enough to parse.
    claims = _json_object(_b64decode(parts[1], what), what)
    missing = [c for c in ("aud", "kid", "token_use") if c not in claims]
    if missing:
        raise LicenseError(f"{what} is missing claims: {', '.join(missing)}")
    if claims["kid"] != kid:
        raise LicenseError(f"{what} kid mismatch")
    if claims["token_use"] != token_use:
        raise LicenseError(f"token is not an {what}" if what[0] in "aeiou" else f"token is not a {what}")
    aud = claims.get("aud")
    if not (aud == AUDIENCE or (isinstance(aud, list) and AUDIENCE in aud)):
        raise LicenseError(f"{what} is for a different audience")
    return claims


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
        grace_until = int(iat + grace)
        if now > grace_until:
            raise LicenseError("entitlement has expired")
        in_grace = True
    return Entitlement(
        sub=claims["sub"], org_id=claims["org_id"], plan=claims["plan"], status=claims["status"],
        features=frozenset(feats), seats=claims["seats"], iat=iat, exp=exp, kid=kid,
        in_grace=in_grace, grace_until=grace_until, role=role, policy_version=policy_version,
        policy_role=policy_role)


def needs_refresh(ent: Entitlement, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return ent.in_grace or (ent.exp - now) < REFRESH_WHEN_LEFT * (ent.exp - ent.iat)


# -- the offline license --------------------------------------------------------------------

LICENSE_FILE = "license.jwt"
MAX_LICENSE_FILE = 64 * 1024


def license_path():
    from brindle.pro._files import private_dir

    return private_dir() / LICENSE_FILE


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
        raw = read_private(license_path(), MAX_LICENSE_FILE)
    except CredentialError as e:
        raise LicenseError(str(e)) from e
    if raw is None:
        return None
    return verify(parse_license_file(raw), issuer=None, now=now, grace=grace)


# -- the current entitlement ---------------------------------------------------------------

_cache: tuple[float, Entitlement] | None = None
CACHE_SECONDS = 300


def clear_cache() -> None:
    global _cache
    _cache = None


def current(*, refresh: bool = True, now: float | None = None, store=None, client=None) -> Entitlement:
    """The entitlement from the stored credentials, fetching a new one (and
    rotating the refresh token if needed) when it is expired or near expiry
    (``refresh``). Offline, an expired entitlement is used within its grace.
    Raises :class:`LicenseError` when there is none."""
    global _cache
    now = time.time() if now is None else now
    default = store is None and client is None
    if default and _cache and _cache[0] > now:
        return _cache[1]
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
                _cache = (min(now + CACHE_SECONDS, ent.grace_until or ent.exp), ent)
            return ent
        raise LicenseError("not logged in to brindle Pro (run `brindle account login`)")
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
    if refresh and (ent is None or needs_refresh(ent, now)) and creds.get("refresh_token"):
        try:
            ent = auth.refresh(client or auth.Client(issuer), store, creds, now=now)
        except auth.AuthError as e:
            if e.revoked:
                raise LicenseError("brindle Pro session was revoked; log in again") from e
            log.info("entitlement refresh failed (%s); using the stored entitlement", e.code)
    if ent is None:
        ent = verify(creds.get("entitlement") or "", issuer=issuer, now=now)   # raises with the reason
    if default:
        _cache = (min(now + CACHE_SECONDS, ent.grace_until or ent.exp), ent)
    return ent


def features() -> frozenset[str]:
    """The entitled features, or an empty set when there is no valid
    entitlement for any reason (fail closed)."""
    try:
        return current().features
    except Exception:  # noqa: BLE001 - fail closed on anything
        return frozenset()


def has(feature: str) -> bool:
    return feature in features()


def require(feature: str) -> Entitlement:
    """The entitlement if it includes ``feature``; raises otherwise."""
    try:
        ent = current()
    except LicenseError:
        raise
    except Exception as e:  # noqa: BLE001 - fail closed on anything
        raise LicenseError("could not check the brindle Pro entitlement") from e
    if feature not in ent.features:
        raise NotEntitled(f"your brindle Pro plan ({ent.plan}) does not include {feature!r}")
    return ent
