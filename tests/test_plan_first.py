"""plan_first workers: propose a plan with submit_plan, wait for the
supervisor's approve_plan, and can't edit files (Claude's PreToolUse hook)
until it's approved."""

import time

import pytest

from brindle import agents, autopilot, mcp_server, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent, Autopilot
from brindle.providers import get_provider


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None,
               plan_first=False, **kw):
    """Stands in for agents.spawn: records a worker without launching a CLI."""
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    if plan_first:
        db.update_agent(a.id, plan_first=1)
    return a


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"pipeline": false}')
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(agents, "send_message",
                        lambda db, to, body, sender_id=None: out.append((to, body, sender_id)) or "ok")
    return out


def planner(db, ws, parent="boss", **kw):
    a = Agent("w1", ws.id, "developer", "claude", parent, "assign", "processing", "", None,
              time.time())
    db.add_agent(a)
    db.update_agent("w1", plan_first=1, **kw)  # add_agent doesn't write these columns
    return db.get_agent("w1")


# -- assign / config ----------------------------------------------------------


# -- the prompt ---------------------------------------------------------------


def test_prompt_mentions_submit_plan_only_when_plan_first(db, repo, boss):
    ws = boss
    p = get_provider("claude")
    plain = agents.decorate_worker_prompt("do it", "abc", ws, None, p, True)
    plan = agents.decorate_worker_prompt("do it", "abc", ws, None, p, True, plan_first=True)
    assert "submit_plan" not in plain
    assert "submit_plan" in plan and "before you edit" in plan


# -- submit_plan / approve_plan ------------------------------------------------


def test_submit_plan_needs_a_plan_first_worker(db, repo, boss, sent):
    db.add_agent(Agent("w2", boss.id, "developer", "claude", "boss", "assign", "processing", "",
                       None, time.time()))
    assert "isn't plan-first" in agents.submit_plan(db, "w2", "x")
    assert not sent


def test_approve_plan_can_ask_for_a_revision(db, repo, boss, sent):
    planner(db, boss, plan_state="proposed")
    agents.approve_plan(db, "boss", "w1", "split it up", approved=False)
    assert db.get_agent("w1").plan_state == "revise"
    assert "revise" in sent[0][1] and "split it up" in sent[0][1]


def test_only_the_parent_may_approve(db, repo, boss, sent):
    planner(db, boss, plan_state="proposed")
    with pytest.raises(agents.AgentError, match="supervisor"):
        agents.approve_plan(db, "someone-else", "w1")
    with pytest.raises(agents.AgentError, match="supervisor"):
        agents.approve_plan(db, None, "w1")
    assert db.get_agent("w1").plan_state == "proposed"
    assert not sent
    # and there must be a proposed plan to approve
    db.update_agent("w1", plan_state=None)
    with pytest.raises(agents.AgentError, match="no plan awaiting"):
        agents.approve_plan(db, "boss", "w1")


def test_plan_approval_reminds_to_split(db, repo, boss, monkeypatch, sent):
    planner(db, boss, plan_state="proposed")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "w1")
    assert "supervisor" in mcp_server.approve_plan("w1")
    for delegation, expected in (("fast", True), ("balanced", True), ("conservative", False)):
        (repo / ".brindle" / "config.json").write_text(
            f'{{"pipeline": false, "delegation": "{delegation}"}}'
        )
        assert "Plan sent" in mcp_server.submit_plan("again")
        assert (autopilot.PLAN_SPLIT_REMINDER in sent[-1][1]) is expected
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    assert "Approved" in mcp_server.approve_plan("w1")


# -- enforcement ---------------------------------------------------------------


@pytest.mark.parametrize("state,tool", [("revise", "NotebookEdit")])
def test_hook_denies_edits_until_approved(db, repo, boss, tool, state):
    a = planner(db, boss, plan_state=state)
    out = agents.pre_tool_decision(db, a, {"tool_name": tool, "tool_input": {}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "submit_plan" in out["hookSpecificOutput"]["permissionDecisionReason"]


# -- autopilot -----------------------------------------------------------------


def test_nudge_mentions_plans_awaiting_approval(db, repo, boss):
    a = planner(db, boss, plan_state="proposed")
    ap = Autopilot("boss", 1, "goal", None, "running", None, 0, 0, None, time.time())
    text = autopilot.nudge(db, ap, RepoConfig(), [a], [])
    assert "Plans awaiting your approval: w1" in text and "approve_plan" in text
