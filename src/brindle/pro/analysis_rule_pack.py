"""Signed managed analysis rules, with the org library's cache policy.

The signature is a pinned-key JWT over the SHA-256 of canonical ``pack``
JSON (sorted keys, compact UTF-8), with version, iat and exp claims. A
cached response is verified on every use. Air-gap mode disables this pack.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

from brindle.pro import license, org_profiles
from brindle.pro._files import private_dir, read_private, write_private

FEATURE = "analysis_docs"
TYP = "brindle-analysis-rule-pack+jwt"
TOKEN_USE = "analysis_rule_pack"
CACHE_FILE = "analysis-rule-pack.json"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedPack:
    name: str
    version: int
    text: str
    iat: int
    exp: int
    fetched_at: float
    body: dict


def canonical(pack: dict) -> bytes:
    return json.dumps(pack, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_pack(body: dict, *, now: float | None = None, fetched_at: float | None = None,
               grace: float = 0.0) -> ManagedPack:
    now = time.time() if now is None else now
    raw = body.get("pack") if isinstance(body, dict) else None
    if not (isinstance(raw, dict) and set(raw) == {"name", "version", "text"}
            and isinstance(raw["name"], str) and org_profiles.NAME_RE.fullmatch(raw["name"])
            and license._is_int(raw["version"]) and raw["version"] >= 0
            and isinstance(raw["text"], str) and raw["text"].strip()
            and len(raw["text"]) <= org_profiles.MAX_TEXT):
        raise org_profiles.LibraryUnavailable("malformed analysis rule pack")
    try:
        claims = license.verify_signed(body.get("signature"), typ=TYP, token_use=TOKEN_USE,
                                       what="analysis rule pack")
    except license.LicenseError as e:
        raise org_profiles.LibraryUnavailable(str(e)) from e
    iat, exp = claims.get("iat"), claims.get("exp")
    if not (license._is_int(iat) and license._is_int(exp) and exp > iat):
        raise org_profiles.LibraryUnavailable("malformed analysis signature lifetime")
    if (claims.get("pack_sha256") != hashlib.sha256(canonical(raw)).hexdigest()
            or claims.get("version") != raw["version"]):
        raise org_profiles.LibraryUnavailable("analysis pack doesn't match its signature")
    if iat > now + license.LEEWAY:
        raise org_profiles.LibraryUnavailable("analysis signature is not valid yet")
    if now > exp + license.LEEWAY and now > iat + min(max(0.0, grace), license.MAX_GRACE):
        raise org_profiles.LibraryUnavailable("analysis signature has expired")
    return ManagedPack(**raw, iat=iat, exp=exp, fetched_at=now if fetched_at is None else fetched_at,
                       body=body)


def load_cached(*, now: float | None = None) -> ManagedPack | None:
    try:
        raw = read_private(private_dir() / CACHE_FILE, org_profiles.MAX_RESPONSE + 1024)
        if raw:
            doc = json.loads(raw)
            return parse_pack(doc["response"], now=now, fetched_at=doc.get("fetched_at"),
                              grace=org_profiles.CACHE_GRACE)
    except Exception as e:  # noqa: BLE001 - an invalid cache is no pack
        log.warning("ignoring cached analysis rule pack (%s)", e)
    return None


def current_pack(client=None, store=None) -> ManagedPack:
    from brindle import airgap
    from brindle.pro import auth, credentials

    if airgap.enabled():
        raise org_profiles.LibraryUnavailable("analysis rule pack disabled in air-gap mode")
    cached = load_cached()
    if cached is not None and org_profiles.fresh(cached, time.time()):
        return cached
    try:
        store = store or credentials.default_store()
        client = client or org_profiles.client_for((store.load() or {}).get("base_url"))
        status, body = auth.authed(client, store, "GET", "/analysis/rule-pack")
        if status != 200:
            raise org_profiles.LibraryUnavailable(f"HTTP {status}")
        result = parse_pack(body)
        write_private(private_dir() / CACHE_FILE,
                      json.dumps({"fetched_at": result.fetched_at, "response": body}).encode())
        return result
    except Exception as e:  # noqa: BLE001 - verified offline copy survives a failed fetch
        if cached is not None:
            return cached
        raise org_profiles.LibraryUnavailable(str(e)) from e


def pack():
    """A RulePack only with a current entitlement and verified managed text."""
    from brindle import airgap
    from brindle.profiles import RulePack, _frontmatter, _list

    try:
        if airgap.enabled() or FEATURE not in license.current().features:
            return None
        managed = current_pack()
        meta, body = _frontmatter(managed.text)
        return RulePack(name=managed.name, description=meta.get("description", ""),
                        prompt=body.strip(), deny_deps=_list(meta.get("deny_deps")) or [],
                        require_tests_for=_list(meta.get("require_tests_for")) or [],
                        deny_patterns=_list(meta.get("deny_patterns")) or [],
                        source="managed analysis rule pack", deliverables=True)
    except Exception:  # noqa: BLE001 - no verified entitlement or pack means no managed rules
        return None
