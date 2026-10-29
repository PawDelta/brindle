"""brindle org device-file and the offline policy export (brindle.pro.org_cli)."""
import io
import json
import time
from dataclasses import replace

import pytest

from brindle.pro import credentials, managed, org_cli, team_policy
from brindle.pro.account import ProAccount
from pro_fixtures import BASE, ISS, TEST_KID, FakeTransport, pro_env, sign, signing_key, token  # noqa: F401 - fixtures
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


@pytest.mark.parametrize("second_org_cached", [True, False])
def test_policy_on_managed_device_is_the_org_contexts(signing_key, token, monkeypatch, second_org_cached):
    """With a lapsed file snapshot, the device's freshly fetched policy is in force, and a second
    org's cached worker limit stays in force while its fetch fails; with no copy of the second
    org's policy at all, its denial stands."""
    device = managed.verify_file(managed_file(signing_key, answer(version=7), policy_exp=NOW - 1), now=NOW)
    assert device.snapshot is None and device.snapshot_expired
    if second_org_cached:
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
    rc = acct.run(["org", "policy"])
    ctx, = contexts
    if not second_org_cached:
        assert (rc, ctx.policy.allowed) == (1, False) and "org_other" in ctx.policy.reason
        return
    assert rc == 0, err.getvalue()
    assert (ctx.source(), ctx.policy.org_id, ctx.policy.version, ctx.policy.max_parallel_workers) == (
        "device", ORG, 12, 1)


@pytest.mark.parametrize("days, tampered, ok", [(0, False, False), (91, False, False), (7, False, True),
                                                (7, True, False)])
def test_export_round_trip(signing_key, tmp_path, monkeypatch, days, tampered, ok):
    """The signed offline policy export is written as the server sent it and loads back once
    verified; a body the token doesn't cover is refused."""
    body = answer(version=4)
    claims = {"iss": ISS, "aud": "brindle-pro", "org_id": ORG, "doc_type": "offline-policy",
              "policy_version": 4, "document_sha256": managed.document_sha256(body),
              "iat": NOW - 60, "exp": NOW + days * 86400, "jti": "o1", "kid": TEST_KID,
              "token_use": "offline-policy"}
    export = {"token": sign(signing_key, claims, {"typ": "brindle-offline-policy+jwt"}),
              "body": answer(version=4, max_parallel_workers=9) if tampered else body}

    def call(method, path, req):
        assert (path, req) == (f"/orgs/{ORG}/policy/offline", {"days": days})
        return 200, export

    monkeypatch.setattr(time, "time", lambda: NOW)
    if not 1 <= days <= 90:
        with pytest.raises(org_cli.OrgCliError):
            org_cli.export_policy(call, ORG, str(tmp_path), days)
        return
    org_cli.export_policy(call, ORG, str(tmp_path), days)
    if not ok:
        with pytest.raises(team_policy.PolicyUnavailable):
            team_policy.load_offline(str(tmp_path), ORG)
        return
    p = team_policy.load_offline(str(tmp_path), ORG)
    assert (p.org_id, p.version, p.max_parallel_workers, p.allowed_models) == (ORG, 4, 2, ("opus",))
