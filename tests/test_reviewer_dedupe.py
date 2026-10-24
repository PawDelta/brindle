"""One reviewer per workspace and commit (a new commit replaces it), and the
"stopped without calling submit_review" notice: none once a verdict is
recorded for the current commit, at most once otherwise, from brindle, and
naming a reviewer rather than a worker."""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, cli, gates, launch_health, mcp_server, pipeline, tmux, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent, Task, Workspace
from brindle.profiles import Profile


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, *x, **k: True)
    monkeypatch.setattr(agents, "load_profile",
                        lambda name, repo_root=None: Profile(name, "", "claude", ""))


@pytest.fixture
def ws(db, repo):
    w = workspaces.create(db, str(repo), "feat").workspace
    (Path(w.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(w.path))
    return w


@pytest.fixture
def boss(db, ws):
    a = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0",
              None, time.time())
    db.add_agent(a)
    return a


def add_reviewer(db, ws, agent_id="rev1", sha=None):
    a = Agent(agent_id, ws.id, "reviewer", "claude", "boss", "review", "processing", "@0",
              None, time.time())
    db.add_agent(a)
    db.update_agent(agent_id, review_sha=sha)
    return a


def fake_spawn(monkeypatch):
    started = []

    def spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review",
              review_sha=None, **kw):
        a = Agent(f"new{len(started)}", ws_.id, profile, "claude", parent_id, mode,
                  "starting", "@0", None, time.time(), review_sha=review_sha, task=prompt)
        db_.add_agent(a)
        started.append(a)
        return a

    monkeypatch.setattr(agents, "spawn", spawn)
    monkeypatch.setattr(agents, "close", lambda db_, aid, *a, **k: db_.update_agent(aid, status="done"))
    return started


def test_second_request_for_the_same_commit_reuses_the_running_reviewer(db, ws, monkeypatch):
    started = fake_spawn(monkeypatch)
    first = agents.request_review(db, None, ws, "reviewer")
    second = agents.request_review(db, None, ws, "reviewer")
    assert second.id == first.id
    assert len(started) == 1


def test_new_commit_replaces_the_old_reviewer(db, ws, monkeypatch):
    started = fake_spawn(monkeypatch)
    old = agents.request_review(db, None, ws, "reviewer")
    (Path(ws.path) / "more.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm more", Path(ws.path))
    new = agents.request_review(db, None, ws, "reviewer")
    assert new.id != old.id
    assert len(started) == 2
    assert db.get_agent(old.id).status == "done"


def piped_worker(db, ws, boss, monkeypatch, cfg):
    """A worker in ``ws`` whose reports go through the pipeline's real review
    dispatch; returns the detached check runs it queues."""
    from test_merge_order import add

    add(db, ws, "worker", "assign", parent=boss.id, result="done")
    monkeypatch.setattr(pipeline, "load_repo_config", lambda _: cfg)
    monkeypatch.setattr("brindle.config.load_repo_config", lambda _: cfg)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    queued = []
    monkeypatch.setattr(agents, "_detach", queued.append)
    return queued


def run_helper(ws, argv):
    """Run a queued ``_review-after-checks`` helper, as its detached process would."""
    args = argv[argv.index(ws.id) + 1:]
    second = "--second-review" in args
    options = dict(zip(*[iter(a for a in args if a != "--second-review")] * 2))
    slot = {"slot": options["--review-slot"]} if "--review-slot" in options else {}
    cli.review_after_checks_cmd(ws.id, options.get("--caller"), options.get("--profile"),
                                options.get("--focus"), options.get("--request-id"), second,
                                **slot)


@pytest.mark.parametrize("checks", [[], ["suite"]], ids=["direct", "checks"])
@pytest.mark.variant("#262: real MCP requests with a live holder of a different profile")
def test_mcp_explicit_profile_keeps_other_profile_running(db, ws, boss, monkeypatch, checks):
    cfg = RepoConfig(checks=checks, review_profile="reviewer")
    queued = piped_worker(db, ws, boss, monkeypatch, cfg)
    monkeypatch.setattr(mcp_server, "load_repo_config", lambda _: cfg)
    monkeypatch.setenv("BRINDLE_AGENT_ID", boss.id)
    started = fake_spawn(monkeypatch)
    now = [time.time()]
    monkeypatch.setattr(agents.time, "time", lambda: now[0])

    def request(profile=None, abandon=False):
        before = len(queued)
        asyncio.run(mcp_server.request_review(ws.id, profile=profile))
        if checks and abandon:
            stale = queued[-1]
            now[0] += agents.LAUNCH_GRACE + 1
            asyncio.run(mcp_server.request_review(ws.id, profile=profile))
            before += 1
            assert len(queued) == before + 1
            run_helper(ws, stale)
            assert agents._review_requested(ws.id, profile, queued[-1][-1])
        for argv in queued[before:]:
            cancellations = []
            asyncio.run(mcp_server.request_review(ws.id, profile=profile))
            assert len(queued) == before + 1

            def check(*args, cancel, **kwargs):
                was_cancelled = cancel()
                if profile is None or abandon:   # a running helper outlives its startup grace
                    now[0] += agents.LAUNCH_GRACE + 1
                asyncio.run(mcp_server.request_review(ws.id, profile=profile))
                cancellations.append((was_cancelled, cancel(),
                                      agents._review_requested(ws.id, profile or "reviewer", argv[-1])))
                return "PASS"

            monkeypatch.setattr(gates, "check_summary", check)
            run_helper(ws, argv)
            assert cancellations == [(False, False, True)]
            assert len(queued) == before + 1

    request()
    original = db.get_agent(started[0].id)
    claim = db.review_claim(ws.id, original.review_sha, original.review_slot)
    # A dispatcher that has not published its reviewer still owns this claim.
    token = db.claim_review(ws.id, original.review_sha, "reviewer-codex")
    request("reviewer-codex")
    assert len(started) == 1
    assert db.review_claim(ws.id, original.review_sha, "reviewer-codex") == (None, token)
    db.hold_review(ws.id, original.review_sha, "reviewer-codex", token, None)
    request("reviewer-codex", abandon=True)
    assert [a.profile for a in started] == ["reviewer", "reviewer-codex"]
    count = len(queued)
    request("reviewer-codex")
    request("reviewer")
    assert len(started) == 2 and len(queued) == count
    assert db.get_agent(original.id).status == "starting"
    assert db.review_claim(ws.id, original.review_sha, original.review_slot) == claim


@pytest.mark.parametrize("weight,picks,reviewers", [
    ("light", ["reviewer", "reviewer-codex"], ["reviewer"]),
    ("heavy", ["reviewer"], ["reviewer", "reviewer-codex"]),
])
def test_one_reviewer_per_commit_when_the_learner_picks_another_profile(db, ws, boss, monkeypatch,
                                                                       weight, picks, reviewers):
    # Two reports of one commit, each with its own check run, and the learner
    # picking a different reviewer for each: both runs used to start one. A
    # required second review still gets its own.
    cfg = RepoConfig(checks=["suite"], review=True, second_review={"heavy": "reviewer-codex"})
    queued = piped_worker(db, ws, boss, monkeypatch, cfg)
    db.add_task(Task("t1", ws.repo_root, "worker", boss.id, ws.id, "developer", "do it", "assign", 1,
                     ws.branch, None, None, None, "started", time.time(), weight=weight))
    picks = itertools.cycle(picks)
    monkeypatch.setattr(agents, "default_review_profile", lambda *a, **k: next(picks))
    monkeypatch.setattr(gates, "check_summary", lambda *a, **k: "PASS")
    started = fake_spawn(monkeypatch)
    for _ in range(2):
        assert pipeline.on_report(db, db.get_agent("worker"), ws, "done")
    for argv in queued:
        run_helper(ws, argv)
    assert sorted(a.profile for a in started) == reviewers


@pytest.mark.parametrize("supersede", ["before checks", "during checks"])
def test_queued_reports_only_dispatch_the_latest_review(db, ws, boss, monkeypatch, supersede):
    cfg = RepoConfig(checks=["suite"], review=True, review_profile="reviewer")
    queued = piped_worker(db, ws, boss, monkeypatch, cfg)
    started = fake_spawn(monkeypatch)
    checked = []

    def report():
        assert pipeline.on_report(db, db.get_agent("worker"), ws, "done")

    def finish(argv):
        run_helper(ws, argv)

    def checks(*args, **kwargs):
        checked.append(gates.head(ws))
        if supersede == "during checks" and len(checked) == 1:
            monkeypatch.setattr(gates, "head", lambda _: "new-sha")
            report()
            assert kwargs["cancel"]()
        return "PASS"

    monkeypatch.setattr(gates, "check_summary", checks)
    report()
    (Path(ws.path) / "more.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm more", Path(ws.path))
    report()
    # The obsolete helper starts late, after the newer report has taken over.
    finish(queued[0])
    assert started == [] and checked == []
    finish(queued[1])
    if supersede == "during checks":
        assert started == []
        finish(queued[2])
    assert len(started) == 1 and started[0].review_sha == gates.head(ws)
    assert len(checked) == (2 if supersede == "during checks" else 1)
    # Repeated reports and delayed helpers must respect the live reviewer.
    report()
    finish(queued[1])
    assert agents.request_review(db, boss, ws).id == started[0].id
    assert len(queued) == (3 if supersede == "during checks" else 2) and len(started) == 1


@pytest.fixture
def mcp_review(db, tmp_path, monkeypatch):
    ws = Workspace("repo/feat", str(tmp_path), "feat", "worktree", "feat", "main",
                   str(tmp_path), None, "session", time.time())
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0",
                 None, time.time())
    db.add_agent(boss)
    monkeypatch.setattr(gates, "head", lambda ws: "old-sha")
    started = fake_spawn(monkeypatch)
    reports = []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body: reports.append(to))
    monkeypatch.setattr(launch_health, "mcp_failed", lambda *a: True)
    monkeypatch.setattr(agents, "_detach_orphan", lambda argv: launch_health.recover_reviewer(db, argv[-1]))

    def arm(a):
        db.update_agent(a.id, session_ref="session-" + a.id)
        a = db.get_agent(a.id)
        launch_health.prepare(db, a, "account")
        return a

    return ws, boss, started, reports, arm


@pytest.mark.parametrize("trigger,outcome", [
    ("hook", "failed"),
    ("request", "failed"),
    pytest.param("hook", "completed", marks=pytest.mark.variant(
        "a delayed recovery helper races with replacement completion")),
])
def test_mcp_failure_replaces_reviewer_once_per_commit(db, mcp_review, monkeypatch, trigger, outcome):
    ws, boss, started, reports, arm = mcp_review
    old = arm(agents.request_review(db, boss, ws, "reviewer"))
    if trigger == "hook":
        launch_health.check_mcp(db, old)
    new = agents.request_review(db, boss, ws, "reviewer")
    assert new.id != old.id and new.review_sha == old.review_sha
    assert db.get_agent(new.id).review_slot == "" == old.review_slot   # its verdict replaces in the same slot
    assert new.task == old.task and new.parent_id == old.parent_id
    assert db.get_agent(old.id).status == "done"
    launch_health.check_mcp(db, old)  # stale hook must respect the replacement
    launch_health.recover_reviewer(db, old.id)  # a queued helper lost to an explicit request
    assert agents.request_review(db, boss, ws, "reviewer").id == new.id
    assert len(started) == 2

    if outcome == "completed":
        monkeypatch.setattr(agents, "finish_review_later", lambda *a: None)
        agents.submit_review(db, new.id, True, "review complete")
        launch_health.recover_reviewer(db, old.id)
        assert len(started) == 2 and reports == []
        requested = agents.request_review(db, boss, ws, "reviewer")
        assert requested.id not in (old.id, new.id) and len(started) == 3
        return

    launch_health.check_mcp(db, arm(new))
    launch_health.check_mcp(db, db.get_agent(new.id))
    assert db.get_agent(new.id).status == "done"
    with pytest.raises(agents.AgentError):
        agents.request_review(db, boss, ws, "reviewer")
    assert len(started) == 2 and reports == [boss.id]
    # A new commit gets its own recovery budget; no real git process is needed.
    monkeypatch.setattr(gates, "head", lambda ws: "new-sha")
    next_review = arm(agents.request_review(db, boss, ws, "reviewer"))
    launch_health.check_mcp(db, next_review)
    assert len(started) == 4 and started[-1].review_sha == "new-sha"


@pytest.mark.parametrize("failure", ["moved", "spawn"])
def test_mcp_recovery_reports_when_replacement_cannot_start(db, mcp_review, monkeypatch, failure):
    ws, boss, started, reports, arm = mcp_review
    old = arm(agents.request_review(db, boss, ws, "reviewer"))
    if failure == "moved":
        monkeypatch.setattr(gates, "head", lambda ws: "new-sha")
    else:
        def unavailable(*a, **k):
            raise agents.AgentError("launch failed")
        monkeypatch.setattr(agents, "spawn", unavailable)
    launch_health.check_mcp(db, old)
    assert db.get_agent(old.id).status == "done"
    assert len(started) == 1 and reports == [boss.id]


def test_no_notice_once_the_reviewer_submitted_for_the_current_commit(db, ws, boss, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    rev = add_reviewer(db, ws)
    db.add_review(ws.id, gates.head(ws), rev.id, True, "fine")
    agents.tell_parent_unreported(db, rev)
    assert sent == []


def test_review_at_an_older_commit_still_gets_its_one_notice(db, ws, boss, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    rev = add_reviewer(db, ws)
    db.add_review(ws.id, "0" * 40, rev.id, True, "old")
    agents.tell_parent_unreported(db, rev)
    assert len(sent) == 1
