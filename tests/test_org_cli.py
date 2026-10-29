"""brindle org device-file and the offline policy export (brindle.pro.org_cli)."""
import io
import json

import pytest

from brindle.pro import managed, org_cli
from brindle.pro.account import ProAccount
from pro_fixtures import signing_key  # noqa: F401 - fixture
from test_managed_device import NOW, ORG, answer, managed_file


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


def test_policy_on_managed_device_ignores_account(signing_key, monkeypatch):
    """A device-sourced policy is the device's signed policy, even signed out."""
    device = managed.verify_file(managed_file(signing_key, answer(version=7)), now=NOW)
    monkeypatch.setattr(org_cli, "policy_source", lambda: "device")
    monkeypatch.setattr(managed, "load", lambda *a, **k: device)
    out = io.StringIO()
    acct = ProAccount(None, out=out)    # no credentials: the account is never consulted
    assert acct.cmd_org_policy(None) == 0
    assert f"Policy for org {ORG}, version 7" in out.getvalue()


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
