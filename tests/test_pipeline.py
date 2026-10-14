"""The pipeline doesn't stall on a review: a reviewer that stops without a
verdict gets one fallback, and a review summary whose supervisor-action
section is negated isn't an escalation."""
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, gates, pipeline, workspaces
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


def test_stalled_reviewer_reopens_own_session_once_before_fallback(db, repo, tmp_path, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", result="done", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer", parent="boss")
    transcript = tmp_path / "sess-rev0.jsonl"
    transcript.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"checking"}]}}\n')
    db.update_agent("rev0", transcript_path=str(transcript))
    alive = {"rev0": True}
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: alive.get(a.id, False))
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    launched, fallbacks = [], []

    def fake_launch(db_, a, ws_, *, prompt, resume, **kw):
        prof = agents.load_profile(a.profile, ws_.repo_root)
        ctx = agents.LaunchContext(a.id, prof, prompt, resume=resume, cwd=ws_.path, mode=a.mode)
        launched.append(agents.get_provider(a.provider).command(ctx))

    monkeypatch.setattr(agents, "_launch", fake_launch)
    monkeypatch.setattr(agents, "request_review", lambda *a, **k: fallbacks.append(1))

    # 1. Recent transcript write on a live reviewer -> treated as still active.
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is False
    assert launched == [] and fallbacks == []

    # 2. Dead reviewer with no recorded session_ref -> never uses --continue or --last.
    old = time.time() - 120
    os.utime(transcript, (old, old))
    alive["rev0"] = False
    db.update_agent("rev0", transcript_path=None, session_ref=None)
    assert agents.reopen_reviewer(db, db.get_agent("rev0"), ws) is False
    assert launched == []

    # 3. Dead reviewer with its own recorded session -> reopened once before any fallback.
    db.update_agent("rev0", session_ref="sess-rev0", transcript_path=str(transcript))
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(launched) == 1 and fallbacks == []
    assert "--resume" in launched[0] and "sess-rev0" in launched[0]
    assert "--continue" not in launched[0] and "--last" not in launched[0]
    assert db.get_agent("rev0").reopened_at is not None

    # 4. Stalling again after reopen -> does not loop; falls back to request_review.
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(launched) == 1 and len(fallbacks) == 1


def test_fallback_reviewer_reuses_cached_checks_and_partial_findings(db, repo, tmp_path, monkeypatch):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["pytest -q"]}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "big.py").write_text("".join(f"line_{i} = {i}\n" for i in range(50)))
    sh("git add big.py && git commit -qm big", Path(ws.path))
    sha = gates.head(ws)
    db.set_check(ws.id, sha, "pytest -q", True, "50 passed")
    transcript = tmp_path / "rev0.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Blocker in big.py line 12"}]}}\n'
    )
    old = time.time() - 120
    os.utime(transcript, (old, old))
    add(db, ws, "w1", "assign", parent="boss", result="added big.py", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer", parent="boss")
    db.update_agent("rev0", review_sha=sha, transcript_path=str(transcript))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_review_profile", lambda db_, ws_, prof, cfg: prof or "reviewer")
    monkeypatch.setattr(gates, "check_summary", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-ran checks")))
    spawned = []
    monkeypatch.setattr(
        agents, "spawn",
        lambda db_, ws_, prof, prompt=None, **kw: spawned.append((prof, prompt)),
    )

    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(spawned) == 1
    prof, prompt = spawned[0]
    assert prof == "reviewer"
    assert "Blocker in big.py line 12" in prompt
    assert "PASS `pytest -q`" in prompt


def test_small_diff_stalled_reviewer_escalates_summary_to_supervisor(db, repo, tmp_path, monkeypatch):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["pytest -q"]}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "small.py").write_text("x = 1\ny = 2\n")
    sh("git add small.py && git commit -qm small", Path(ws.path))
    sha = gates.head(ws)
    db.set_check(ws.id, sha, "pytest -q", True, "2 passed")
    transcript = tmp_path / "rev0.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Looks clean so far"}]}}\n'
    )
    old = time.time() - 120
    os.utime(transcript, (old, old))
    add(db, ws, "w1", "assign", parent="boss", result="added small.py", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer", parent="boss")
    db.update_agent("rev0", review_sha=sha, transcript_path=str(transcript))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "request_review", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned fallback")))

    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert db.get_agent("w1").pipeline is None
    [body] = drain(db)
    assert "small.py" in body and "PASS `pytest -q`" in body
    assert "Looks clean so far" in body and "added small.py" in body


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

