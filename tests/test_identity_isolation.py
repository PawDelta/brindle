"""A company-identity session stays away from other identities: while the
identity signed in now differs from the one an agent launched under, nothing
resumes, rewinds, messages or starts in its worktree or conversation, and the
views show it as locked without its conversation."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, identity_lock, rewind, signin_pause, tasks, view, workspaces
from brindle.cli import app
from brindle.db import Agent

ACME = "claude org Acme (org-1)"
OTHER = "claude org Me (org-9)"
LOCKED = f"This session belongs to {ACME}; sign in as it to continue (`brindle login`)"


class Who:
    """The identity signed in now; ``kind`` is what the check says about it."""

    def __init__(self):
        self.ident, self.kind = ACME, "ok"

    def check(self, provider, cache=False):
        return signin_pause.Check(self.kind, ident=self.ident if self.kind == "ok" else ""), False


@pytest.fixture
def who(monkeypatch):
    w = Who()
    monkeypatch.setattr(signin_pause, "check", w.check)
    return w


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def worker(db, ws, agent_id="w1", *, recorded=ACME, status="processing", parent=None, mode="assign",
           **extra):
    db.add_agent(Agent(agent_id, ws.id, "developer", "claude", parent, mode, status, "", None,
                       time.time(), task="add a login page", **extra))
    if recorded:
        state = signin_pause._load()
        state["identity"][agent_id] = recorded
        signin_pause._save(state)
    return db.get_agent(agent_id)


def refused(fn, *args, **kwargs):
    with pytest.raises(agents.AgentError) as e:
        fn(*args, **kwargs)
    assert str(e.value) == LOCKED


# -- resume ----------------------------------------------------------------------------


@pytest.fixture
def launches(monkeypatch):
    got = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: got.append(a.id))
    return got


def test_resume_is_refused_under_a_different_identity(db, ws, who, launches):
    worker(db, ws, status="paused", parent="boss")
    worker(db, ws, "boss", status="paused", mode="interactive", recorded=OTHER)
    who.ident = OTHER
    refused(agents.resume, db, "boss")
    assert launches == []   # nothing of the session restarted, not even the unlocked chat


def test_resume_works_under_the_recorded_identity(db, ws, who, launches):
    worker(db, ws, status="paused", parent="boss")
    worker(db, ws, "boss", status="paused", mode="interactive")
    assert [a.id for a in agents.resume(db, "boss")] == ["boss", "w1"]
    assert sorted(launches) == ["boss", "w1"]


def test_continue_command_is_refused_under_a_different_identity(db, repo, ws, who, launches, monkeypatch):
    main = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("chat", main.id, "supervisor", "claude", None, "interactive", "paused", "",
                       None, time.time()))
    worker(db, ws, status="paused", parent="chat")
    monkeypatch.chdir(repo)
    who.ident = OTHER
    res = CliRunner().invoke(app, ["continue", "--no-attach"])
    assert res.exit_code != 0 and LOCKED in res.output and launches == []


# -- rewind ----------------------------------------------------------------------------


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    def fake_spawn(db, ws, profile_name, *, prompt=None, parent_id=None, mode="interactive", **kw):
        calls.append(ws.id)
        a = Agent(f"new{len(calls)}", ws.id, profile_name, "claude", parent_id, mode, "processing",
                  "", None, time.time(), task=prompt)
        db.add_agent(a)
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    return calls


def one_turn(db, ws):
    agents.handle_hook(db, "w1", "session-start", {})
    with open(os.path.join(ws.path, "app.py"), "w") as f:
        f.write("x\n")
    agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True})


def test_rewind_is_refused_under_a_different_identity(db, ws, who, spawned):
    worker(db, ws)
    one_turn(db, ws)
    who.ident = OTHER
    refused(rewind.rewind, db, "w1", 1)
    assert spawned == [] and db.get_agent("w1").status != "rewound"
    out = asyncio.run(__import__("brindle.mcp_server", fromlist=["x"]).rewind_agent("w1", 1))
    assert out == f"Not rewound: {LOCKED}"


def test_rewind_works_under_the_recorded_identity(db, ws, who, spawned):
    worker(db, ws)
    one_turn(db, ws)
    assert rewind.rewind(db, "w1", 1).id == "new1"
    assert spawned == [ws.id]


# -- messages and handoffs --------------------------------------------------------------


def test_messages_into_a_locked_agent_are_refused(db, ws, who, monkeypatch):
    worker(db, ws)
    sent = []
    monkeypatch.setattr(agents, "_send_message", lambda db, a, body, sender: sent.append(body) or "delivered")
    who.ident = OTHER
    refused(agents.send_message, db, "w1", "hello")
    refused(agents.send_message, db, "w1", "hello", person=True)
    assert sent == []


def test_messages_flow_under_the_recorded_identity(db, ws, who, monkeypatch):
    worker(db, ws)
    monkeypatch.setattr(agents, "_send_message", lambda db, a, body, sender: "delivered")
    monkeypatch.setattr(agents, "runs_process", lambda a: False)
    assert agents.send_message(db, "w1", "hello") == "delivered"


# -- a new agent in the worktree ----------------------------------------------------------


def test_no_new_agent_in_a_locked_agents_worktree(db, ws, who):
    worker(db, ws)
    who.ident = OTHER
    refused(agents.spawn, db, ws, "developer")
    refused(agents.spawn, db, ws, "reviewer", mode="review")
    boss = worker(db, ws, "boss", recorded=None, mode="interactive")
    refused(agents.delegate, db, boss, ws, "developer", "more work", "assign", isolate=False)


def test_naming_the_branch_of_a_locked_worktree_does_not_reuse_it(db, repo, ws, who):
    worker(db, ws)
    main = workspaces.adopt_root(db, str(repo))
    boss = worker(db, main, "boss", recorded=None, mode="interactive")
    who.ident = OTHER
    refused(agents.delegate, db, boss, main, "developer", "again", "assign", branch="feature")


def test_a_new_agent_may_start_under_the_recorded_identity(db, ws, who, launches):
    identity_lock.ensure_workspace(db, ws)   # no refusal
    worker(db, ws)
    agents.spawn(db, ws, "developer", mode="assign", prompt="more")
    assert launches


# -- the supervisor's tools --------------------------------------------------------------


def test_depends_on_cannot_pull_in_a_locked_branch(db, ws, who):
    worker(db, ws)
    main = db.find_workspaces(ws.repo_root)[0]
    who.ident = OTHER
    for dep in ("w1", "feature"):
        refused(tasks.unmet_dependencies, db, main, [dep])


def test_depends_on_works_under_the_recorded_identity(db, ws, who):
    worker(db, ws)
    main = db.find_workspaces(ws.repo_root)[0]
    assert tasks.unmet_dependencies(db, main, ["w1"]) == []   # answers (no commits: trivially merged)


def test_assign_with_depends_on_a_locked_branch_says_why(db, repo, ws, who, monkeypatch):
    from brindle import mcp_server

    worker(db, ws)
    main = workspaces.adopt_root(db, str(repo))
    worker(db, main, "boss", recorded=None, mode="interactive")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    who.ident = OTHER
    out = asyncio.run(mcp_server.assign(task="build on it", depends_on=["w1"]))
    assert out == LOCKED


def test_merge_workspace_refuses_a_locked_branch(db, ws, who):
    from brindle import mcp_server

    worker(db, ws)
    who.ident = OTHER
    out = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert out == f"Not merged: {LOCKED}"


# -- what the views show -----------------------------------------------------------------


def test_view_shows_a_locked_agent_without_reading_its_conversation(db, ws, who, monkeypatch):
    worker(db, ws, transcript_path="/nonexistent/t.jsonl")
    who.ident = OTHER
    read = []
    monkeypatch.setattr(view.usage_mod, "agent_usage", lambda db, a: read.append(a.id))
    entry = view.agent_entry(db, db.get_agent("w1"), detail=True)
    assert entry["status"] == f"locked: {ACME}" and entry["locked"] == ACME
    assert entry["id"] == "w1" and "tokens" not in entry and "unread" not in entry
    assert read == []


def test_the_sidebar_snapshot_shows_the_locked_agent(db, ws, who, monkeypatch):
    worker(db, ws)
    who.ident = OTHER
    monkeypatch.setattr(view.agents, "is_alive", lambda a, panes=None: True)
    snap = view.snapshot(db, ws.repo_root, panes={})
    entries = [a for e in snap for a in e["agents"] if a["id"] == "w1"]
    assert [e["status"] for e in entries] == [f"locked: {ACME}"]


def test_agent_turns_hide_the_conversation_of_a_locked_agent(db, ws, who):
    from brindle import mcp_server

    worker(db, ws)
    one_turn(db, ws)
    assert "turn   1" in mcp_server.agent_turns("w1")
    who.ident = OTHER
    out = mcp_server.agent_turns("w1")
    assert f"locked: {ACME}" in out and "turn   1" not in out
    res = CliRunner().invoke(app, ["agent", "turns", "w1"])
    assert res.exit_code != 0 and f"locked: {ACME}" in res.output


def test_agent_peek_is_refused(db, ws, who):
    worker(db, ws)
    db.update_agent("w1", tmux_window="brindle:1")
    who.ident = OTHER
    res = CliRunner().invoke(app, ["agent", "peek", "w1"])
    assert res.exit_code != 0 and LOCKED in res.output


def test_ls_lists_the_agent_as_locked(db, repo, ws, who, monkeypatch):
    worker(db, ws)
    monkeypatch.chdir(repo)
    who.ident = OTHER
    res = CliRunner().invoke(app, ["ls"])
    assert f"locked: {ACME}" in res.output
    res = CliRunner().invoke(app, ["ls", "--json"])
    assert f'"locked: {ACME}"' in res.output
    who.ident = ACME
    assert "locked" not in CliRunner().invoke(app, ["ls"]).output


def test_list_agents_tool_shows_locked_not_the_transcript_activity(db, repo, ws, who, tmp_path, monkeypatch):
    from brindle import mcp_server

    monkeypatch.chdir(repo)
    t = tmp_path / "t.jsonl"
    t.write_text('{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebFetch"}]}}\n')
    worker(db, ws, transcript_path=str(t))
    assert "last: WebFetch" in mcp_server.list_agents()
    who.ident = OTHER
    out = mcp_server.list_agents()
    assert f"locked: {ACME}" in out and "WebFetch" not in out


def test_agent_rewind_command_is_refused(db, ws, who, spawned):
    worker(db, ws)
    one_turn(db, ws)
    who.ident = OTHER
    res = CliRunner().invoke(app, ["agent", "rewind", "w1", "--to", "1"])
    assert res.exit_code != 0 and LOCKED in res.output and spawned == []


def test_merge_workspace_goes_ahead_under_the_recorded_identity(db, ws, who, monkeypatch):
    from brindle import mcp_server, pipeline

    worker(db, ws)
    monkeypatch.setattr(pipeline, "merge", lambda db, caller, ws, squash=False: "Merged.")
    assert asyncio.run(mcp_server.merge_workspace(ws.id)) == "Merged."
    who.ident = OTHER
    assert asyncio.run(mcp_server.merge_workspace(ws.id)) == f"Not merged: {LOCKED}"


def test_assign_with_depends_on_goes_ahead_under_the_recorded_identity(db, repo, ws, who, monkeypatch):
    from brindle import mcp_server

    worker(db, ws)
    main = workspaces.adopt_root(db, str(repo))
    worker(db, main, "boss", recorded=None, mode="interactive")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(agents, "delegate", lambda *a, **k: (_ for _ in ()).throw(agents.AgentError("stub")))
    out = asyncio.run(mcp_server.assign(task="build on it", depends_on=["w1"]))
    assert LOCKED not in out and "stub" in out   # got past the dependency check to the start


def test_view_shows_the_agent_normally_under_the_recorded_identity(db, ws, who):
    worker(db, ws)
    entry = view.agent_entry(db, db.get_agent("w1"), alive=True)
    assert entry["status"] != f"locked: {ACME}" and "locked" not in entry


# -- who is not locked ---------------------------------------------------------------------


def test_legacy_agents_without_a_recorded_identity_are_unaffected(db, ws, who, launches, spawned, monkeypatch):
    worker(db, ws, status="paused", recorded=None)
    who.ident = OTHER
    assert identity_lock.locked_by("w1") is None
    assert [a.id for a in agents.resume(db, "w1")] == ["w1"]
    monkeypatch.setattr(agents, "_send_message", lambda db, a, body, sender: "delivered")
    assert agents.send_message(db, "w1", "hi") == "delivered"
    identity_lock.ensure_workspace(db, ws)
    assert "locked" not in view.agent_entry(db, db.get_agent("w1"), alive=True)["status"]
    assert tasks.unmet_dependencies(db, db.find_workspaces(ws.repo_root)[0], ["w1"]) == []


@pytest.mark.parametrize("kind", ["signed_out", "undetermined"])
def test_an_unknown_current_identity_never_locks(db, ws, who, kind):
    worker(db, ws)
    who.kind = kind
    assert identity_lock.locked_by("w1") is None


def test_lock_and_resume_agree_on_the_same_identity(db, ws, who):
    # resume compares with company_identity.matches; the lock must use the same test
    worker(db, ws)
    who.ident = ACME.upper()
    assert identity_lock.locked_by("w1") is None
    who.ident = OTHER
    assert identity_lock.locked_by("w1") == ACME


def test_codex_agents_are_not_recorded_or_locked(db, ws, who):
    # Codex reports only "signed in", so its sessions are paused on sign-out but never
    # tied to one account (the limit the README states)
    db.add_agent(Agent("c1", ws.id, "developer", "codex", None, "assign", "processing", "", None,
                       time.time(), task="add a login page"))
    signin_pause.record_launch(db.get_agent("c1"))
    assert signin_pause.recorded_identity("c1") is None
    who.ident = OTHER
    assert identity_lock.locked_by("c1") is None
