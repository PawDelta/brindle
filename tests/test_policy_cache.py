"""The org's signed policy and status (brindle.pro.policy_cache): a stored
copy counts only while it verifies for this member."""
import json

import pytest

from brindle.pro import license, policy_cache, signed
from brindle.pro._files import write_private
from pro_fixtures import ISS, TEST_KID, pro_env, sign, signing_key, token  # noqa: F401 - fixtures

NOW, DAY = 1_800_000_000, 86400
ME = policy_cache.Binding(sub="user_1", org_id="org_1", role="member", policy_role=None)
BODIES = {
    "policy": {"org_id": "org_1", "version": 3, "role": "member", "policy_role": None,
               "policy": {"protected_paths": ["infra/**"], "budget": {"seat_month_usd": 50.0}},
               "effective": {"protected_paths": ["infra/**"]}},
    "status": {"policy_version": 3, "paused": True, "paused_reason": "Spend cap reached",
               "notices": [], "seat": {"spent_usd": 50.0, "budget_usd": 50.0}},
}
OFFLINE = ("{} and brindle Pro couldn't be reached for a fresh one. "
           "Connect to the network and run the command again.")


def answer(key, kind, **over):
    """The server's answer for ``kind``: its body plus a token signed for ME."""
    body = BODIES[kind]
    iat = over.pop("iat", NOW - 60)
    claims = {"iss": ISS, "aud": "brindle-pro", "kid": TEST_KID, "token_use": kind, "jti": "j",
              "sub": "user_1", "org_id": "org_1", "doc_type": kind, "role": "member",
              "policy_role": None, "policy_version": 3,
              "document_sha256": signed.document_sha256(body), "iat": iat, "exp": iat + 7 * DAY, **over}
    return dict(body, token=sign(key, claims, {"typ": signed.header_typ(kind)}))


@pytest.mark.parametrize("kind,stored_claims,edit,floor,fresh_claims,want", [
    pytest.param("policy", {}, {"version": 4}, None, {}, "server", id="edited_raised_version"),
    pytest.param("policy", {}, {"policy": {"budget": {"seat_month_usd": 50.0}}}, None, {}, "server",
                 id="edited_dropped_rules"),
    pytest.param("policy", {"role": "admin"}, {}, None, {}, "server", id="another_roles_copy"),
    pytest.param("policy", {}, {}, [4, NOW - 120], {"policy_version": 4}, "server",
                 id="older_than_last_good"),
    pytest.param("status", {}, {"paused": False}, None, None, OFFLINE.format(
        "The saved signed status for org org_1 can't be used (it doesn't match what brindle Pro signed)"),
        id="edited_status_lifting_pause"),
    pytest.param("policy", {"iat": NOW - 8 * DAY}, {}, None, None, OFFLINE.format(
        "The saved signed policy for org org_1 can't be used (it has expired)"), id="expired_offline"),
    pytest.param("policy", {}, {}, None, None, "cache", id="good_copy"),
])
def test_get_verified(tmp_path, signing_key, token, kind, stored_claims, edit, floor, fresh_claims, want):
    home = tmp_path / "home"
    d = policy_cache._dir(home)
    write_private(d / f"{kind}.json", json.dumps({**answer(signing_key, kind, **stored_claims), **edit}).encode())
    if floor:
        write_private(d / "floors.json", json.dumps({"org_1": {kind: floor}}).encode())
    calls = []

    def fetch(k, fresh=fresh_claims):
        calls.append(k)
        if fresh is None:
            raise OSError("offline")
        return answer(signing_key, k, **fresh)

    got = policy_cache.get_verified(kind, binding=ME, fetch=fetch, now=NOW, home=home)
    assert calls == ([] if want == "cache" else [kind])
    if want not in ("cache", "server"):
        assert got == policy_cache.Unavailable(want)
        return
    assert (got.source, got.body) == (want, BODIES[kind])
    again = policy_cache.get_verified(kind, binding=ME, fetch=lambda k: fetch(k, None), now=NOW, home=home)
    assert (again.source, again.body) == ("cache", BODIES[kind])      # stored as received, no fetch
    assert policy_cache.enforced(license.verify(token(policy_signed=True), issuer=None))
