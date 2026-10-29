"""brindle org device-file and the offline policy export (brindle.pro.org_cli)."""
import io
import json
import time

import pytest

from brindle.pro import managed, org_cli
from brindle.pro.account import ProAccount
from pro_fixtures import FakeTransport, pro_env, signing_key  # noqa: F401 - fixtures
from test_managed_device import NOW, ORG, answer, managed_file, signed_answer


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


def test_policy_on_managed_device_uses_newest_signed_copy(signing_key, monkeypatch):
    """Signed out, with a lapsed file snapshot, the device's freshly fetched policy is the one in force."""
    device = managed.verify_file(managed_file(signing_key, answer(version=7), policy_exp=NOW - 1), now=NOW)
    assert device.snapshot is None and device.snapshot_expired
    fetched = signed_answer(signing_key, answer(version=9))
    transport = FakeTransport({f"GET /managed/{ORG}/policy": [(200, fetched)]})
    chosen = []
    real = org_cli.device_policy
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(org_cli, "policy_source", lambda: "device")
    monkeypatch.setattr(managed, "load", lambda *a, **k: device)
    monkeypatch.setattr(org_cli, "device_policy", lambda *a, **k: chosen.append(real(*a, **k)) or chosen[-1])
    err = io.StringIO()
    acct = ProAccount(None, transport=transport, out=io.StringIO(), err=err)
    assert acct.run(["org", "policy"]) == 0, err.getvalue()
    assert (chosen[0].org_id, chosen[0].version) == (ORG, 9)


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
