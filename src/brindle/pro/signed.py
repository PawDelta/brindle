"""Offline verification of the documents the brindle backend signs.

Every document is a compact JWS signed with Ed25519 (``alg: EdDSA``) by one
of the keys pinned in :mod:`brindle.pro.keys`, named by the header's ``kid``.
A document of type ``<type>`` has the header ``typ: brindle-<type>+jwt`` and
the claim ``token_use: <type>``; the types are :data:`TYPES`. :func:`verify`
checks a document of one of those types end to end; :func:`verify_signed` is
the shared core (signature, header, ``kid``, ``token_use``, audience and
revocations, no expiry) that other token kinds build on.

Revocations: a ``brindle-revocation+jwt`` document with the claim
``revoked_kid`` withdraws trust in that key. It counts only when a different
pinned key signed it; one signed by the key it names is ignored. Accepted
revocations are kept in ``$BRINDLE_HOME/pro/revocations.json`` (0600) and
only ever added to; every load re-verifies them, so nothing written to that
file counts unless a pinned key signed it. :func:`refresh_revocations`
merges the backend's public list (``GET /keys/revocations``) in; it never raises,
so a failed fetch leaves verification as it was.

Errors are :class:`SignedError`, with one subclass per verdict a caller may
want to tell apart. Tokens are never logged or put in exception messages.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from brindle.pro.keys import PINNED_KEYS

log = logging.getLogger(__name__)

AUDIENCE = "brindle-pro"
ALG = "EdDSA"
TYPES = frozenset({"entitlement", "policy", "status", "managed", "offline-policy", "revocation"})
LEEWAY = 60                   # clock skew tolerated on iat (and, by license, on exp)
MAX_TOKEN_BYTES = 8192
REVOCATIONS_PATH = "/keys/revocations"
REVOCATIONS_FILE = "revocations.json"
MAX_REVOCATIONS = 64          # kept locally; a revocation is one key, and keys are few


class SignedError(Exception):
    """No valid signed document. The message never contains a token."""


class Expired(SignedError):
    """A document past its expiry (and any grace its caller allows)."""


class WrongType(SignedError):
    """A valid signature on a document of another type."""


class UnknownKey(SignedError):
    """Signed by a key that isn't pinned."""


class Revoked(SignedError):
    """Signed by a pinned key that has since been revoked."""


class BadSignature(SignedError):
    """The signature doesn't match the named key."""


class ClaimMismatch(SignedError):
    """A claim differs from what the caller expected."""


def header_typ(doc_type: str) -> str:
    return f"brindle-{doc_type}+jwt"


# -- keys --------------------------------------------------------------------------------

_test_keys: dict[str, Ed25519PublicKey] = {}
_keys_lock = threading.Lock()


def _trusted_key(kid: str, what: str = "entitlement") -> Ed25519PublicKey:
    raw = PINNED_KEYS.get(kid)
    if raw is not None:
        try:
            return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))
        except ValueError as e:
            raise SignedError("pinned key is malformed") from e
    if kid in _test_keys:
        return _test_keys[kid]
    raise UnknownKey(f"{what} signed by an unknown key")


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


# -- verification ----------------------------------------------------------------------------

_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _b64decode(part: str, what: str = "entitlement", error: type = SignedError) -> bytes:
    if not part or any(c not in _B64URL for c in part) or len(part) % 4 == 1:
        raise error(f"malformed {what}")
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as e:
        raise error(f"malformed {what}") from e


def _json_object(raw: bytes, what: str = "entitlement", error: type = SignedError) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise error(f"malformed {what}") from e
    if not isinstance(obj, dict):
        raise error(f"malformed {what}")
    return obj


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _not_a(what: str) -> str:
    return f"token is not an {what}" if what[0] in "aeiou" else f"token is not a {what}"


def verify_signed(token: str, *, typ: str, token_use: str, what: str = "entitlement",
                  max_bytes: int = MAX_TOKEN_BYTES, _check_revoked: bool = True) -> dict:
    """The claims of ``token`` once its signature checks out against a pinned,
    unrevoked key and its header says ``typ``, its ``token_use`` claim says
    ``token_use``, its ``kid`` claim matches the header and its audience is
    brindle Pro. Nothing else is checked (no ``exp``, ``iat`` or issuer):
    callers do that for their own token kind. ``what`` names the token kind
    in errors. Raises :class:`SignedError`.

    The checks run in the backend verifier's order, so both report the same
    verdict: a malformed token or algorithm, ``typ``, the key, the signature,
    ``token_use``, then ``aud`` and the claimed ``kid``, then revocation."""
    if not isinstance(token, str) or len(token) > max_bytes:
        raise BadSignature(f"malformed {what}")
    parts = token.split(".")
    if len(parts) != 3:
        raise BadSignature(f"malformed {what}")
    header = _json_object(_b64decode(parts[0], what, BadSignature), what, BadSignature)
    if header.get("alg") != ALG:
        raise BadSignature(f"{what} uses an unsupported algorithm")
    if header.get("typ") != typ:
        raise WrongType(_not_a(what))
    if "crit" in header:
        raise BadSignature(f"{what} has unsupported critical headers")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise UnknownKey(f"{what} names no key")
    key = _trusted_key(kid, what)
    sig = _b64decode(parts[2], what, BadSignature)
    try:
        key.verify(sig, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except InvalidSignature as e:
        raise BadSignature(f"{what} signature is invalid") from e
    # Only now is the payload trusted enough to parse.
    claims = _json_object(_b64decode(parts[1], what, ClaimMismatch), what, ClaimMismatch)
    missing = [c for c in ("aud", "kid", "token_use") if c not in claims]
    if missing:
        error = WrongType if "token_use" in missing else ClaimMismatch
        raise error(f"{what} is missing claims: {', '.join(missing)}")
    if claims["token_use"] != token_use:
        raise WrongType(_not_a(what))
    if claims["kid"] != kid:
        raise ClaimMismatch(f"{what} kid mismatch")
    aud = claims.get("aud")
    if not (aud == AUDIENCE or (isinstance(aud, list) and AUDIENCE in aud)):
        raise ClaimMismatch(f"{what} is for a different audience")
    if _check_revoked and kid in revoked_kids():
        raise Revoked(f"{what} is signed by a revoked key")
    return claims


def verify(token: str, typ: str, expect: dict, *, now: float | None = None) -> dict:
    """The claims of ``token``, a document of type ``typ`` (one of
    :data:`TYPES`), once :func:`verify_signed` accepts it, it is within its
    ``iat`` (``LEEWAY`` seconds of clock skew allowed) and ``exp``, and every
    claim in ``expect`` has exactly the given value. Raises :class:`SignedError`."""
    if typ not in TYPES:
        raise ValueError(f"unknown signed document type {typ!r}")
    now = time.time() if now is None else now
    claims = verify_signed(token, typ=header_typ(typ), token_use=typ, what=typ)
    iat, exp = claims.get("iat"), claims.get("exp")
    if not (_is_int(iat) and _is_int(exp)):
        raise ClaimMismatch(f"{typ} has no valid iat or exp")
    if now >= exp:
        raise Expired(f"{typ} has expired")
    if iat > now + LEEWAY:
        raise Expired(f"{typ} is not valid yet")
    for name, value in expect.items():
        if name not in claims or claims[name] != value:
            raise ClaimMismatch(f"{typ} claim {name!r} does not match")
    return claims


# -- revocations ------------------------------------------------------------------------------


def _accepted_revocation(token: str) -> str | None:
    """The kid ``token`` revokes, or None unless it is a revocation signed by
    a pinned key other than the one it names."""
    try:
        claims = verify_signed(token, typ=header_typ("revocation"), token_use="revocation",
                               what="revocation", _check_revoked=False)
    except SignedError:
        return None
    revoked = claims.get("revoked_kid")
    if not isinstance(revoked, str) or not revoked or revoked == claims["kid"]:
        return None
    return revoked


def _revocations_path(create: bool = False):
    from brindle.pro._files import private_dir

    d = private_dir(create)
    return None if d is None else d / REVOCATIONS_FILE


def _stored_revocations() -> list[str]:
    from brindle.pro._files import read_private

    try:
        path = _revocations_path()
        raw = read_private(path, MAX_REVOCATIONS * (MAX_TOKEN_BYTES + 4) + 64) if path else None
        data = json.loads(raw) if raw else {}
        toks = data.get("revocations") if isinstance(data, dict) else None
        return [t for t in toks if isinstance(t, str)] if isinstance(toks, list) else []
    except Exception:  # noqa: BLE001 - an unreadable cache holds no revocations
        log.debug("couldn't read the revocation cache", exc_info=True)
        return []


def revoked_kids() -> frozenset[str]:
    """The kids revoked by the stored revocations (each re-verified)."""
    kids = (_accepted_revocation(t) for t in _stored_revocations())
    return frozenset(k for k in kids if k is not None)


def save_revocations(tokens: list) -> frozenset[str]:
    """Add the accepted revocations among ``tokens`` to the local cache and
    return the kids that are newly revoked. Others are ignored."""
    import fcntl
    import tempfile

    from brindle.pro._files import private_dir

    fresh = [t for t in tokens if isinstance(t, str) and _accepted_revocation(t) is not None]
    if not fresh:
        return frozenset()
    d = private_dir()
    fd = os.open(d / f"{REVOCATIONS_FILE}.lock",
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        stored = [t for t in _stored_revocations() if _accepted_revocation(t) is not None]
        before = {_accepted_revocation(t) for t in stored}
        merged = stored + [t for t in dict.fromkeys(fresh) if t not in stored]
        merged = merged[:MAX_REVOCATIONS]
        if merged == stored:
            return frozenset()
        tmp_fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{REVOCATIONS_FILE}.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump({"revocations": merged}, fh)
            os.replace(tmp, d / REVOCATIONS_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(fd)   # releases the lock
    return frozenset({_accepted_revocation(t) for t in merged} - before)


def refresh_revocations(client) -> frozenset[str]:
    """Fetch the backend's public revocation list with ``client`` (an
    :class:`brindle.pro.auth.Client`) and keep the accepted ones. Returns the
    newly revoked kids; any failure is logged and returns none."""
    try:
        status, body = client.get(REVOCATIONS_PATH)
        toks = body.get("revocations") if status == 200 and isinstance(body, dict) else None
        if not isinstance(toks, list):
            return frozenset()
        return save_revocations(toks[:MAX_REVOCATIONS])
    except Exception:  # noqa: BLE001 - revocations are never worth failing a refresh over
        log.info("couldn't refresh the revocation list", exc_info=True)
        return frozenset()
