"""Only one supervisor stays live per project: a scratch session's chat is
paused once its work moves into a repo, and an agent whose pane id a newer
agent took over doesn't count as live."""

import time

import pytest

from brindle import agents, cli, mcp_server, scratch, workspaces
from brindle.db import Agent

from conftest import sh


def _sup(db, aid, ws, pane):
    a = Agent(aid, ws.id, "supervisor", "claude", None, "interactive", "idle", pane, None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def live(monkeypatch):
    """Every recorded agent counts as alive; pause just records the status."""
    paused = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes=None: {})
    monkeypatch.setattr(agents, "pause", lambda db, aid, **k: paused.append(aid) or db.set_status(aid, "paused"))
    return paused


@pytest.fixture
def moved(db, repo, tmp_path):
    """A scratch session with a live supervisor, its work already moved into ``repo``."""
    elsewhere = tmp_path / "Downloads"
    elsewhere.mkdir()
    s = scratch.create(db, str(elsewhere))
    (open(f"{s.path}/notes.md", "w")).write("plan\n")
    sh("git add -A && git commit -q -m notes", s.path)
    _sup(db, "scratchsup", s, "%1")
    return s


def test_transfer_pauses_the_scratch_chat(db, repo, moved, live):
    scratch.transfer(db, moved, str(repo))
    assert live == ["scratchsup"]
    assert db.get_agent("scratchsup").status == "paused"


def test_mcp_transfer_keeps_the_caller_until_the_repo_session_starts(db, repo, moved, live, monkeypatch):
    monkeypatch.setenv("BRINDLE_AGENT_ID", "scratchsup")
    monkeypatch.setattr(mcp_server, "DB", lambda: db)
    assert "Moved 1 commit" in mcp_server.transfer_to_repo(str(repo))
    assert live == []                                    # the reply can still be delivered
    # Then a supervisor starting in the repo pauses it.
    ws = workspaces.adopt_root(db, str(repo))
    cli._pause_running(db, ws)
    assert live == ["scratchsup"]


def test_starting_in_the_repo_pauses_the_scratch_chat_of_a_cli_transfer(db, repo, moved, live):
    scratch.transfer(db, moved, str(repo), keep_running="scratchsup")   # as the MCP tool does
    ws = workspaces.adopt_root(db, str(repo))
    repo_sup = _sup(db, "reposup", ws, "%2")
    cli._pause_running(db, ws)
    assert sorted(live) == ["reposup", "scratchsup"]     # the old one is gone before the new one spawns
    assert repo_sup.id in live


def test_other_scratch_sessions_are_left_alone(db, repo, moved, live, tmp_path):
    other_dir = tmp_path / "Other"
    other_dir.mkdir()
    other = scratch.create(db, str(other_dir))
    _sup(db, "othersup", other, "%3")
    ws = workspaces.adopt_root(db, str(repo))
    scratch.transfer(db, moved, str(repo), keep_running="scratchsup")
    cli._pause_running(db, ws)
    assert "othersup" not in live


def test_a_pane_id_taken_over_by_a_newer_agent_is_not_live(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    _sup(db, "old", ws, "%337")
    time.sleep(0.01)
    _sup(db, "new", ws, "%337")                          # same pane id after a tmux restart
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "pane_owners", lambda db_, panes=None: {"%337": "new"})
    assert [a.id for a in cli._live_sessions(db, ws)] == ["new"]
    assert cli._running_session(db, ws).id == "new"
