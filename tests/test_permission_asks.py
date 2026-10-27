"""Permission asks are kept after they are answered, and reported (brindle.permission_asks)."""

import json
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from brindle import agents, permission_asks, permissions
from brindle.cli import app
from brindle.config import set_local
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.permissions import Rule

TOKEN = "t" * 43


@pytest.fixture
def ws(db, tmp_path, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    ws = Workspace("ws1", str(tmp_path), "feature", "worktree", "feature", "main", str(tmp_path), None,
                   "brindle-ws1", time.time())
    db.add_workspace(ws)
    return ws


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(permission_asks, "time", SimpleNamespace(time=lambda: now[0]))
    return now


def ask_about(db, ws, agent, command):
    return agents.permission_request_decision(db, agent, {
        "tool_name": "Bash", "tool_input": {"command": command}, "hook_event_name": "PermissionRequest"})


def test_decisions_and_asks_are_kept_and_reported(db, ws, monkeypatch):
    set_local(ws.repo_root, "permission_policy", "on")
    with permissions.editing() as store:
        store.rules = [Rule("bash", "git status", "prefix", "allow"), Rule("bash", "rm", "prefix", "deny"),
                       Rule("bash", "uv run", "prefix", "ask")]
    a = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(a)
    monkeypatch.setattr(agents, "_await_app_answer", lambda *args: "allow_once")
    monkeypatch.setattr("brindle.daemon.holds.Holder.register", lambda self, until: True)
    ask_about(db, ws, a, "git status")
    ask_about(db, ws, a, "rm -rf x")
    ask_about(db, ws, a, "uv run pytest")
    # The person approves at the CLI's own prompt while the hold is still waiting.
    def approved_at_terminal(*args):
        agents.note_permission_outcome(db, a, {
            "tool_name": "Bash", "tool_input": {"command": "uv run ruff check"}, "tool_use_id": "t1"})
    monkeypatch.setattr(agents, "_await_app_answer", approved_at_terminal)
    ask_about(db, ws, a, "uv run ruff check")
    # ...or the instant the request becomes visible, before the hold starts waiting.
    monkeypatch.setattr(agents, "_await_app_answer", lambda *args: None)
    add = db.add_permission_request

    def add_then_approved(*args, **kw):
        add(*args, **kw)
        approved_at_terminal()
    monkeypatch.setattr(db, "add_permission_request", add_then_approved)
    ask_about(db, ws, a, "uv run ruff check")
    # A PreToolUse approval by the profile's allowed_tools is a decision too.
    assert agents.pre_tool_decision(db, a, {"tool_name": "Bash", "tool_input": {"command": "git status"}})

    data = permission_asks.report(db, days=1, repo_root=ws.repo_root)
    assert (data["decisions"], data["asks"], data["ask_rate"]) == (6, 3, 0.5)
    assert data["resolved_by"] == {"person": 3}
    assert data["outcomes"] == {"allow": 3}
    [cause] = data["causes"]
    assert cause["count"] == 3 and cause["kinds"] == ["bash"]
    assert permission_asks.report(db, days=1, repo_root="/elsewhere")["decisions"] == 0


def test_a_multi_file_codex_request_is_credited_to_the_saved_action_that_settles_it(db, ws):
    set_local(ws.repo_root, "permission_policy", "on")
    a = Agent("c1", ws.id, "developer", "codex", "boss", "assign", "processing", "%c1", None, time.time())
    db.add_agent(a)
    patch = "*** Begin Patch\n*** Update File: a.py\n*** Update File: b.py\n*** End Patch\n"
    payload = {"cwd": ws.path, "tool_name": "apply_patch", "tool_input": {"command": patch}}
    assert agents.permission_request_decision(db, a, payload, provider="codex") is None   # Codex's prompt asks
    permissions.save_action("edit", "*.py", "allow", scope="general")
    out = agents.permission_request_decision(db, a, payload, provider="codex")
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    data = permission_asks.report(db, days=1)
    assert (data["asks"], data["resolved_by"]) == (2, {"saved_action": 1, "unheld": 1})


def _closing(db, agent):
    db.set_status(agent.id, "done")
    return True, None


@pytest.mark.parametrize("pane, looked, hold, resolved_by, wait", [
    ("busy", lambda db, a: (True, None), 3600, "person", 10.0),   # its pane read past the prompt for 3 looks
    (None, lambda db, a: (True, "terminal"), 3600, "terminal", None),
    (None, _closing, 3600, "agent_exit", None),
    (None, lambda db, a: (False, None), 3600, "daemon_lost", None),
    (None, lambda db, a: (True, None), 0, "timeout", None),
])
def test_an_ask_ends_with_its_hold_not_with_the_tool(db, ws, clock, monkeypatch, pane, looked, hold, resolved_by,
                                                     wait):
    set_local(ws.repo_root, "permission_policy", "on")
    set_local(ws.repo_root, "permission_hold_seconds", hold)
    with permissions.editing() as store:
        store.rules = [Rule("bash", "make", "prefix", "ask")]
    a = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(a)
    monkeypatch.setattr("brindle.daemon.holds.Holder.register", lambda self, until: True)
    monkeypatch.setattr("brindle.daemon.holds.Holder.end", lambda self: None)
    monkeypatch.setattr(agents, "_pane_state", lambda agent: pane)

    def look(self, seconds):
        clock[0] += agents.HOLD_LOOK_SECONDS
        return looked(db, a)
    monkeypatch.setattr("brindle.daemon.holds.Holder.wait", look)
    ask_about(db, ws, a, "make build")

    def row():
        return tuple(db.conn.execute("SELECT resolved_by, wait_seconds FROM permission_asks").fetchone())
    assert row() == (resolved_by, wait)
    # The build runs five minutes before its PostToolUse says the person allowed it.
    clock[0] += 300
    agents.note_permission_outcome(db, a, {
        "tool_name": "Bash", "tool_input": {"command": "make build"}, "tool_use_id": "t1"})
    assert row()[1] == wait


def test_report_covers_the_window_and_leaves_pending_asks_unanswered(db, clock):
    permission_asks.record_decision(db, "w1", "/r", "bash", "git push", "allow",
                                    "covered by your saved action ab12 (bash git push)", saved=True)
    permission_asks.start_ask(db, "w1", "/r", "q1", "bash", "uv run x", "ask rule; asking the user")
    answered = permission_asks.start_ask(db, "w1", "/r", "q2", "bash", "uv run y", "ask rule; asking the user")
    clock[0] += 30
    permission_asks.finish_ask(db, answered, "allow_once")
    clock[0] -= 10 * 86400
    permission_asks.record_unheld_ask(db, "w1", "/r", "bash", "uv run z", "no rule allows it; asking the user")
    clock[0] += 10 * 86400
    data = permission_asks.report(db, days=7, now=clock[0])
    assert (data["decisions"], data["asks"]) == (3, 3)
    assert data["resolved_by"] == {"person": 1, "saved_action": 1}
    assert data["avg_wait_seconds"] == 15.0          # the saved action's 0 and the person's 30
    assert permission_asks.report(db, days=30, now=clock[0])["resolved_by"] == {
        "person": 1, "saved_action": 1, "unheld": 1}


def test_report_is_reachable_from_the_cli_and_the_daemon(db, monkeypatch):
    monkeypatch.setattr("brindle.cli._here_repo", lambda: None)
    permission_asks.finish_ask(db, permission_asks.start_ask(
        db, "w1", "/r", "q1", "bash", "uv run x", "ask rule; asking the user"), "deny")
    out = CliRunner().invoke(app, ["permissions", "report", "--days", "1"])
    assert out.exit_code == 0
    with TestClient(build_app(TOKEN)) as client:
        got = client.get("/v1/permissions/report?days=1", headers={"Authorization": f"Bearer {TOKEN}"})
    assert json.loads(got.text)["asks"] == 1
