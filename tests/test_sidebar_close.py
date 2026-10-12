"""Closing agents from the sidebar (`x` in `brindle watch`, `brindle close`):
closed agents are hidden for good and stopped if running, and nothing on
disk is removed."""

import os
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, tmux, view, watch, workspaces
from brindle.cli import app
from brindle.db import Agent


def add_agent(db, ws, agent_id, *, mode="assign", status="idle", window="", parent=None,
              profile="developer"):
    a = Agent(agent_id, ws.id, profile, "claude", parent, mode, status, window, None, time.time())
    db.add_agent(a)
    return a


def running_window(ws):
    """A real (private-socket) tmux pane standing in for an agent's CLI."""
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    return tmux.new_window(ws.tmux_session, "agent", ws.path, ["sleep", "300"], {})


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    yield ws
    tmux.kill_session(ws.tmux_session)


def shown_ids(db, repo_root):
    return [a["id"] for ws in view.snapshot(db, repo_root) for a in ws["agents"]]


def test_a_worktree_whose_agents_are_all_closed_disappears_but_stays_on_disk(db, repo, worker_ws):
    add_agent(db, worker_ws, "w1")
    agents.close(db, "w1")
    assert [ws["id"] for ws in view.snapshot(db, str(repo))] == []
    assert os.path.isdir(worker_ws.path)
    assert db.get_workspace(worker_ws.id) is not None
    assert db.get_agent("w1") is not None


def test_cli_close_one_agent(db, repo, worker_ws, monkeypatch):
    pane = running_window(worker_ws)
    add_agent(db, worker_ws, "abc123", status="processing", window=pane)
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "abc"])
    assert res.exit_code == 0, res.output
    assert "closed abc123" in res.output and "stopped" in res.output
    assert db.get_agent("abc123").dismissed_at is not None
    assert not agents.is_alive(db.get_agent("abc123"))


def test_cli_close_unknown_agent_fails(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "nope"])
    assert res.exit_code == 1


def test_cli_close_exited_closes_only_stopped_agents(db, repo, worker_ws, monkeypatch):
    pane = running_window(worker_ws)
    add_agent(db, worker_ws, "live", status="processing", window=pane)
    add_agent(db, worker_ws, "dead", status="idle", window="%99999")
    add_agent(db, worker_ws, "paused", status="paused")
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "--exited"])
    assert res.exit_code == 0, res.output
    assert "closed 2 agent(s)" in res.output
    assert db.get_agent("live").dismissed_at is None
    assert agents.is_alive(db.get_agent("live"))
    assert db.get_agent("dead").dismissed_at is not None
    assert db.get_agent("paused").dismissed_at is not None
    assert shown_ids(db, str(repo)) == ["live"]


def test_cli_close_needs_an_id_or_exited(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert CliRunner().invoke(app, ["close"]).exit_code == 1
    assert CliRunner().invoke(app, ["close", "x", "--exited"]).exit_code == 1


def test_resuming_shows_a_closed_agent_again(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add_agent(db, root, "boss", mode="interactive", status="paused", profile="supervisor")
    agents.close(db, "boss")
    monkeypatch.setattr(agents, "_launch", lambda *a, **k: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")  # resume carries the shell's key along
    agents.resume(db, "boss")
    assert db.get_agent("boss").dismissed_at is None
    assert db.get_session_credentials_with_timestamps("boss")["ANTHROPIC_API_KEY"][0] == "sk-from-env"


# -- the `x` key -----------------------------------------------------------------


def test_x_on_a_stopped_row_keeps_another_rows_arming():
    _, armed, _ = watch.close_request({"id": "live", "status": "processing"}, None, 100.0)
    close, still, _ = watch.close_request({"id": "dead", "status": "exited"}, armed, 101.0)
    assert close and still == armed
    close, _, _ = watch.close_request({"id": "live", "status": "processing"}, still, 102.0)
    assert close


def test_watch_sidebar_flag_reaches_the_loop(db, repo, monkeypatch):
    import sys

    seen = {}
    monkeypatch.setattr(watch.curses, "wrapper", lambda fn, root, sidebar: seen.setdefault("sidebar", sidebar))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(agents, "dismiss_sidebar", lambda *a: None)
    monkeypatch.chdir(repo)
    from brindle.cli import watch as watch_cmd

    watch_cmd(all_repos=False, once=False, sidebar=True)
    assert seen["sidebar"] is True


# -- pane ids tmux has reused ------------------------------------------------------


def reused_pane(db, ws, *, stale_status="idle", stale_result=None, age=0.0):
    """An old agent ("stale") whose recorded pane id tmux has since given to a
    newer agent ("current"), which is running in it."""
    pane = running_window(ws)
    old = time.time() - age
    db.add_agent(Agent("stale", ws.id, "developer", "claude", None, "assign", stale_status,
                       pane, stale_result, old - 1))
    db.add_agent(Agent("current", ws.id, "developer", "claude", None, "assign", "processing",
                       pane, None, old))
    return pane


def test_kill_of_a_stale_row_leaves_the_newer_agents_pane(db, worker_ws):
    pane = reused_pane(db, worker_ws)
    agents.kill(db, "stale")
    assert tmux.window_alive(pane) and db.get_agent("stale") is None


def test_quitting_brindle_from_the_sidebar_takes_two_presses():
    go, armed, notice = watch.quit_request(None, 100.0)
    assert not go and armed == ("quit", 100.0) and notice
    go, armed, notice = watch.quit_request(armed, 102.0)
    assert go and armed is None
    # Too slow: it arms again instead.
    go, armed, _ = watch.quit_request(("quit", 100.0), 110.0)
    assert not go and armed == ("quit", 110.0)
    # An armed close of some agent doesn't count as a first q.
    go, _, _ = watch.quit_request(("a1b2c3d4", 100.0), 101.0)
    assert not go


def test_quit_pauses_the_session(db, repo, monkeypatch):
    from brindle import cli

    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0",
                       None, time.time()))
    paused = []
    monkeypatch.setattr(cli.agents, "pause", lambda db, root_id: paused.append(root_id))
    assert CliRunner().invoke(cli.app, ["_quit", "boss"]).exit_code == 0
    assert paused == ["boss"]
