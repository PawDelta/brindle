"""Permission asks are kept after they are answered, and reported (brindle.permission_asks)."""

import json
import time

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


class Clock:
    """The time module, with time() and monotonic() at ``now``."""
    now = 1000.0

    def time(self):
        return self.now
    monotonic = time

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(permission_asks, "time", fake)
    monkeypatch.setattr(agents, "time", fake)
    return fake


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
    grant = permissions.save_action("edit", "*.py", "allow", scope="general")
    with permissions.editing() as store:
        store.saved_action(grant.id).uses_left = 2
    out = agents.permission_request_decision(db, a, payload, provider="codex")
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert permissions.load_store().saved_action(grant.id).uses_left == 1   # one call, one use
    data = permission_asks.report(db, days=1)
    assert (data["asks"], data["resolved_by"]) == (2, {"saved_action": 1, "unheld": 1})


def _just_before_the_end(clock, holder):
    clock.now = holder.until - 0.5


def _closed(db, agent):
    db.set_status(agent.id, "done")


@pytest.mark.variant("review regression: a wait must end with its hold, not with the tool's PostToolUse")
@pytest.mark.parametrize("pane, looked, resolved_by, wait", [
    ("busy", lambda c, h, db, a: (True, None), "person", 10.0),   # its pane read past the prompt for 3 looks
    (None, lambda c, h, db, a: (True, "allow_once"), "person", 1.0),   # one look, HOLD_LOOK_SECONDS
    (None, lambda c, h, db, a: (True, "terminal"), "terminal", None),
    (None, lambda c, h, db, a: (_closed(db, a), (True, None))[1], "agent_exit", None),
    # The daemon lets go of a hold early only for a closed agent, or when it is gone...
    (None, lambda c, h, db, a: (_just_before_the_end(c, h), _closed(db, a), (False, None))[2], "agent_exit", None),
    (None, lambda c, h, db, a: (_just_before_the_end(c, h), (False, None))[1], "daemon_lost", None),
    # ...and of every hold at its hold_until.
    (None, lambda c, h, db, a: (setattr(c, "now", h.until), (False, None))[1], "timeout", None),
])
def test_an_ask_ends_with_its_hold_not_with_the_tool(db, ws, clock, monkeypatch, pane, looked, resolved_by, wait):
    set_local(ws.repo_root, "permission_policy", "on")
    set_local(ws.repo_root, "permission_hold_seconds", 3600)
    with permissions.editing() as store:
        store.rules = [Rule("bash", "make", "prefix", "ask")]
    a = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(a)

    def register(self, until):
        self.until = until
        return True

    def look(self, seconds):
        clock.now += agents.HOLD_LOOK_SECONDS
        return looked(clock, self, db, a)

    def slow_end(self):       # the daemon takes five seconds to hear the hold is over
        clock.now += 5
    monkeypatch.setattr("brindle.daemon.holds.Holder.register", register)
    monkeypatch.setattr("brindle.daemon.holds.Holder.wait", look)
    monkeypatch.setattr("brindle.daemon.holds.Holder.end", slow_end)
    monkeypatch.setattr(agents, "_pane_state", lambda agent: pane)
    ask_about(db, ws, a, "make build")

    def row():
        return tuple(db.conn.execute("SELECT resolved_by, wait_seconds FROM permission_asks").fetchone())
    assert row() == (resolved_by, wait)
    # The build runs five minutes before its PostToolUse says the person allowed it.
    clock.now += 300
    agents.note_permission_outcome(db, a, {
        "tool_name": "Bash", "tool_input": {"command": "make build"}, "tool_use_id": "t1"})
    assert row()[1] == wait


def test_report_covers_the_window_and_leaves_pending_asks_unanswered(db, clock):
    permission_asks.record_decision(db, "w1", "/r", "bash", "git push", "allow",
                                    "covered by your saved action ab12 (bash git push)", saved=True)
    permission_asks.start_ask(db, "w1", "/r", "q1", "bash", "uv run x", "ask rule; asking the user")
    answered = permission_asks.start_ask(db, "w1", "/r", "q2", "bash", "uv run y", "ask rule; asking the user")
    clock.now += 30
    permission_asks.finish_ask(db, answered, "allow_once")
    clock.now -= 10 * 86400
    permission_asks.record_unheld_ask(db, "w1", "/r", "bash", "uv run z", "no rule allows it; asking the user")
    clock.now += 10 * 86400
    data = permission_asks.report(db, days=7, now=clock.now)
    assert (data["decisions"], data["asks"]) == (3, 3)
    assert data["resolved_by"] == {"person": 1, "saved_action": 1}
    assert data["avg_wait_seconds"] == 15.0          # the saved action's 0 and the person's 30
    assert permission_asks.report(db, days=30, now=clock.now)["resolved_by"] == {
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
