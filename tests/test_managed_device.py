"""Managed devices (brindle.pro.managed): the device rules apply whoever is
signed in, with the server's managed signing vectors verified the same way."""
import base64
import json
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from brindle import budget
from brindle.pro import license, managed, managed_models, signed, team_policy
from brindle.pro.team_policy import parse_policy
from pro_fixtures import ISS, TEST_KID, pro_env, sign, signing_key  # noqa: F401 - fixtures

# The managed rows of the server's signing vectors (pawdelta-web
# scripts/gen_signing_vectors.py), copied unchanged.
VECTORS = json.loads((Path(__file__).parent / "fixtures" / "managed_vectors.json").read_text())
NOW = 1_800_000_000
ORG, DEVICE = "org_dev", "md_" + "1" * 32
SUB = managed.DEVICE_SUB_PREFIX + DEVICE
DEVICE_TOKEN = "mdt_" + "A" * 43


def answer(version=3, **rules):
    """A device policy answer's body (without its token)."""
    policy = {"allowed_models": ["opus"], "max_parallel_workers": 2, "require_human_review": False,
              "budget": {"seat_month_usd": 50.0}, "protected_paths": ["infra/**"], **rules}
    return {"org_id": ORG, "version": version, "role": "device", "policy_role": None,
            "policy": policy, "updated_at": NOW - 600, "updated_by": "owner"}


def signed_answer(key, body, *, sub=SUB, exp=NOW + 3600, iat=NOW - 60):
    claims = {"iss": ISS, "aud": "brindle-pro", "sub": sub, "org_id": body["org_id"], "doc_type": "policy",
              "role": "device", "policy_role": None, "policy_version": body["version"],
              "document_sha256": managed.document_sha256(body), "iat": iat, "exp": exp, "jti": "p1",
              "kid": TEST_KID, "token_use": "policy"}
    return {**body, "token": sign(key, claims, {"typ": "brindle-policy+jwt"})}


def managed_file(key, body, *, allow_airgap=False, install=None, policy_sub=SUB, policy_exp=NOW + 3600,
                 policy_iat=NOW - 60, exp=NOW + 86400) -> bytes:
    claims = {"iss": ISS, "aud": "brindle-pro", "sub": SUB, "org_id": ORG, "device_id": DEVICE,
              "device_token": DEVICE_TOKEN, "allow_airgap": allow_airgap,
              "policy_token": signed_answer(key, body, sub=policy_sub, exp=policy_exp, iat=policy_iat)["token"],
              "iat": NOW - 60, "exp": exp, "jti": "m1", "kid": TEST_KID, "token_use": "managed"}
    doc = {"managed": sign(key, claims, {"typ": "brindle-managed+jwt"}),
           "policy_body": install if install is not None else body, "device_id": DEVICE}
    return json.dumps(doc).encode()


# The budget guard only counts spend dated the current UTC month, at the real clock.
THIS_MONTH = time.strftime("%Y-%m", time.gmtime())
LAST_MONTH = time.strftime("%Y-%m", time.gmtime(time.mktime(time.strptime(THIS_MONTH + "-01", "%Y-%m-%d")) - 86400))


def member_policy(spend=None, month=THIS_MONTH, **rules):
    """A signed-in member's policy for another org, as the client parsed it."""
    body = {"version": 9, "policy": {"allowed_models": ["opus", "sonnet"], **rules},
            "role": "member", "policy_role": None}
    if spend is not None:
        body["spend"] = {"month": month, "seat_usd": spend}
    return parse_policy("org_other", body)


def both_orders(a, b):
    return [(managed.stricter(a, b), a) for a, b in ((a, b), (b, a))]


GRANTS = {"allow": True, "kinds": ["bash"], "overridable": [], "max_minutes": 60, "max_uses": 5,
          "deny": ["bash prefix rm"], "allow_persistent": False, "lifetimes": ["session"], "scopes": ["branch"],
          "controls": [{"decision": "deny", "group": "deploy/trigger", "reason": "no deploys"}], "org_name": "Other"}


def refused(m):
    raise managed.DeviceTokenRefused("device token revoked")


@pytest.mark.parametrize("case", [
    "signed_out", "airgap_switch", "other_org_signed_in", "unsigned_airgap_file",
    "revoked_device_token", "tampered_policy_body", "no_file",
    "one_sided_grants", "deny_personal_keys", "exhausted_member_budget", "plan_rules",
    "expired_snapshot", "bad_outer_token",
    "vector:managed_with_policy_ok", "vector:managed_with_policy_tampered_body",
])
def test_managed_device(signing_key, tmp_path, monkeypatch, case):
    if case.startswith("vector:"):
        vector = next(v for v in VECTORS["vectors"] if v["name"] == case[7:])
        data = json.dumps({"managed": vector["token"], "policy_body": vector["body"],
                           "device_id": vector["expect"]["device_id"]}).encode()
        with ExitStack() as stack:
            for kid, raw in VECTORS["keys"].items():
                pub = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                stack.enter_context(signed._test_signing_key(kid, Ed25519PublicKey.from_public_bytes(pub)))
            if vector["verdict"] == "claim_mismatch":
                with pytest.raises(signed.ClaimMismatch):
                    managed.verify_file(data, now=VECTORS["now"])
                return
            m = managed.verify_file(data, now=VECTORS["now"])
        assert (m.org_id, m.device_id) == (vector["expect"]["org_id"], vector["expect"]["device_id"])
        assert m.snapshot.version == vector["body"]["version"] and m.sub == "device:" + m.device_id
        return
    if case == "no_file":
        assert managed.load(tmp_path) is None
        assert managed.airgap_allowed(None) and managed.local_policy_allowed(None, signed=False)
        return
    body = answer()
    if case == "bad_outer_token":
        # The managed document itself is judged at the current clock.
        (tmp_path / managed.FILE).write_bytes(managed_file(signing_key, body, exp=NOW - 1))
        assert isinstance(managed.load(tmp_path, now=NOW), managed.Invalid)
        doc = json.loads(managed_file(signing_key, body))
        # A middle character: the last one of a signature carries unused bits, so changing it can be a no-op.
        doc["managed"] = doc["managed"][:-5] + ("A" if doc["managed"][-5] != "A" else "B") + doc["managed"][-4:]
        (tmp_path / managed.FILE).write_bytes(json.dumps(doc).encode())
        m = managed.load(tmp_path, now=NOW)
        assert isinstance(m, managed.Invalid) and DEVICE_TOKEN not in m.reason
        assert not managed.airgap_allowed(m) and not managed.local_policy_allowed(m, signed=False)
        return
    if case == "expired_snapshot":
        # A snapshot that is sound but lapsed leaves the file valid without a
        # usable snapshot; one bound to another device is the file's fault.
        other = "device:md_" + "2" * 32
        for faulty in (managed_file(signing_key, body, policy_sub=other, policy_exp=NOW - 1),
                       managed_file(signing_key, body, policy_iat=NOW + 3600, policy_exp=NOW + 7200),
                       managed_file(signing_key, answer(plan="bogus"), policy_exp=NOW - 1)):
            (tmp_path / managed.FILE).write_bytes(faulty)
            assert isinstance(managed.load(tmp_path, now=NOW), managed.Invalid)
        (tmp_path / managed.FILE).write_bytes(managed_file(signing_key, body, policy_exp=NOW - 1))
        m = managed.load(tmp_path, now=NOW)
        assert isinstance(m, managed.Managed) and m.snapshot is None and m.snapshot_expired
        assert managed.device_policy(m, fetch=refused, now=NOW) is None
        fresh = signed_answer(signing_key, answer(version=4))
        assert managed.device_policy(m, fetch=lambda dev: fresh, now=NOW).version == 4
        return
    tampered = answer(version=4) if case == "tampered_policy_body" else None
    (tmp_path / managed.FILE).write_bytes(managed_file(
        signing_key, body, allow_airgap=case == "airgap_switch", install=tampered))
    m = managed.load(tmp_path, now=NOW)
    if case == "tampered_policy_body":
        assert isinstance(m, managed.Invalid) and DEVICE_TOKEN not in m.reason
        assert not managed.airgap_allowed(m) and not managed.local_policy_allowed(m, signed=False)
        return
    assert isinstance(m, managed.Managed) and m.device_token == DEVICE_TOKEN
    assert DEVICE_TOKEN not in repr(m) and not m.snapshot_expired
    if case == "signed_out":
        seen = []

        def fetch(dev):
            seen.append(managed.device_request(dev))
            return signed_answer(signing_key, answer(version=5, allowed_models=["opus", "haiku"]))

        p = managed.device_policy(m, fetch=fetch, now=NOW)
        assert (p.version, p.allowed_models) == (5, ("opus", "haiku"))
        assert seen == [(f"/managed/{ORG}/policy", {"Authorization": f"Bearer {DEVICE_TOKEN}"})]
        assert p.raw_body["token"] and managed.device_request(m, "status")[0] == f"/managed/{ORG}/status"
    elif case == "airgap_switch":
        monkeypatch.setenv("BRINDLE_AIRGAP", "1")
        assert managed.airgap_allowed(m)
        assert not managed.airgap_allowed(managed.verify_file(managed_file(signing_key, body), now=NOW))

        def fetch(dev):
            raise OSError("air-gap mode: refusing the request")

        p = managed.device_policy(m, fetch=fetch, now=NOW)
        assert (p.version, p.allowed_models, p.max_parallel_workers) == (3, ("opus",), 2)
    elif case == "other_org_signed_in":
        device = managed.device_policy(m, fetch=refused, now=NOW)
        member = parse_policy("org_other", {
            "version": 9, "policy": {"allowed_models": ["opus", "sonnet"], "max_parallel_workers": 1,
                                     "require_human_review": True, "budget": {"seat_month_usd": 20.0},
                                     "protected_paths": ["secrets/**", "infra/**"], "kill_switch": True,
                                     "min_version": "2.0"},
            "role": "member", "policy_role": None})
        for a, b in ((device, member), (member, device)):
            s = managed.stricter(a, b)
            assert s.org_id == a.org_id and s.enforced is s and s.version == 9
            assert (s.allowed_models, s.max_parallel_workers, s.budget_seat_month_usd) == (("opus",), 1, 20.0)
            assert s.protected_paths == tuple(dict.fromkeys((*a.enforced.protected_paths,
                                                             *b.enforced.protected_paths)))
            assert s.require_human_review and s.kill_switch and s.min_version == "2.0"
            assert s.permission_grants is None and s.allowed_providers is None
    elif case == "one_sided_grants":
        # The device grants nothing; the member's org denies and controls still stand.
        device = managed.device_policy(m, fetch=refused, now=NOW)
        assert device.permission_grants is None
        for s, _ in both_orders(device, member_policy(permission_grants=GRANTS)):
            g = s.permission_grants
            assert g is not None and not g.allow and g.kinds == () and g.lifetimes == ()
            assert g.deny == ("bash prefix rm",) and [c.group for c in g.controls] == ["deploy/trigger"]
        open_grants = {**GRANTS, "deny": [], "controls": []}
        for s, _ in both_orders(device, member_policy(permission_grants=open_grants)):
            assert s.permission_grants is None
    elif case == "deny_personal_keys":
        # Each side's effective rule (agent_setup over the top-level field), then either side's wins,
        # written where managed_models reads it: the merged agent_setup and the top-level field.
        device = managed.device_policy(m, fetch=refused, now=NOW)
        lenient = member_policy(deny_personal_keys=True, agent_setup={"deny_personal_keys": False})
        for s, _ in both_orders(device, lenient):
            assert not s.deny_personal_keys and s.agent_setup.deny_personal_keys is False
        strict = member_policy(agent_setup={"deny_personal_keys": True})
        for s, _ in both_orders(device, strict):
            assert s.deny_personal_keys and s.agent_setup.deny_personal_keys is True
        # A device that denies personal keys at the top level (no agent_setup) against a member
        # whose agent_setup allows them: the denial survives, where managed_models reads it.
        denying = managed.device_policy(m, fetch=refused, cached=signed_answer(
            signing_key, answer(version=4, deny_personal_keys=True)), now=NOW)
        assert denying.deny_personal_keys and denying.agent_setup is None
        monkeypatch.setattr(license, "has", lambda feature: feature == "managed_models")
        for s, _ in both_orders(denying, lenient):
            assert s.deny_personal_keys and s.agent_setup.deny_personal_keys is True
            monkeypatch.setattr(team_policy, "managed_models", lambda root, s=s: s)
            assert managed_models.current("repo").deny_personal_keys
    elif case == "exhausted_member_budget":
        # The lower cap with the larger spend: the guard can't count less than either side.
        device = managed.device_policy(m, fetch=refused, cached=signed_answer(
            signing_key, {**answer(version=4), "spend": {"month": THIS_MONTH, "seat_usd": 5.0}}), now=NOW)
        assert (device.budget_seat_month_usd, device.spend_seat_usd) == (50.0, 5.0)
        exhausted = member_policy(spend=20.0, budget={"seat_month_usd": 20.0})
        for s, _ in both_orders(device, exhausted):
            assert (s.budget_seat_month_usd, s.spend_seat_usd, s.spend_month) == (20.0, 20.0, THIS_MONTH)
        # Month rollover: a larger amount from last month never displaces this month's usage,
        # as the budget guard sees it.
        stale = managed.device_policy(m, fetch=refused, cached=signed_answer(
            signing_key, {**answer(version=4), "spend": {"month": LAST_MONTH, "seat_usd": 100.0}}), now=NOW)
        for s, _ in both_orders(stale, exhausted):
            assert (s.spend_seat_usd, s.spend_month) == (20.0, THIS_MONTH)
            monkeypatch.setattr(team_policy, "org_budgets", lambda root, s=s: s)
            lim = budget.org_limits("repo")
            assert (lim.month_usd, lim.seat_spent_usd) == (20.0, 20.0)
    elif case == "plan_rules":
        device = managed.device_policy(m, fetch=refused, cached=signed_answer(signing_key, answer(
            version=4, plan="auto", plan_paths=["infra/**"], plan_approvers=["u_dev"],
            plan_reviewer_may_approve_low_risk=True)), now=NOW)
        assert (device.plan, device.plan_paths, device.plan_approvers) == ("auto", ("infra/**",), ("u_dev",))
        assert device.plan_approver_groups == (("u_dev",),)
        always = member_policy(plan="always", plan_paths=["db/**"], plan_approvers=["u_other"],
                               plan_two_person=True, plan_reviewer_may_approve_low_risk=False)
        for s, a in both_orders(device, always):
            assert (s.plan, s.plan_two_person, s.plan_reviewer_may_approve_low_risk) == ("always", True, False)
            assert set(s.plan_paths) == {"infra/**", "db/**"} and s.plan_paths[0] in a.plan_paths
            # Both orgs' approver lists stay separate groups; neither widens the other.
            assert set(s.plan_approver_groups) == {("u_dev",), ("u_other",)} and s.plan_approvers == a.plan_approvers
        none = member_policy(plan="never", plan_reviewer_may_approve_low_risk=True)
        for s, _ in both_orders(device, none):
            assert (s.plan, s.plan_two_person, s.plan_reviewer_may_approve_low_risk) == ("auto", False, True)
            assert s.plan_approver_groups == (("u_dev",),)
    elif case == "unsigned_airgap_file":
        assert not managed.local_policy_allowed(m, signed=False)
        assert managed.local_policy_allowed(m, signed=True)
    elif case == "revoked_device_token":
        other = signed_answer(signing_key, answer(version=6), sub="device:md_" + "2" * 32)
        lapsed = signed_answer(signing_key, answer(version=7), exp=NOW - 1)
        assert managed.device_policy(m, fetch=refused, cached=other, now=NOW).version == 3
        assert managed.device_policy(m, fetch=refused, cached=lapsed, now=NOW).version == 3
        newer = signed_answer(signing_key, answer(version=4, max_parallel_workers=1))
        p = managed.device_policy(m, fetch=refused, cached=parse_policy(ORG, newer), now=NOW)
        assert (p.version, p.max_parallel_workers) == (4, 1)
        assert managed.device_policy(m, fetch=refused, now=NOW + 7200) is None
