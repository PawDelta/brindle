"""Verified copies of an org's signed policy and signed status.

``GET /orgs/{id}/policy`` and ``GET /orgs/{id}/status`` answer with their
usual fields plus ``token``: a ``brindle-policy+jwt`` or
``brindle-status+jwt`` document bound to the member who asked (``sub``,
``org_id``, ``role``, ``policy_role``) that carries the ``policy_version``
and the SHA-256 of the rest of the answer (:func:`brindle.pro.signed.verify`
with ``body``). It is good for 7 days.

:func:`get_verified` keeps the answer exactly as received in
``$BRINDLE_HOME/pro/signed/<kind>.json`` (0600) and verifies it on every
read, so nothing written to that file counts unless brindle Pro signed it
for this member. A copy that doesn't verify, is bound to another member, org,
role or policy role, has expired, or is older than the newest copy this home
has verified for the org is refetched once; if there is still no good copy
the answer is :class:`Unavailable`, whose reason says what to do. The
newest verified ``(policy_version, iat)`` per org and kind is kept in
``floors.json`` beside the copies.

:func:`enforced` says whether the member's entitlement requires the signed
policy (its ``policy_signed`` claim).
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from brindle.pro import signed
from brindle.pro._files import private_dir, read_private, write_private
from brindle.pro.credentials import FileStore

KINDS = ("policy", "status")
DIR = "signed"
FLOORS_FILE = "floors.json"
MAX_FILE = 512 * 1024
_BOUND = ("sub", "org_id", "role", "policy_role")


@dataclass(frozen=True)
class Binding:
    """Whom a signed document must be for: the member's entitlement says."""
    sub: str
    org_id: str
    role: str | None
    policy_role: str | None


@dataclass(frozen=True)
class VerifiedCopy:
    kind: str           # "policy" or "status"
    claims: dict        # the token's verified claims
    body: dict          # the answer without its token, as signed
    source: str         # "cache" or "server"


@dataclass(frozen=True)
class Unavailable:
    reason: str         # for people: what is wrong and what to do


class _Unusable(Exception):
    """A copy that can't be used; the message says why, for people."""


def enforced(entitlement) -> bool:
    """Whether the signed entitlement requires the signed policy."""
    return getattr(entitlement, "policy_signed", False) is True


def _dir(home: Path | None) -> Path:
    d = private_dir(home=home) / DIR
    old = os.umask(0o077)
    try:
        d.mkdir(mode=0o700, exist_ok=True)
    finally:
        os.umask(old)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise signed.SignedError(f"{d} is not a directory")
    FileStore._check(st, "signed policy directory", 0o700)
    return d


def _read_json(path: Path):
    try:
        raw = read_private(path, MAX_FILE + 1)
    except Exception:  # noqa: BLE001 - an unreadable file holds no copy
        raise _Unusable("it can't be read")
    if raw is None:
        return None
    if len(raw) > MAX_FILE:
        raise _Unusable("it is too large")
    try:
        return json.loads(raw)
    except ValueError:
        raise _Unusable("it can't be read")


def _floors(d: Path) -> dict:
    try:
        data = _read_json(d / FLOORS_FILE)
    except _Unusable:
        data = None
    return data if isinstance(data, dict) else {}


def _floor(floors: dict, org_id: str, kind: str) -> tuple | None:
    by_kind = floors.get(org_id)
    mark = by_kind.get(kind) if isinstance(by_kind, dict) else None
    if isinstance(mark, list) and len(mark) == 2 and all(signed._is_int(v) for v in mark):
        return tuple(mark)
    return None


def _check(kind: str, response, binding: Binding, now: float, floor: tuple | None):
    """(claims, body, mark) of ``response`` once it verifies for ``binding``
    and is no older than ``floor``. Raises :class:`signed.Expired` or
    :class:`_Unusable`."""
    if not isinstance(response, dict) or not isinstance(response.get("token"), str):
        raise _Unusable("it carries no signature")
    body = {k: v for k, v in response.items() if k != "token"}
    try:
        claims = signed.verify(response["token"], kind, {}, now=now, body=body)
    except signed.Expired:
        raise
    except signed.Revoked:
        raise _Unusable("it was signed by a key that has since been revoked")
    except signed.ClaimMismatch:
        raise _Unusable("it doesn't match what brindle Pro signed")
    except signed.SignedError:
        raise _Unusable("its signature doesn't verify")
    if any(name not in claims or claims[name] != getattr(binding, name) for name in _BOUND):
        raise _Unusable("it was issued for another account, org or role")
    version = claims.get("policy_version")
    if not signed._is_int(version):
        raise _Unusable("it carries no policy version")
    mark = (version, claims["iat"])
    if floor is not None and mark < floor:
        raise _Unusable("it is older than the copy brindle last verified")
    return claims, body, mark


def _keep(d: Path, kind: str, org_id: str, response: dict, mark: tuple) -> None:
    """Store ``response`` and raise the org's floor to ``mark``, unless a
    newer copy was stored meanwhile."""
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        floors = _floors(d)
        current = _floor(floors, org_id, kind)
        if current is not None and mark < current:
            return
        write_private(d / f"{kind}.json", json.dumps(response).encode())
        if not isinstance(floors.get(org_id), dict):
            floors[org_id] = {}
        floors[org_id][kind] = list(mark)
        write_private(d / FLOORS_FILE, json.dumps(floors).encode())
    finally:
        os.close(fd)   # releases the lock


def get_verified(kind: str, *, binding: Binding, fetch: Callable[[str], dict],
                 now: float | None = None, home: Path | None = None) -> VerifiedCopy | Unavailable:
    """The org's signed ``kind`` ("policy" or "status") for ``binding``,
    verified now. The stored copy is used when it verifies; otherwise
    ``fetch(kind)`` (the decoded answer of ``GET /orgs/{org_id}/<kind>``,
    raising when brindle Pro can't be reached) is called once, and its answer
    is stored and used when it verifies. ``home`` is brindle's home."""
    if kind not in KINDS:
        raise ValueError(f"unknown signed document kind {kind!r}")
    now = time.time() if now is None else now
    what, org = f"signed {kind}", binding.org_id
    d = _dir(home)
    floor = _floor(_floors(d), org, kind)
    try:
        cached = _read_json(d / f"{kind}.json")
        if cached is not None:
            claims, body, _ = _check(kind, cached, binding, now, floor)
            return VerifiedCopy(kind, claims, body, "cache")
        problem = None
    except signed.Expired:
        problem = "it has expired"
    except _Unusable as e:
        problem = str(e)
    try:
        response = fetch(kind)
    except Exception:  # noqa: BLE001 - any failure to fetch is brindle Pro being out of reach
        saved = (f"No {what} for org {org} is saved on this machine" if problem is None
                 else f"The saved {what} for org {org} can't be used ({problem})")
        return Unavailable(f"{saved} and brindle Pro couldn't be reached for a fresh one. "
                           "Connect to the network and run the command again.")
    try:
        claims, body, mark = _check(kind, response, binding, now, floor)
    except signed.Expired:
        return Unavailable(f"brindle Pro sent a {what} for org {org} that has already expired. "
                           "Check this machine's clock, then run the command again.")
    except _Unusable as e:
        return Unavailable(f"brindle Pro sent a {what} for org {org} that brindle can't use ({e}). "
                           "Log in again with `brindle account login`, then run the command again.")
    _keep(d, kind, org, response, mark)
    return VerifiedCopy(kind, claims, body, "server")
