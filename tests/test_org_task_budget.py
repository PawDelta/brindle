"""Per-member budgets in team_policy and budget.limits: the org's task_usd and the
member's effective budget, allowed_models, max_parallel_workers and pause."""
import pytest
from org_status_fixtures import ORG, policy_of

from brindle import budget
from brindle.pro import license, team_policy
from brindle.pro.team_policy import PolicyUnavailable, parse_policy


def test_the_members_effective_policy_carries_their_own_values():
    body = {"org_id": ORG, "version": 3, "role": "member",
            "policy": {"budget": {"seat_month_usd": 100, "task_usd": 10},
                       "allowed_models": ["a", "b"], "max_parallel_workers": 4,
                       "member_overrides": {"user_9": {"paused": True}}},
            "effective": {"budget": {"seat_month_usd": 40, "task_usd": 2},
                          "allowed_models": ["a"], "max_parallel_workers": 1,
                          "paused": True, "paused_reason": "over budget",
                          "budget_sources": {"seat_month_usd": "member", "task_usd": "role"}}}
    p = parse_policy(ORG, body)
    assert p.member_overrides == {"user_9": {"paused": True}}
    e = p.enforced
    assert (e.budget_seat_month_usd, e.budget_task_usd) == (40.0, 2.0)
    assert e.allowed_models == ("a",) and e.max_parallel_workers == 1
    assert e.paused and e.paused_reason == "over budget"
    assert e.budget_sources == {"seat_month_usd": "member", "task_usd": "role"}


@pytest.mark.parametrize("over", [{"paused": "yes"}, {"budget_sources": []}])
def test_malformed_member_fields_make_the_policy_unusable(over):
    with pytest.raises(PolicyUnavailable):
        policy_of(**over)


class Cfg:
    budget = None


def org_limits_of(monkeypatch, **over):
    p = policy_of(**over)
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: p)
    monkeypatch.setattr(budget, "entitled", lambda: True)
    monkeypatch.setattr(budget, "_approved_month", lambda: 0.0)
    return p


def test_limits_take_the_org_task_budget(monkeypatch):
    org_limits_of(monkeypatch, budget={"task_usd": 5})
    lim = budget.limits(Cfg(), "/r")
    assert lim.task_usd == 5.0 and lim.org


def test_a_paused_member_gets_no_new_worker(monkeypatch):
    from types import SimpleNamespace

    from brindle.policy import AssignInfo
    from brindle.pro import status

    p = parse_policy(ORG, {"org_id": ORG, "version": 1, "policy": {},
                           "effective": {"paused": True, "paused_reason": "over budget"}})
    monkeypatch.setattr(team_policy, "current_policy", lambda *a, **k: p)
    monkeypatch.setattr(license, "has", lambda f: False)
    monkeypatch.setattr(status, "_org_id", lambda: None)
    pol = team_policy.ProPolicy("/r", entitlement=lambda: SimpleNamespace(
        features=["team"], org_id=ORG, role="member", policy_role=None))
    d = pol.check_assign(AssignInfo(repo_root="/r", task="t", profile="d", provider="claude",
                                    model="m", actor="user", running_workers=0))
    assert not d.allowed and "paused" in d.reason and "over budget" in d.reason
