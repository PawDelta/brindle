"""Managed analysis rules use signed responses and verified private caches."""

import hashlib
import json
import time

import pytest

from brindle import airgap, profiles
from brindle.pro import analysis_rule_pack as rules, auth, credentials, license, org_profiles
from brindle.pro._files import private_dir, write_private
from pro_fixtures import BASE, b64, backend, claims, pro_env, sign, signing_key  # noqa: F401


def answer(key, **over):
    pack = {"name": "analysis", "version": 1, "text": "Managed analysis instructions."}
    payload = claims(token_use=rules.TOKEN_USE, version=1,
                     pack_sha256=hashlib.sha256(rules.canonical(pack)).hexdigest())
    payload.update(over)
    return {"pack": pack, "signature": sign(key, payload, {"typ": rules.TYP})}


@pytest.fixture
def managed(backend, monkeypatch):
    store = credentials.default_store()
    issued = backend.issue()
    store.save({**issued, "base_url": BASE, "access_expires_at": time.time() + 900,
                "entitlement": sign(backend.key, claims(features=[rules.FEATURE]))})
    backend.routes["GET /analysis/rule-pack"] = [(200, answer(backend.key))]
    monkeypatch.setattr(org_profiles, "client_for", lambda base=None: auth.Client(BASE, backend))
    return backend


def test_fetch_cache_refresh_and_profile_inclusion(managed):
    profile = profiles.Profile("worker", "", "claude", "")
    loaded = profiles.load_rule_packs(profile)
    assert len(loaded) == 1 and loaded[0].deliverables
    assert loaded[0].prompt == "Managed analysis instructions."
    cache = private_dir() / rules.CACHE_FILE
    assert cache.stat().st_mode & 0o777 == 0o600
    assert rules.pack() is not None
    assert managed.paths() == ["GET /analysis/rule-pack"]
    doc = json.loads(cache.read_text())
    doc["fetched_at"] = 0
    write_private(cache, json.dumps(doc).encode())
    managed.routes["GET /analysis/rule-pack"] = [(503, {})]
    assert rules.pack() is not None  # last verified copy survives a failed refresh
    assert len(managed.paths()) == 2
    doc["response"]["pack"]["text"] = "edited on disk"
    write_private(cache, json.dumps(doc).encode())
    assert rules.pack() is None


@pytest.mark.parametrize("failure", ["entitlement", "signature", "airgap", "expired", "future",
                                      "malformed", "version", "digest", "unavailable", "grace"])
def test_fail_closed(managed, monkeypatch, failure):
    if failure in ("entitlement", "airgap"):
        assert rules.pack() is not None  # a valid cache must not bypass either switch
        if failure == "airgap":
            monkeypatch.setattr(airgap, "enabled", lambda: True)
        else:
            store = credentials.default_store()
            doc = store.load()
            doc["entitlement"] = sign(managed.key, claims(features=[]))
            store.save(doc)
            license.clear_cache()
    else:
        body = answer(managed.key)
        if failure == "signature":
            body["signature"] = body["signature"].rsplit(".", 1)[0] + "." + b64(bytes(64))
        elif failure in ("expired", "grace"):
            start = int(time.time()) - license.MAX_GRACE - 1000
            body = answer(managed.key, iat=start, exp=start + 1)
        elif failure == "future":
            start = int(time.time()) + 1000
            body = answer(managed.key, iat=start, exp=start + 1000)
        elif failure == "malformed":
            body["pack"]["version"] = True
        elif failure == "version":
            body = answer(managed.key, version=2)
        elif failure == "digest":
            body["pack"]["text"] = "tampered response"
        if failure == "grace":
            write_private(private_dir() / rules.CACHE_FILE,
                          json.dumps({"fetched_at": 0, "response": body}).encode())
        managed.routes["GET /analysis/rule-pack"] = [(503, {})] if failure in (
            "unavailable", "grace") else [(200, body)]
    assert rules.pack() is None
    if failure in ("entitlement", "airgap"):
        assert managed.paths() == ["GET /analysis/rule-pack"]


def test_cached_signature_offline_grace(managed):
    start = int(time.time()) - 7200
    body = answer(managed.key, iat=start, exp=start + 3600)
    write_private(private_dir() / rules.CACHE_FILE,
                  json.dumps({"fetched_at": 0, "response": body}).encode())
    managed.routes["GET /analysis/rule-pack"] = [(503, {})]
    assert rules.pack() is not None
