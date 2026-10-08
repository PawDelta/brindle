"""The org profile and rule-pack library (Team feature ``org_profiles``).

An org's admins publish profiles and rule packs to the backend
(``brindle account org profiles push``); every member's brindle reads them
here and ``brindle.profiles`` puts them in its lookup order: repo, user,
org, built-in. An item the admins *pinned* is looked up first, so a repo or
user file of the same name can't loosen it.

* No ``org_profiles`` entitlement (not logged in, other plan, anything that
  stops the license from verifying): no org items; fail closed.
* With one, the library is read from the signed answer of
  ``GET /orgs/{org_id}/profiles``, cached in ``$BRINDLE_HOME/pro/
  profiles-<org>/library.json`` (0600) and fetched again after an hour or
  when the signature is nearly out. The answer's signature (a JWT over the
  SHA-256 of the canonical library, made with the pinned entitlement keys) is
  verified on every fetch *and* every read of the cache, so a file edited on
  disk is worth nothing. If a fetch fails the last good copy is used for as
  long as the signature's offline grace lasts; after that, or if there has
  never been one, the org's items are unavailable (one warning) and brindle
  carries on with what it has locally. Air-gap mode never fetches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from brindle.pro import license
from brindle.pro._files import private_dir, read_private, write_private

log = logging.getLogger(__name__)

FEATURE = "org_profiles"
TYP = "brindle-org-library+jwt"
TOKEN_USE = "org_library"
KINDS = ("profile", "pack")
FETCH_TIMEOUT = 5.0
MAX_RESPONSE = 2 * 1024 * 1024        # the backend caps a library near 1 MB
MAX_TEXT = 32 * 1024
MAX_ITEMS = 32
REFRESH_EVERY = 3600                  # fetch again after this long...
REFRESH_WHEN_LEFT = 0.25              # ...or once less than this share of the signature's life is left
MEMO_SECONDS = 60                     # how long one answer (even "none") is reused in this process
CACHE_FILE = "library.json"
ORG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The backend's rule for item names: "backend-auditor", "security/backend".
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63}){0,3}$")


class LibraryUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Item:
    kind: str          # "profile" | "pack"
    name: str
    text: str          # the markdown file, frontmatter and all
    pinned: bool = False


@dataclass(frozen=True)
class Library:
    org_id: str
    version: int
    profiles: dict = field(default_factory=dict)   # name -> Item
    packs: dict = field(default_factory=dict)
    iat: int = 0
    exp: int = 0
    fetched_at: float = 0.0
    body: dict | None = None                        # the verified answer, as cached

    def of(self, kind: str) -> dict:
        return self.profiles if kind == "profile" else self.packs


# -- verifying ----------------------------------------------------------------


def canonical(org_id: str, version: int, profiles: list, packs: list) -> bytes:
    """What the backend signs the digest of (orgs.library_bytes): sorted keys,
    no spaces, UTF-8."""
    return json.dumps({"org_id": org_id, "version": version, "profiles": profiles, "packs": packs},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def _items(raw, kind: str) -> dict:
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise LibraryUnavailable("malformed org library")
    out = {}
    for i in raw:
        if not (isinstance(i, dict) and i.get("kind") == kind and isinstance(i.get("name"), str)
                and NAME_RE.match(i["name"]) and isinstance(i.get("text"), str)
                and 0 < len(i["text"]) <= MAX_TEXT and isinstance(i.get("pinned", False), bool)):
            raise LibraryUnavailable("malformed org library")
        if i["name"] in out:
            raise LibraryUnavailable("malformed org library")
        out[i["name"]] = Item(kind, i["name"], i["text"], bool(i.get("pinned", False)))
    return out


def parse_library(org_id: str, body: dict, *, now: float | None = None,
                  fetched_at: float | None = None, grace: float = 0.0) -> Library:
    """The :class:`Library` in a ``GET /orgs/{org_id}/profiles`` answer, once
    its signature is verified against the pinned keys and matches the content.
    ``grace`` is how long past the signature's expiry (counted from its
    issue time, as for entitlements) the answer is still accepted: 0 for a
    fresh fetch, more for a cached copy. Raises :class:`LibraryUnavailable`."""
    now = time.time() if now is None else now
    if not isinstance(body, dict) or not isinstance(body.get("signed"), str):
        raise LibraryUnavailable("malformed org library")
    if body.get("org_id") != org_id:
        raise LibraryUnavailable("library is for another org")
    version = body.get("version")
    if not license._is_int(version) or version < 0:
        raise LibraryUnavailable("malformed org library version")
    profiles, packs = body.get("profiles"), body.get("packs")
    parsed_p, parsed_k = _items(profiles, "profile"), _items(packs, "pack")
    if len(parsed_p) + len(parsed_k) > MAX_ITEMS:
        raise LibraryUnavailable("malformed org library")
    try:
        claims = license.verify_signed(body["signed"], typ=TYP, token_use=TOKEN_USE,
                                       what="org library")
    except license.LicenseError as e:
        raise LibraryUnavailable(str(e)) from e
    iat, exp = claims.get("iat"), claims.get("exp")
    if not (license._is_int(iat) and license._is_int(exp) and exp > iat):
        raise LibraryUnavailable("org library signature is malformed")
    if claims.get("org_id") != org_id or claims.get("sub") != "org:" + org_id:
        raise LibraryUnavailable("library is for another org")
    if claims.get("version") != version:
        raise LibraryUnavailable("org library version doesn't match its signature")
    digest = hashlib.sha256(canonical(org_id, version, profiles, packs)).hexdigest()
    if claims.get("library_sha256") != digest or body.get("library_sha256") != digest:
        raise LibraryUnavailable("org library doesn't match its signature")
    if iat > now + license.LEEWAY:
        raise LibraryUnavailable("org library signature is not valid yet")
    if now > exp + license.LEEWAY and now > iat + min(max(0.0, grace), license.MAX_GRACE):
        raise LibraryUnavailable("org library signature has expired")
    return Library(org_id=org_id, version=version, profiles=parsed_p, packs=parsed_k, iat=iat,
                   exp=exp, fetched_at=float(fetched_at or now), body=body)


# -- the cache ------------------------------------------------------------------


def cache_dir(org_id: str) -> Path:
    """``$BRINDLE_HOME/pro/profiles-<org>/``, created 0700."""
    from brindle.pro.credentials import CredentialError, FileStore

    if not ORG_RE.match(org_id):
        raise LibraryUnavailable("invalid org id")
    d = private_dir() / f"profiles-{org_id}"
    old = os.umask(0o077)
    try:
        d.mkdir(mode=0o700, exist_ok=True)
    finally:
        os.umask(old)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise CredentialError(f"{d} is not a directory")
    FileStore._check(st, "org library directory", 0o700)
    return d


def load_cached(org_id: str, *, now: float | None = None) -> Library | None:
    """The cached library, re-verified; None if there is none or it no longer
    verifies (past its grace, edited, for another org)."""
    try:
        raw = read_private(cache_dir(org_id) / CACHE_FILE, MAX_RESPONSE + 1024)
        if not raw:
            return None
        doc = json.loads(raw)
        return parse_library(org_id, doc["response"], now=now, fetched_at=doc.get("fetched_at"),
                             grace=license.DEFAULT_GRACE)
    except Exception as e:  # noqa: BLE001 - an unusable cache is no cache
        log.warning("ignoring the cached org library (%s)", e)
        return None


def save_cached(lib: Library) -> None:
    write_private(cache_dir(lib.org_id) / CACHE_FILE,
                  json.dumps({"fetched_at": lib.fetched_at, "response": lib.body}).encode())


def fresh(lib: Library, now: float) -> bool:
    return (now - lib.fetched_at < REFRESH_EVERY
            and now < lib.iat + (1 - REFRESH_WHEN_LEFT) * (lib.exp - lib.iat))


# -- fetching ---------------------------------------------------------------------


def client_for(base: str | None = None, transport=None):
    """A backend client whose responses may be as large as a library."""
    from brindle.pro import auth

    return auth.Client(base, transport or auth.UrllibTransport(timeout=FETCH_TIMEOUT,
                                                               max_bytes=MAX_RESPONSE))


def _path(org_id: str) -> str:
    if not ORG_RE.match(org_id):
        raise LibraryUnavailable("invalid org id")
    return f"/orgs/{urllib.parse.quote(org_id, safe='')}/profiles"


def fetch_library(org_id: str, client=None, store=None) -> Library:
    """Fetch, verify and cache the org's library."""
    from brindle.pro import auth, credentials

    store = store or credentials.default_store()
    if client is None:
        client = client_for((store.load() or {}).get("base_url"))
    try:
        status, body = auth.authed(client, store, "GET", _path(org_id))
    except auth.AuthError as e:
        raise LibraryUnavailable(e.code) from e
    if status != 200:
        raise LibraryUnavailable(f"HTTP {status}")
    lib = parse_library(org_id, body, now=time.time())
    save_cached(lib)
    clear_memo()
    return lib


def current_library(ent, client=None, store=None, *, now: float | None = None) -> Library:
    """The library for ``ent``'s org: the cached copy while it is fresh, else
    a fetched one, else the last good copy; raises :class:`LibraryUnavailable`
    when there is none. Air-gap mode: the cached copy and nothing else."""
    from brindle import airgap

    now = time.time() if now is None else now
    cached = load_cached(ent.org_id, now=now)
    if airgap.enabled():
        if cached is None:
            raise LibraryUnavailable("air-gap mode: no cached org library")
        return cached
    if cached is not None and fresh(cached, now):
        return cached
    try:
        return fetch_library(ent.org_id, client, store)
    except Exception as e:  # noqa: BLE001
        if cached is not None:
            log.warning("org library unavailable (%s); using cached v%s", e, cached.version)
            return cached
        raise LibraryUnavailable(str(e)) from e


# -- what brindle.profiles reads ------------------------------------------------------


_memo: tuple[float, Library | None] | None = None
_warned: set[str] = set()


def clear_memo() -> None:
    global _memo
    _memo = None


license._clear_hooks.append(clear_memo)


def library() -> Library | None:
    """The org's library for this machine's entitlement, or None: no
    ``org_profiles`` entitlement, or no usable library (fails closed: nothing
    from the org is used). Reused for a minute, so looking up many profiles
    costs one check."""
    global _memo
    now = time.monotonic()
    if _memo is not None and _memo[0] > now:
        return _memo[1]
    lib: Library | None = None
    try:
        ent = license.current()
        if FEATURE in ent.features:
            try:
                lib = current_library(ent)
            except LibraryUnavailable as e:
                if ent.org_id not in _warned:
                    _warned.add(ent.org_id)
                    log.warning("brindle: the org library for %s is unavailable (%s); org profiles "
                                "and rule packs are not used until it is (`brindle account org "
                                "profiles` to retry)", ent.org_id, e)
    except Exception:  # noqa: BLE001 - no verified entitlement: no org items
        lib = None
    _memo = (now + MEMO_SECONDS, lib)
    return lib


def items(kind: str) -> dict:
    """The org's profiles (``kind="profile"``) or rule packs, by name; empty
    without a usable library."""
    lib = library()
    return lib.of(kind) if lib is not None else {}


# -- publishing (admins) ----------------------------------------------------------------


def _frontmatter_name(text: str) -> str | None:
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "name" and value.strip():
                return value.strip().strip("'\"")
    return None


def literal_credential(text: str) -> str | None:
    """The name of a model credential an ``env.NAME: value`` line of this
    profile text sets to a literal value (the org library would store and
    serve the key), or None. ``api_key_env: NAME`` is not such a line, and
    neither is an empty value or a ``$VAR`` reference."""
    from brindle import secrets

    known = set(secrets.PERSONAL_KEYS)
    for names in secrets._CREDENTIALS.values():
        known.update(names)
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        key, value = key.strip(), value.strip().strip("'\"")
        if sep and key.startswith("env.") and key[4:] in known and value and not value.startswith("$"):
            return key[4:]
    return None


def _refuse_literal_credential(text: str, what: str) -> None:
    from brindle.pro import auth

    name = literal_credential(text)
    if name:
        raise auth.AuthError(f"{what} sets {name} to a literal value; the org library must not hold "
                             f"credentials. Name the variable with api_key_env instead",
                             code="bad_request")


def read_item(path: str | Path, kind: str, *, name: str | None = None, pinned: bool = False) -> dict:
    """The item to publish from a profile or rule-pack file: named ``name``,
    else by the file's frontmatter ``name``, else by its file name."""
    from brindle.pro import auth

    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise auth.AuthError(f"cannot read {p}: {e}", code="bad_request") from e
    name = name or _frontmatter_name(text) or p.stem
    if not NAME_RE.match(name):
        raise auth.AuthError(f"{name!r} is not a valid org {kind} name: lowercase letters, digits, "
                             "'.', '_' and '-', with '/' for packs like security/backend "
                             "(use --name)", code="bad_request")
    if not text.strip() or len(text) > MAX_TEXT:
        raise auth.AuthError(f"{p} must be 1 to {MAX_TEXT} characters", code="bad_request")
    if kind == "profile":
        _refuse_literal_credential(text, str(p))
    return {"kind": kind, "name": name, "text": text, "pinned": bool(pinned)}


def push(client, store, org_id: str, *, publish: list[dict] = (), delete: list[dict] = ()) -> dict:
    """Publish and delete library items (``POST /orgs/{org_id}/profiles``,
    admin+); the answer is the library's new version and its items."""
    from brindle.pro import auth

    for it in publish:
        if it.get("kind") == "profile":
            _refuse_literal_credential(str(it.get("text", "")), f"profile {it.get('name')!r}")
    status, body = auth.authed(client, store, "POST", _path(org_id),
                               auth.JSONBody({"publish": list(publish), "delete": list(delete)}))
    if status != 200:
        raise auth._error(status, body)
    clear_memo()
    return body
