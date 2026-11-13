import pytest

from brindle import budget, watch_costs
from brindle.watch_costs import Local, Org, Remote


def ws(agents):
    return {"id": "repo/feat", "name": "feat", "branch": "feat", "base_branch": "main",
            "path": "/", "ahead": 0, "behind": 0, "dirty": 0, "agents": agents}


def agent(status="processing", **kw):
    return {"id": "a1b2c3d4", "profile": "developer", "provider": "claude", "status": status,
            "mode": "assign", "status_since": 1000.0, "pending": 0, "reported": False,
            "window": "@1", **kw}


def texts(lines):
    return [ln.text for ln in lines]


def section(local, remote=None, now=1000.0, width=60, collapsed=False):
    return watch_costs.render(local, remote, now, width, collapsed)


# -- the org call ------------------------------------------------------------------------------


class Stub:
    def __init__(self, status, body):
        self.status, self.body, self.calls = status, body, []

    def __call__(self, client, store, method, path, *a, **kw):
        self.calls.append((method, path))
        return self.status, self.body


SPEND = {
    "org_id": "org_1", "month": "2026-10",
    "seats": [{"sub": "u1", "role": "owner", "usd": 60.0}, {"sub": "u2", "role": "member", "usd": 40.0},
              {"sub": "u3", "role": "member", "usd": 20.5}],
    "total_usd": 120.5,
    "budget": {"seat_month_usd": 50.0, "goal_usd": None},
    "cost_centers": {"centers": [{"name": "eng", "usd": 90.0, "budget_usd": 200.0},
                                 {"name": "ops", "usd": 10.0, "budget_usd": None}],
                     "unattributed_usd": 4.5},
}


def test_fetch_spend_parses_the_org_answer(monkeypatch):
    from brindle.pro import auth

    stub = Stub(200, SPEND)
    monkeypatch.setattr(auth, "authed", stub)
    org = watch_costs.fetch_spend("org_1", client=object(), store=object())
    assert stub.calls == [("GET", "/orgs/org_1/spend")]
    # cap: $50 a seat over 3 seats; one seat ($60) is over its budget
    assert org == Org(True, 120.5, 150.0, [("eng", 90.0, 200.0), ("ops", 10.0, None)], 1, 4.5)


def test_fetch_spend_is_silent_on_403_and_errors(monkeypatch):
    from brindle.pro import auth

    monkeypatch.setattr(auth, "authed", Stub(403, {"error": "forbidden"}))
    assert watch_costs.fetch_spend("org_1", client=object(), store=object()) is None

    def boom(*a, **kw):
        raise OSError("offline")

    monkeypatch.setattr(auth, "authed", boom)
    assert watch_costs.fetch_spend("org_1", client=object(), store=object()) is None


def _ent(role):
    class Ent:
        features = frozenset({"org_budgets", "team"})
        org_id = "org_1"
    Ent.role = role
    return Ent


def test_admin_calls_spend_and_member_never_does(monkeypatch):
    from brindle.pro import license

    calls = []
    monkeypatch.setattr(watch_costs, "fetch_spend",
                        lambda org_id, **kw: calls.append(org_id) or Org(True, 1.0))
    limits = budget.Limits(month_usd=50.0, seat_spent_usd=7.0, org=True)
    monkeypatch.setattr(budget, "limits", lambda cfg, root=None: limits)
    monkeypatch.setattr(budget, "org_limits", lambda root: (_ for _ in ()).throw(AssertionError("twice")))
    monkeypatch.setattr("brindle.config.load_repo_config", lambda root: object())

    for role in ("owner", "admin"):
        monkeypatch.setattr(license, "current", lambda **kw: _ent(role))
        assert watch_costs.collect("/r").org == Org(True, 1.0)
    assert calls == ["org_1", "org_1"]

    monkeypatch.setattr(license, "current", lambda **kw: _ent("member"))
    assert watch_costs.collect("/r").org == Org(False, 7.0, 50.0)
    assert calls == ["org_1", "org_1"]


@pytest.mark.variant("a signed-out cost snapshot must survive license lookup failure without an organization")
def test_offline_collect_has_no_org(monkeypatch):
    from brindle.pro import license

    def down(**kw):
        raise license.LicenseError("signed out")

    monkeypatch.setattr(license, "current", down)
    monkeypatch.setattr(budget, "limits", lambda cfg, root=None: None)
    monkeypatch.setattr("brindle.config.load_repo_config", lambda root: object())
    assert watch_costs.collect("/r").org is None


# -- the cache and thread ----------------------------------------------------------------------


def test_a_failing_collector_leaves_an_empty_remote():
    def bad(root):
        raise RuntimeError("x")

    feed = watch_costs.Feed("/r", collector=bad)
    feed.get()
    feed.wait()
    remote = feed.get()
    assert remote is not None and remote.org is None and remote.limits is None


def test_provider_capacity_in_sidebar():
    lines = section(Local(providers=[("agy", 0), ("claude", 64), ("codex", 97)],
                          windows={"claude": [("5h", 64), ("wk", 91), ("Fable", 19)],
                                   "codex": [("wk", 20)]}))
    body = [t.strip() for t in texts(lines)[1:]]
    assert body[0] == "agy    limit reached"
    assert body[1] == "claude 5h 36% · wk 9% · Fable 81% used"
    # past 80% used of any window, the row is bold in the warning colour
    assert [ln.style for ln in lines[1:4]] == ["alert", "alert", "normal"]
    assert "$" not in "".join(body)
    assert "█" not in "".join(body) and "░" not in "".join(body)


