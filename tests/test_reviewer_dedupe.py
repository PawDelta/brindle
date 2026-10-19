"""One reviewer per workspace and commit (a new commit replaces it), and the
"stopped without calling submit_review" notice: none once a verdict is
recorded for the current commit, at most once otherwise, from brindle, and
naming a reviewer rather than a worker."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, gates, launch_health, tmux, workspaces
from brindle.db import Agent, Workspace
from brindle.profiles import Profile


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, *x, **k: True)
    monkeypatch.setattr(agents, "load_profile",
                        lambda name, repo_root=None: Profile(name, "", "claude", ""))


@pytest.fixture
def ws(db, repo):
    w = workspaces.create(db, str(repo), "feat").workspace
    (Path(w.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(w.path))
    return w


@pytest.fixture
def boss(db, ws):
    a = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0",
              None, time.time())
    db.add_agent(a)
    return a


def add_reviewer(db, ws, agent_id="rev1", sha=None):
    a = Agent(agent_id, ws.id, "reviewer", "claude", "boss", "review", "processing", "@0",
              None, time.time())
    db.add_agent(a)
    db.update_agent(agent_id, review_sha=sha)
    return a


def fake_spawn(monkeypatch):
    started = []

    def spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review",
              review_sha=None, **kw):
        a = Agent(f"new{len(started)}", ws_.id, profile, "claude", parent_id, mode,
                  "starting", "@0", None, time.time(), review_sha=review_sha, task=prompt)
        db_.add_agent(a)
        started.append(a)
        return a

    monkeypatch.setattr(agents, "spawn", spawn)
    monkeypatch.setattr(agents, "close", lambda db_, aid, *a, **k: db_.update_agent(aid, status="done"))
    return started


def test_second_request_for_the_same_commit_reuses_the_running_reviewer(db, ws, monkeypatch):
    started = fake_spawn(monkeypatch)
    first = agents.request_review(db, None, ws, "reviewer")
    second = agents.request_review(db, None, ws, "reviewer")
    assert second.id == first.id
    assert len(started) == 1


def test_new_commit_replaces_the_old_reviewer(db, ws, monkeypatch):
    started = fake_spawn(monkeypatch)
    old = agents.request_review(db, None, ws, "reviewer")
    (Path(ws.path) / "more.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm more", Path(ws.path))
    new = agents.request_review(db, None, ws, "reviewer")
    assert new.id != old.id
    assert len(started) == 2
    assert db.get_agent(old.id).status == "done"


@pytest.fixture
def mcp_review(db, tmp_path, monkeypatch):
    ws = Workspace("repo/feat", str(tmp_path), "feat", "worktree", "feat", "main",
                   str(tmp_path), None, "session", time.time())
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0",
                 None, time.time())
    db.add_agent(boss)
    monkeypatch.setattr(gates, "head", lambda ws: "old-sha")
    started = fake_spawn(monkeypatch)
    reports = []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body: reports.append(to))
    monkeypatch.setattr(launch_health, "mcp_failed", lambda *a: True)
    monkeypatch.setattr(agents, "_detach_orphan", lambda argv: launch_health.recover_reviewer(db, argv[-1]))

    def arm(a):
        db.update_agent(a.id, session_ref="session-" + a.id)
        a = db.get_agent(a.id)
        launch_health.prepare(db, a, "account")
        return a

    return ws, boss, started, reports, arm


@pytest.mark.parametrize("trigger", ["hook", "request"])
def test_mcp_failure_replaces_reviewer_once_per_commit(db, mcp_review, monkeypatch, trigger):
    ws, boss, started, reports, arm = mcp_review
    old = arm(agents.request_review(db, boss, ws, "reviewer"))
    if trigger == "hook":
        launch_health.check_mcp(db, old)
    new = agents.request_review(db, boss, ws, "reviewer")
    assert new.id != old.id and new.review_sha == old.review_sha
    assert new.task == old.task and new.parent_id == old.parent_id
    assert db.get_agent(old.id).status == "done"
    launch_health.check_mcp(db, old)  # stale hook must respect the replacement
    launch_health.recover_reviewer(db, old.id)  # a queued helper lost to an explicit request
    assert agents.request_review(db, boss, ws, "reviewer").id == new.id
    assert len(started) == 2

    launch_health.check_mcp(db, arm(new))
    launch_health.check_mcp(db, db.get_agent(new.id))
    assert db.get_agent(new.id).status == "done"
    with pytest.raises(agents.AgentError):
        agents.request_review(db, boss, ws, "reviewer")
    assert len(started) == 2 and reports == [boss.id]
    # A new commit gets its own recovery budget; no real git process is needed.
    monkeypatch.setattr(gates, "head", lambda ws: "new-sha")
    next_review = arm(agents.request_review(db, boss, ws, "reviewer"))
    launch_health.check_mcp(db, next_review)
    assert len(started) == 4 and started[-1].review_sha == "new-sha"


@pytest.mark.parametrize("failure", ["moved", "spawn"])
def test_mcp_recovery_reports_when_replacement_cannot_start(db, mcp_review, monkeypatch, failure):
    ws, boss, started, reports, arm = mcp_review
    old = arm(agents.request_review(db, boss, ws, "reviewer"))
    if failure == "moved":
        monkeypatch.setattr(gates, "head", lambda ws: "new-sha")
    else:
        def unavailable(*a, **k):
            raise agents.AgentError("launch failed")
        monkeypatch.setattr(agents, "spawn", unavailable)
    launch_health.check_mcp(db, old)
    assert db.get_agent(old.id).status == "done"
    assert len(started) == 1 and reports == [boss.id]


def test_no_notice_once_the_reviewer_submitted_for_the_current_commit(db, ws, boss, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    rev = add_reviewer(db, ws)
    db.add_review(ws.id, gates.head(ws), rev.id, True, "fine")
    agents.tell_parent_unreported(db, rev)
    assert sent == []


def test_review_at_an_older_commit_still_gets_its_one_notice(db, ws, boss, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    rev = add_reviewer(db, ws)
    db.add_review(ws.id, "0" * 40, rev.id, True, "old")
    agents.tell_parent_unreported(db, rev)
    assert len(sent) == 1
