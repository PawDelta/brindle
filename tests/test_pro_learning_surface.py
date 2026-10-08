"""Pro's learners are visible: approvals are recorded by default, get_progress
shows what they learned, and assign/handoff carry the brief warning."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from brindle import agents, mcp_server, permissions, quota, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.pro import brief_learning, license

ASK = {"tool_name": "Bash", "tool_input": {"command": "terraform apply"}, "tool_use_id": "t1"}


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "learned_rules")


@pytest.fixture
def worker(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    agent = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(agent)
    return agent


def approve(db, agent, payload=ASK):
    """The hook leaves ``payload`` to the person, who approves it."""
    decision = agents.permission_request_decision(db, agent, payload)
    agents.note_permission_outcome(db, agent, payload)
    return decision


# -- the default policy ------------------------------------------------------------------------


def test_pro_defaults_to_recording_approvals(db, repo, worker, pro):
    assert load_repo_config(str(repo)).permission_policy == "on"
    approve(db, worker)
    [entry] = permissions.load_store().approvals.values()
    assert (entry["kind"], entry["match"], entry["count"]) == ("bash", "terraform apply", 1)


def test_free_records_nothing(db, repo, worker, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert load_repo_config(str(repo)).permission_policy == "off"
    assert approve(db, worker) is None
    assert permissions.load_store().approvals == {}


def test_pro_can_still_turn_it_off(db, repo, worker, pro):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"permission_policy": "off"}))
    assert load_repo_config(str(repo)).permission_policy == "off"
    approve(db, worker)
    assert permissions.load_store().approvals == {}


# -- get_progress -------------------------------------------------------------------------------


def progress(monkeypatch, repo):
    monkeypatch.setattr(mcp_server, "_session", lambda db: ("root", SimpleNamespace(repo_root=str(repo))))
    monkeypatch.setattr(mcp_server.autopilot, "progress", lambda db, root: "Autopilot on.")
    monkeypatch.setattr(quota, "notes", lambda repo_root: [])
    return mcp_server.get_progress()


def test_get_progress_shows_permission_suggestions_and_savings(db, repo, monkeypatch, pro):
    req = permissions.from_claude(ASK)
    for _ in range(permissions.SUGGEST_AFTER):
        permissions.record_approval(req)
    text = progress(monkeypatch, repo)
    assert "1 permission rule suggested" in text and "brindle permissions suggestions" in text
    assert "Learning" in text and "brindle account savings" in text


def test_get_progress_shows_neither_on_free(db, repo, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)
    req = permissions.from_claude(ASK)
    for _ in range(permissions.SUGGEST_AFTER):
        permissions.record_approval(req)
    text = progress(monkeypatch, repo)
    assert "permission rule" not in text and "Learning" not in text


# -- assign and handoff -------------------------------------------------------------------------


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"pipeline": False}))
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)

    def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
        a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
                  None, time.time(), task=prompt, done_when=done_when)
        db.add_agent(a)
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    return ws


WARNING = "Briefs with no weight merged cleanly 1 of 5 times here; set one."


def test_assign_carries_the_brief_warning(db, repo, boss, monkeypatch):
    calls = []

    def warn(db, repo_root, task, done_when, files, weight):
        calls.append((task, done_when, files, weight))
        return WARNING

    monkeypatch.setattr(brief_learning, "warning", warn)
    reply = asyncio.run(mcp_server.assign(task="do A", branch="feat-a", done_when="tests pass",
                                          files=["a.py"], weight="medium"))
    assert f"Warning: {WARNING}" in reply
    assert calls == [("do A", "tests pass", ["a.py"], "medium")]


def test_assign_adds_nothing_when_the_learner_is_quiet(db, repo, boss, monkeypatch):
    monkeypatch.setattr(brief_learning, "warning", lambda *a, **k: None)
    reply = asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="medium"))
    assert reply.startswith("Started worker") and "Briefs" not in reply


def test_handoff_carries_the_brief_warning(db, repo, boss, monkeypatch):
    monkeypatch.setattr(brief_learning, "warning", lambda *a, **k: WARNING)
    monkeypatch.setattr(mcp_server, "_await_worker", lambda db, agent_id, wait: "still running")
    reply = asyncio.run(mcp_server.handoff(task="do A", branch="feat-a", weight="medium"))
    assert f"Warning: {WARNING}" in reply
