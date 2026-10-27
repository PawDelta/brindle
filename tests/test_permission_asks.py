"""Permission asks are kept after they are answered, and reported (brindle.permission_asks)."""

import json
import time

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from brindle import agents, permission_asks, permissions, workspaces
from brindle.cli import app
from brindle.config import set_local
from brindle.daemon.app import build_app
from brindle.db import Agent
from brindle.permissions import Rule

TOKEN = "t" * 43


@pytest.fixture
def ws(db, repo, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    return workspaces.create(db, str(repo), "feature").workspace


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
    # With no one holding it (the app is closed) the request times out to the CLI's own prompt...
    monkeypatch.setattr("brindle.daemon.holds.Holder.register", lambda self, until: False)
    ask_about(db, ws, a, "uv run ruff check")
    # ...and the person approving it there resolves the ask afterwards.
    agents.note_permission_outcome(db, a, {
        "tool_name": "Bash", "tool_input": {"command": "uv run ruff check"}, "tool_use_id": "t1"})

    data = permission_asks.report(db, days=1, repo_root=ws.repo_root)
    assert (data["decisions"], data["asks"], data["ask_rate"]) == (4, 2, 0.5)
    assert data["resolved_by"] == {"person": 2}
    assert data["outcomes"] == {"allow": 2}
    [cause] = data["causes"]
    assert cause["count"] == 2 and cause["kinds"] == ["bash"]
    assert permission_asks.report(db, days=1, repo_root="/elsewhere")["decisions"] == 0
    assert "50% of decisions" in permission_asks.format_report(data)


def test_report_covers_the_window_and_saved_actions(db):
    permission_asks.record_decision(db, "w1", "/r", "bash", "git push", "allow",
                                    "covered by your saved action ab12 (bash git push)")
    permission_asks.record_ask(db, "w1", "/r", "q1", "bash", "uv run x", "no rule allows or denies it; asking the user",
                               None, 12.0)
    old = time.time() - 10 * 86400
    with db.tx() as c:
        c.execute("UPDATE permission_asks SET ts=? WHERE request_id='q1'", (old,))
    data = permission_asks.report(db, days=7)
    assert (data["asks"], data["resolved_by"]) == (1, {"saved_action": 1})
    assert permission_asks.report(db, days=30)["resolved_by"] == {"saved_action": 1, "timeout": 1}


def test_report_is_reachable_from_the_cli_and_the_daemon(db, monkeypatch):
    monkeypatch.setattr("brindle.cli._here_repo", lambda: None)
    permission_asks.record_ask(db, "w1", "/r", "q1", "bash", "uv run x", "ask rule; asking the user", "deny", 3.0)
    out = CliRunner().invoke(app, ["permissions", "report", "--days", "1"])
    assert "Top causes" in out.output and "ask rule" in out.output
    with TestClient(build_app(TOKEN)) as client:
        got = client.get("/v1/permissions/report?days=1", headers={"Authorization": f"Bearer {TOKEN}"})
    assert json.loads(got.text)["asks"] == 1
