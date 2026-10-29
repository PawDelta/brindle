"""brindle org device-file and the offline policy export (brindle.pro.org_cli)."""
import io
import json
import time
from dataclasses import replace

import pytest

from brindle.pro import credentials, managed, org_cli, team_policy
from brindle.pro.account import ProAccount
from pro_fixtures import BASE, FakeTransport, pro_env, signing_key, token  # noqa: F401 - fixtures
from test_managed_device import NOW, ORG, answer, managed_file, member_policy, signed_answer


def test_device_file_verifies_and_binds_org(signing_key, tmp_path):
    seen = []

    def call(method, path, body):
        seen.append((method, path, body))
        return 200, json.loads(managed_file(signing_key, answer()))

    org_cli.create_device_file(call, ORG, name="laptop", out=str(tmp_path), now=NOW)
    device = managed.load(tmp_path, now=NOW)
    assert isinstance(device, managed.Managed) and device.org_id == ORG
    assert seen == [("POST", f"/orgs/{ORG}/managed-devices", {"name": "laptop", "allow_airgap": False})]
    assert (tmp_path / managed.FILE).stat().st_mode & 0o777 == 0o600

    with pytest.raises(org_cli.OrgCliError):
        org_cli.create_device_file(call, "org_other", out=str(tmp_path / "x"), now=NOW)
    assert not (tmp_path / "x").exists()


def test_policy_on_managed_device_is_the_org_contexts(signing_key, token, monkeypatch):
    """With a lapsed file snapshot, the device's freshly fetched policy is in force, and a second
    org's cached worker limit stays in force while its fetch fails."""
    device = managed.verify_file(managed_file(signing_key, answer(version=7), policy_exp=NOW - 1), now=NOW)
    assert device.snapshot is None and device.snapshot_expired
    team_policy.save_cached(replace(member_policy(max_parallel_workers=1), cached_for=("member", None)))
    credentials.default_store().save({"base_url": BASE, "entitlement": token(
        org_id="org_other", features=[team_policy.FEATURE], role="member", policy_version=10,
        iat=NOW - 60, exp=NOW + 3600)})
    transport = FakeTransport({f"GET /managed/{ORG}/policy": [(200, signed_answer(signing_key, answer(version=12)))],
                               "GET /orgs/org_other/policy": [(500, {})]})
    contexts = []
    real = org_cli.context
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(managed, "load", lambda *a, **k: device)
    monkeypatch.setattr(org_cli, "context", lambda *a, **k: contexts.append(real(*a, **k)) or contexts[-1])
    err = io.StringIO()
    acct = ProAccount(None, transport=transport, out=io.StringIO(), err=err)
    assert acct.run(["org", "policy"]) == 0, err.getvalue()
    ctx, = contexts
    assert (ctx.source(), ctx.policy.org_id, ctx.policy.version, ctx.policy.max_parallel_workers) == (
        "device", ORG, 12, 1)


@pytest.mark.parametrize("days, ok", [(0, False), (91, False), (7, True)])
def test_export_days(tmp_path, days, ok):
    def call(method, path, body):
        assert (path, body) == (f"/orgs/{ORG}/policy/offline", {"days": days})
        return 200, {"token": "t", "body": {"version": 1}}

    if not ok:
        with pytest.raises(org_cli.OrgCliError):
            org_cli.export_policy(call, ORG, str(tmp_path), days)
        return
    org_cli.export_policy(call, ORG, str(tmp_path), days)
    written = json.loads((tmp_path / ".brindle" / "policy.json").read_text())
    assert written == {"token": "t", "body": {"version": 1}}
