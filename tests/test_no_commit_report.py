"""A report with nothing to merge leaves the close/keep decision to its supervisor."""
import pytest

from brindle import agents, history, pipeline, usage
from brindle.config import RepoConfig
from brindle.db import Agent, Workspace


@pytest.mark.variant("real incident: 10 no-commit stress workers sat idle with no notice to their supervisor")
def test_no_commit_report_leaves_close_decision_to_supervisor(db, monkeypatch):
    ws = Workspace("proj/feat", "/fake/proj", "feat", "worktree", "feat", "main",
                   "/fake/feat", None, "fake-session", 1)
    db.add_workspace(ws)
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle",
                       "%fake-boss", None, 1))
    db.add_agent(Agent("worker", ws.id, "developer", "claude", "boss", "assign", "idle",
                       "%fake-worker", None, 1))
    monkeypatch.setattr(agents, "_branch_head", lambda ws: "head")
    monkeypatch.setattr(agents.git, "ahead_behind", lambda *a: (0, 0))
    monkeypatch.setattr(agents.git, "dirty_files", lambda *a, **kw: [])
    monkeypatch.setattr(history, "record_safely", lambda *a, **kw: None)
    monkeypatch.setattr(usage, "agent_usage", lambda *a: None)
    monkeypatch.setattr(agents, "warm_checks", lambda ws: None)
    monkeypatch.setattr(agents, "_notify_supervisor", lambda *a: None)
    monkeypatch.setattr(agents, "pulls_messages", lambda *a: True)
    monkeypatch.setattr(pipeline, "load_repo_config", lambda *a, **kw: RepoConfig(pipeline=True))
    started = []
    monkeypatch.setattr(agents, "start_review", lambda *a, **kw: started.append(ws.id))

    agents.report_result(db, "worker", "findings")

    worker = db.get_agent("worker")
    mail = db.unread("boss")
    assert not started
    assert worker.pipeline is None
    assert db.get_workspace(ws.id) is not None
    assert len(mail) == 1
    # Check the actionable tool/id, not the surrounding prose.
    assert f'remove_workspace(id="{ws.id}")' in mail[0].body
