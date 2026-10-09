"""A heavy task whose worker failed is retried once on developer-heavy (Fable),
only when Fable is enabled; otherwise it goes to the supervisor as before."""
import json
import time

import pytest

from brindle import agents, pipeline
from brindle.db import Task

from test_pipeline import add, piped  # noqa: F401  (the fixture)


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


def test_a_rejected_heavy_task_is_retried_once_on_developer_heavy(db, heavy):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    assert reject(db, ws) is True
    assert [r[0] for r in retries] == ["developer-heavy"]
    assert "the design is wrong" in retries[0][1] and "do the hard thing" in retries[0][1]
    assert retries[0][2]["isolate"] is False                  # same branch, same worktree
    assert db.get_agent("w1").pipeline is None
    assert db.get_agent("retry1").pipeline == "fixing"
    msg = db.pop_pending("boss")
    assert msg and "Retrying once on developer-heavy" in msg.body and "needs you" not in msg.body


def test_the_retry_never_starts_while_the_original_worker_is_alive(db, heavy, monkeypatch):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    state = {"alive": True}
    seen = []
    delegate = agents.delegate

    def closing(db_, agent_id, panes=None):
        seen.append("close")
        state["alive"] = False
        return db_.get_agent(agent_id)

    def checked(*a, **kw):
        seen.append(f"delegate(alive={state['alive']})")
        return delegate(*a, **kw)

    other = agents.is_alive
    monkeypatch.setattr(agents, "is_alive",
                        lambda agent, panes=None: state["alive"] if agent.id == "w1" else other(agent, panes))
    monkeypatch.setattr(agents, "close", closing)
    monkeypatch.setattr(agents, "delegate", checked)
    assert reject(db, ws) is True
    assert seen == ["close", "delegate(alive=False)"]
    assert [r[0] for r in retries] == ["developer-heavy"]


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


def test_the_retry_failing_too_goes_to_the_supervisor(db, heavy):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    reject(db, ws)
    db.pop_pending("boss")
    # the retry reports and its review rejects: no second retry
    agents.report_result(db, "retry1", "tried harder")
    pipeline.on_review(db, db.get_agent("rev1"), ws, False, "still wrong")
    assert len(retries) == 1
    msg = db.pop_pending("boss")
    assert msg and "needs you" in msg.body and "still wrong" in msg.body


def test_without_fable_the_failure_goes_to_the_supervisor(db, heavy):
    ws, retries, configure = heavy
    configure(fable_escalation=False)
    reject(db, ws)
    assert retries == []
    msg = db.pop_pending("boss")
    assert msg and "needs you" in msg.body
    assert db.get_agent("w1").pipeline is None


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


def test_a_cloud_account_can_switch_fable_on(db, heavy, monkeypatch):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    reject(db, ws)
    assert [r[0] for r in retries] == ["developer-heavy"]


def test_only_heavy_tasks_escalate(db, heavy):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    db.update_task("t1", weight="medium")
    reject(db, ws)
    assert retries == []
    assert "needs you" in db.pop_pending("boss").body


def test_failing_checks_retry_a_heavy_task_too(db, heavy, monkeypatch):
    ws, retries, configure = heavy
    configure(fable_escalation=True)
    agents.report_result(db, "w1", "did it")
    db.update_agent("w1", pipeline="ready")
    monkeypatch.setattr(pipeline, "merge", lambda *a, **kw: "Not merged. check `pytest` failed: 2 failed")
    pipeline._finish_locked(db, db.get_agent("w1"), ws, None)
    assert [r[0] for r in retries] == ["developer-heavy"]
    assert "check `pytest` failed" in retries[0][1]


def test_failing_checks_without_fable_go_to_the_supervisor(db, heavy, monkeypatch):
    ws, retries, configure = heavy
    configure(fable_escalation=False)
    agents.report_result(db, "w1", "did it")
    db.update_agent("w1", pipeline="ready")
    monkeypatch.setattr(pipeline, "merge", lambda *a, **kw: "Not merged. check `pytest` failed: 2 failed")
    pipeline._finish_locked(db, db.get_agent("w1"), ws, None)
    assert retries == []
    assert "needs you" in db.pop_pending("boss").body
