"""The pipeline doesn't stall on a review: a reviewer that stops without a
verdict gets one fallback, and a review summary whose supervisor-action
section is negated isn't an escalation."""
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
from brindle import agents, autopilot, bus, cli, cull, gates, pipeline, procs, providers, workspaces
from brindle.config import RepoConfig
from test_merge_order import add
from test_pipeline_idempotent import drain


@pytest.mark.parametrize("guard", ["health", "unreported", "verdict", "queued", "standalone",
                                    "resumed", pytest.param("repane", marks=pytest.mark.variant(
                                        "a resumed review can exit in its new pane before stale death handling")),
                                    pytest.param("os-error", marks=pytest.mark.variant("process creation can raise OSError instead of TmuxError"))])
def test_dead_reviewer_restarts_once_then_needs_the_person(db, repo, monkeypatch, guard):
    from brindle import launch_health, tmux
    from brindle.service import inbox

    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    sha = gates.head(ws)
    add(db, ws, "w1", "assign", parent="boss", result="done", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer", parent="boss", review_sha=sha)
    db.update_agent("rev0", tmux_window="%2", status="processing", status_since=1, created_at=1)
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({"%2": False})
    panes.exits = {"%2": (1, None)}
    monkeypatch.setattr(tmux, "list_panes", lambda **kw: panes)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "other")
    monkeypatch.setattr(agents, "command_running", lambda *a: False)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "_review_profile", lambda *a: "reviewer")
    monkeypatch.setattr(agents, "close_later", lambda *a: None)
    secret = "sk-abcdefghijklmnop123456789"
    monkeypatch.setattr(launch_health, "capture", lambda a: f"startup\nError: workspace routing discovery failed {secret}\n\n")
    launched = []

    class FakeProvider:
        name = "codex"
        launches_process = True

        def command(self, ctx):
            launched.append(ctx.agent_id)
            return []

    monkeypatch.setattr(agents, "get_provider", lambda name: FakeProvider())

    def launch(db_, a, ws_, **kw):
        if a.id == "rev0":
            FakeProvider().command(agents.LaunchContext(a.id, agents.load_profile(a.profile), kw["prompt"]))
            db.update_agent(a.id, status="processing", tmux_window="%4" if guard == "repane" else "%2")
            return
        if guard in ("tmux-error", "os-error"):
            raise (tmux.TmuxError if guard == "tmux-error" else OSError)(f"new-window failed {secret}")
        FakeProvider().command(agents.LaunchContext(a.id, agents.load_profile(a.profile), kw["prompt"]))
        pipeline.on_reviewer_dead(db, db.get_agent("rev0"), ws)  # second detector during dispatch
        db.update_agent(a.id, tmux_window="%3", status="processing", status_since=1, created_at=1)
        panes["%3"] = True

    monkeypatch.setattr(agents, "_launch_resolved", launch)
    initial = db.get_agent("rev0")
    initial.task = "review"
    launch_health.prepare(db, initial, "test")
    launch_health.bind(db, initial, "%2")
    if guard == "verdict":
        db.add_review(ws.id, sha, "rev0", True, "ok")
    elif guard == "running":
        add(db, ws, "other", "review", "reviewer", parent="boss", review_sha=sha)
    elif guard == "queued":
        agents._review_request_file(ws.id, "reviewer-gemini").write_text(f"queued\n{sha}\n{time.time()}\n")
    elif guard == "moved":
        db.update_agent("rev0", review_sha="old-commit")
    elif guard == "standalone":
        db.update_agent("w1", pipeline=None)
    elif guard in ("resumed", "repane"):
        dispatch_lock = agents.review_dispatch_lock
        resumed = False

        @contextlib.contextmanager
        def resumed_before_recovery(workspace_id, **kw):
            nonlocal resumed
            with dispatch_lock(workspace_id, **kw):
                if not resumed:
                    resumed = True
                    with monkeypatch.context() as launching:
                        launching.setattr(agents, "review_dispatch_lock", dispatch_lock)
                        agents._launch(db, initial, ws, prompt="review", resume=None, watch_pane=False)
                    if guard == "resumed":
                        monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "rev0")
                yield

        monkeypatch.setattr(agents, "review_dispatch_lock", resumed_before_recovery)

    if guard == "health":
        launch_health.inspect(db, "rev0", wait=False)
    elif guard == "unreported":
        agents.tell_parent_unreported(db, initial)
    else:
        cull.note_dead_panes(db, time.time(), panes)
    if guard == "standalone":
        launch_health.inspect(db, "rev0", wait=False)
        messages = drain(db)
        assert len(messages) == 2 and launched == []
        assert all("workspace routing discovery failed" in m and secret not in m for m in messages)
        return
    if guard in ("verdict", "running", "queued", "moved", "resumed", "repane"):
        assert launched == (["rev0"] if guard in ("resumed", "repane") else [])
        assert db.get_agent("w1").pipeline == "reviewing"
        assert drain(db) == []
        if guard in ("resumed", "repane"):
            pipeline.on_reviewer_dead(db, initial, ws)  # handler also rejects the old observation
            assert db.get_agent("rev0").status == "processing"
            assert not db.conn.execute("SELECT 1 FROM review_death_restarts").fetchall()
        return
    failed_launch = guard in ("tmux-error", "os-error")
    if not failed_launch:
        assert len(launched) == 1
        assert db.get_agent(launched[0]).review_sha == sha
        assert db.get_agent("w1").pipeline == "reviewing"
        assert drain(db) == []
        launch_health.inspect(db, "rev0", wait=False)
        agents.tell_parent_unreported(db, db.get_agent("rev0"))
        with pytest.raises(agents.AgentError):
            agents._launch(db, initial, ws, prompt="review", resume=None, watch_pane=False)
        assert len(launched) == 1
        panes["%3"] = False
        panes.exits["%3"] = (1, None)
    cull.note_dead_panes(db, time.time(), panes)
    cull.note_dead_panes(db, time.time(), panes)
    assert len(launched) == (0 if failed_launch else 1)
    assert db.get_agent("w1").pipeline is None
    assert gates.verdict(db, ws.id, sha) is None
    messages = drain(db)
    assert len(messages) == 1 and "workspace routing discovery failed" in messages[0]
    assert secret not in messages[0] and "[redacted]" in messages[0]
    if failed_launch:
        assert "new-window failed" in messages[0]
    assert pipeline.review_state(db, ws).startswith("needs you")
    row = db.get_agent("rev0" if failed_launch else launched[0])
    assert "workspace routing discovery failed" in inbox.needs_you(vars(row), {})
    assert secret not in row.failed_why
    monkeypatch.setattr(inbox.view, "snapshot", lambda *a, **kw: [{"id": ws.id, "agents": [vars(row)]}])
    card = next(item for item in inbox.items(db, str(repo), panes) if item.agent_id == row.id)
    assert "workspace routing discovery failed" in card.detail and secret not in card.detail
    if not failed_launch:
        agents.request_review(db, db.get_agent("boss"), ws, "reviewer")
        assert db.get_agent(row.id).failed_why is None


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

    monkeypatch.setattr(agents, "start_review", fallback)
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
    monkeypatch.setattr(agents, "start_review", lambda *a, **k: fallbacks.append(1))

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
    spawned = []
    # The fallback goes through start_review, like every reviewer: with checks
    # a detached helper runs them (reusing the cached results for this commit)
    # before it starts the reviewer.
    monkeypatch.setattr(agents, "_detach", lambda argv: spawned.append(argv))

    # Unpinned config -> routes to the cheaper reviewer-light tier and carries the findings.
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[argv.index("--profile") + 1] == "reviewer-light"
    assert "Blocker in big.py line 12" in argv[argv.index("--focus") + 1]

    # Pinned config -> keeps the pinned reviewer profile instead of downgrading.
    (repo / ".brindle" / "config.json").write_text(
        json.dumps({"checks": ["pytest -q"], "review_profile": "reviewer"})
    )
    db.update_agent("rev0", unreported_noted=None)
    assert agents.tell_parent_unreported(db, db.get_agent("rev0")) is True
    assert spawned[-1][spawned[-1].index("--profile") + 1] == "reviewer"

    # Run the detached helper itself. On the unchanged commit passing checks
    # start the fallback with their results in its prompt.
    from brindle.config import load_repo_config

    argv = spawned[-1]   # the pinned run's
    cfg = load_repo_config(str(repo))
    prompts, sent = [], []
    monkeypatch.setattr(agents, "spawn", lambda db_, ws_, prof, *, prompt=None, parent_id=None, mode="review", **kw:
                        prompts.append(prompt) or agents.Agent("rev9", ws_.id, prof, "claude", parent_id, mode,
                                                               "starting", "@0", None, time.time()))
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append((to, body)))
    rid = argv[argv.index("--request-id") + 1]
    monkeypatch.setattr(gates, "check_summary", lambda *a, **k: "PASS `pytest -q`\n50 passed")
    agents.review_after_checks(db, "boss", ws, "reviewer", argv[argv.index("--focus") + 1], cfg,
                               request_id=rid, slot="")
    assert len(prompts) == 1 and "50 passed" in prompts[0] and "Blocker in big.py line 12" in prompts[0]

    # The branch moves on and its checks fail now: the old passing results
    # don't carry over, no fallback starts, and the worker gets the failure.
    db.update_agent("w1", reviewing_since=time.time())
    (Path(ws.path) / "more.py").write_text("y = 2\n")
    sh("git add more.py && git commit -qm more", Path(ws.path))
    monkeypatch.setattr(gates, "check_summary", lambda *a, **k: "FAIL `pytest -q`\nexit 1: current boom")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "w1")
    agents.review_after_checks(db, "boss", ws, "reviewer", None, cfg, slot="")
    assert len(prompts) == 1 and [to for to, _ in sent] == ["w1"] and "current boom" in sent[0][1]


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
    monkeypatch.setattr(agents, "start_review", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned fallback")))

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


@pytest.mark.parametrize("moves", [99])
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
        assert spawned[-1] == gates.head(ws) and db.get_agent("w1").pipeline == "reviewing"
    else:
        assert db.get_agent("w1").pipeline is None
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
            with contextlib.suppress(ProcessLookupError):
                os.kill(helper, signal.SIGKILL)


@pytest.mark.parametrize("check_run, delay", [("launching", 15)])
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
        # A report of its own: the helper still pending for the earlier one retires.
        assert claim.read_text() != active
    assert db.get_agent("w1").pipeline == "reviewing"


def test_unchanged_branch_is_not_reviewed_again(db, tmp_path, monkeypatch):
    from brindle.db import Workspace

    ws = Workspace("ws", str(tmp_path), "work", "worktree", "work", "main",
                   str(tmp_path), None, "fake", 1)
    db.add_workspace(ws)
    add(db, ws, "boss", "interactive", "supervisor", status="processing")
    add(db, ws, "w1", "assign", parent="boss", result="done", pipeline="fixing")
    db.update_agent("w1", pipeline_rounds=1)
    findings = "The changed handler must validate the incoming request."
    reason = "This handler predates my task and is out of scope."
    add(db, ws, "rejecting-reviewer", "review", "reviewer", parent="boss")
    db.update_agent("rejecting-reviewer", review_slot="")
    db.add_review(ws.id, "head", "rejecting-reviewer", False, findings)
    other_finding = "The new endpoint also needs an authorization check."
    db.add_review(ws.id, "head", "second-reviewer", False, other_finding)
    add(db, ws, "approving-reviewer", "review", "reviewer", parent="boss")
    db.update_agent("approving-reviewer", review_slot="independent")
    db.add_review(ws.id, "head", "approving-reviewer", True, "approved")
    head = "head"
    monkeypatch.setattr(pipeline, "load_repo_config", lambda *a, **k: RepoConfig(checks=["true"], review_profile="reviewer"))
    monkeypatch.setattr(agents, "_branch_head", lambda *a: head)
    monkeypatch.setattr(gates, "head", lambda *a: head)
    monkeypatch.setattr(agents.git, "ahead_behind", lambda *a: (0, 1))
    monkeypatch.setattr(agents.git, "dirty_files", lambda *a, **k: [])
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "is_alive", lambda *a: False)
    starts = []
    monkeypatch.setattr(agents, "_detach", lambda argv: starts.append(argv))

    agents.report_result(db, "w1", reason)
    assert not starts
    assert db.get_agent("w1").pipeline is None
    messages = drain(db)
    assert len(messages) == 1 and findings in messages[0] and reason in messages[0]
    assert other_finding in messages[0]
    agents.report_result(db, "w1", reason)
    agents.report_result(db, "w1", "Still out of scope; no changes made.")
    assert not starts and not drain(db)
    assert db.get_agent("w1").pipeline_rounds == 1
    assert len(bus.tail(db, kinds=("pipeline.escalated",))) == 1

    head = "new-commit"
    agents.report_result(db, "w1", "Committed a fix.")
    assert len(starts) == 1 and db.get_agent("w1").pipeline == "reviewing"
    assert not drain(db)

    # An approval of the current commit must not be treated as an open finding.
    db.add_review(ws.id, head, None, True, "approved")
    agents.report_result(db, "w1", "Ready to merge.")
    assert len(starts) == 2 and not drain(db)

    db.add_review(ws.id, head, None, False, findings)
    agents.report_result(db, "w1", "The new finding also needs a scope decision.")
    assert len(starts) == 2 and db.get_agent("w1").pipeline is None
    messages = drain(db)
    assert len(messages) == 1 and findings in messages[0]


def test_report_dispatch_preserves_the_current_claim(db, tmp_path, monkeypatch):
    """Duplicate, superseding, failed and legacy dispatches share one branch's history."""
    from brindle.db import Workspace

    ws = Workspace("ws", str(tmp_path), "work", "worktree", "work", "main",
                   str(tmp_path), None, "fake", 1)
    db.add_workspace(ws)
    add(db, ws, "boss", "interactive", "supervisor", status="processing")
    add(db, ws, "w1", "assign", parent="boss", result="done")
    monkeypatch.setattr(pipeline, "load_repo_config", lambda *a, **k: RepoConfig(checks=["true"], review_profile="reviewer"))
    monkeypatch.setattr(agents, "_branch_head", lambda *a: "head")
    monkeypatch.setattr(gates, "head", lambda *a: "head")
    monkeypatch.setattr(agents.git, "ahead_behind", lambda *a: (0, 1))
    monkeypatch.setattr(agents.git, "dirty_files", lambda *a, **k: [])
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "is_alive", lambda *a: False)
    starts, claims = [], []
    phase = "duplicate"

    def detach(argv):
        starts.append(argv)
        claims.append(db.get_agent("w1").reviewing_since)
        if phase == "duplicate" and len(starts) == 1:
            agents.report_result(db, "w1", "done")
        elif phase == "newer-report" and db.get_agent("w1").result == "older":
            agents.report_result(db, "w1", "newer")
            raise agents.AgentError("old launch failed after the new report")
        elif phase in ("fallback-moved", "legacy-fallback"):
            raise agents.AgentError("reviewer unavailable")

    original = agents.condense

    def condense(*a, **kw):
        if phase == "fallback-moved":
            db.update_agent("w1", pipeline="fixing")  # a verdict landed during preparation
        return original(*a, **kw)

    monkeypatch.setattr(agents, "_detach", detach)
    monkeypatch.setattr(agents, "condense", condense)
    agents.report_result(db, "w1", "done")
    assert len(starts) == 1 and db.get_agent("w1").reviewing_since == claims[0]
    assert db.get_agent("w1").pipeline == "reviewing" and db.pending_count("boss") == 0

    phase = "newer-report"
    agents.report_result(db, "w1", "older")
    current = db.get_agent("w1")
    assert len(starts) == 3 and current.reviewing_since == claims[-1]
    assert current.result == "newer" and claims[-2] != claims[-1]
    assert current.pipeline == "reviewing" and db.pending_count("boss") == 0

    phase = "fallback-moved"
    agents.report_result(db, "w1", "ready for another review")
    current = db.get_agent("w1")
    assert len(starts) == 4 and current.reviewing_since == claims[-1]
    assert current.pipeline == "fixing" and db.pending_count("boss") == 0

    # Recovery can also inherit a claim made before any outbox receipt existed.
    phase = "legacy-fallback"
    db.update_agent("w1", pipeline="reviewing", reviewing_since=1.0)
    pipeline._start_review(db, pipeline.load_repo_config(ws.repo_root), db.get_agent("boss"),
                           db.get_agent("w1"), ws, "done", 1.0)
    row = db.conn.execute("SELECT * FROM report_outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert row["state"] == "delivered" and db.pending_count("boss") == 1
    assert db.get_agent("w1").pipeline is None

    # A direct legacy report must retain its receipt when the recipient is locked.
    from brindle import identity_lock

    phase = "legacy-dirty"
    monkeypatch.setattr(agents.git, "dirty_files", lambda *a, **k: ["work.py"])
    monkeypatch.setattr(identity_lock, "locked_by", lambda aid: "other" if aid == "boss" else None)
    with pytest.raises(agents.AgentError):
        pipeline.on_report(db, db.get_agent("w1"), ws, "dirty report")
    assert db.pending_count("boss") == 1
    monkeypatch.setattr(identity_lock, "locked_by", lambda aid: None)
    pipeline.on_report(db, db.get_agent("w1"), ws, "dirty report")
    pipeline.on_report(db, db.get_agent("w1"), ws, "dirty report")
    row = db.conn.execute("SELECT * FROM report_outbox WHERE canonical_id IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    assert row["state"] == "delivered" and db.pending_count("boss") == 2
    assert db.get_agent("w1").pipeline is None


@pytest.mark.parametrize("session, exits_mid_delivery", [
    (None, False),
    # Alive when the verdict lands, gone by the time send_message delivers.
    ("sess-w1", True),
])
def test_fix_round_resumes_exited_worker(db, repo, monkeypatch, session, exits_mid_delivery):
    """Live incident (session-17): a worker that had exited after
    report_result got a blocker review, and the pipeline handed its branch to
    the supervisor "after 0 fix round(s)" instead of starting a fix round.
    The worker is reopened (its own session, else a fresh start on its task)
    with the findings past the suggested cap, which notifies the supervisor once."""
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"review_stall_stop": False}))
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
    for rounds in (1, 2, 3, 4):
        db.update_agent("w1", pipeline="reviewing")
        live[:] = [True] if exits_mid_delivery else []
        assert pipeline.on_review(db, db.get_agent("rev"), ws, False, summary)
        worker, resume, prompt = launched[-1]
        assert (worker, resume) == ("w1", session) and "missing check for None" in prompt
        assert session or "Add the None check" in prompt
        assert (db.get_agent("w1").pipeline, db.get_agent("w1").pipeline_rounds) == ("fixing", rounds)
        assert len(drain(db)) == (1 if rounds == 3 else 0)
    db.update_agent("w1", pipeline="reviewing")
    assert pipeline.on_review(db, db.get_agent("rev"), ws, False,
                              "Needs supervisor action: ask the user about scope.")
    assert len(launched) == 4 and db.get_agent("w1").pipeline is None
    assert [e["agent_id"] for e in bus.tail(db, kinds=("pipeline.escalated",))] == ["w1"]


def _reported_branch_in_a_paused_session(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="paused")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", status="done", result="reported", pipeline="reviewing")
    add(db, ws, "rev", "review", "reviewer", parent="boss", status="done", result="no")
    db.update_agent("w1", session_ref="sess-w1", reviewing_since=1000.0)
    monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: True))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, *, prompt, resume, **kw:
                        (db_.set_status(a.id, "idle"), launched.append((a.id, prompt))))
    return ws, launched


BLOCKER = "- blocker: missing check for None.\nNeeds supervisor action: None."


@pytest.mark.parametrize("summary, landing, locked, restarted", [
    (BLOCKER, "as the chat starts", None, ["boss", "w1"]),      # past the resume's first look
])
def test_a_verdict_that_outlives_the_pause_waits_for_the_resume(db, repo, monkeypatch, summary, landing, locked, restarted):
    """Check runs and _finish-review outlive a pause: nothing starts, and nothing they say is lost."""
    from brindle import identity_lock
    ws, launched = _reported_branch_in_a_paused_session(db, repo, monkeypatch)
    monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: locked if agent_id == "w1" else None)
    launch, verdict = agents._launch, lambda: pipeline.on_review(db, db.get_agent("rev"), ws, False, summary)
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, **kw: (
        a.id == "boss" and landing == "as the chat starts" and verdict(), launch(db_, a, ws_, **kw)))
    if landing == "before the resume":
        assert verdict() and launched == [] and db.get_agent("boss").status == "paused"
    agents.resume(db, "boss")
    assert [a for a, _ in launched] == restarted    # queued findings, not a prompt, start its turn
    if summary == BLOCKER:
        assert (db.get_agent("w1").pipeline, dict(launched).get("w1")) == ("fixing", None)
        assert "missing check for None" in db.pop_pending("w1").body
    else:
        assert db.get_agent("w1").pipeline is None and "ask about scope" in db.pop_pending("boss").body


@pytest.mark.parametrize("read_by_first_look", [True])
def test_a_verdict_reopening_its_worker_keeps_that_launch_from_the_resume(db, repo, monkeypatch, read_by_first_look):
    """The verdict is past pipeline=fixing, under the merge lock, as the resume reads that row and the chat starts."""
    from brindle.db import DB
    ws, launched = _reported_branch_in_a_paused_session(db, repo, monkeypatch)
    published, scanned, stopped, outcome = threading.Event(), threading.Event(), [], []
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: stopped.extend(ids) or 0)
    real_hold, launch = pipeline.fix_round_hold, agents._launch
    thread = threading.Thread(daemon=True, target=lambda: outcome.append(
        pipeline.on_review(own := DB(), own.get_agent("rev"), ws, False, BLOCKER)))
    @contextlib.contextmanager
    def hold():
        if threading.current_thread() is thread:
            published.set()     # suspended here until the resume's later look is over
            assert scanned.wait(30)
        with real_hold():
            yield
        scanned.set()
    def start_verdict():
        thread.start()
        assert published.wait(30)
    monkeypatch.setattr(pipeline, "fix_round_hold", hold)
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, **kw: (
        a.id == "boss" and not read_by_first_look and start_verdict(), launch(db_, a, ws_, **kw)))
    if read_by_first_look:
        start_verdict()
    agents.resume(db, "boss")
    thread.join(30)
    assert outcome == [True] and stopped == [] and [a for a, _ in launched] == ["boss", "w1"]
    assert "missing check for None" in launched[1][1] and (db.get_agent("w1").status, db.get_agent("w1").pipeline) == ("idle", "fixing")


def test_branches_in_the_pipeline_carry_on_after_the_resume(db, repo, monkeypatch):
    """A check or merge run that finished in the pause or ended with the machine starts again."""
    ws, _launched = _reported_branch_in_a_paused_session(db, repo, monkeypatch)
    approved = workspaces.create(db, str(repo), "approved").workspace
    add(db, approved, "w2", "assign", parent="boss", status="done", result="reported", pipeline="ready")
    (Path(approved.path) / "feature.py").write_text("x = 1\n")
    sh("git add feature.py && git commit -qm feature", approved.path)
    db.add_review(approved.id, gates.head(approved), "rev", True, "lgtm")
    monkeypatch.setattr(gates, "check_summary", lambda *a, **kw: "PASS `true`")
    reviewers, restarts, started = [], [], []
    monkeypatch.setattr(agents, "request_review", lambda *a, **kw: reviewers.append(a))
    monkeypatch.setattr(pipeline, "_start_review", lambda *a, **kw: restarts.append(a))
    monkeypatch.setattr(agents, "_detach_orphan", started.append)
    now = time.time() + 600     # past the wait since the reviewer before it started
    assert agents.review_after_checks(db, "boss", ws, cfg=RepoConfig(checks=["true"]), report=1000.0) is None
    cull.note_reported_stuck(db, now, {})
    assert reviewers == [] and restarts == []
    db.update_agent("w1", review_restarted=1000.0)     # the one restart this wait gets, used before the pause
    agents.resume(db, "boss")
    cull.note_reported_stuck(db, now, {})
    assert len(restarts) == 1
    assert CliRunner().invoke(cli.app, started[0][started[0].index("_drain-ready"):]).exit_code == 0
    assert "feature.py" in sh("git ls-tree --name-only main", repo)


@pytest.mark.parametrize("outcomes", [
    pytest.param(("FAIL", "PASS"), marks=pytest.mark.variant(
        "review blocker: a pending second review passed after the first one's failure sent a fix round")),
])
def test_reviewer_starts_only_after_passing_checks(db, repo, monkeypatch, outcomes):
    """No reviewer runs while the checks do: it starts once they pass, with
    their results in its prompt. When they fail none starts at all: the worker
    gets the failing output as one fix round, and a required second review's
    run for the same report (even one whose checks pass, queued before the
    failure) neither sends it again nor reviews the report the worker left."""
    passes = outcomes == ("PASS", "PASS")
    since = time.time()
    ws = _reported(db, repo, reviewing_since=since)
    sent, spawned, running = [], [], []
    results = list(outcomes)

    def spawn(db_, ws_, prof, **kw):
        assert not running, "a reviewer started while the checks were running"
        spawned.append(kw["prompt"])
        add(db, ws, f"rev{len(spawned)}", "review", prof, parent="boss", review_sha=kw["review_sha"])
        return db.get_agent(f"rev{len(spawned)}")

    def checks(db_, ws_, cfg, cancel=None):
        running.append(1)
        try:
            return "PASS `true`" if results.pop(0) == "PASS" else "FAIL `true`\nexit 1: boom"
        finally:
            running.clear()

    monkeypatch.setattr(agents, "spawn", spawn)
    monkeypatch.setattr(gates, "check_summary", checks)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "w1")
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append((to, body)))
    cfg = workspaces.config_for(db, ws)

    def detach(argv):   # the helper process, run to its end before start_review returns
        opt = lambda name: argv[argv.index(name) + 1] if name in argv else None  # noqa: E731
        agents.review_after_checks(db, "boss", ws, opt("--profile"), None, cfg, request_id=opt("--request-id"),
                                   second="--second-review" in argv, slot=opt("--review-slot"),
                                   report=float(opt("--report")))

    monkeypatch.setattr(agents, "_detach", detach)
    # As the pipeline starts them, one report: the first helper's checks end
    # between the two calls, so a failure has sent the worker back before the second.
    agents.start_review(db, db.get_agent("boss"), ws, None, None, cfg, slot="", report=since)
    agents.start_review(db, db.get_agent("boss"), ws, "reviewer", None, cfg, second=True, report=since)
    if passes:
        assert len(spawned) == 2 and all("PASS `true`" in p for p in spawned) and sent == []
        assert db.get_agent("w1").pipeline == "reviewing"
    else:
        assert spawned == [] and [to for to, _ in sent] == ["w1"] and "boom" in sent[0][1]
        assert (db.get_agent("w1").pipeline, db.get_agent("w1").pipeline_rounds) == ("fixing", 1)


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
    pytest.param("Needs supervisor action: No items.",
                 marks=pytest.mark.variant("incident: a reviewer's 'No items.' stalled the pipeline")),
])
def test_supervisor_action_negations(summary):
    assert pipeline.has_supervisor_action(summary) is False


@pytest.mark.parametrize("case", [None, "old reviewer", "recorded"])
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


@pytest.mark.parametrize("base_file, reused", [
    ("other.py", True),          # disjoint from the branch's files
    pytest.param("web/npm-shrinkwrap.json", False, marks=pytest.mark.variant(
        "review finding: a nested lockfile not named *.lock / *-lock.*")),
])
def test_merge_reuses_checks_only_when_the_base_brought_in_disjoint_files(db, repo, base_file, reused):
    """A clean sync with the base keeps the branch's passing checks when the
    base touched none of its files; the check here fails whenever it runs."""
    (repo / "shared.py").write_text("".join(f"line{i}\n" for i in range(10)))
    sh("git add shared.py && git commit -qm shared", repo)
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["false"]}))
    workspaces.adopt_root(db, str(repo))
    ws = workspaces.create(db, str(repo), "feat").workspace
    path = Path(ws.path)
    (path / "shared.py").write_text("changed\n" + "".join(f"line{i}\n" for i in range(1, 10)))
    sh("git commit -qam work", path)
    db.set_check(ws.id, gates.head(ws), "false", True, "1 passed")
    target = repo / base_file
    target.parent.mkdir(exist_ok=True)
    target.write_text("".join(f"line{i}\n" for i in range(9)) + "base\n" if base_file == "shared.py" else "x = 1\n")
    sh(f"git add {base_file} && git commit -qm base", repo)

    out = pipeline.merge(db, None, ws)
    assert out.startswith("Merged") is reused, out


@pytest.mark.parametrize("amend, base_file, second, delta", [
    pytest.param(True, None, False, "work.py",
                 marks=pytest.mark.variant("an amended commit leaves the reviewed commit out of the branch's history")),
    (True, "base.py", False, "work.py"),
    pytest.param(True, "work.py", False, "work.py",
                 marks=pytest.mark.variant("the base merge conflicts with the reviewed code")),
    pytest.param(False, None, False, "",
                 marks=pytest.mark.variant("the worker reported again with no new commit")),
    pytest.param(False, None, True, None,
                 marks=pytest.mark.variant("a second reviewer of the commit reads the branch for itself")),
])
def test_review_round_scope_rereview_after_an_amend_and_a_base_merge_reads_only_the_delta(
        db, repo, monkeypatch, amend, base_file, second, delta):
    """The commit a re-review follows was amended away, and the base may have
    been merged in on top: its diff is still only what the worker changed."""
    ws = _reported(db, repo)
    path = Path(ws.path)
    db.add_review(ws.id, gates.head(ws), "rev0", False, "Blocker: x is wrong in work.py.")
    if amend:
        (path / "work.py").write_text("x = 2\n")
        sh("git add work.py && git commit -q --amend --no-edit", path)
    if base_file:
        (repo / base_file).write_text("b = 1\n")
        sh(f"git add {base_file} && git commit -qm base", repo)
        sh("git merge -q --no-edit main || true", path)
        (path / "work.py").write_text("x = 2\n")   # the worker's resolution, when it conflicted
        sh("git add work.py && git commit -q --no-edit || true", path)
    prompts = []
    monkeypatch.setattr(agents, "spawn", lambda db_, ws_, prof, **kw: prompts.append(kw["prompt"]))

    agents._request_review(db, None, ws, "reviewer", None, RepoConfig(), None, second=second)

    scope = re.search(r"`git diff (\S+?)(?<!\.)\.\.HEAD`", prompts[0])   # not the opening base...HEAD
    if delta is None:
        assert scope is None, prompts[0]
    else:
        assert scope, prompts[0]
        assert sh(f"git diff --name-only {scope.group(1)}..HEAD", path) == delta


@pytest.mark.parametrize("rounds, tests_only, paths, summary, merges", [
    pytest.param(2, True, ["tests/test_work.py"], "Blocker: tests/test_work.py:1 asserts nothing.", True,
                 marks=pytest.mark.variant("past the cap, a tests-only review merges and notifies once")),
    pytest.param(2, True, ["tests/test_work.py"], "Blocker: work.py:1 is wrong, tests/test_work.py:1 misses it.",
                 False, marks=pytest.mark.variant("a product finding flagged tests-only, citing the delta")),
    pytest.param(2, True, ["work.py"], "Blocker: this is wrong.",
                 False, marks=pytest.mark.variant("a product path among the finding's paths")),
    pytest.param(2, True, [], "Blocker: tests are thin.",
                 False, marks=pytest.mark.variant("flagged tests-only with no path to check")),
    pytest.param(2, True, ["tests/work.py"], "Blocker: tests/work.py:1 asserts nothing.",
                 True, marks=pytest.mark.variant("a test file sharing its name with the product file")),
    pytest.param(2, True, ["tests/work.py"], "Blocker: tests/work.py:1 misses that work.py:1 is wrong.",
                 False, marks=pytest.mark.variant("the product citation follows a same-named test citation")),
    pytest.param(2, True, ["tests/work.py"], "Blocker: [tests/work.py]({ws}/tests/work.py:1) asserts nothing.",
                 True, marks=pytest.mark.variant("an absolute link to the same-named test file")),
    pytest.param(2, True, ["tests/work.py"], "Blocker: see {ws}/work.py:1.",
                 False, marks=pytest.mark.variant("an absolute link to the product file")),
    pytest.param(2, True, ["tests/work.py"], "Blocker: see {ws}/tests/../work.py:1.",
                 False, marks=pytest.mark.variant("an absolute link climbing out of tests/ to the product file")),
    pytest.param(2, True, ["tests/test_work.py"], "{rereview}Blocker: work.py:1 is still wrong.", False,
                 marks=pytest.mark.variant("a re-review whose delta is tests-only cites a product file "
                                           "an earlier round changed")),
    pytest.param(2, False, ["tests/test_work.py"], "Blocker: tests/test_work.py:1 asserts nothing.", False,
                 marks=pytest.mark.variant("a test finding not flagged tests-only still blocks past the cap")),
    (1, True, ["tests/test_work.py"], "Blocker: tests/test_work.py:1 asserts nothing.", False),
])
def test_review_round_scope_tests_only_findings_past_the_cap(db, repo, monkeypatch, rounds, tests_only,
                                                             paths, summary, merges):
    """Past the standard rounds a review whose findings are all about tests
    merges, with one note to the supervisor; anything else goes back."""
    ws = _reported(db, repo, reviewing_since=time.time())
    (repo / ".brindle" / "config.local.json").write_text('{"auto_merge_default_branch": true}')
    path = Path(ws.path)
    if summary.startswith("{rereview}"):   # an earlier round reviewed work.py: this one reads only tests/
        summary = summary.removeprefix("{rereview}")
        db.add_review(ws.id, gates.head(ws), None, False, "Blocker: work.py:1 is wrong.")
    (path / "tests").mkdir()
    (path / "tests" / "test_work.py").write_text("def test_work(): pass\n")
    (path / "tests" / "work.py").write_text("helper = 1\n")
    sh("git add tests && git commit -qm tests", path)
    db.update_agent("w1", pipeline_rounds=rounds)
    add(db, ws, "rev1", "review", "reviewer", parent="boss", status="processing")
    db.update_agent("rev1", review_sha=gates.head(ws))
    reopened = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "reopen_worker", lambda db_, a, ws_, prompt: reopened.append(prompt) or True)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)

    summary = summary.replace("{ws}", ws.path)
    agents.submit_review(db, "rev1", False, summary, tests_only=tests_only, finding_paths=paths)

    assert (repo / "work.py").exists() is merges
    told = drain(db)
    if merges:
        assert not reopened
        assert any("Merged feat" in b for b in told), told
        assert sum(summary in b for b in told if "Merged feat" not in b) == 1, told
    else:
        assert len(reopened) == 1 and db.get_agent("w1").pipeline == "fixing"
