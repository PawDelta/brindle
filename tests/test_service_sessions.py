"""brindle.service.sessions: the CLI's prompts come back as a Conflict the
caller answers, and the answer is what the next call does."""

import dataclasses
import time

import pytest

from brindle import agents, codemap, doctor, workspaces
from brindle.db import Agent
from brindle.service import sessions as svc


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
    ("p", "main", ["sup1"]),
])
def test_start_asks_about_a_running_session_then_does_what_was_chosen(db, repo, running, choice,
                                                                      branch, paused):
    db.update_agent("sup1", task="Build the screen.\nKeep every detail of this request.")
    asked = svc.place(db, workspaces.adopt_root(db, str(repo)))
    assert isinstance(asked, svc.Conflict)
    conflict = asked.to_json()["conflict"]
    assert [s["id"] for s in conflict["sessions"]] == ["sup1"] and conflict["default"] == 0
    assert conflict["sessions"][0]["request"] == db.get_agent("sup1").task
    got = svc.start(db, str(repo), choice=choice, session_id="sup1")
    if branch is None:
        assert isinstance(got, svc.Opened) and got.root.id == "sup1"
    else:
        assert got.workspace.branch == branch and got.to_json()["id"] == "sup2"
    assert running == paused


@pytest.mark.parametrize("start, branch, rebuild_fails", [
    ({"choice": "n", "session_id": "sup1"}, "brindle/session-2", False),
    # Options → Branch: a worktree checkout_for creates; a failed rebuild still leaves its setup to run.
    ({"branch": "feat/cache"}, "feat/cache", True),
])
def test_a_new_worktree_session_launches_before_its_setup_and_code_map_rebuild(db, repo, running, monkeypatch,
                                                                                start, branch, rebuild_fails):
    """Under load a new session's worktree setup (npm ci) and graphify's rebuild
    (every core) ran before its supervisor started: they now run after it."""
    order = []

    def rebuild(*a, **kw):
        order.append("rebuild")
        if rebuild_fails:
            raise OSError("graph lock unreadable")

    real_config = workspaces.load_repo_config
    monkeypatch.setattr(workspaces, "load_repo_config", lambda root, **kw: dataclasses.replace(
        real_config(root, **kw), setup=["npm ci"]))
    monkeypatch.setattr(workspaces, "run_setup_commands", lambda ws, cfg, **kw: order.append("setup"))
    monkeypatch.setattr(codemap, "seed_session", lambda checkout, repo_root=None: codemap.refresh_later(checkout, repo_root))
    monkeypatch.setattr(codemap, "_request_refresh", rebuild)
    monkeypatch.setattr(agents, "spawn", lambda db_, ws_, profile, **kw: order.append("launch") or Agent(
        "sup2", ws_.id, profile, "claude", None, "interactive", "starting", "%6", None, time.time()))
    monkeypatch.setattr(svc, "after_launch", lambda work: work())
    got = svc.start(db, str(repo), **start)
    assert got.workspace.branch == branch
    assert order == ["launch", "rebuild", "setup"]


@pytest.mark.variant("Send beside a paused session only: no running session to pick, and it must not start in the checkout")
def test_a_new_worktree_is_made_when_asked_with_only_a_paused_session_here(db, repo, running, monkeypatch):
    """The app's Send goes on a new branch beside a paused session too: no
    session runs, so none is picked, and it must not start in the checkout."""
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
    db.update_agent("sup1", status="paused")
    monkeypatch.setattr(svc, "after_launch", lambda work: None)
    got = svc.start(db, str(repo), choice="n")
    assert got.workspace.branch == "brindle/session-2"
    assert got.workspace.path != workspaces.adopt_root(db, str(repo)).path


@pytest.mark.parametrize("caller_id", [pytest.param("caller", id="concurrent_sessions",
    marks=pytest.mark.variant("Continuing another session must not pause the live session issuing the command"))])
def test_continue_refuses_to_pause_the_session_running_it(db, repo, monkeypatch, caller_id):
    ws = workspaces.adopt_root(db, str(repo))
    caller = add(db, ws, caller_id)
    add(db, ws, "other")
    add(db, ws, "target", status="paused")
    resumed, paused = [], []
    monkeypatch.setattr(agents, "is_alive",
                        lambda a, panes=None: a.id in (caller_id, "other") or bool(resumed))
    monkeypatch.setattr(agents, "pane_owners", lambda db_, panes=None: {})
    monkeypatch.setattr(agents, "pause", lambda db_, aid, **kw: paused.append(aid))
    monkeypatch.setattr(agents, "resume", lambda db_, rid, **kw: resumed.append(rid) or [db.get_agent(rid)])
    monkeypatch.setenv("BRINDLE_AGENT_ID", caller.id)
    got = svc.continue_(db, ws, "target")
    assert got.state == "resumed" and resumed == ["target"] and paused == []
    # Both now run here; a command from the caller's pane means the caller's session.
    assert svc.session_root(db, str(repo)) == caller_id


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
