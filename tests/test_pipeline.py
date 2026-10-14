"""The pipeline doesn't stall on a review: a reviewer that stops without a
verdict gets one fallback, and a review summary whose supervisor-action
section is negated isn't an escalation."""
import shutil

import pytest

from brindle import agents, pipeline, workspaces
from test_merge_order import add
from test_pipeline_idempotent import drain


def test_reviewer_without_verdict_gets_fallback(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")  # busy: messages queue
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", result="done", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer-gemini", parent="boss")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    started = []

    def fallback(db_, caller, ws_, profile=None, focus=None, cfg=None, check_summary=None):
        started.append(profile)
        add(db, ws, "rev1", "review", profile, parent=caller.id)

    monkeypatch.setattr(agents, "request_review", fallback)

    agents.tell_parent_unreported(db, db.get_agent("rev0"))
    assert started == ["reviewer"]
    assert db.get_agent("w1").pipeline == "reviewing"

    agents.tell_parent_unreported(db, db.get_agent("rev1"))   # the fallback stops too
    assert started == ["reviewer"]
    assert db.get_agent("w1").pipeline is None
    assert any("review of `feat` never came back" in b for b in drain(db))


def test_reviewer_stopping_on_a_deleted_worktree_tells_the_supervisor(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", result="done", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer-gemini", parent="boss")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    shutil.rmtree(ws.path)   # removed outside brindle: the row outlives it

    assert agents.tell_parent_unreported(db, db.get_agent("rev0"))
    assert db.get_agent("w1").pipeline is None
    assert any(f"worktree {ws.path} no longer exists" in b for b in drain(db))


@pytest.mark.parametrize("summary", [
    "Needs supervisor action: None — all fixable by the worker.",
    pytest.param("Needs supervisor action: No items.",
                 marks=pytest.mark.variant("incident: a reviewer's 'No items.' stalled the pipeline")),
])
def test_supervisor_action_negations(summary):
    assert pipeline.has_supervisor_action(summary) is False
