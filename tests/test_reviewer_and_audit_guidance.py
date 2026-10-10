import pytest

from brindle import agents, autopilot, pipeline
from brindle.db import DB, Workspace, Task, Autopilot
from brindle.config import RepoConfig


def test_request_review_passes_files_from_db_task_and_done_when(db, repo, monkeypatch):
    ws = Workspace("ws-rev", str(repo), "feat/rev", "main", "main", "main", str(repo), 20000, "tmux-rev", 0)
    db.add_workspace(ws)
    # Persisted worker in DB (no .files attribute on Agent)
    worker = agents.spawn(db, ws, "developer", mode="assign")
    worker.done_when = "pytest tests/test_auth.py passes"
    # Store task in DB with files
    t = Task("t-rev", str(repo), worker.id, None, ws.id, "developer", "Implement auth flow", "assign", 1, "feat/rev", worker.done_when, '["src/auth.py", "tests/test_auth.py"]', None, "started", 0.0)
    db.add_task(t)

    spawned_prompts = []
    def fake_spawn(db_arg, ws_arg, profile_arg, **kwargs):
        spawned_prompts.append(kwargs.get("prompt", ""))
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = "rev-123"
        return m

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    # worker loaded from DB has no worker.files
    worker_from_db = db.get_agent(worker.id)
    monkeypatch.setattr(agents, "workspace_worker", lambda db_arg, ws_arg: worker_from_db)

    agents.request_review(db, None, ws)
    assert len(spawned_prompts) == 1
    prompt = spawned_prompts[0]
    assert "Declared files: src/auth.py, tests/test_auth.py" in prompt
    assert "finish line: pytest tests/test_auth.py passes" in prompt.lower()
    assert "Worker profile: developer" in prompt


def test_submit_review_succeeds_for_completion_auditor(db, repo, monkeypatch):
    ws = Workspace("ws-aud", str(repo), "feat/aud", "main", "main", "main", str(repo), 20000, "tmux-aud", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    # Auditor spawned
    auditor = agents.spawn(db, ws, "reviewer", mode="review", parent_id=supervisor.id)
    # Set autopilot in auditing state
    db.add_autopilot(supervisor.id)
    db.update_autopilot(supervisor.id, auditor_id=auditor.id, audit_sha="headsha", state="auditing")

    # Even if detached:
    agents.detach(db, auditor.id)

    # Calling submit_review as auditor must NOT return 'Only a reviewer started with request_review...'
    ret = agents.submit_review(db, auditor.id, False, "Audit findings: gaps")
    assert "Only a reviewer started with request_review" not in ret
    assert "Audit recorded" in ret


def test_check_milestones_preserves_prior_audit_note_on_re_audit(db, repo, monkeypatch):
    ws = Workspace("ws-check", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-check", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    # Set autopilot with a prior audit note
    db.add_autopilot(supervisor.id)
    db.set_milestones(supervisor.id, [("M1", "true", "")])
    m = db.milestones(supervisor.id)[0]
    db.record_check(m.id, True, "passed")
    db.update_autopilot(supervisor.id, state="running", note="Prior audit accepted exclusion of X", audit_ok=0, audit_sha="oldsha")

    monkeypatch.setattr(autopilot, "audit_due", lambda ap_arg, cfg_arg, head_arg: "start")
    monkeypatch.setattr(autopilot, "_commit", lambda ws_arg: "newsha")
    autopilot.check_milestones(db, supervisor.id, ws)

    updated_ap = db.get_autopilot(supervisor.id)
    assert updated_ap.state == "auditing"
    assert updated_ap.note == "Prior audit accepted exclusion of X"


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


def test_audit_history_persists_across_need_user_and_user_spoke(db, repo):
    ws = Workspace("ws-hist", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-hist", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    db.add_autopilot(supervisor.id)

    # 1. Record an audit with gaps
    autopilot.record_audit(db, supervisor.id, False, "Gap 1: Missing auth tests")
    ap = db.get_autopilot(supervisor.id)
    assert ap.audit_history is not None
    assert "Gap 1: Missing auth tests" in ap.audit_history

    # 2. need_user overwrites note
    autopilot.need_user(db, supervisor.id, "Which model should we use?")
    ap = db.get_autopilot(supervisor.id)
    assert ap.note == "Which model should we use?"
    assert "Gap 1: Missing auth tests" in ap.audit_history

    # 3. user_spoke clears note
    autopilot.user_spoke(db, supervisor)
    ap = db.get_autopilot(supervisor.id)
    assert ap.note is None
    assert "Gap 1: Missing auth tests" in ap.audit_history

    # 4. record_accepted_decision appends
    autopilot.record_accepted_decision(db, supervisor.id, "Accepted omission of legacy export")
    ap = db.get_autopilot(supervisor.id)
    assert "Gap 1: Missing auth tests" in ap.audit_history
    assert "Accepted omission of legacy export" in ap.audit_history

    # 5. audit_prompt renders full audit_history
    prompt = autopilot.audit_prompt(db, ap, "headsha")
    assert "Gap 1: Missing auth tests" in prompt
    assert "Accepted omission of legacy export" in prompt


def test_all_builtin_reviewer_profiles_include_tiering_guidance():
    from brindle.profiles import load_profile

    for name in ("reviewer", "reviewer-codex", "reviewer-local"):
        prof = load_profile(name)
        prompt = prof.prompt
        assert "`blocker`" in prompt, f"{name} missing blocker"
        assert "`minor`" in prompt, f"{name} missing minor"
        assert "`needs supervisor action" in prompt, f"{name} missing needs supervisor action"
        assert "actionable by the worker" in prompt, f"{name} missing actionability"
