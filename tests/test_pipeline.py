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
from brindle import agents, autopilot, bus, cull, gates, pipeline, procs, providers, workspaces
from brindle.config import RepoConfig
from test_merge_order import add
from test_pipeline_idempotent import drain


def test_reviewer_without_verdict_gets_fallback_then_an_escalation_event(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")  # busy: messages queue
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", result="done", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer-gemini", parent="boss")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)  # supervisor stays busy
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    started = []

    def fallback(db_, caller, ws_, profile=None, focus=None, cfg=None, check_summary=None, **kw):
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
    assert [(e["agent_id"], e["root_id"], e["payload"]["actor"])
            for e in bus.tail(db, kinds=("pipeline.escalated",))] == [("w1", "boss", "brindle")]


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
    (Path(ws.path) / "work.py").write_text("x = 1\n")   # a branch with no commits isn't reviewed
    sh("git add work.py && git commit -qm work", Path(ws.path))
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


@pytest.mark.parametrize("check_run, delay", [("gone", 90), ("at work", 90), ("launching", 15)])
def test_issue186_a_reported_branch_nobody_is_checking_is_reviewed_again(db, repo, monkeypatch, check_run, delay):
    """#186: a reported branch sat with no check run, no reviewer and no
    verdict until the 30-minute reminder asked the supervisor to call
    request_review, which then always worked. The sweep starts it again
    itself, once per report, and never while its check run is still at work
    (queued for a slot, say)."""
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    ws = _reported(db, repo)
    (repo / ".brindle" / "config.local.json").write_text(json.dumps({"review_start_delay": delay}))
    launched, told = [], []
    monkeypatch.setattr(agents, "_detach", launched.append)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: told.append(body))
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    agents.report_result(db, "w1", "done")
    claim = agents._review_request_file(ws.id, "reviewer")
    original = claim.read_text()
    assert len(launched) == 1
    now += delay - 1
    cull.note_reported_stuck(db, now, {})
    assert claim.read_text() == original
    now += 1
    with contextlib.ExitStack() as stack:
        if check_run == "at work":   # in a process of its own, in production
            stack.enter_context(pipeline.review_run(ws.id))
            stack.enter_context(gates.marker("review-request", original.splitlines()[0]))
        cull.note_reported_stuck(db, now, {})
        if check_run == "launching":
            assert db.get_agent("w1").review_restarted is None
            now += agents.LAUNCH_GRACE
        cull.note_reported_stuck(db, now, {})
        cull.note_reported_stuck(db, now, {})
        assert len(launched) == (1 if check_run == "at work" else 2) and told == []
        active = claim.read_text()
        agents.report_result(db, "w1", "done again")
        assert claim.read_text() == active
    assert db.get_agent("w1").pipeline == "reviewing"


@pytest.mark.parametrize("session, exits_mid_delivery", [
    ("sess-w1", False), (None, False),
    # Alive when the verdict lands, gone by the time send_message delivers.
    ("sess-w1", True),
])
def test_fix_round_resumes_exited_worker(db, repo, monkeypatch, session, exits_mid_delivery):
    """Live incident (session-17): a worker that had exited after
    report_result got a blocker review, and the pipeline handed its branch to
    the supervisor "after 0 fix round(s)" instead of starting a fix round.
    The worker is reopened (its own session, else a fresh start on its task)
    with the findings, and only a branch out of rounds goes to the supervisor."""
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")  # busy: messages queue
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", status="done", result="done", task="Add the None check")
    db.update_agent("w1", session_ref=session)
    add(db, ws, "rev", "review", "reviewer", parent="boss")
    live = []   # w1 answers alive once per entry, then is gone
    monkeypatch.setattr(agents, "is_alive",
                        lambda a, panes=None: a.id == "boss" or (a.id == "w1" and bool(live) and live.pop()))
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: ref == "sess-w1"))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, *, prompt, resume, **kw:
                        launched.append((a.id, resume, prompt)))
    summary = "- blocker: missing check for None.\nNeeds supervisor action: None."
    for rounds in (1, 2):
        db.update_agent("w1", pipeline="reviewing")
        live[:] = [True] if exits_mid_delivery else []
        assert pipeline.on_review(db, db.get_agent("rev"), ws, False, summary)
        worker, resume, prompt = launched[-1]
        assert (worker, resume) == ("w1", session) and "missing check for None" in prompt
        assert session or "Add the None check" in prompt
        assert (db.get_agent("w1").pipeline, db.get_agent("w1").pipeline_rounds) == ("fixing", rounds)
        assert drain(db) == []
    db.update_agent("w1", pipeline="reviewing")
    assert pipeline.on_review(db, db.get_agent("rev"), ws, False, summary)
    assert len(launched) == 2 and db.get_agent("w1").pipeline is None
    assert [e["agent_id"] for e in bus.tail(db, kinds=("pipeline.escalated",))] == ["w1"]


def test_no_duplicate_reviewer_while_the_first_is_still_starting(db, repo, monkeypatch):
    """Live incident: three reviewers on one branch. Check runs for a branch
    don't wait on each other (pipeline.review_run), so a second one reached
    the reviewer dedupe while the first run's reviewer was being spawned,
    its window not recorded yet, took it for dead and started another."""
    ws = _reported(db, repo, reviewing_since=time.time())
    add(db, ws, "r1", "review", "reviewer", parent="boss", status="starting")
    db.update_agent("r1", tmux_window="", review_sha=gates.head(ws))
    spawned = []
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: spawned.append(k))
    agents.review_after_checks(db, "boss", ws)
    agents.request_review(db, db.get_agent("boss"), ws)
    assert spawned == []


@pytest.mark.parametrize("summary", [
    "Needs supervisor action: None — all fixable by the worker.",
    pytest.param("Needs supervisor action: No items.",
                 marks=pytest.mark.variant("incident: a reviewer's 'No items.' stalled the pipeline")),
])
def test_supervisor_action_negations(summary):
    assert pipeline.has_supervisor_action(summary) is False


@pytest.mark.parametrize("case", [None, "old merge", "old verdict", "paused reviewer", "old reviewer", "recorded", "standing"])
def test_issue186_review_state_tells_silence_apart(db, repo, tmp_path, monkeypatch, case):
    """Both list tools and the sidebar expose the branch's actual review stage."""
    from dataclasses import replace

    from brindle import mcp_server, tasks, view, watch
    from brindle.db import Workspace

    if case is not None:
        ws = Workspace("repo/feat", str(tmp_path), "feat", "worktree", "feat", "main",
                       str(tmp_path), None, "session", time.time())
        db.add_workspace(ws)
        monkeypatch.setattr(gates, "head", lambda ws_: "current")
        add(db, ws, "w1", "assign", result="done", pipeline="reviewing")
        expected = "waiting for review: nothing is checking or reviewing it"
        if case == "old merge":
            db.set_merged(ws.id, "old")
        elif case in ("paused reviewer", "old reviewer"):
            add(db, ws, "r1", "review", status="paused" if case == "paused reviewer" else "processing")
            db.update_agent("r1", review_sha="old" if case == "old reviewer" else "current")
        else:
            db.add_review(ws.id, "old" if case == "old verdict" else "current", "r1", False, "fix")
            if case != "recorded":
                db.update_agent("w1", pipeline=None)
            if case == "standing":
                db.add_review(ws.id, "current", "r2", True, "ok")
            expected = None if case == "old verdict" else "changes requested at current"
        assert pipeline.review_state(db, ws) == expected
        return

    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"review": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", Path(ws.path))
    add(db, ws, "w1", "assign", parent="boss", status="done", result="added new.py")
    task = tasks.record_started(db, root, db.get_agent("w1"), "developer", "work", "assign",
                                isolate=True, branch=None, done_when=None, files=None, depends_on=None)
    other = replace(ws, id=ws.id + "-other", name="other", branch="other")
    db.add_workspace(other)
    add(db, other, "w2", "assign", parent="boss", status="done", result="done")
    other_task = replace(task, id=task.id + "-other", agent_id="w2")
    db.add_task(other_task)
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    sha = gates.head(ws)[:8]
    assert pipeline.review_state(db, ws) is None

    def state_is(expected):
        assert pipeline.review_state(db, ws) == expected
        for render, identifier in ((mcp_server.list_agents, "w1"), (mcp_server.list_tasks, task.id)):
            [line] = [line for line in render().splitlines() if line.startswith(identifier + " ")]
            assert f"branch={ws.branch}" in line and line.endswith(" review=" + expected)
        assert other_task.id not in mcp_server.list_tasks()
        [other_line] = [line for line in mcp_server.list_agents().splitlines() if line.startswith("w2 ")]
        assert " review=" not in other_line

    db.update_agent("w1", pipeline="reviewing")
    assert "nothing" in pipeline.review_state(db, ws)
    with pipeline.review_run(ws.id):
        state_is("waiting for checks")
        with gates.marker("checks-running", ws.id):
            state_is("checks running")
    add(db, ws, "rev0", "review", "reviewer", parent="boss", status="processing")
    db.update_agent("rev0", review_sha=gates.head(ws))
    state_is("reviewer running (rev0)")

    db.set_status("rev0", "done")
    db.add_review(ws.id, gates.head(ws), "rev0", False, "no")
    db.update_agent("w1", pipeline=None)
    state_is(f"changes requested at {sha}")
    db.add_review(ws.id, gates.head(ws), "rev0", True, "lgtm")
    state_is(f"approved at {sha}")
    db.update_agent("w1", pipeline="ready")
    with gates.marker("merge-run", ws.id), gates.marker("checks-running", ws.id):
        state_is("approved, merging, checks running")
    db.update_agent("w1", pipeline=None)

    db.update_agent("w1", merge_failed="check `pytest` failed:\nboom\n" + "x" * 300)
    state = pipeline.review_state(db, ws)
    assert state.startswith("merge failed: check `pytest` failed: boom x") and len(state) < 230
    state_is(state)
    [entry] = [e for e in view.snapshot(db, ws.repo_root, panes={}) if e["id"] == ws.id]
    assert entry["review_state"] == state
    [w1] = [a for a in entry["agents"] if a["id"] == "w1"]
    assert watch.awaiting_review(w1, entry) == state
