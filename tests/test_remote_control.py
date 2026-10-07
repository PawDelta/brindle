"""Remote control (Enterprise managed_rollout) and a member's pause: running workers
are stopped through rollout.sweep, new ones refused, one ack per control, auto-resume
at `until`, and a throttle narrows the effective policy."""
import time

import pytest
from org_status_fixtures import ORG, control, org, policy_of, poller, rollout_on  # noqa: F401

from brindle import agents, workspaces
from brindle.db import Agent
from brindle.policy import AssignInfo
from brindle.pro import license, rollout, status, team_policy
from brindle.pro.team_policy import ProPolicy


@pytest.fixture
def worker(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing",
                       "@1", None, time.time()))
    stopped, sent = [], []
    monkeypatch.setattr(agents, "runs_process", lambda a: True)
    monkeypatch.setattr(agents, "pause_worker", lambda db, a: (stopped.append(a.id),
                                                               db.set_status(a.id, "paused")))
    monkeypatch.setattr(agents, "send_message",
                        lambda db, to, body, sender_id=None, person=False: sent.append((to, body)))
    monkeypatch.setattr(rollout, "current", lambda root=None: policy_of())
    return stopped, sent


def poll(org_, **body):
    """The server says so; one poll."""
    org_.body = {"policy_version": 1, **body}
    poller().poll_once()


def pro_policy():
    return ProPolicy("/work/repo", entitlement=lambda: type(
        "Ent", (), {"features": ["team"], "org_id": ORG, "role": "member", "policy_role": None,
                    "policy_version": 1})())


def assign():
    return AssignInfo(repo_root="/work/repo", task="t", profile="developer", provider="claude",
                      model="m", actor="user", running_workers=0)


# -- shutdown -----------------------------------------------------------------------------------------


def test_a_shutdown_stops_running_workers_and_keeps_their_work(rollout_on, org, db, worker):
    stopped, sent = worker
    poll(org, control=control("shutdown", reason="security incident"))
    assert rollout.sweep(db) == ["stopped worker w1: remote shutdown"]
    assert stopped == ["w1"]
    assert sent[0][0] == "boss" and "shut down by your org: security incident" in sent[0][1]
    assert "worktree and branch are kept" in sent[0][1]


def test_the_shutdown_is_acked_once_with_the_stopped_count(rollout_on, org, db, worker):
    poll(org, control=control("shutdown", cid="c7"))
    rollout.sweep(db)
    rollout.sweep(db)
    rollout.sweep(db)
    acks = [c for c in org.calls if c[1].endswith("/status/ack")]
    assert len(acks) == 1
    assert acks[0][2]["control_id"] == "c7" and acks[0][2]["stopped_workers"] == 1
    assert acks[0][2]["session"]


def test_a_failed_ack_is_retried_and_counts_add_up(rollout_on, org, db, worker):
    poll(org, control=control("shutdown", cid="c8"))
    org.fail = OSError("offline")
    rollout.sweep(db)
    assert "c8" not in status.load().acked and status.load().pending == {"c8": 1}
    org.fail = None
    rollout.sweep(db)
    acks = [c for c in org.calls if c[1].endswith("/status/ack") and c[2]]
    assert acks and acks[-1][2]["stopped_workers"] == 1
    assert status.load().acked == ["c8"] and status.load().pending == {}


def test_a_new_control_is_acked_again(rollout_on, org, db, worker):
    poll(org, control=control("shutdown", cid="a"))
    rollout.sweep(db)
    poll(org, control=control("shutdown", cid="b"))
    rollout.sweep(db)
    assert [c[2]["control_id"] for c in org.calls if c[1].endswith("/ack")] == ["a", "b"]


def test_new_reviewers_are_refused(rollout_on, org, db, repo, worker):
    poll(org, control=control("shutdown", reason="maintenance"))
    assert "shut down by your org: maintenance" in rollout.kill_switch_reason("/work/repo")
    ws = workspaces.adopt_root(db, str(repo))
    with pytest.raises(agents.AgentError, match="shut down by your org: maintenance"):
        agents.request_review(db, None, ws)


def test_check_assign_denies_with_the_reason(rollout_on, org, monkeypatch):
    p = policy_of()
    monkeypatch.setattr(team_policy, "current_policy", lambda *a, **k: p)
    poll(org, control=control("shutdown", reason="maintenance"))
    d = pro_policy().check_assign(assign())
    assert not d.allowed and "shut down by your org: maintenance" in d.reason


def test_without_managed_rollout_a_shutdown_is_ignored(org, db, worker, monkeypatch):
    stopped, _ = worker
    monkeypatch.setattr(license, "has", lambda feature: False)
    poll(org, control=control("shutdown"))
    assert rollout.sweep(db) == [] and not stopped
    assert rollout.kill_switch_reason("/work/repo") is None


def test_auto_resume_at_until(rollout_on, org, db, worker, monkeypatch):
    stopped, _ = worker
    soon = time.time() + 3600
    poll(org, control=control("shutdown", until=soon))
    assert status.block_reason(now=soon - 1)
    assert status.block_reason(now=soon + 1) is None
    # the sweep no longer stops anything once `until` has passed
    monkeypatch.setattr(time, "time", lambda: soon + 5)
    assert rollout.sweep(db, soon + 5) == [] and not stopped


def test_the_server_dropping_the_control_resumes_too(rollout_on, org):
    poll(org, control=control("shutdown"))
    assert status.block_reason()
    poll(org)
    assert status.block_reason() is None


# -- pause --------------------------------------------------------------------------------------------


def test_a_paused_member_has_running_workers_stopped_without_managed_rollout(org, db, worker,
                                                                           monkeypatch):
    stopped, sent = worker
    monkeypatch.setattr(license, "has", lambda feature: False)
    poll(org, paused=True, paused_reason="over budget")
    assert rollout.sweep(db) == ["stopped worker w1: member paused"]
    assert stopped == ["w1"] and "over budget" in sent[0][1]
    assert "paused by your org: over budget" in status.block_reason()


def test_resuming_lifts_the_pause(org):
    poll(org, paused=True, paused_reason="x")
    assert status.block_reason()
    poll(org, paused=False)
    assert status.block_reason() is None


# -- throttle -------------------------------------------------------------------------------------------


def throttle(**t):
    return control("throttle", throttle=t)


def test_a_throttle_narrows_the_effective_policy(rollout_on, org):
    base = policy_of(allowed_models=["opus", "sonnet", "haiku"], max_parallel_workers=8,
                     budget={"seat_month_usd": 100, "goal_usd": 50})
    poll(org, control=throttle(allowed_models=["sonnet", "haiku", "other"], max_parallel_workers=2,
                               budget={"seat_month_usd": 30, "task_usd": 1}))
    p = status.narrow(base.enforced)
    assert p.allowed_models == ("sonnet", "haiku")
    assert p.max_parallel_workers == 2
    assert (p.budget_seat_month_usd, p.budget_goal_usd, p.budget_task_usd) == (30.0, 50.0, 1.0)


def test_a_throttle_never_widens(rollout_on, org):
    base = policy_of(allowed_models=["haiku"], max_parallel_workers=1,
                     budget={"seat_month_usd": 10})
    poll(org, control=throttle(allowed_models=["opus", "haiku"], max_parallel_workers=9,
                               budget={"seat_month_usd": 500}))
    p = status.narrow(base.enforced)
    assert p.allowed_models == ("haiku",) and p.max_parallel_workers == 1
    assert p.budget_seat_month_usd == 10.0


def test_a_throttle_limits_an_open_policy(rollout_on, org):
    poll(org, control=throttle(allowed_models=["haiku"]))
    assert status.narrow(policy_of()).allowed_models == ("haiku",)


def test_the_policy_plugin_enforces_the_throttle(rollout_on, org, monkeypatch):
    p = policy_of(allowed_models=["opus", "haiku"])
    monkeypatch.setattr(team_policy, "current_policy", lambda *a, **k: p)
    poll(org, control=throttle(allowed_models=["haiku"], max_parallel_workers=1))
    got = pro_policy().policy()
    assert got.allowed_models == ("haiku",) and got.max_parallel_workers == 1
    d = pro_policy().check_assign(assign())          # model "m" is not allowed
    assert not d.allowed


def test_throttle_ends_at_until_and_without_the_feature(rollout_on, org, monkeypatch):
    base = policy_of(allowed_models=["opus", "haiku"])
    poll(org, control=control("throttle", until=time.time() + 60, throttle={"allowed_models": ["haiku"]}))
    assert status.narrow(base, now=time.time() + 120).allowed_models == ("opus", "haiku")
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert status.narrow(base).allowed_models == ("opus", "haiku")
