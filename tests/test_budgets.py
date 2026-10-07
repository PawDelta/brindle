"""Dollar budgets: routing demotes or refuses, running workers are warned or stopped,
and none of it happens without the Pro `cost` feature."""
import json
import time

import pytest

from brindle import agents, autopilot, budget, quota, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.pro import license

PRICES = {"big-model": {"input": 10, "output": 50, "cache_write": 12, "cache_read": 1},
          "small-model": {"input": 1, "output": 5, "cache_write": 1.2, "cache_read": 0.1}}
# a typical task at those prices (budget.TYPICAL): big ~ $4.60, small ~ $0.46


@pytest.fixture(autouse=True)
def everything_available(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)


@pytest.fixture(autouse=True)
def pro(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")


def profile(repo, name, model):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: t\nprovider: claude\nmodel: {model}\n---\nGo.\n")


def config(repo, **kw):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"pricing": PRICES, "routing": {"medium": ["big", "small"]}, **kw}))


@pytest.fixture
def boss(db, repo):
    profile(repo, "big", "big-model")
    profile(repo, "small", "small-model")
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_autopilot("boss")
    db.update_autopilot("boss", goal="Ship it", enabled=1, state="running")
    return ws


def choose(db, repo, **kw):
    why, decision = [], {}
    name, _ = autopilot.choose_profile(db, "boss", str(repo), weight="medium", why=why,
                                       decision=decision, **kw)
    return name, decision, why


def spend(db, repo, dollars, agent="x", ts=None):
    """History worth ``dollars`` at big-model's price ($10 per million input tokens)."""
    db.add_history(str(repo), "worker_result", agent_id=agent, profile="big",
                   tokens=json.dumps({"model": "big-model", "input": int(dollars * 100_000), "output": 0}))
    if ts is not None:
        db.conn.execute("UPDATE history SET ts=? WHERE agent_id=?", (ts, agent))
        db.conn.commit()


# -- config -----------------------------------------------------------------------------------------


def test_budget_merges_per_key_like_rules(repo, brindle_home):
    brindle_home.mkdir(exist_ok=True)
    (brindle_home / "config.json").write_text(json.dumps({"budget": {"task_usd": 5, "month_usd": 100}}))
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"budget": {"task_usd": 2}}))
    (repo / ".brindle" / "config.local.json").write_text(json.dumps({"budget": {"goal_usd": 20, "stop": True}}))
    cfg = load_repo_config(repo)
    assert cfg.budget == {"task_usd": 2, "month_usd": 100, "goal_usd": 20, "stop": True}
    lim = budget.limits(cfg)
    assert (lim.task_usd, lim.goal_usd, lim.month_usd, lim.stop) == (2, 20, 100, True)


def test_malformed_limits_are_ignored():
    lim = budget.parse({"task_usd": "lots", "goal_usd": -1, "month_usd": True, "stop": "yes"})
    assert not lim.any() and lim.stop is False


# -- routing ----------------------------------------------------------------------------------------


def test_no_budget_routes_as_before(db, repo, boss):
    config(repo)
    assert choose(db, repo)[0] == "big"


def test_over_the_task_budget_demotes_to_the_cheaper_candidate(db, repo, boss):
    config(repo, budget={"task_usd": 1})
    name, decision, why = choose(db, repo)
    assert name == "small" and decision["demoted_from"] == "big"
    assert "over budget, skipped big" in why[0]


def test_a_budget_that_fits_changes_nothing(db, repo, boss):
    config(repo, budget={"task_usd": 10})
    name, decision, _ = choose(db, repo)
    assert name == "big" and decision["demoted_from"] is None


def test_the_months_spend_counts_toward_month_usd(db, repo, boss):
    config(repo, budget={"month_usd": 6})
    assert choose(db, repo)[0] == "big"
    spend(db, repo, 3)
    assert choose(db, repo)[0] == "small"        # 3 + 4.6 > 6, 3 + .46 fits


def test_last_months_spend_does_not_count(db, repo, boss):
    config(repo, budget={"month_usd": 6})
    spend(db, repo, 3, ts=time.time() - 40 * 86400)
    assert choose(db, repo)[0] == "big"


def test_the_goals_spend_counts_toward_goal_usd(db, repo, boss):
    config(repo, budget={"goal_usd": 6})
    db.add_agent(Agent("w1", boss.id, "big", "claude", "boss", "assign", "processing", "", None, time.time()))
    spend(db, repo, 3, agent="w1")
    assert choose(db, repo)[0] == "small"


def test_nothing_fits_refuses_and_asks_the_user(db, repo, boss):
    config(repo, budget={"task_usd": 0.1})
    with pytest.raises(autopilot.AutopilotError) as e:
        choose(db, repo)
    assert "over budget" in str(e.value) and "Raise `budget`" in str(e.value)
    ap = db.get_autopilot("boss")
    assert ap.state == "blocked" and "over budget" in ap.note


def test_history_replaces_the_typical_estimate(db, repo, boss):
    config(repo, budget={"task_usd": 1})
    for n in range(3):    # three finished big tasks that each cost $0.50
        db.add_routing_decision(str(repo), task_id=f"t{n}", agent_id=f"h{n}", weight="medium",
                                baseline_profile="big", profile="big", learned=False)
        db.note_routing_outcome(f"h{n}", outcome="merged")
        spend(db, repo, 0.5, agent=f"h{n}")
    assert choose(db, repo)[0] == "big"


def test_without_the_cost_feature_budgets_do_nothing(db, repo, boss, monkeypatch):
    config(repo, budget={"task_usd": 0.1})
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert choose(db, repo)[0] == "big"


def test_a_broken_license_fails_closed(db, repo, boss, monkeypatch):
    config(repo, budget={"task_usd": 0.1})

    def broken(feature):
        raise RuntimeError("no license store")

    monkeypatch.setattr(license, "has", broken)
    assert choose(db, repo)[0] == "big"
    assert budget.entitled() is False


def test_an_explicit_profile_is_not_second_guessed(db, repo, boss):
    config(repo, budget={"task_usd": 0.1})
    name, _ = autopilot.choose_profile(db, "boss", str(repo), requested="big")
    assert name == "big"


def test_demotion_is_recorded_on_the_decision(db, repo, boss):
    from brindle import savings

    config(repo, budget={"task_usd": 1})
    _, decision, _ = choose(db, repo)
    savings.record(db, str(repo), decision, task_id="t1", agent_id="w1")
    row = db.routing_decision_for_agent("w1")
    assert row.profile == "small" and row.demoted_from == "big"


# -- running workers --------------------------------------------------------------------------------


@pytest.fixture
def worker(db, repo, boss, monkeypatch):
    db.add_agent(Agent("w1", boss.id, "big", "claude", "boss", "assign", "processing", "", None, time.time()))
    db.add_routing_decision(str(repo), task_id="t1", agent_id="w1", weight="medium",
                            baseline_profile="big", profile="big", learned=False)
    sent, closed = [], []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, *a, **kw: sent.append((to, body)))
    monkeypatch.setattr(agents, "close", lambda db, aid, panes=None: closed.append(aid))
    return sent, closed


def test_a_worker_is_warned_at_80_percent_once(db, repo, worker):
    sent, closed = worker
    config(repo, budget={"task_usd": 10})
    spend(db, repo, 7, agent="w1")
    assert budget.sweep(db) == []
    spend(db, repo, 1.5, agent="w1")
    assert len(budget.sweep(db)) == 1
    assert sent and sent[0][0] == "boss" and "80%" in sent[0][1]
    assert budget.sweep(db) == [] and closed == []     # not repeated, and warn-only by default


def test_going_over_warns_again_but_does_not_stop_by_default(db, repo, worker):
    sent, closed = worker
    config(repo, budget={"task_usd": 10})
    spend(db, repo, 8.5, agent="w1")
    budget.sweep(db)
    spend(db, repo, 2, agent="w1")
    assert len(budget.sweep(db)) == 1 and "task budget" in sent[-1][1]
    assert budget.sweep(db) == [] and closed == []


def test_stop_closes_the_worker_at_100_percent(db, repo, worker):
    sent, closed = worker
    config(repo, budget={"task_usd": 10, "stop": True})
    spend(db, repo, 10.5, agent="w1")
    assert len(budget.sweep(db)) == 1
    assert closed == ["w1"] and "stopped" in sent[0][1]
    assert db.routing_decision_for_agent("w1").budget_state == "stopped"
    assert budget.sweep(db) == []


def test_a_failing_close_is_retried_by_the_next_sweep(db, repo, worker, monkeypatch):
    sent, closed = worker
    config(repo, budget={"task_usd": 10, "stop": True})
    spend(db, repo, 10.5, agent="w1")

    def broken(db, aid, panes=None):
        raise RuntimeError("tmux is gone")

    monkeypatch.setattr(agents, "close", broken)
    assert budget.sweep(db) == []
    assert db.routing_decision_for_agent("w1").budget_state is None and not sent

    monkeypatch.setattr(agents, "close", lambda db, aid, panes=None: closed.append(aid))
    assert len(budget.sweep(db)) == 1
    assert closed == ["w1"]
    assert db.routing_decision_for_agent("w1").budget_state == "stopped"


def test_live_agent_decisions_exclude_done_dismissed_and_missing_agents(db, repo, boss):
    for aid, status in (("live", "processing"), ("fin", "done"), ("gone", "processing")):
        db.add_agent(Agent(aid, boss.id, "big", "claude", "boss", "assign", status, "", None, time.time()))
    db.update_agent("gone", dismissed_at=time.time())
    for aid in ("live", "fin", "gone", "ghost"):    # "ghost" has no agent row at all
        db.add_routing_decision(str(repo), task_id=f"t-{aid}", agent_id=aid, weight="medium",
                                baseline_profile="big", profile="big", learned=False)
    assert [r.agent_id for r in db.list_routing_decisions_for_live_agents()] == ["live"]


def test_sweep_does_nothing_without_the_feature(db, repo, worker, monkeypatch):
    sent, closed = worker
    config(repo, budget={"task_usd": 1, "stop": True})
    spend(db, repo, 5, agent="w1")
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert budget.sweep(db) == [] and not sent and not closed


# -- fail closed (security review) --------------------------------------------------------------------


def org_budget(monkeypatch, **kw):
    from brindle.pro import team_policy

    monkeypatch.setattr(team_policy, "org_budgets",
                        lambda root: team_policy.OrgPolicy(org_id="org_acme", version=1, **kw))


def gate(db, repo):
    return budget.Gate(db, load_repo_config(repo), str(repo), "boss")


def test_an_unpriced_profile_does_not_slip_past_an_org_budget(db, repo, boss, monkeypatch):
    profile(repo, "mystery", "no-such-model")
    config(repo)
    org_budget(monkeypatch, budget_seat_month_usd=1000.0)
    assert "no known price" in gate(db, repo).why_not("mystery", "medium")


def test_an_unpriced_profile_is_still_never_skipped_by_a_repo_budget(db, repo, boss):
    profile(repo, "mystery", "no-such-model")
    config(repo, budget={"task_usd": 1})
    assert gate(db, repo).why_not("mystery", "medium") is None


def test_a_goal_whose_spend_cant_be_totalled_fails_the_goal_budget(db, repo, boss, monkeypatch):
    from brindle import cost

    config(repo, budget={"goal_usd": 1000})

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(cost, "priced_rows", boom)
    assert "can't be totalled" in gate(db, repo).why_not("small", "medium")
