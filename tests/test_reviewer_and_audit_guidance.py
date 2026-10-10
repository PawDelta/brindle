import pytest

from brindle import agents, autopilot, pipeline
from brindle.db import DB, Workspace
from brindle.config import RepoConfig


def test_request_review_passes_files_and_done_when(db, repo, monkeypatch):
    ws = Workspace("ws-rev", str(repo), "feat/rev", "main", "main", "main", str(repo), 20000, "tmux-rev", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")
    worker.task = "Implement auth flow"
    worker.files = ["src/auth.py", "tests/test_auth.py"]
    worker.done_when = "pytest tests/test_auth.py passes"

    spawned_prompts = []
    def fake_spawn(db_arg, ws_arg, profile_arg, **kwargs):
        spawned_prompts.append(kwargs.get("prompt", ""))
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = "rev-123"
        return m

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "workspace_worker", lambda db_arg, ws_arg: worker)
    agents.request_review(db, None, ws)
    assert len(spawned_prompts) == 1
    prompt = spawned_prompts[0]
    assert "Declared files: src/auth.py, tests/test_auth.py" in prompt
    assert "finish line: pytest tests/test_auth.py passes" in prompt.lower()


def test_needs_supervisor_finding_skips_fix_rounds(db, repo, monkeypatch):
    ws = Workspace("ws-pip", str(repo), "feat/pip", "main", "main", "worktree", str(repo), 20000, "tmux-pip", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    reviewer = agents.spawn(db, ws, "reviewer", mode="review")
    db.update_agent(worker.id, pipeline="reviewing")

    sent_messages = []
    def fake_send(db_arg, to_id, msg, **kwargs):
        sent_messages.append((to_id, msg))

    monkeypatch.setattr(agents, "send_message", fake_send)
    monkeypatch.setattr(pipeline, "_retry_on_heavy", lambda *args: False)

    summary = "Finding 1: needs supervisor action: run external database migration"
    # Call _on_review with approved=False and "needs supervisor action"
    pipeline._on_review(db, reviewer, ws, approved=False, summary=summary)

    # Worker should NOT receive a fix round message; message should go straight to supervisor
    assert not any(to_id == worker.id for to_id, msg in sent_messages)
    assert any(to_id == supervisor.id and "needs supervisor action" in msg for to_id, msg in sent_messages)


def test_audit_prompt_includes_convergence_rules_and_prior_notes(db, repo):
    from brindle.db import Autopilot
    ap = Autopilot(
        root_id="boss",
        enabled=1,
        goal="Build feature X",
        detail="Must include metrics",
        state="auditing",
        note="Prior audit accepted omission of legacy export",
        progress=0,
        nudges=0,
        nudged_at=None,
        created_at=0.0,
    )
    prompt = autopilot.audit_prompt(db, ap, "headsha")
    assert "Disclosed and awaiting owner decision" in prompt
    assert "Prior audit notes / accepted decisions" in prompt
    assert "Prior audit accepted omission of legacy export" in prompt
