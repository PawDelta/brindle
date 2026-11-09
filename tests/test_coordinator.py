import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from typer.testing import CliRunner

from brindle import agents, bus, cli, coordinator, knowledge, mcp_server, tasks, workspaces
from brindle.db import Agent, Task
from brindle.daemon.routes_actions import patch_config
from brindle.pro import license
from brindle.service.errors import ServiceError


@pytest.mark.parametrize("setting,features,stop,expected", [
    pytest.param(None, {coordinator.FEATURE}, False, True, id="unset-pro"),
    pytest.param(None, set(), False, False, id="unset-free"),
    pytest.param(None, RuntimeError(), False, False, id="unset-unknown"),
    pytest.param(None, license.LicenseError(), False, False, id="unset-signed-out",
                 marks=pytest.mark.variant("signed-out accounts have no stored entitlement")),
    pytest.param(False, {coordinator.FEATURE}, False, False, id="explicit-off-pro"),
    pytest.param(True, {coordinator.FEATURE}, True, False, id="stop-pro"),
])
def test_coordinator_default_for_pro(setting, features, stop, expected, db, repo, monkeypatch):
    def current(*, refresh=True):
        assert refresh is False, "the sweep must only read the cached license"
        if isinstance(features, Exception):
            raise features
        return SimpleNamespace(features=features)

    monkeypatch.setattr(license, "current", current)
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(
        json.dumps({} if setting is None else {"coordinator": setting}))
    with pytest.raises(ServiceError):
        patch_config(db, {}, {"values": {"coordinator": "false"}})
    if setting is not None:
        patch_config(db, {}, {"scope": "shared", "repo": str(repo), "values": {"coordinator": setting}})
    if stop:
        assert coordinator.enabled(str(repo))
        monkeypatch.chdir(repo)
        assert CliRunner().invoke(cli.app, ["coordinator", "stop"]).exit_code == 0
    assert coordinator.enabled(str(repo)) is expected


def _repo_with_sessions(db, repo, monkeypatch, coord=True):
    """Sessions a and b, each with a worker, and (``coord``) the coordinator."""
    ws = workspaces.adopt_root(db, str(repo))
    rows = [("a", "interactive", None, "supervisor"), ("b", "interactive", None, "supervisor"),
            ("w-a", "assign", "a", "developer"), ("w-b", "assign", "b", "developer")]
    if coord:
        rows.append(("coord", coordinator.MODE, None, coordinator.PROFILE))
    for aid, mode, parent, profile in rows:
        db.add_agent(Agent(aid, ws.id, profile, "claude", parent, mode, "processing", "", None,
                           time.time()))
    monkeypatch.setattr(license, "current", lambda **kw: SimpleNamespace(features={coordinator.FEATURE}))
    monkeypatch.setattr(license, "require", lambda feature, **kw: None)
    return ws


def _task(db, ws, task_id, state, depends_on=None, agent=None):
    db.add_task(Task(task_id, ws.repo_root, agent, "b", ws.id, "developer", f"do {task_id}",
                     "assign", True, f"feat-{task_id}", None, '["src/x.py"]',
                     json.dumps(depends_on) if depends_on else None, state, time.time()))


def _hold_task_only_pending_and_allowlisted(db, repo, monkeypatch):
    ws = _repo_with_sessions(db, repo, monkeypatch)
    _task(db, ws, "queued", "pending", ["w-b"])
    _task(db, ws, "running", "started", agent="w-b")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "coord")

    mcp_server.hold_task("queued", "a's auth work lands first")    # another session's task
    assert db.get_task("queued").state == "held"
    assert db.pop_pending("b") is not None                          # its supervisor is told
    mcp_server.hold_task("running", "pause it")
    assert db.get_task("running").state == "started"
    with pytest.raises(ToolError):    # it may only advise
        asyncio.run(mcp_server.mcp.call_tool("send_message", {"to_agent_id": "b", "message": "x"}))
    assert db.pop_pending("b") is None
    monkeypatch.setenv("BRINDLE_AGENT_ID", "a")                     # not a supervisor's tool
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.mcp.call_tool("release_task", {"task_id": "queued", "reason": "x"}))
    # Nor a worker a supervisor put on the coordinator profile.
    with pytest.raises(agents.AgentError):
        agents.spawn(db, ws, coordinator.PROFILE, prompt="hold b's work", mode="assign")
    db.add_agent(Agent("posing", ws.id, coordinator.PROFILE, "claude", "a", "assign", "processing",
                       "", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "posing")
    mcp_server.release_task("queued", "x")
    assert db.get_task("queued").state == "held"


def _events_wake_one_coordinator(db, repo, monkeypatch):
    _repo_with_sessions(db, repo, monkeypatch, coord=False)
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text("{}")
    started, prompts = [], []

    def spawn(db, ws, profile, *, prompt, mode, **kw):
        a = Agent(f"c{len(started)}", ws.id, profile, "claude", None, mode, "processing", "",
                  None, time.time())
        db.add_agent(a)
        started.append(a.id)
        prompts.append(prompt)
        return a

    monkeypatch.setattr(agents, "spawn", spawn)
    monkeypatch.setattr(agents, "close", lambda db, aid, panes=None: db.set_status(aid, "done"))
    monkeypatch.setattr(agents, "close_later", lambda aid, delay=5.0: None)
    key = knowledge.repo_key(repo)
    for s in ("a", "b"):
        knowledge.append(db, key, knowledge.INTENT, {"summary": s, "paths": ["src/x.py"]}, session=s)

    coordinator.wake(db)
    coordinator.wake(db)
    knowledge.append(db, key, knowledge.INTENT, {"summary": "a2", "paths": []}, session="a")
    coordinator.wake(db)                                          # one is running: none starts
    assert started == ["c0"]
    monkeypatch.setenv("BRINDLE_AGENT_ID", "c0")
    mcp_server.report_result("nothing to do yet")
    coordinator.wake(db)                    # reported: the next one remembers its report

    # Hung past its lifetime (still "processing"): closed, and what it was
    # woken for goes to a fresh one.
    coordinator.wake(db, now=time.time() + coordinator.LIFETIME + 1)
    assert started == ["c0", "c1", "c2"] and db.get_agent("c1").status == "done"
    assert '"a2"' in prompts[2] and "nothing to do yet" in prompts[1]

    monkeypatch.chdir(repo)
    log = CliRunner().invoke(cli.app, ["coordinator", "log"])
    assert "nothing to do yet" in log.output
    assert [h.result for h in db.list_history(kind="coordinator")] == ["nothing to do yet"]
    assert CliRunner().invoke(cli.app, ["coordinator", "stop"]).exit_code == 0
    coordinator.wake(db)                                          # the unseen event stays unwoken
    assert started == ["c0", "c1", "c2"] and db.get_agent("c2").status == "done"


def _undo_restores_queue(db, repo, monkeypatch):
    ws = _repo_with_sessions(db, repo, monkeypatch)
    _task(db, ws, "queued", "pending", ["w-b"])
    monkeypatch.setenv("BRINDLE_AGENT_ID", "coord")
    mcp_server.hold_task("queued", "a's auth work lands first")
    mcp_server.reorder_queue("queued", ["w-a"], "start from a's branch")
    assert tasks._loads(db.get_task("queued").depends_on) == ["w-b", "w-a"]

    events = db.conn.execute("SELECT id FROM kb_events WHERE kind = ? ORDER BY rowid",
                             (knowledge.COORDINATOR,)).fetchall()
    monkeypatch.chdir(repo)
    for (event_id,) in events:
        assert CliRunner().invoke(cli.app, ["coordinator", "undo", event_id[:8]]).exit_code == 0
    t = db.get_task("queued")
    assert (t.state, tasks._loads(t.depends_on)) == ("pending", ["w-b"])
    assert [(e["payload"]["action"], e["task_id"])
            for e in bus.tail(db, kinds=("coordinator.action",))] == [
        ("hold_task", "queued"), ("reorder_queue", "queued"), ("undo", "queued"), ("undo", "queued")]
    assert CliRunner().invoke(cli.app, ["coordinator", "undo", events[0][0][:8]]).exit_code != 0


@pytest.mark.parametrize("scenario", [
    _hold_task_only_pending_and_allowlisted, _events_wake_one_coordinator, _undo_restores_queue,
], ids=lambda f: f.__name__.strip("_"))
def test_coordinator_scenarios(scenario, db, repo, monkeypatch):
    scenario(db, repo, monkeypatch)
