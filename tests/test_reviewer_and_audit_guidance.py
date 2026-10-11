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


def test_audit_prompt_re_audit_bounded_incremental(db, repo):
    # Initial audit without history uses AUDIT template
    ap_initial = Autopilot(
        root_id="boss_initial",
        enabled=1,
        goal="Build feature X",
        detail="Must include metrics",
        state="auditing",
        note=None,
        audit_history=None,
        progress=0,
        nudges=0,
        nudged_at=None,
        created_at=0.0,
    )
    prompt_initial = autopilot.audit_prompt(db, ap_initial, "headsha")
    assert "Be adversarial" in prompt_initial
    assert "bounded re-audit" not in prompt_initial

    # Re-audit with history uses RE_AUDIT template with strict bounding
    ap_re = Autopilot(
        root_id="boss_re",
        enabled=1,
        goal="Build feature X",
        detail="Must include metrics",
        state="auditing",
        note=None,
        audit_history="Audit of abc1234 (gaps found):\nMissing edge case test for null input",
        progress=0,
        nudges=0,
        nudged_at=None,
        created_at=0.0,
    )
    prompt_re = autopilot.audit_prompt(db, ap_re, "headsha2")
    assert "bounded re-audit" in prompt_re
    assert "DO NOT move the goalposts" in prompt_re
    assert "Strictly bounded scope" in prompt_re
    assert "Missing edge case test for null input" in prompt_re



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
    import re
    from brindle.profiles import load_profile

    # Comprehensive structural parsing of tiering guidance across all reviewer profiles
    profiles_to_check = ("reviewer", "reviewer-gemini", "reviewer-codex", "reviewer-local")
    for name in profiles_to_check:
        prof = load_profile(name)
        prompt = prof.prompt

        # 1. Structural schema parsing: Classification tiers must be defined with clear semantics
        # Must have blocker bullet with must be fixed semantics
        blocker_match = re.search(r"-\s*`blocker`:\s*must be fixed before the branch can merge", prompt, re.I)
        assert blocker_match, f"{name} missing structural blocker specification"

        # Must have minor bullet with style nits / non-blocking semantics
        minor_match = re.search(r"-\s*`minor`:\s*style nits, minor comments or suggestions that do not block merging", prompt, re.I)
        assert minor_match, f"{name} missing structural minor specification"

        # Must have needs supervisor action with details and explanation
        supervisor_match = re.search(
            r"-\s*`needs supervisor action:\s*<details>`:\s*an issue the worker cannot fix within its allowed tools or permissions",
            prompt,
            re.I,
        )
        assert supervisor_match, f"{name} missing structural supervisor action specification"

        # 2. Actionability guidance: instruction to assess worker capabilities
        actionable_match = re.search(
            r"Check whether your findings are actionable by the worker:\s*if a fix requires capabilities outside the worker's brief,\s*flag it as `needs supervisor action`",
            prompt,
            re.I,
        )
        assert actionable_match, f"{name} missing actionable evaluation rule"

        # 3. Substance verification for finish line
        substance_match = re.search(
            r"Check the finish line \(`done_when`\) for substance,\s*not just placeholder presence",
            prompt,
            re.I,
        )
        assert substance_match, f"{name} missing finish line substance requirement"

        # 4. Strict approval condition
        approval_match = re.search(
            r"Approve \(`approved=true`\) only if there are no blockers and no supervisor-action items\.\s*If only minor findings remain,\s*approve and list them under minor findings",
            prompt,
            re.I,
        )
        assert approval_match, f"{name} missing approval criteria rule"


def test_reviewer_cross_provider_parity_and_allowed_tools():
    from brindle.profiles import load_profile

    base_tools = load_profile("reviewer").allowed_tools
    for name in ("reviewer-gemini", "reviewer-codex", "reviewer-local"):
        prof = load_profile(name)
        assert prof.permission_mode == "dontAsk", f"{name} missing dontAsk permission_mode"
        assert prof.allowed_tools == base_tools, f"{name} allowed_tools does not match reviewer"


def test_reviewer_tiered_guidance_application_and_routing_scenarios(db, repo, monkeypatch):
    """Scenario tests verifying that the tiered guidance is effectively applied and routed:
    - Minor findings only -> approved, no supervisor action, no fix loop
    - Blocker findings -> not approved, worker fix round triggered
    - Needs supervisor action -> not approved, worker fix loop bypassed, routed to supervisor
    """
    from brindle import agents, pipeline
    from brindle.db import Workspace

    ws = Workspace("ws-tier-scenarios", str(repo), "feat/tier", "main", "main", "worktree", str(repo), 20000, "tmux-tier", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")

    sent_messages = []
    def fake_send(db_arg, to_id, msg, **kwargs):
        sent_messages.append((to_id, msg))

    monkeypatch.setattr(agents, "send_message", fake_send)
    monkeypatch.setattr(pipeline, "_retry_on_heavy", lambda *args: False)
    monkeypatch.setattr(agents, "is_alive", lambda *args, **kwargs: True)

    # Scenario 1: Minor findings only - reviewer approves according to guidance
    minor_summary = "All checks pass.\n- minor: consider renaming variable `x` to `timeout_seconds` for clarity.\nNeeds supervisor action: None."
    assert not pipeline.has_supervisor_action(minor_summary)
    worker1 = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    reviewer1 = agents.spawn(db, ws, "reviewer", mode="review")
    db.update_agent(worker1.id, pipeline="reviewing")
    handled, ready = pipeline._on_review(db, reviewer1, ws, approved=True, summary=minor_summary)
    assert handled is True
    # In accordance with guidance, minor findings do not trigger worker fix rounds
    assert not any(to_id == worker1.id for to_id, _ in sent_messages)
    # Supervisor receives the approved notification with minor findings
    assert any(to_id == supervisor.id and "approved" in msg and "minor" in msg for to_id, msg in sent_messages)

    # Scenario 2: Blocker actionable by worker - reviewer rejects, triggers worker fix round
    sent_messages.clear()
    blocker_summary = "Review failed.\n- blocker: missing check for None in process_data() leading to AttributeError.\nNeeds supervisor action: None."
    assert not pipeline.has_supervisor_action(blocker_summary)
    worker2 = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    reviewer2 = agents.spawn(db, ws, "reviewer", mode="review")
    db.update_agent(worker2.id, pipeline="reviewing")
    handled, ready = pipeline._on_review(db, reviewer2, ws, approved=False, summary=blocker_summary)
    assert ready is False
    assert any(to_id == worker2.id and "missing check for None" in msg for to_id, msg in sent_messages)
    assert not any(to_id == supervisor.id for to_id, _ in sent_messages)

    # Scenario 3: Needs supervisor action - reviewer flags capability gap, routes directly to supervisor
    sent_messages.clear()
    sup_action_summary = "Review finding:\n- needs supervisor action: requires production database credentials for migration."
    assert pipeline.has_supervisor_action(sup_action_summary)
    worker3 = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    reviewer3 = agents.spawn(db, ws, "reviewer", mode="review")
    db.update_agent(worker3.id, pipeline="reviewing")
    handled, ready = pipeline._on_review(db, reviewer3, ws, approved=False, summary=sup_action_summary)
    assert ready is False
    assert not any(to_id == worker3.id for to_id, _ in sent_messages)
    assert any(to_id == supervisor.id and "needs supervisor action" in msg for to_id, msg in sent_messages)


def test_record_audit_decision_mcp_records_for_subsequent_audit(db, repo, monkeypatch):
    from brindle import mcp_server
    ws = Workspace("ws-mcp-dec", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-dec", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    db.add_autopilot(supervisor.id)
    autopilot.set_goal(db, supervisor.id, "Build widget", [("Step 1", "true", "")])

    monkeypatch.setattr(mcp_server, "DB", lambda: db)
    monkeypatch.setattr(mcp_server, "_caller", lambda db_arg: (supervisor, ws))

    res = mcp_server.record_audit_decision("Omit legacy export", "Superseded by v2 export")
    assert "Recorded audit decision" in res

    ap = db.get_autopilot(supervisor.id)
    prompt = autopilot.audit_prompt(db, ap, "headsha")
    assert "Accepted finding 'Omit legacy export': Superseded by v2 export" in prompt


def test_audit_history_goal_isolation(db, repo):
    ws = Workspace("ws-iso", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-iso", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    db.add_autopilot(supervisor.id)

    # 1. Set Goal 1 and record accepted decision
    autopilot.set_goal(db, supervisor.id, "Goal 1", [("Step 1", "true", "")])
    autopilot.record_accepted_decision(db, supervisor.id, "Accepted finding 'Omit legacy export'")
    ap = db.get_autopilot(supervisor.id)
    assert "Accepted finding 'Omit legacy export'" in (ap.audit_history or "")

    # 2. Same-goal revision preserves audit_history
    autopilot.set_goal(db, supervisor.id, "Goal 1", [("Step 1", "true", ""), ("Step 2", "true", "")])
    ap = db.get_autopilot(supervisor.id)
    assert "Accepted finding 'Omit legacy export'" in (ap.audit_history or "")

    # 3. Setting unrelated new goal resets audit_history
    autopilot.set_goal(db, supervisor.id, "Unrelated Goal 2", [("Task", "true", "")])
    ap = db.get_autopilot(supervisor.id)
    assert ap.audit_history is None
    assert "Omit legacy export" not in autopilot.audit_prompt(db, ap, "headsha")

    # 4. Finishing goal clears audit_history
    autopilot.record_accepted_decision(db, supervisor.id, "Accepted something in Goal 2")
    assert db.get_autopilot(supervisor.id).audit_history is not None
    autopilot.clear_finished_goal(db, supervisor.id)
    assert db.get_autopilot(supervisor.id).audit_history is None


def test_request_review_includes_worker_profile_restrictions(db, repo, monkeypatch):
    agents_dir = repo / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "restricted.md").write_text(
        "---\nname: restricted\nprovider: claude\nallowed_tools: [\"Bash(git add:*)\"]\npermission_mode: acceptEdits\n---\nRestricted agent\n"
    )
    ws = Workspace("ws-restr", str(repo), "feat/restr", "main", "main", "main", str(repo), 20000, "tmux-restr", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "restricted", mode="assign")
    worker_from_db = db.get_agent(worker.id)
    monkeypatch.setattr(agents, "workspace_worker", lambda db_arg, ws_arg: worker_from_db)

    spawned_prompts = []
    def fake_spawn(db_arg, ws_arg, profile_arg, **kwargs):
        spawned_prompts.append(kwargs.get("prompt", ""))
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = "rev-restr"
        return m

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    agents.request_review(db, None, ws)
    assert len(spawned_prompts) == 1
    prompt = spawned_prompts[0]
    assert "Worker restrictions:" in prompt
    assert "allowed_tools: Bash(git add:*)" in prompt
    assert "permission_mode: acceptEdits" in prompt


def test_accepted_decision_invalidates_verdict_allowing_re_audit_without_commit(db, repo, monkeypatch):
    ws = Workspace("ws-reaud", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-reaud", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    db.add_autopilot(supervisor.id)
    autopilot.set_goal(db, supervisor.id, "Goal", [("M1", "true", "")])
    m = db.milestones(supervisor.id)[0]
    db.record_check(m.id, True, "passed", "commit-1", passed_sha="commit-1")

    # 1. Audit fails on commit-1
    db.update_autopilot(supervisor.id, audit_sha="commit-1")
    autopilot.record_audit(db, supervisor.id, False, "Gap 1: Missing feature X")
    ap = db.get_autopilot(supervisor.id)
    assert ap.audit_ok == 0
    assert ap.audit_sha == "commit-1"
    cfg = RepoConfig()
    monkeypatch.setattr(autopilot, "_commit", lambda ws_arg: "commit-1")
    assert autopilot.audit_due(ap, cfg, "commit-1") == "gaps"

    # 2. Record accepted decision for Gap 1
    autopilot.record_accepted_decision(db, supervisor.id, "Accepted exclusion of feature X")

    # 3. audit_due at the SAME commit must now return "start" (re-audit allowed without code change)
    ap = db.get_autopilot(supervisor.id)
    assert autopilot.audit_due(ap, cfg, "commit-1") == "start"


def test_pipeline_supervisor_action_parsing_distinguishes_negation_from_actionable_findings(db, repo, monkeypatch):
    ws = Workspace("ws-pip-parse", str(repo), "feat/pip-parse", "main", "main", "main", str(repo), 20000, "tmux-pp", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=sup.id)
    reviewer = agents.spawn(db, ws, "reviewer", mode="review", parent_id=sup.id)
    db.update_agent(worker.id, pipeline="reviewing", pipeline_rounds=0)

    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    sent_messages = []
    monkeypatch.setattr(agents, "send_message", lambda db, to, text, **kwargs: sent_messages.append((to, text)))

    # Case A: Summary has 'Needs supervisor action: None' but actionable blocker
    summary_negated = "Blocker: fix the missing assertion\n\nNeeds supervisor action: None\n\nMinor: none"
    handled, ready = pipeline._on_review(db, reviewer, ws, False, summary_negated)
    assert handled is True
    assert ready is False
    # Worker must be in 'fixing' mode, NOT escalated
    w = db.get_agent(worker.id)
    assert w.pipeline == "fixing"
    assert w.pipeline_rounds == 1
    # Worker received the fix instructions
    assert any(m[0] == worker.id for m in sent_messages)

    # Case A2: Punctuated 'None.' and 'N/A' through _on_review
    for punct in ("Needs supervisor action: None.", "Needs supervisor action: N/A", "Needs supervisor action: - None."):
        db.update_agent(worker.id, pipeline="reviewing", pipeline_rounds=0)
        sent_messages.clear()
        summary_punct = f"Blocker: fix assertion\n\n{punct}\n\nMinor: none"
        h, r = pipeline._on_review(db, reviewer, ws, False, summary_punct)
        assert h is True
        assert r is False
        assert db.get_agent(worker.id).pipeline == "fixing"

    # Case B: Summary has substantive supervisor action
    db.update_agent(worker.id, pipeline="reviewing", pipeline_rounds=0)
    sent_messages.clear()
    summary_actionable = "Blocker: change API signature\n\nNeeds supervisor action: Require supervisor to approve public API deprecation"
    handled, ready = pipeline._on_review(db, reviewer, ws, False, summary_actionable)
    assert handled is True
    assert ready is False
    # Worker pipeline cleared (escalated)
    w = db.get_agent(worker.id)
    assert w.pipeline is None
    # Supervisor received escalation message
    assert any(m[0] == sup.id and "needs supervisor action" in m[1] for m in sent_messages)


def test_record_audit_decision_rejects_unauthorized_callers(db, repo, monkeypatch):
    from brindle import mcp_server
    ws = Workspace("ws-authz", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-az", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive")
    db.add_autopilot(sup.id)
    autopilot.set_goal(db, sup.id, "Goal", [("M1", "true", "")])
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=sup.id)
    reviewer = agents.spawn(db, ws, "reviewer", mode="review", parent_id=sup.id)

    # 1. Worker tries to waive finding
    monkeypatch.setattr(mcp_server, "_caller", lambda db_arg: (worker, ws))
    res_worker = mcp_server.record_audit_decision("Finding 1", "Worker waives finding")
    assert "Not authorized" in res_worker
    ap = db.get_autopilot(sup.id)
    assert ap.audit_history is None or "Finding 1" not in ap.audit_history

    # 2. Reviewer tries to waive finding
    monkeypatch.setattr(mcp_server, "_caller", lambda db_arg: (reviewer, ws))
    res_rev = mcp_server.record_audit_decision("Finding 2", "Reviewer waives finding")
    assert "Not authorized" in res_rev
    ap = db.get_autopilot(sup.id)
    assert ap.audit_history is None or "Finding 2" not in ap.audit_history

    # 3. Supervisor authorizes acceptance
    monkeypatch.setattr(mcp_server, "_caller", lambda db_arg: (sup, ws))
    res_sup = mcp_server.record_audit_decision("Finding 3", "Supervisor accepted exclusion")
    assert "Recorded audit decision" in res_sup
    ap = db.get_autopilot(sup.id)
    assert "Finding 3" in ap.audit_history
    assert "Supervisor accepted exclusion" in ap.audit_history


def test_session_handover_preserves_audit_history_and_accepted_decisions(db, repo, monkeypatch):
    from brindle import sessions
    ws1 = Workspace("ws-ho1", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-ho1", 0)
    ws2 = Workspace("ws-ho2", str(repo), "feat/ho2", "main", "main", "worktree", str(repo), 20010, "tmux-ho2", 0)
    db.add_workspace(ws1)
    db.add_workspace(ws2)

    old_sup = agents.spawn(db, ws1, "developer", mode="interactive", autopilot=True)
    autopilot.set_goal(db, old_sup.id, "Refactor core engine", [("Step 1", "true", "")])
    autopilot.record_accepted_decision(db, old_sup.id, "Accepted exclusion of deprecated driver")
    old_ap = db.get_autopilot(old_sup.id)
    assert "Accepted exclusion of deprecated driver" in (old_ap.audit_history or "")

    monkeypatch.setattr(agents, "pause", lambda db, id, **kwargs: None)
    monkeypatch.setattr(agents, "_launch", lambda *args, **kwargs: None)
    new_sup = sessions.handover(db, old_sup.id, ws2, note="Handing over to feature branch")

    new_ap = db.get_autopilot(new_sup.id)
    assert new_ap is not None
    assert new_ap.audit_history == old_ap.audit_history
    assert "Accepted exclusion of deprecated driver" in (new_ap.audit_history or "")

    prompt = autopilot.audit_prompt(db, new_ap, "commit-head")
    assert "Accepted exclusion of deprecated driver" in prompt


def test_request_review_passes_full_brief_beyond_2000_chars(db, repo, monkeypatch):
    ws = Workspace("ws-long", str(repo), "feat/long", "main", "main", "main", str(repo), 20000, "tmux-long", 0)
    db.add_workspace(ws)
    long_task = "Task preamble: " + ("x" * 2100) + "\n\nSCOPE EXCEPTION: Do not refactor legacy database connector."
    worker = agents.spawn(db, ws, "developer", mode="assign", prompt=long_task)
    worker_from_db = db.get_agent(worker.id)
    monkeypatch.setattr(agents, "workspace_worker", lambda db_arg, ws_arg: worker_from_db)

    spawned_prompts = []
    def fake_spawn(db_arg, ws_arg, profile_arg, **kwargs):
        spawned_prompts.append(kwargs.get("prompt", ""))
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = "rev-long"
        return m

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    agents.request_review(db, None, ws)
    assert len(spawned_prompts) == 1
    prompt = spawned_prompts[0]
    # Verify that content beyond character 2000 is preserved
    assert "SCOPE EXCEPTION: Do not refactor legacy database connector." in prompt


def test_clean_install_reviewer_selection_with_gemini(db, repo, monkeypatch):
    from brindle.config import RepoConfig
    from brindle import agents, profiles

    cfg = RepoConfig()
    # On a clean installation with antigravity usable:
    monkeypatch.setattr(agents, "unusable", lambda p, *args, **kwargs: None if p == "antigravity" else "not installed")

    from brindle import learning
    monkeypatch.setattr(learning, "plugin", lambda c, r: object())
    monkeypatch.setattr(learning, "choose_why", lambda db, cfg, repo, task, candidates, default: ("reviewer-gemini", "evidence"))

    chosen = agents.default_review_profile(cfg, None, db=db, repo_root=str(repo), task="Test task")
    assert chosen == "reviewer-gemini"

    # Profile must be loadable on clean install without KeyError
    p = profiles.load_profile(chosen, str(repo))
    assert p.name == "reviewer-gemini"
    assert p.provider == "antigravity"
    assert "You are a code reviewer" in p.prompt


def test_default_review_profile_prefers_gemini_over_codex(db, repo, monkeypatch):
    from brindle.config import RepoConfig
    from brindle import agents

    cfg = RepoConfig()
    monkeypatch.setattr(agents, "unusable", lambda p, *args, **kwargs: None)
    chosen = agents.default_review_profile(cfg, None, db=db, repo_root=str(repo), task="Test task")
    assert chosen == "reviewer-gemini"


def test_custom_configured_reviewer_honored_even_with_antigravity_installed(db, repo, monkeypatch):
    from brindle.config import RepoConfig
    from brindle import agents

    cfg = RepoConfig(reviewer="reviewer-local")
    monkeypatch.setattr(agents, "unusable", lambda p, *args, **kwargs: None)
    chosen = agents.default_review_profile(cfg, None, db=db, repo_root=str(repo), task="Test task")
    assert chosen == "reviewer-local"


def test_has_supervisor_action_markdown_formatting():
    from brindle import pipeline

    # Bold headings with following lists
    assert pipeline.has_supervisor_action(
        "**Needs supervisor action**\n- Run the production migration; worker has no network access."
    ) is True
    assert pipeline.has_supervisor_action(
        "**Needs supervisor action:**\n- Run the production migration; worker has no network access."
    ) is True
    assert pipeline.has_supervisor_action("Needs supervisor action: run migration") is True

    # Empty and negated headings
    assert pipeline.has_supervisor_action("### Needs supervisor action\nNone.") is False
    assert pipeline.has_supervisor_action("Needs supervisor action: None") is False
    assert pipeline.has_supervisor_action("Needs supervisor action: N/A") is False
    assert pipeline.has_supervisor_action("Needs supervisor action: No action needed.") is False
    assert pipeline.has_supervisor_action("Needs supervisor action: None needed. Everything looks good.") is False


def test_on_review_escalates_on_markdown_supervisor_action(db, repo, monkeypatch):
    from brindle import agents, pipeline
    from brindle.db import Workspace

    ws = Workspace("ws-pipe", str(repo), "feat/pipe", "main", "main", "main", str(repo), 20000, "tmux-pipe", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=sup.id)
    reviewer = agents.spawn(db, ws, "reviewer", mode="review", parent_id=sup.id)
    db.update_agent(worker.id, pipeline="reviewing")

    escalated = []
    messages = []
    monkeypatch.setattr(pipeline, "_escalate", lambda db_arg, ws_arg, worker_arg: escalated.append(worker_arg.id))
    monkeypatch.setattr(pipeline, "_tell", lambda db_arg, parent_arg, text, sender: messages.append((parent_arg, text)))
    monkeypatch.setattr(agents, "is_alive", lambda a: True)

    summary = "**Needs supervisor action:**\n- Run the production migration; worker has no network access."
    handled, ready = pipeline._on_review(db, reviewer, ws, approved=False, summary=summary)

    assert handled is True
    assert ready is False
    # Must immediately escalate to supervisor without sending worker to 'fixing'
    assert worker.id in escalated
    w_updated = db.get_agent(worker.id)
    assert w_updated.pipeline is None
    assert len(messages) == 1
    assert "needs supervisor action" in messages[0][1]



