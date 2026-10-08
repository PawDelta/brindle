"""GET /entitlement reports which agent providers this machine is signed in to:
names only, omitted when there are none."""

import time

import pytest

from brindle import doctor, providers
from brindle.pro import auth
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures

# tests/conftest.py stubs doctor.signed_in_providers for every test; keep the
# real one (captured at import) to test the detection itself.
REAL_SIGNED_IN = doctor.signed_in_providers


def fetch(backend, org_id=None):
    """Call _fetch_entitlement; return the "GET /entitlement..." key it requested."""
    client = auth.Client(transport=backend)
    got = auth._fetch_entitlement(client, backend.issue()["access_token"], time.time(), org_id)
    assert got != 401
    return next(p for p in backend.paths() if p.startswith("GET /entitlement"))


def test_signed_in_providers_are_sent_comma_separated(backend, monkeypatch):
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["claude", "codex", "antigravity"])
    backend.routes["GET /entitlement?providers=claude,codex,antigravity"] = backend._entitlement
    assert fetch(backend) == "GET /entitlement?providers=claude,codex,antigravity"


def test_no_providers_omits_the_param(backend, monkeypatch):
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: [])
    assert fetch(backend) == "GET /entitlement"


def test_providers_are_sent_alongside_the_org_id(backend, monkeypatch):
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["claude", "native"])
    seen = []
    backend.routes["GET /entitlement?org_id=org_team1&providers=claude,native"] = \
        lambda f, h: (seen.append(1), (404, {"error": "not_found"}))[1]
    with pytest.raises(auth.AuthError):
        auth._fetch_entitlement(auth.Client(transport=backend), backend.issue()["access_token"],
                                time.time(), "org_team1")
    assert seen


def test_a_failing_probe_sends_nothing_and_does_not_break_the_fetch(backend, monkeypatch):
    def boom():
        raise OSError("probe failed")

    monkeypatch.setattr(doctor, "signed_in_providers", boom)
    assert fetch(backend) == "GET /entitlement"


def test_only_plain_names_are_ever_sent(backend, monkeypatch):
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["claude", "sk-ant-SECRET/x"])
    backend.routes["GET /entitlement?providers=claude"] = backend._entitlement
    assert fetch(backend) == "GET /entitlement?providers=claude"


def test_detection_uses_doctors_signin_rules(monkeypatch):
    """claude through its environment key, codex through a status check that
    answered "signed in"; only the names come back, never the key."""
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude", "codex", "antigravity"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(providers, "signed_out",
                        lambda p, env=None: "not signed in" if p == "antigravity" else None)
    monkeypatch.setattr(providers, "seen_signed_in", lambda p: p == "codex")
    assert REAL_SIGNED_IN() == ["claude", "codex"]


def test_detection_skips_a_cli_that_is_signed_out_or_unknown(monkeypatch):
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["codex", "antigravity"])
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(providers, "signed_out",
                        lambda p, env=None: "Codex isn't signed in" if p == "codex" else None)
    monkeypatch.setattr(providers, "seen_signed_in", lambda p: False)   # antigravity: unknown
    assert REAL_SIGNED_IN() == []
