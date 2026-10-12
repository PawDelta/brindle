"""A heavy task whose worker failed is retried once on developer-heavy (Fable),
only when Fable is enabled; otherwise it goes to the supervisor as before."""
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, pipeline, workspaces
from brindle.db import Task
from test_merge_order import add


@pytest.fixture
def piped(db, repo, monkeypatch):
    """A supervisor in the checkout, a worker on a branch with a commit, and a
    reviewer that brindle can start without a real process."""
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"review": True, "auto_merge_default_branch": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")  # busy: messages queue
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", Path(ws.path))
    add(db, ws, "w1", "assign", parent="boss", status="processing")
    started = []

    def fake_review(db_, caller, ws_, profile=None, focus=None, cfg=None):
        add(db_, ws_, f"rev{len(started)}", "review", "reviewer", parent=caller.id)
        started.append(ws_.id)
        return db_.get_agent(f"rev{len(started) - 1}")

    monkeypatch.setattr(agents, "request_review", fake_review)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_detach", lambda argv: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    return root, ws, started


@pytest.fixture
def heavy(db, piped, repo, monkeypatch):
    """A piped branch whose task was sized heavy, no review rounds left, and a
    ``delegate`` that records the retry instead of starting a process."""
    root, ws, started = piped
    for name in pipeline.CLOUD_ENV:
        monkeypatch.delenv(name, raising=False)
    db.add_task(Task("t1", str(repo), "w1", "boss", root.id, "developer", "do the hard thing", "assign",
                     1, None, None, None, None, "started", time.time(), weight="heavy"))
    db.update_agent("w1", task="do the hard thing")
    retries = []

    def fake_delegate(db_, caller, ws_, profile, task, mode, **kw):
        add(db_, ws_, "retry1", mode, profile, parent=caller.id, status="processing")
        retries.append((profile, task, kw))
        return db_.get_agent("retry1"), ws_

    monkeypatch.setattr(agents, "delegate", fake_delegate)
    is_alive = agents.is_alive
    monkeypatch.setattr(agents, "is_alive",
                        lambda agent, panes=None: False if agent.id == "w1" else is_alive(agent, panes))

    def configure(**kw):
        (repo / ".brindle" / "config.json").write_text(json.dumps(
            {"review": True, "auto_merge_default_branch": True, "review_rounds": 0, **kw}))

    return ws, retries, configure


def reject(db, ws, summary="the design is wrong"):
    agents.report_result(db, "w1", "did it")
    return pipeline.on_review(db, db.get_agent("rev0"), ws, False, summary)


def test_no_retry_when_the_original_worker_cannot_be_stopped(db, heavy, monkeypatch):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    other = agents.is_alive
    monkeypatch.setattr(agents, "is_alive",
                        lambda agent, panes=None: True if agent.id == "w1" else other(agent, panes))
    monkeypatch.setattr(agents, "close", lambda db_, agent_id, panes=None: db_.get_agent(agent_id))
    reject(db, ws)
    assert retries == []                       # two agents never share the worktree
    msg = db.pop_pending("boss")
    assert msg and "needs you" in msg.body


def test_fable_is_off_by_default_on_a_cloud_account(db, heavy, monkeypatch):
    ws, retries, configure = heavy
    configure()
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    reject(db, ws)
    assert retries == []
    assert "needs you" in db.pop_pending("boss").body


def test_fable_is_on_by_default_off_the_cloud(db, heavy):
    ws, retries, configure = heavy
    configure()
    reject(db, ws)
    assert [r[0] for r in retries] == ["developer-heavy"]


def test_failing_checks_retry_a_heavy_task_too(db, heavy, monkeypatch):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    agents.report_result(db, "w1", "did it")
    db.update_agent("w1", pipeline="ready")
    monkeypatch.setattr(pipeline, "merge", lambda *a, **kw: "Not merged. check `pytest` failed: 2 failed")
    pipeline._finish_locked(db, db.get_agent("w1"), ws, None)
    assert [r[0] for r in retries] == ["developer-heavy"]
    assert "check `pytest` failed" in retries[0][1]
