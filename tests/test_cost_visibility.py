"""The org's ``visibility.own_dollars`` setting: when false, this person's org spend,
limit and remaining budget show as a status word, never dollars; enforcement is the same."""
import json
import time

import pytest
from org_status_fixtures import ORG

from brindle import agents, budget, cost, watch_costs
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.pro import license, team_policy
from brindle import workspaces

LIMIT = 100.0
PRICES = {"big-model": {"input": 10, "output": 50, "cache_write": 12, "cache_read": 1}}
WORDS = {0: "within limit", 85: "getting close", 100: "at limit"}


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


def test_a_missing_field_shows_dollars(org):
    p = policy()
    assert p.visibility == {"own_dollars": True, "team_spend": True, "org_spend": True}
    org(p)
    assert budget.limits(Cfg(), "/r").hide_dollars is False


def test_own_dollars_false_is_parsed_and_survives_the_cache(org):
    p = policy(visibility=hidden())
    assert p.visibility["own_dollars"] is False and p.enforced.visibility["own_dollars"] is False
    q = team_policy.parse_policy(ORG, p.to_json())
    assert q.visibility["own_dollars"] is False


def test_a_missing_key_is_true_and_a_malformed_value_hides():
    assert policy(visibility={"team_spend": False}).visibility == {
        "own_dollars": True, "team_spend": False, "org_spend": True}
    assert policy(visibility={"own_dollars": "yes"}).visibility["own_dollars"] is False


def test_the_effective_policy_carries_it(org):
    body = {"org_id": ORG, "version": 1, "policy": {"budget": {"seat_month_usd": 50}},
            "effective": {"budget": {"seat_month_usd": LIMIT}}, "visibility": hidden()}
    p = team_policy.parse_policy(ORG, body)
    assert p.enforced.visibility["own_dollars"] is False


# -- the status word ----------------------------------------------------------------------------


@pytest.mark.parametrize("pct,word", WORDS.items())
def test_status_word(pct, word):
    assert budget.status_word(pct, LIMIT) == word
    assert budget.status_word(pct + 20 if pct == 100 else pct, LIMIT) == word


def test_status_word_at_the_edges():
    assert budget.status_word(79.99, LIMIT) == "within limit"
    assert budget.status_word(80, LIMIT) == "getting close"
    assert budget.status_word(0, 0) == "at limit"


# -- each surface -------------------------------------------------------------------------------


def watch_text(lim):
    lines = watch_costs.render(watch_costs.Local(month=0.0), watch_costs.Remote(limits=lim),
                               time.time(), 60, False)
    return "\n".join(line.text for line in lines)


def test_the_watch_pane_shows_dollars_when_the_field_is_missing(org):
    org(policy(spent=85))
    text = watch_text(budget.limits(Cfg(), "/r"))
    assert "$85.00 of $100.00" in text


@pytest.mark.parametrize("pct,word", WORDS.items())
def test_the_watch_pane_shows_only_a_status(org, pct, word):
    org(policy(spent=pct, visibility=hidden()))
    text = watch_text(budget.limits(Cfg(), "/r"))
    assert word in text and "$" not in text


def test_the_cost_report_line_shows_dollars_when_the_field_is_missing(org):
    org(policy(spent=85))
    assert cost.org_budget_line("/r") == "Org budget this month: $85.00 of $100.00"


@pytest.mark.parametrize("pct,word", WORDS.items())
def test_the_cost_report_shows_only_a_status(org, pct, word):
    org(policy(spent=pct, visibility=hidden()))
    line = cost.org_budget_line("/r")
    assert line == f"Org budget this month: {word}" and "$" not in line


def test_the_cost_report_is_unchanged_without_an_org(monkeypatch):
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: None)
    assert cost.org_budget_line("/r") is None


def test_the_status_bar_never_shows_a_members_dollars():
    from brindle.pro import status

    st = status.parse({"policy_version": 1, "paused": False,
                       "seat": {"spent_usd": 90, "budget_usd": 100, "budget_source": "member"}})
    text, _ = status.bar_text(status.Saved(status=st))
    assert "$" not in text


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


def test_enforcement_is_unchanged_but_the_reason_has_no_dollars(db, repo, gate_env, org):
    # a typical "big" task costs about $4.60: with $100 spent of $100, it must be refused
    org(policy(spent=100, visibility=hidden()))
    why = budget.Gate(db, load_repo_config(repo), str(repo), "boss").why_not("big", "medium")
    assert why and "$" not in why and "at limit" in why


def test_enforcement_shows_dollars_when_the_field_is_missing(db, repo, gate_env, org):
    org(policy(spent=100))
    why = budget.Gate(db, load_repo_config(repo), str(repo), "boss").why_not("big", "medium")
    assert why and "$100.00 budget" in why


def test_a_fitting_task_is_not_refused_either_way(db, repo, gate_env, org):
    org(policy(spent=0, visibility=hidden()))
    assert budget.Gate(db, load_repo_config(repo), str(repo), "boss").why_not("big", "medium") is None


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
