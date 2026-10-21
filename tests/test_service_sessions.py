"""brindle.service.sessions: the CLI's prompts come back as a Conflict the
caller answers, and the answer is what the next call does."""

import time

import pytest
from conftest import sh

from brindle import agents, doctor, procs, workspaces
from brindle.db import Agent, Workspace
from brindle.service import sessions as svc
from brindle.service.errors import ServiceError


def add(db, ws, aid, status="idle", since=None):
    db.add_agent(Agent(aid, ws.id, "supervisor", "claude", None, "interactive", status, f"%{aid}",
                       None, time.time(), since))
    return db.get_agent(aid)


@pytest.fixture
def running(db, repo, monkeypatch):
    """sup1 runs in the checkout; pausing it is recorded, starting one is faked."""
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "sup1")
    paused = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "sup1" and not paused)
    monkeypatch.setattr(agents, "pause", lambda db_, aid, **kw: paused.append(aid))
    monkeypatch.setattr(doctor, "preflight", lambda name: [])
    monkeypatch.setattr(svc, "cull_detached", lambda *a: None)
    monkeypatch.setattr(agents, "spawn", lambda db_, ws_, profile, **kw: Agent(
        "sup2", ws_.id, profile, "claude", None, "interactive", "starting", "%6", None, time.time()))
    return paused


@pytest.mark.parametrize("choice, branch, paused", [
    ("o", None, []),
    ("n", "brindle/session-2", []),
    ("p", "main", ["sup1"]),
])
def test_start_asks_about_a_running_session_then_does_what_was_chosen(db, repo, running, choice,
                                                                      branch, paused):
    asked = svc.start(db, str(repo))
    assert isinstance(asked, svc.Conflict)
    conflict = asked.to_json()["conflict"]
    assert [s["id"] for s in conflict["sessions"]] == ["sup1"] and conflict["default"] == 0
    got = svc.start(db, str(repo), choice=choice, session_id="sup1")
    if branch is None:
        assert isinstance(got, svc.Opened) and got.root.id == "sup1"
    else:
        assert got.workspace.branch == branch and got.to_json()["id"] == "sup2"
    assert running == paused


def test_closing_a_session_dismisses_and_stops_every_agent_and_keeps_the_branches(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    sup = add(db, root, "sup")
    for n in (1, 2):
        sh(f"git branch feat-{n}", repo)
        db.add_workspace(Workspace(f"w-{n}", root.repo_root, f"feat-{n}", "worktree", f"feat-{n}", "main",
                                   str(repo) + f"-{n}", None, "brindle-x", time.time()))
        db.add_agent(Agent(f"wk{n}", f"w-{n}", "developer", "claude", sup.id, "assign", "idle", f"%w{n}",
                           None, time.time()))
    stopped = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id not in stopped)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: stopped.append(a.id))
    monkeypatch.setattr(procs, "stop", lambda *a, **k: None)
    closed = svc.close_session(db, "sup")
    assert sorted(a.id for a, _ in closed) == ["sup", "wk1", "wk2"] and all(was for _, was in closed)
    assert sorted(stopped) == ["sup", "wk1", "wk2"]
    assert all(db.get_agent(i).dismissed_at for i in ("sup", "wk1", "wk2"))
    assert {"feat-1", "feat-2"} <= set(sh("git branch --format='%(refname:short)'", repo).split())
    assert svc.close_session(db, "sup") == []     # already closed: nothing to do


def test_continue_refuses_to_pause_the_session_running_it(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    caller = add(db, ws, "caller")
    add(db, ws, "target", status="paused")
    resumed = []
    # The caller is the one live session; no tmux state decides that.
    monkeypatch.setattr(svc, "live_here", lambda db_, ws_: [caller])
    monkeypatch.setattr(agents, "resume", lambda *a, **k: resumed.append(a) or [])
    monkeypatch.setenv("BRINDLE_AGENT_ID", caller.id)
    with pytest.raises(ServiceError) as e:
        svc.continue_(db, ws, "target")
    assert e.value.http_status == 409 and resumed == []


def test_continue_lists_the_paused_sessions_then_resumes_the_one_picked(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "new", status="paused", since=200)
    add(db, ws, "old", status="paused", since=100)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
    monkeypatch.setattr(agents, "resume", lambda db_, rid, **kw: [db.get_agent(rid)])
    asked = svc.continue_(db, ws, ask=True)
    assert [s.root.id for s in asked.sessions] == ["new", "old"]
    got = svc.continue_(db, ws, "old")
    assert got.to_json()["state"] == "resumed" and [s.root.id for s in got.others] == ["new"]
