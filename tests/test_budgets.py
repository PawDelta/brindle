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


# -- routing ----------------------------------------------------------------------------------------


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


def test_a_broken_license_fails_closed(db, repo, boss, monkeypatch):
    config(repo, budget={"task_usd": 0.1})

    def broken(feature):
        raise RuntimeError("no license store")

    monkeypatch.setattr(license, "has", broken)
    assert choose(db, repo)[0] == "big"
    assert budget.entitled() is False


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


def test_sweep_does_nothing_without_the_feature(db, repo, worker, monkeypatch):
    sent, closed = worker
    config(repo, budget={"task_usd": 1, "stop": True})
    spend(db, repo, 5, agent="w1")
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert budget.sweep(db) == [] and not sent and not closed


# -- fail closed (security review) --------------------------------------------------------------------


def gate(db, repo):
    return budget.Gate(db, load_repo_config(repo), str(repo), "boss")


def test_a_goal_whose_spend_cant_be_totalled_fails_the_goal_budget(db, repo, boss, monkeypatch):
    from brindle import cost

    config(repo, budget={"goal_usd": 1000})

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(cost, "priced_rows", boom)
    assert "can't be totalled" in gate(db, repo).why_not("small", "medium")
