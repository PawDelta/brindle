"""A second `brindle` in a checkout where a session is running asks what to
do instead of always pausing the first one."""

import time

import pytest

from brindle import agents, cli, workspaces
from brindle.db import Agent



@pytest.fixture
def running(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("sup1", ws.id, "supervisor", "claude", None, "interactive", "idle", "%5", None,
                       time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "sup1")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.chdir(repo)
    return ws


def _start(**kw):
    cli.start(agent="supervisor", prompt=None, provider=None, attach=True, watch=True,
              autopilot=False, branch=None, worktree=None, **kw)


def test_open_attaches_to_the_running_session(db, running, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_ask_about_running", lambda a: "o")
    monkeypatch.setattr(cli, "_attach", lambda ws, window=None: seen.append((ws.id, window)))
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: pytest.fail("must not start another"))
    _start()
    assert seen == [(running.id, "%5")]
    assert db.get_agent("sup1").status == "idle"            # not paused


def test_any_number_of_sessions_each_get_their_own_worktree(db, running, monkeypatch):
    spawned = []
    monkeypatch.setattr(cli, "_ask_about_running", lambda a: "n")
    monkeypatch.setattr(cli, "_attach", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_cull_detached", lambda *a: None)
    monkeypatch.setattr(cli, "_local_models_detached", lambda *a: None)
    monkeypatch.setattr(agents, "spawn", lambda db_, ws, *a, **k: spawned.append(ws) or
                        Agent(f"sup{len(spawned) + 1}", ws.id, "supervisor", "claude", None, "interactive",
                              "idle", "%6", None, time.time()))
    for _ in range(3):
        _start()
    assert [ws.branch for ws in spawned] == ["brindle/session-2", "brindle/session-3", "brindle/session-4"]
    assert len({ws.path for ws in spawned}) == 3
    assert db.get_agent("sup1").status == "idle"            # the first keeps running
