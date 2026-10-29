"""Managed devices (brindle.pro.managed): the device rules apply whoever is
signed in, with the server's managed signing vectors verified the same way."""
import base64
import json
from contextlib import ExitStack
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from brindle.pro import managed, signed
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


def managed_file(key, body, *, allow_airgap=False, install=None) -> bytes:
    claims = {"iss": ISS, "aud": "brindle-pro", "sub": SUB, "org_id": ORG, "device_id": DEVICE,
              "device_token": DEVICE_TOKEN, "allow_airgap": allow_airgap,
              "policy_token": signed_answer(key, body)["token"], "iat": NOW - 60, "exp": NOW + 86400,
              "jti": "m1", "kid": TEST_KID, "token_use": "managed"}
    doc = {"managed": sign(key, claims, {"typ": "brindle-managed+jwt"}),
           "policy_body": install if install is not None else body, "device_id": DEVICE}
    return json.dumps(doc).encode()


def refused(m):
    raise managed.DeviceTokenRefused("device token revoked")


@pytest.mark.parametrize("case", [
    "signed_out", "airgap_switch", "other_org_signed_in", "unsigned_airgap_file",
    "revoked_device_token", "tampered_policy_body", "no_file",
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
    tampered = answer(version=4) if case == "tampered_policy_body" else None
    (tmp_path / managed.FILE).write_bytes(managed_file(
        signing_key, body, allow_airgap=case == "airgap_switch", install=tampered))
    m = managed.load(tmp_path, now=NOW)
    if case == "tampered_policy_body":
        assert isinstance(m, managed.Invalid) and "cover" in m.reason and DEVICE_TOKEN not in m.reason
        assert not managed.airgap_allowed(m) and not managed.local_policy_allowed(m, signed=False)
        return
    assert isinstance(m, managed.Managed) and m.device_token == DEVICE_TOKEN
    assert DEVICE_TOKEN not in repr(m)
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
