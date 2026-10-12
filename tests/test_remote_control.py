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


# -- pause --------------------------------------------------------------------------------------------


def test_a_throttle_is_acked_once_with_no_workers_stopped(rollout_on, org, db, worker):
    stopped, _ = worker
    poll(org, control=control("throttle", cid="t1", throttle={"max_parallel_workers": 1}))
    rollout.sweep(db)
    rollout.sweep(db)
    acks = [c[2] for c in org.calls if c[1].endswith("/status/ack")]
    assert len(acks) == 1 and acks[0]["control_id"] == "t1" and acks[0]["stopped_workers"] == 0
    assert not stopped


def test_the_sweep_stops_workers_when_the_cached_policy_says_paused(org, db, worker, monkeypatch):
    stopped, _ = worker
    monkeypatch.setattr(license, "has", lambda feature: False)
    team_policy.save_cached(team_policy.parse_policy(ORG, {
        "org_id": ORG, "version": 2, "policy": {},
        "effective": {"paused": True, "paused_reason": "budget review"}}))
    poll(org)                                    # the status poll itself says: not paused
    assert rollout.sweep(db) == ["stopped worker w1: member paused"] and stopped == ["w1"]
    assert "budget review" in status.block_reason()


# -- throttle -------------------------------------------------------------------------------------------


def throttle(**t):
    return control("throttle", throttle=t)


def test_the_policy_plugin_enforces_the_throttle(rollout_on, org, monkeypatch):
    p = policy_of(allowed_models=["opus", "haiku"])
    monkeypatch.setattr(team_policy, "current_policy", lambda *a, **k: p)
    poll(org, control=throttle(allowed_models=["haiku"], max_parallel_workers=1))
    got = pro_policy().policy()
    assert got.allowed_models == ("haiku",) and got.max_parallel_workers == 1
    d = pro_policy().check_assign(assign())          # model "m" is not allowed
    assert not d.allowed
