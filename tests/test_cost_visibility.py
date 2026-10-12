"""The org's ``visibility.own_dollars`` setting: when false, this person's org spend,
limit and remaining budget show as a status word, never dollars; enforcement is the same."""
import json
import time

import pytest
from org_status_fixtures import ORG

from brindle import agents, budget, cost, watch_costs
from brindle.db import Agent
from brindle.pro import license, team_policy
from brindle import workspaces

LIMIT = 100.0
PRICES = {"big-model": {"input": 10, "output": 50, "cache_write": 12, "cache_read": 1}}


def policy(spent=0.0, visibility="absent", **budget_kw):
    body = {"org_id": ORG, "version": 1,
            "policy": {"budget": {"seat_month_usd": LIMIT, **budget_kw}},
            "spend": {"month": time.strftime("%Y-%m", time.gmtime()), "seat_usd": spent}}
    if visibility != "absent":
        body["visibility"] = visibility
    return team_policy.parse_policy(ORG, body)


def hidden(**kw):
    return {"own_dollars": False, "team_spend": True, "org_spend": True, **kw}


@pytest.fixture
def org(monkeypatch):
    """Install ``policy(...)`` as the org policy ``budget`` reads."""
    def install(p):
        monkeypatch.setattr(team_policy, "org_budgets", lambda root: p)
        monkeypatch.setattr(budget, "entitled", lambda: True)
        monkeypatch.setattr(budget, "_approved_month", lambda: 0.0)
        return p
    return install


class Cfg:
    budget = None


# -- parsing ------------------------------------------------------------------------------------


def test_own_dollars_false_is_parsed_and_survives_the_cache(org):
    p = policy(visibility=hidden())
    assert p.visibility["own_dollars"] is False and p.enforced.visibility["own_dollars"] is False
    q = team_policy.parse_policy(ORG, p.to_json())
    assert q.visibility["own_dollars"] is False


# -- each surface -------------------------------------------------------------------------------


def watch_text(lim):
    lines = watch_costs.render(watch_costs.Local(month=0.0), watch_costs.Remote(limits=lim),
                               time.time(), 60, False)
    return "\n".join(line.text for line in lines)


def test_the_watch_pane_and_cost_report_show_only_a_status(org):
    org(policy(spent=0, visibility=hidden()))
    text = watch_text(budget.limits(Cfg(), "/r"))
    assert "within limit" in text and "$" not in text
    org(policy(spent=100, visibility=hidden()))
    assert cost.org_budget_line("/r") == "Org budget this month: at limit"


def test_the_cost_report_line_shows_dollars_when_the_field_is_missing(org):
    org(policy(spent=85))
    assert cost.org_budget_line("/r") == "Org budget this month: $85.00 of $100.00"


# -- routing refusals and worker notes (fixtures as in test_budgets) ------------------------------


def make_profile(repo, name, model):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: t\nprovider: claude\nmodel: {model}\n---\nGo.\n")


@pytest.fixture
def gate_env(db, repo, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")
    make_profile(repo, "big", "big-model")
    (repo / ".brindle" / "config.json").write_text(json.dumps({"pricing": PRICES}))
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    return ws


def test_the_task_budget_still_stops_workers_and_the_notes_have_no_dollars(db, repo, gate_env, org,
                                                                           monkeypatch):
    org(policy(visibility=hidden(), task_usd=10))
    db.add_agent(Agent("w1", gate_env.id, "big", "claude", "boss", "assign", "processing", "", None,
                       time.time()))
    db.add_routing_decision(str(repo), task_id="t1", agent_id="w1", weight="medium",
                            baseline_profile="big", profile="big", learned=False)
    sent, closed = [], []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, *a, **kw: sent.append(body))
    monkeypatch.setattr(agents, "close", lambda db, aid, panes=None: closed.append(aid))
    monkeypatch.setattr(budget, "worker_spend", lambda db, agent, root: 8.5)
    assert len(budget.sweep(db)) == 1 and "getting close" in sent[-1]
    monkeypatch.setattr(budget, "worker_spend", lambda db, agent, root: 10.5)
    assert len(budget.sweep(db)) == 1 and "at limit" in sent[-1]
    assert all("$" not in s for s in sent)


# -- the org line and personal limits -------------------------------------------------------------


def member(monkeypatch, role="member"):
    from types import SimpleNamespace

    monkeypatch.setattr(license, "current", lambda refresh=False: SimpleNamespace(
        features={team_policy.BUDGETS_FEATURE}, role=role, org_id=ORG))


def test_a_member_whose_org_hides_dollars_gets_no_org_line(org, monkeypatch):
    member(monkeypatch)
    org(policy(spent=40, visibility=hidden()))
    assert watch_costs._org("/r", budget.limits(Cfg(), "/r")) is None


def test_a_member_whose_org_shows_dollars_still_gets_the_org_line(org, monkeypatch):
    member(monkeypatch)
    org(policy(spent=40))
    got = watch_costs._org("/r", budget.limits(Cfg(), "/r"))
    assert got == watch_costs.Org(False, 40.0, LIMIT)


class Personal:
    def __init__(self, **kw):
        self.budget = kw


def test_the_orgs_limit_is_still_hidden_when_a_looser_personal_one_is_set(db, repo, gate_env, org):
    org(policy(spent=100, visibility=hidden()))
    why = budget.Gate(db, Personal(month_usd=500), str(repo), "boss").why_not("big", "medium")
    assert why and "$" not in why and "at limit" in why
