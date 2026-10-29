"""The daemon's governance routes: the status line's facts by where the signed
policy came from, the offline policy export, the managed device file and plan
approvals, against faked org context modules and a fake account API."""
import json
import sys
from types import SimpleNamespace

import pytest

from brindle import config
from brindle.pro import auth
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401
from test_daemon_actions import _send
from test_pro_team import ORG, login, team, team_claims  # noqa: F401

PREFIX = "/v1/governance"


class Invalid:
    def __init__(self, reason):
        self.reason = reason


def fake_org(monkeypatch, *, source, blocked=None, managed=None, verified=True):
    """brindle.pro.org_context, .managed and .policy_cache as they will answer."""
    ctx = SimpleNamespace(source=lambda: source, blocked_reason=lambda: blocked, org_name="Acme", org_id=ORG)
    modules = {"org_context": SimpleNamespace(OrgContext=SimpleNamespace(resolve=lambda: ctx)),
               "managed": SimpleNamespace(load=lambda: managed, Invalid=Invalid),
               "policy_cache": SimpleNamespace(get_verified=lambda org_id: {"org_id": org_id} if verified else None)}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, f"brindle.pro.{name}", module)


@pytest.mark.parametrize("org,expected", [
    ({"source": "device", "managed": SimpleNamespace(org_id=ORG)},
     {"status": "managed", "verified": True, "source": "device", "org_name": "Acme", "problem": None}),
    ({"source": "snapshot", "blocked": "The saved policy expired 3 days ago."},
     {"status": "problem", "verified": False, "problem": "The saved policy expired 3 days ago.",
      "action": "Connect to the internet so brindle can fetch a fresh signed policy, "
                "or ask your organization's admin for an offline policy export."}),
    ({"source": "account", "verified": False}, {"status": "problem", "verified": False, "source": "account"}),
    ({"source": None, "managed": Invalid("signature does not verify")},
     {"status": "problem", "problem": "This device's managed device file can't be used: signature does not verify"}),
    ({"source": None}, {"status": "unmanaged", "org_name": None}),
    (None, {"status": "unavailable", "verified": False}),
])
def test_status(monkeypatch, org, expected):
    if org is None:     # an engine without the org context modules yet
        monkeypatch.setitem(sys.modules, "brindle.pro.org_context", None)
    else:
        fake_org(monkeypatch, **org)
    code, got = _send("GET", PREFIX)
    assert code == 200
    assert got | expected == got


@pytest.fixture
def admin(team, pro_env, monkeypatch):
    monkeypatch.setattr(auth, "UrllibTransport", lambda: team)
    login(team, team_claims(plan="enterprise", role="admin"))
    return team


def test_offline_export_and_device_file(admin, monkeypatch):
    sent = []
    # A signed policy naming another org is refused, and nothing is written.
    admin.routes[f"POST /orgs/{ORG}/policy/offline"] = lambda f, h: (200, {"token": "t", "body": {"org_id": "org_other"}})
    assert _send("POST", PREFIX + "/offline-policy", {"days": 30})[0] == 502
    assert not (config.brindle_home() / "policy.json").exists()
    admin.routes[f"POST /orgs/{ORG}/policy/offline"] = lambda f, h: (
        sent.append(dict(f)) or (200, {"token": "signed.token", "body": {"org_id": ORG, "exp": 99}}))
    assert _send("POST", PREFIX + "/offline-policy", {"days": 91})[0] == 400
    code, got = _send("POST", PREFIX + "/offline-policy", {"days": 30})
    assert code == 200 and sent == [{"days": 30}]
    written = json.loads((config.brindle_home() / "policy.json").read_text())
    assert got["path"] == str(config.brindle_home() / "policy.json")
    assert written == {"token": "signed.token", "body": {"org_id": ORG, "exp": 99}}

    admin.routes[f"POST /orgs/{ORG}/managed-devices"] = lambda f, h: (
        sent.append(dict(f)) or (200, {"managed": {"org_id": ORG, "device_token": "dt"},
                                       "policy_body": {}, "device_id": "dev_1"}))
    code, got = _send("POST", PREFIX + "/device-file", {"name": "build-01", "allow_airgap": True,
                                                       "audit_sink": "https://siem.example.test"})
    assert code == 200
    assert sent[-1] == {"name": "build-01", "allow_airgap": True, "audit_sink": "https://siem.example.test"}
    assert got == {"filename": "managed.json", "device_id": "dev_1", "managed": {"org_id": ORG, "device_token": "dt"}}
    # The signed-in Enterprise admin may hand out device files.
    fake_org(monkeypatch, source="account")
    assert _send("GET", PREFIX)[1]["enterprise_admin"] is True


def test_plan_default_and_project_override(tmp_path):
    repo = str(tmp_path)
    assert _send("PUT", PREFIX + "/plan", {"scope": "user", "plan": "sometimes"})[0] == 400
    config.set_local(repo, "plan_first", False)     # an old project setting reads as its override
    assert _send("GET", f"{PREFIX}/plan?repo={repo}")[1] == {"user": "auto", "project": "never", "effective": "never"}
    assert _send("PUT", PREFIX + "/plan", {"scope": "user", "plan": "always"})[1] == {
        "user": "always", "project": None, "effective": None}
    code, got = _send("PUT", PREFIX + "/plan", {"scope": "repo", "repo": repo, "plan": "auto"})
    assert code == 200 and got == {"user": "always", "project": "auto", "effective": "auto"}
    code, got = _send("PUT", PREFIX + "/plan", {"scope": "repo", "repo": repo, "plan": None})
    assert got == {"user": "always", "project": None, "effective": "always"}
