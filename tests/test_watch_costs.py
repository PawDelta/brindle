import threading
import time

from brindle import budget, watch, watch_costs
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


def test_local_spend_today_and_month():
    lines = section(Local(today=1.5, month=12.25))
    assert texts(lines)[0].startswith("▾ Costs")
    assert "today $1.50 · month $12.25" in lines[1].text
    assert len(lines) == 2


def test_worker_dollars_sit_next_to_tokens():
    snap = [ws([agent(tokens="12k tokens")])]
    lines = watch.render(snap, 1090, 80, costs=(Local(workers={"a1b2c3d4": 0.42}), None))
    row = next(t for t in texts(lines) if "12k tokens" in t)
    assert "12k tokens · $0.42" in row


def test_no_costs_means_no_section():
    assert not any("Costs" in t for t in texts(watch.render([ws([agent()])], 1090)))


def test_budget_bar_and_alert_threshold():
    below = section(Local(month=7.0), Remote(limits=budget.Limits(month_usd=10.0)))
    assert "$7.00 of $10.00" in below[2].text and below[2].style == "ok"
    assert "███████░░░" in below[2].text
    at = section(Local(month=8.0), Remote(limits=budget.Limits(month_usd=10.0)))
    assert "$8.00 of $10.00" in at[2].text and at[2].style == "alert"


def test_seat_spend_counted_by_the_org_wins_when_larger():
    lines = section(Local(month=1.0), Remote(limits=budget.Limits(month_usd=10.0, seat_spent_usd=9.0)))
    assert "$9.00 of $10.00" in lines[2].text and lines[2].style == "alert"


def test_admin_sees_org_total_and_cost_centers():
    org = Org(True, 400.0, 1000.0, [("ops", 100.0, None), ("eng", 300.0, 500.0)], seats_over=2,
              unattributed=5.0)
    lines = section(Local(month=1.0), Remote(org=org, at=970.0), now=1000.0, width=100)
    assert "org $400.00 of $1,000.00" in lines[2].text and "updated 30s ago" in lines[2].text
    assert "2 seats over" in lines[2].text and lines[2].style == "alert"
    assert lines[3].text.strip() == "eng $300.00 of $500.00 · ops $100.00 · unattributed $5.00"
    assert len(lines) == 4


def test_zero_unattributed_is_not_shown():
    lines = section(Local(), Remote(org=Org(True, 4.0, 10.0, [], 0, 0.0), at=1000.0))
    assert len(lines) == 3 and "unattributed" not in "".join(texts(lines))


def test_admin_org_alerts_at_80_percent():
    lines = section(Local(), Remote(org=Org(True, 850.0, 1000.0), at=1000.0))
    assert lines[2].style == "alert"


def test_member_sees_own_seat_not_the_org():
    remote = Remote(limits=budget.Limits(month_usd=50.0, seat_spent_usd=20.0),
                    org=Org(False, 20.0, 50.0), at=1000.0)
    text = "\n".join(texts(section(Local(month=5.0), remote)))
    assert "$20.00 of $50.00" in text
    assert "org " not in text


def test_offline_hides_the_org_line():
    lines = section(Local(today=1.0, month=2.0), Remote(at=1000.0))
    assert len(lines) == 2 and "updated" not in "".join(texts(lines))
    assert len(section(Local(), None)) == 2        # nothing fetched yet


def test_collapsed_is_one_selectable_line():
    lines = section(Local(month=3.0), collapsed=True)
    assert len(lines) == 1 and lines[0].group == watch_costs.GROUP
    assert lines[0].text.startswith("▸ Costs")


def test_costs_header_folds_with_space():
    state = watch.NavState()
    costs = (Local(month=1.0), None)
    lines = watch.render([ws([agent()])], 1090, 40, state=state, costs=costs)
    i = next(i for i, ln in enumerate(lines) if ln.group == watch_costs.GROUP)
    state.selected = f"g:{watch_costs.GROUP}"
    watch.handle_key(state, ord(" "), lines, 20)
    assert watch_costs.GROUP in state.collapsed
    folded = watch.render([ws([agent()])], 1090, 40, state=state, costs=costs)
    assert len(folded) < len(lines) and folded[i].text.startswith("▸")
    watch.handle_key(state, ord(" "), folded, 20)
    assert watch_costs.GROUP not in state.collapsed


def test_lines_fit_a_narrow_sidebar():
    org = Org(True, 400.0, 1000.0, [("engineering", 300.0, 500.0), ("operations", 100.0, None)], 1, 3.0)
    for ln in section(Local(today=1.0, month=2.0), Remote(limits=budget.Limits(month_usd=10.0),
                                                        org=org, at=1000.0), width=30):
        assert len(ln.text) <= 30


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


def test_spend_without_cost_centers_or_a_seat_budget():
    org = watch_costs.parse_spend({"org_id": "o", "month": "2026-10", "total_usd": 9.0,
                                   "seats": [{"sub": "a", "role": "member", "usd": 9.0}],
                                   "budget": {"seat_month_usd": None, "goal_usd": None}})
    assert org == Org(True, 9.0, None, [], 0, 0.0)
    assert watch_costs.parse_spend({"budget_usd": 5}) is None     # not the server's shape


def test_unattributed_is_not_a_cost_center():
    org = watch_costs.parse_spend(SPEND)
    assert [c[0] for c in org.centers] == ["eng", "ops"]
    assert org.unattributed == 4.5


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


def test_offline_collect_has_no_org(monkeypatch):
    from brindle.pro import license

    def down(**kw):
        raise license.LicenseError("signed out")

    monkeypatch.setattr(license, "current", down)
    monkeypatch.setattr(budget, "limits", lambda cfg, root=None: None)
    monkeypatch.setattr("brindle.config.load_repo_config", lambda root: object())
    assert watch_costs.collect("/r").org is None


# -- the cache and thread ----------------------------------------------------------------------


def test_feed_never_blocks_and_caches_for_30_seconds():
    gate, calls = threading.Event(), []

    def slow(root):
        calls.append(root)
        gate.wait(5)
        return Remote(at=time.time())

    feed = watch_costs.Feed("/r", collector=slow)
    t0 = time.time()
    assert feed.get() is None                     # returns at once, the fetch is in the background
    assert time.time() - t0 < 1.0
    feed.get()
    assert calls == ["/r"]                        # not started twice
    gate.set()
    feed.wait()
    first = feed.get()
    assert first is not None and calls == ["/r"]  # fresh: served from the cache
    assert feed.get(now=first.at + 29) is first and calls == ["/r"]
    feed.get(now=first.at + 31)
    feed.wait()
    assert len(calls) == 2


def test_a_failing_collector_leaves_an_empty_remote():
    def bad(root):
        raise RuntimeError("x")

    feed = watch_costs.Feed("/r", collector=bad)
    feed.get()
    feed.wait()
    remote = feed.get()
    assert remote is not None and remote.org is None and remote.limits is None
