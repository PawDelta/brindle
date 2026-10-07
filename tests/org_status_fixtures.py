"""A fake org backend for the ``/status`` client tests: it answers
``GET /orgs/{id}/status`` (and the policy), records ``POST .../notices/{nid}/read``
and ``POST .../status/ack``, and stands in for ``auth.authed``."""
import time
from types import SimpleNamespace

import pytest

from brindle.pro import license, rollout, status, team_policy

ORG = "org_1"


class FakeOrg:
    def __init__(self):
        self.status_code = 200
        self.body = {"policy_version": 1, "paused": False, "notices": []}
        self.policy = {"org_id": ORG, "version": 1, "policy": {}}
        self.calls = []                 # (method, path, form)
        self.fail = None                # an exception to raise

    def authed(self, client, store, method, path, form=None, *, now=None):
        self.calls.append((method, path, form))
        if self.fail:
            raise self.fail
        if path == f"/orgs/{ORG}/status":
            return self.status_code, self.body
        if path == f"/orgs/{ORG}/policy":
            return 200, self.policy
        return 200, {}

    def count(self, method, suffix):
        return sum(1 for m, p, _ in self.calls if m == method and p.endswith(suffix))


@pytest.fixture
def org(monkeypatch):
    from brindle.pro import auth

    fake = FakeOrg()
    monkeypatch.setattr(auth, "authed", fake.authed)
    monkeypatch.setattr(status, "_org_id", lambda: ORG)
    monkeypatch.setattr(status.Poller, "_make_client", lambda self: (object(), object()))
    monkeypatch.setattr(status.Poller, "_ent",
                        lambda self: self._entitlement() if self._entitlement else ent())
    return fake


def ent(features=("team", "org_budgets"), role="member"):
    return SimpleNamespace(org_id=ORG, features=frozenset(features), role=role, policy_role=None,
                           policy_version=1)


@pytest.fixture
def rollout_on(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == rollout.FEATURE)


def poller(**kw):
    kw.setdefault("entitlement", lambda: ent())
    return status.Poller(**kw)


def control(action="shutdown", cid="c1", reason="incident", until=None, **extra):
    c = {"id": cid, "action": action, "reason": reason}
    if until is not None:
        c["until"] = until
    c.update(extra)
    return c


def notice(nid="n1", text="Over budget, please slow down", scope="member"):
    return {"id": nid, "text": text, "from_role": "admin", "at": time.time(), "scope": scope}


def policy_of(**over):
    return team_policy.parse_policy(ORG, {"org_id": ORG, "version": 1, "policy": over})
