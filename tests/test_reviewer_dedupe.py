"""One reviewer per workspace and commit (a new commit replaces it), and the
"stopped without calling submit_review" notice: none once a verdict is
recorded for the current commit, at most once otherwise, from copse, and
naming a reviewer rather than a worker."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import sh
from copse import agents, gates, tmux, workspaces
from copse.db import Agent
from copse.profiles import Profile


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
                  "starting", "@0", None, time.time(), review_sha=review_sha)
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


def test_finished_reviewer_does_not_block_a_new_one(db, ws, monkeypatch):
    started = fake_spawn(monkeypatch)
    old = agents.request_review(db, None, ws, "reviewer")
    db.update_agent(old.id, result="verdict")
    agents.request_review(db, None, ws, "reviewer")
    assert len(started) == 2


def test_no_notice_once_the_reviewer_submitted_for_the_current_commit(db, ws, boss, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    rev = add_reviewer(db, ws)
    db.add_review(ws.id, gates.head(ws), rev.id, True, "fine")
    agents.tell_parent_unreported(db, rev)
    assert sent == []


def test_notice_once_from_copse_naming_a_reviewer(db, ws, boss, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    rev = add_reviewer(db, ws)
    agents.tell_parent_unreported(db, rev)
    agents.tell_parent_unreported(db, db.get_agent(rev.id))
    assert len(sent) == 1
    _, to_id, body = sent[0][:3]
    assert to_id == "boss" and len(sent[0]) == 3   # no sender: it comes from copse
    assert body.startswith("Reviewer rev1") and "Worker" not in body


def test_review_at_an_older_commit_still_gets_its_one_notice(db, ws, boss, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    rev = add_reviewer(db, ws)
    db.add_review(ws.id, "0" * 40, rev.id, True, "old")
    agents.tell_parent_unreported(db, rev)
    assert len(sent) == 1
