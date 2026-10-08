"""Conflict-aware merging, part three: an approved branch whose sync with
its base conflicts goes back to its worker to resolve (a light worker if
that one has finished), is reviewed and checked again, and then merges.
Protected paths are never resolved by a worker."""
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, conflicts, gates, pipeline, workspaces
from brindle.db import Agent


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", result=None,
        pipeline=None, **kw):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", result,
                       time.time(), **kw))
    if pipeline:   # add_agent writes the core columns only
        db.update_agent(agent_id, pipeline=pipeline)


# -- protected paths -----------------------------------------------------------------


@pytest.mark.parametrize("path, patterns, expected", [
    ("app.py", ["app.py"], True),
    ("src/app.py", ["app.py"], False),
    ("uv.lock", ["*.lock"], True),
    ("migrations/0002_x.py", ["migrations/"], True),
    ("migrations/0002_x.py", ["migrations"], True),
    ("migrations/deep/0002_x.py", ["migrations/**"], True),
    ("src/migrations/0002_x.py", ["**/migrations/*"], True),
    ("src/other.py", ["migrations/", "*.lock"], False),
    ("anything.py", [], False),
    ("./app.py", ["app.py"], True),
])
def test_is_protected(path, patterns, expected):
    assert conflicts.is_protected(path, patterns) is expected


def test_protected_paths_config_defaults_to_empty_and_reads_a_list(repo):
    from brindle.config import load_repo_config

    assert load_repo_config(str(repo)).protected_paths == []
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"protected_paths": ["migrations/", "*.lock"]}))
    assert load_repo_config(str(repo)).protected_paths == ["migrations/", "*.lock"]


# -- the pipeline on a conflicting branch ---------------------------------------------


@pytest.fixture
def conflicted(db, repo, monkeypatch):
    """A busy supervisor; a worker's approved branch that rewrote app.py;
    main that rewrote app.py differently since. The repo's check passes only
    once the resolved file carries both sides."""
    def setup(**config):
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps({
            "review": True, "auto_merge_default_branch": True,
            "checks": ["grep -q mine app.py && grep -q theirs app.py"], **config}))
        root = workspaces.adopt_root(db, str(repo))
        add(db, root, "boss", "interactive", "supervisor", status="processing")
        ws = workspaces.create(db, str(repo), "feat").workspace
        (Path(ws.path) / "app.py").write_text("mine\n")
        sh("git add -A && git commit -qm mine", Path(ws.path))
        (repo / "app.py").write_text("theirs\n")
        sh("git add -A && git commit -qm theirs", repo)
        add(db, ws, "w1", "assign", parent="boss", status="processing",
            result="rewrote app.py", pipeline="reviewing")
        add(db, ws, "rev0", "review", "reviewer", parent="boss")
        db.add_review(ws.id, gates.head(ws), "rev0", True, "lgtm")
        return ws

    started = []

    def fake_review(db_, caller, ws_, profile=None, focus=None, cfg=None):
        rid = f"rev{len(started) + 1}"
        add(db_, ws_, rid, "review", "reviewer", parent=caller.id)
        started.append(ws_.id)
        return db_.get_agent(rid)

    monkeypatch.setattr(agents, "request_review", fake_review)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    def detach(argv):
        # The repo has checks, so the review starts from a detached process
        # once they finish: stand in for it.
        if "_review-after-checks" in argv:
            ws_ = db.get_workspace(argv[argv.index("_review-after-checks") + 1])
            fake_review(db, db.get_agent("boss"), ws_)

    monkeypatch.setattr(agents, "_detach", detach)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    setup.reviews = started
    return setup


def resolve(ws):
    """What the worker does: merge main, keep both sides, commit."""
    sh("git merge main || true", Path(ws.path))
    (Path(ws.path) / "app.py").write_text("mine\ntheirs\n")
    sh("git add -A && git commit -qm resolved", Path(ws.path))


def boss_messages(db):
    return [db.pop_pending("boss").body for _ in range(db.pending_count("boss"))]


def approve(db, ws, reviewer_id, summary):
    """What submit_review records before it hands the verdict to the pipeline."""
    db.add_review(ws.id, gates.head(ws), reviewer_id, True, summary)


def test_a_live_worker_is_asked_to_resolve_and_the_branch_is_reviewed_and_checked_again(db, repo, conflicted):
    ws = conflicted()
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True

    w1 = db.get_agent("w1")
    assert w1.pipeline == "resolving"
    ask = db.pop_pending("w1")
    assert ask and "git merge main" in ask.body and "app.py" in ask.body and "report_result" in ask.body
    assert "grep -q mine app.py" in ask.body                     # told which checks must pass
    [fyi] = boss_messages(db)
    assert "conflicts in: app.py" in fyi and "w1" in fyi and "needs you" not in fyi
    assert "theirs\n" == (repo / "app.py").read_text()           # nothing merged yet
    assert db.get_workspace(ws.id) is not None and git_clean(ws)

    # The worker resolves and reports: a fresh review, not a merge.
    resolve(ws)
    out = agents.report_result(db, "w1", "resolved the conflict")
    assert "having your branch reviewed" in out
    assert conflicted.reviews == [ws.id] and db.get_agent("w1").pipeline == "reviewing"
    assert (repo / "app.py").read_text() == "theirs\n"

    # The second approval merges: the check ran on the resolved commit first.
    approve(db, ws, "rev1", "resolved cleanly")
    assert pipeline.on_review(db, db.get_agent("rev1"), ws, True, "resolved cleanly") is True
    assert (repo / "app.py").read_text() == "mine\ntheirs\n"
    assert db.get_workspace(ws.id) is None
    [merged] = boss_messages(db)
    assert "Merged feat into main" in merged and "resolved cleanly" in merged
    assert "grep -q mine app.py" in merged and "passed" in merged   # the gate summary names the check


def git_clean(ws) -> bool:
    from brindle import git

    return git.dirty_files(ws.path) == [] and not git.ok(["rev-parse", "-q", "--verify", "MERGE_HEAD"], ws.path)


def test_a_failing_recheck_after_resolution_does_not_merge(db, repo, conflicted):
    ws = conflicted()
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    db.pop_pending("w1")
    boss_messages(db)
    sh("git merge main || true", Path(ws.path))
    (Path(ws.path) / "app.py").write_text("mine\n")              # dropped main's side
    sh("git add -A && git commit -qm resolved-badly", Path(ws.path))
    agents.report_result(db, "w1", "resolved")
    approve(db, ws, "rev1", "looks fine")
    pipeline.on_review(db, db.get_agent("rev1"), ws, True, "looks fine")
    assert (repo / "app.py").read_text() == "theirs\n"
    [msg] = boss_messages(db)
    assert "needs you" in msg and "Check failed" in msg


def test_a_finished_workers_conflict_goes_to_a_light_worker_in_its_worktree(db, repo, conflicted, monkeypatch):
    ws = conflicted()
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "w1")
    calls = []

    def fake_delegate(db_, caller, caller_ws, profile, task, mode, *, isolate=True, branch=None,
                      done_when=None, plan_first=None):
        calls.append(dict(caller=caller.id, ws=caller_ws.id, profile=profile, task=task, mode=mode,
                          isolate=isolate, done_when=done_when, plan_first=plan_first))
        add(db_, caller_ws, "fixer", "assign", profile, parent=caller.id, status="processing", task=task)
        return db_.get_agent("fixer"), caller_ws

    monkeypatch.setattr(agents, "delegate", fake_delegate)
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True

    [call] = calls
    assert call["caller"] == "boss" and call["ws"] == ws.id and call["mode"] == "assign"
    assert call["isolate"] is False and call["plan_first"] is False
    assert "git merge main" in call["task"] and "app.py" in call["task"]
    assert "no conflict markers" in call["done_when"]
    assert db.get_agent("fixer").pipeline == "resolving" and db.get_agent("w1").pipeline is None
    assert db.pending_count("w1") == 0
    [fyi] = boss_messages(db)
    assert "fixer" in fyi and "light worker" in fyi and "needs you" not in fyi

    # The light worker's report goes through the pipeline like the original's.
    resolve(ws)
    agents.report_result(db, "fixer", "resolved")
    assert db.get_agent("fixer").pipeline == "reviewing"
    approve(db, ws, "rev1", "ok")
    pipeline.on_review(db, db.get_agent("rev1"), ws, True, "ok")
    assert (repo / "app.py").read_text() == "mine\ntheirs\n" and db.get_workspace(ws.id) is None
    [merged] = boss_messages(db)
    assert "Merged feat into main" in merged and "resolved" in merged


def test_when_no_resolver_can_start_the_conflict_needs_the_supervisor(db, repo, conflicted, monkeypatch):
    ws = conflicted()
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "w1")

    def refuse(*a, **k):
        raise agents.AgentError("max_agents reached")

    monkeypatch.setattr(agents, "delegate", refuse)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    [msg] = boss_messages(db)
    assert "needs you" in msg and "max_agents reached" in msg and "app.py" in msg
    assert db.get_agent("w1").pipeline is None and db.get_workspace(ws.id) is not None


def test_a_protected_path_is_never_resolved_by_a_worker(db, repo, conflicted, monkeypatch):
    ws = conflicted(protected_paths=["app.py"])
    delegated = []
    monkeypatch.setattr(agents, "delegate", lambda *a, **k: delegated.append(a))
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    [msg] = boss_messages(db)
    assert "needs you" in msg and "protected path" in msg and "app.py" in msg and "protected_paths" in msg
    assert db.pending_count("w1") == 0 and delegated == []       # the live worker wasn't asked either
    assert db.get_agent("w1").pipeline is None
    assert db.get_workspace(ws.id) is not None and (repo / "app.py").read_text() == "theirs\n"


def test_a_protected_file_with_a_quoted_name_is_still_protected(db, repo, conflicted):
    """git C-quotes names with spaces or non-ASCII characters in its plain
    output; read raw, the name still matches the protected glob."""
    name = "café lock.py"
    ws = conflicted(protected_paths=["*.py"])
    (Path(ws.path) / name).write_text("mine\n")
    sh("git add -A && git commit -qm quoted", Path(ws.path))
    (repo / name).write_text("theirs\n")
    sh("git add -A && git commit -qm quoted-theirs", repo)
    db.add_review(ws.id, gates.head(ws), "rev0", True, "lgtm")
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    [msg] = boss_messages(db)
    assert "protected path" in msg and name in msg and db.get_agent("w1").pipeline is None


def test_an_approval_for_a_stale_commit_does_not_make_the_branch_ready(db, repo, conflicted):
    ws = conflicted()
    (Path(ws.path) / "later.py").write_text("x\n")
    sh("git add -A && git commit -qm later", Path(ws.path))     # committed after rev0 read the branch
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    assert db.get_agent("w1").pipeline is None and db.pending_count("w1") == 0
    [msg] = boss_messages(db)
    assert "needs you" in msg and "hasn't been reviewed" in msg
    assert (repo / "app.py").read_text() == "theirs\n"


def test_protected_globs_and_directories_count(db, repo, conflicted):
    ws = conflicted(protected_paths=["*.py"])
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    [msg] = boss_messages(db)
    assert "protected path" in msg and db.get_agent("w1").pipeline is None


def test_the_worker_in_the_pipelines_hands_is_the_latest_piped_one(db, repo, conflicted):
    ws = conflicted()
    add(db, ws, "fixer", "assign", parent="boss", status="processing", pipeline="resolving")
    assert pipeline.piped_worker(db, ws).id == "fixer"
    db.update_agent("fixer", pipeline=None)
    db.update_agent("w1", pipeline=None)
    assert pipeline.piped_worker(db, ws).id == "w1"               # falls back to the task's worker
