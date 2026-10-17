"""The pipeline doesn't stall on a review: a reviewer that stops without a
verdict gets one fallback, and a review summary whose supervisor-action
section is negated isn't an escalation."""
import contextlib
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, autopilot, cull, gates, pipeline, procs, providers, workspaces
from brindle.config import RepoConfig
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
    # A second review's verdict on the same commit isn't rev0's (#172).
    db.add_review(ws.id, gates.head(ws), "rev-second", True, "lgtm")

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
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "_paste_blocked", lambda a: True)
    launched, fallbacks = [], []

    def fake_launch(db_, a, ws_, *, prompt, resume, **kw):
        prof = agents.load_profile(a.profile, ws_.repo_root)
        ctx = agents.LaunchContext(a.id, prof, prompt, resume=resume, cwd=ws_.path, mode=a.mode)
        launched.append(agents.get_provider(a.provider).command(ctx))

    monkeypatch.setattr(agents, "_launch", fake_launch)
    monkeypatch.setattr(agents, "request_review", lambda *a, **k: fallbacks.append(1))

    # 1. Recent transcript write on a live reviewer -> treated as still active.
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is False
    assert db.pending_count("rev0") == 0 and launched == [] and fallbacks == []

    # 2. Live idle reviewer -> real send_message/flush queues reminder; repeated trigger respects it.
    old = time.time() - 120
    os.utime(transcript, (old, old))
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert db.pending_count("rev0") == 1 and fallbacks == []
    assert db.get_agent("rev0").reopened_at is not None
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert db.pending_count("rev0") == 1 and fallbacks == []

    # Once the queued turn has executed (or its window expired) without a verdict, fallback runs.
    db.pop_pending("rev0")
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(fallbacks) == 1

    # 3. Dead reviewer with no recorded session_ref -> never uses --continue or --last.
    alive["rev0"] = False
    db.update_agent("rev0", reopened_at=None, unreported_noted=None, transcript_path=None, session_ref=None)
    assert agents.reopen_reviewer(db, db.get_agent("rev0"), ws) is False
    assert launched == []

    # 4. Dead reviewer with its own recorded session -> reopened once before any fallback.
    db.update_agent("rev0", session_ref="sess-rev0", transcript_path=str(transcript))
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(launched) == 1 and len(fallbacks) == 1
    assert "--resume" in launched[0] and "sess-rev0" in launched[0]
    assert "--continue" not in launched[0] and "--last" not in launched[0]
    assert db.get_agent("rev0").reopened_at is not None

    # 5. Stalling again after reopen -> does not loop; falls back to request_review.
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(launched) == 1 and len(fallbacks) == 2


def test_fallback_reviewer_reuses_cached_checks_and_partial_findings(db, repo, tmp_path, monkeypatch):
    (repo / ".brindle" / "agents").mkdir(parents=True)
    (repo / ".brindle" / "agents" / "reviewer-light.md").write_text(
        "---\nname: reviewer-light\nextends: reviewer\nmodel: haiku\neffort: low\n---\n"
    )
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
    db.update_agent("rev0", review_sha=sha, transcript_path=str(transcript), reopened_at=1.0)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_review_profile", lambda db_, ws_, prof, cfg: prof or "reviewer")
    monkeypatch.setattr(autopilot, "_unavailable", lambda *a, **k: None)
    monkeypatch.setattr(gates, "check_summary", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-ran checks")))
    spawned = []
    monkeypatch.setattr(
        agents, "spawn",
        lambda db_, ws_, prof, prompt=None, **kw: spawned.append((prof, prompt)),
    )

    # Unpinned config -> routes to the cheaper reviewer-light tier and reuses cached checks + findings.
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(spawned) == 1
    prof, prompt = spawned[0]
    assert prof == "reviewer-light"
    assert "Blocker in big.py line 12" in prompt
    assert "pytest -q" in prompt

    # Pinned config -> keeps the pinned reviewer profile instead of downgrading.
    (repo / ".brindle" / "config.json").write_text(
        json.dumps({"checks": ["pytest -q"], "review_profile": "reviewer"})
    )
    db.update_agent("rev0", unreported_noted=None)
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert spawned[-1][0] == "reviewer"


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
    db.update_agent("rev0", review_sha=sha, transcript_path=str(transcript), reopened_at=1.0)
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


@pytest.mark.parametrize("moves", [1, 99])
def test_a_reported_branch_that_moves_during_its_checks_is_not_dropped(db, repo, monkeypatch, moves):
    """#168: the base moved and the branch took new commits while its checks
    ran, with no new report to start another run. The branch is checked
    again on its new head, or, if it keeps moving, handed to the supervisor."""
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", result="done", pipeline="reviewing")
    db.update_agent("w1", reviewing_since=time.time())
    runs = []

    def moving(db_, ws_, cfg, cancel=None):
        runs.append(1)
        if len(runs) <= moves:
            (repo / f"base{len(runs)}.py").write_text("z = 3\n")
            sh(f"git add -A && git commit -qm base{len(runs)}", repo)
            sh("git merge -q --no-edit main", Path(ws.path))
            raise gates.Abandoned("moved")
        return "PASS `suite`"

    monkeypatch.setattr(gates, "check_summary", moving)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    spawned = []
    monkeypatch.setattr(agents, "spawn", lambda db_, ws_, prof, **kw: spawned.append(kw["review_sha"]))
    agents.review_after_checks(db, "boss", ws, "reviewer", None, RepoConfig(checks=["suite"]))
    if moves == 1:
        assert spawned == [gates.head(ws)] and db.get_agent("w1").pipeline == "reviewing"
    else:
        assert spawned == [] and db.get_agent("w1").pipeline is None
        assert any("needs you" in b and "`feat`" in b for b in drain(db))


def _reported(db, repo, reviewing_since=None):
    """A piped worker that reported on `feat`, in a repo with checks."""
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(
        json.dumps({"checks": ["true"], "review_profile": "reviewer"}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", result="done")
    if reviewing_since is not None:
        db.update_agent("w1", pipeline="reviewing", reviewing_since=reviewing_since)
    return ws


@pytest.mark.variant("live incident #190: what changed is the process tree, which coverage doesn't see")
def test_issue190_the_check_run_before_a_review_outlives_the_reporting_worker(db, repo, monkeypatch):
    """#190: the run that checks a reported branch and then starts its
    reviewer was a child of the reporting worker's MCP server. Stopping an
    agent stops every process descended from it (brindle.procs), so a worker
    closed once it went idle took the run with it, its checks still queued or
    running (for long, under load), and no reviewer ever started."""
    ws = _reported(db, repo)
    marker = f"issue190-{ws.id}"
    monkeypatch.setattr(providers, "brindle_invocation",
                        lambda: [sys.executable, "-c", "import time; time.sleep(60)", marker])
    assert pipeline.on_report(db, db.get_agent("w1"), ws, "done")
    deadline, helper, ours = time.time() + 10, None, True
    while ours and time.time() < deadline:   # its shell exits at once, or never was
        table = procs.table()
        helper = next((p.pid for p in table.values() if marker in p.text and "sh -c" not in p.text), helper)
        ours = helper is None or helper in procs.descendants([os.getpid()], table)
    try:
        assert helper is not None and not ours
    finally:
        if helper is not None:
            os.kill(helper, signal.SIGKILL)


@pytest.mark.parametrize("check_run", ["gone", "at work"])
def test_issue186_a_reported_branch_nobody_is_checking_is_reviewed_again(db, repo, monkeypatch, check_run):
    """#186: a reported branch sat with no check run, no reviewer and no
    verdict until the 30-minute reminder asked the supervisor to call
    request_review, which then always worked. The sweep starts it again
    itself, once per report, and never while its check run is still at work
    (queued for a slot, say)."""
    ws = _reported(db, repo, reviewing_since=time.time() - pipeline.RESTART_REVIEW_AFTER - 60)
    launched, told = [], []
    monkeypatch.setattr(agents, "_detach", launched.append)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: told.append(body))
    with contextlib.ExitStack() as stack:
        if check_run == "at work":   # in a process of its own, in production
            stack.enter_context(pipeline.review_run(ws.id))
        cull.note_reported_stuck(db, time.time(), {})
        cull.note_reported_stuck(db, time.time(), {})
    runs = [argv for argv in launched if "_review-after-checks" in argv]
    assert len(runs) == (1 if check_run == "gone" else 0) and told == []
    assert db.get_agent("w1").pipeline == "reviewing"


@pytest.mark.parametrize("summary", [
    "Needs supervisor action: None — all fixable by the worker.",
    pytest.param("Needs supervisor action: No items.",
                 marks=pytest.mark.variant("incident: a reviewer's 'No items.' stalled the pipeline")),
])
def test_supervisor_action_negations(summary):
    assert pipeline.has_supervisor_action(summary) is False
