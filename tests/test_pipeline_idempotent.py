"""A workspace merges once: a second verdict or merge for a branch that has
merged (or whose worktree is gone, or going) runs no gate, merges nothing and
reports no failure."""
import json
import threading
import time
from pathlib import Path

import pytest

from conftest import sh
from frith import agents, gates, pipeline, workspaces
from frith.db import DB, Agent


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", **kw):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", None,
                       time.time(), **kw))


@pytest.fixture
def approved(db, repo, monkeypatch):
    """A piped worker's branch with two reviewers that both approved its
    commit (what a duplicate review looks like), and a count of gate runs."""
    (repo / ".frith").mkdir()
    (repo / ".frith" / "config.json").write_text(json.dumps(
        {"review": True, "auto_merge_default_branch": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")  # busy: messages queue
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", Path(ws.path))
    add(db, ws, "w1", "assign", parent="boss", status="processing")
    db.update_agent("w1", result="added new.py", pipeline="reviewing")
    for rid in ("rev0", "rev1"):
        add(db, ws, rid, "review", "reviewer", parent="boss", status="processing")
        db.add_review(ws.id, gates.head(ws), rid, True, "lgtm")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    runs = []
    real_run = gates.run
    monkeypatch.setattr(gates, "run", lambda *a, **k: (runs.append(1), real_run(*a, **k))[1])
    return ws, runs


def drain(db, agent_id="boss"):
    bodies = []
    while (msg := db.pop_pending(agent_id)):
        bodies.append(msg.body)
    return bodies


def keep_worktree(monkeypatch):
    def boom(*a, **k):
        raise workspaces.WorkspaceError("teardown failed")

    monkeypatch.setattr(workspaces, "remove", boom)


def test_a_second_verdict_after_the_merge_and_removal_is_dropped(db, approved):
    ws, runs = approved
    rev0, rev1 = db.get_agent("rev0"), db.get_agent("rev1")
    assert pipeline.on_review(db, rev0, ws, True, "lgtm") is True
    assert db.get_workspace(ws.id) is None
    bodies = drain(db)
    assert len(bodies) == 1 and "Merged feat into main" in bodies[0]
    assert pipeline.on_review(db, rev1, ws, True, "lgtm") is True   # handled: nothing forwarded
    assert drain(db) == [] and len(runs) == 1                        # no gate on the gone worktree


def test_a_second_verdict_while_the_merged_worktree_is_kept_is_dropped(db, approved, monkeypatch):
    ws, runs = approved
    keep_worktree(monkeypatch)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    bodies = drain(db)
    assert sum("Merged feat" in b for b in bodies) == 1
    assert pipeline.on_review(db, db.get_agent("rev1"), ws, True, "lgtm") is True
    assert drain(db) == [] and len(runs) == 1


def test_merging_a_merged_branch_again_says_so_without_running_gates(db, approved, monkeypatch):
    ws, runs = approved
    keep_worktree(monkeypatch)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    out = pipeline.merge(db, db.get_agent("boss"), ws)
    assert out.startswith("Already merged") and len(runs) == 1


def test_new_commits_after_a_merge_can_merge_again(db, approved, monkeypatch, repo):
    ws, runs = approved
    keep_worktree(monkeypatch)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    (Path(ws.path) / "more.py").write_text("y = 2\n")
    sh("git add more.py && git commit -qm more", Path(ws.path))
    db.add_review(ws.id, gates.head(ws), "rev1", True, "lgtm")
    out = pipeline.merge(db, db.get_agent("boss"), ws)
    assert out.startswith("Merged feat into main")
    assert "more.py" in sh("git ls-tree --name-only HEAD", repo)


def test_merging_a_removed_workspace_runs_nothing(db, approved):
    ws, runs = approved
    workspaces.remove(db, ws, force=True)
    out = pipeline.merge(db, db.get_agent("boss"), ws)
    assert out.startswith("Nothing to merge") and runs == []


def test_a_worktree_removed_under_the_gates_is_not_a_failed_check(db, approved, monkeypatch):
    """The gates fail in a worktree that is being removed (the suite can't even
    be collected): that isn't the branch's failure, and nobody hears of it."""
    ws, runs = approved

    def gone_under_it(db_, ws_, cfg, review_required):
        workspaces.remove(db_, ws_, force=True)
        return gates.Report().fail("Check failed in feat:\n83 errors during collection")

    monkeypatch.setattr(gates, "run", gone_under_it)
    assert pipeline.merge(db, db.get_agent("boss"), ws).startswith("Nothing to merge")


def test_a_verdict_for_a_worktree_removed_under_the_gates_reports_no_failure(db, approved, monkeypatch):
    ws, runs = approved
    rev0 = db.get_agent("rev0")

    def gone_under_it(db_, ws_, cfg, review_required):
        workspaces.remove(db_, ws_, force=True)
        return gates.Report().fail("Check failed in feat:\n83 errors during collection")

    monkeypatch.setattr(gates, "run", gone_under_it)
    assert pipeline.on_review(db, rev0, ws, True, "lgtm") is True
    assert drain(db) == []


def test_two_verdicts_at_once_merge_once(db, approved, monkeypatch):
    """Two reviewers approve the same commit at the same moment, each in its
    own process: the second waits for the first, then finds the branch merged."""
    ws, runs = approved
    in_gates, release = threading.Event(), threading.Event()
    real_run = gates.run

    def slow_run(*a, **k):
        in_gates.set()
        assert release.wait(10)
        return real_run(*a, **k)

    monkeypatch.setattr(gates, "run", slow_run)
    results = {}

    def verdict(rid):
        own = DB()   # as another process would have
        results[rid] = pipeline.on_review(own, own.get_agent(rid), ws, True, "lgtm")

    first = threading.Thread(target=verdict, args=("rev0",))
    first.start()
    assert in_gates.wait(10)
    second = threading.Thread(target=verdict, args=("rev1",))
    second.start()
    second.join(0.5)
    assert second.is_alive() and "rev1" not in results   # waiting for the first one's merge
    release.set()
    first.join(20)
    second.join(20)
    assert results == {"rev0": True, "rev1": True}
    bodies = drain(db)
    assert len(bodies) == 1 and "Merged feat into main" in bodies[0]
    assert len(runs) == 1


def test_a_failed_merge_is_reported_once_and_not_retried_by_a_second_verdict(db, approved, monkeypatch):
    ws, runs = approved
    attempts = []

    def failing(db_, ws_, cfg, review_required):
        attempts.append(1)
        return gates.Report().fail("Unable to write index.")

    monkeypatch.setattr(gates, "run", failing)
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    bodies = drain(db)
    assert len(bodies) == 1 and "couldn't be merged" in bodies[0]
    # The branch is the supervisor's now: a second verdict doesn't merge behind its back.
    assert pipeline.on_review(db, db.get_agent("rev1"), ws, True, "lgtm") is False
    assert len(attempts) == 1 and drain(db) == []
    assert db.get_workspace(ws.id) is not None


def test_submitting_the_same_review_twice_records_it_once(db, approved, monkeypatch):
    ws, runs = approved
    keep_worktree(monkeypatch)
    assert "frith takes it from here" in agents.submit_review(db, "rev0", True, "lgtm")
    merged = [b for b in drain(db) if "Merged feat" in b]
    assert len(merged) == 1
    reviews = db.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert "already" in agents.submit_review(db, "rev0", True, "lgtm")
    assert drain(db) == [] and len(runs) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == reviews


def test_a_second_reviewer_submitting_after_the_removal_is_told_it_is_done(db, approved):
    ws, runs = approved
    agents.submit_review(db, "rev0", True, "lgtm")
    assert db.get_workspace(ws.id) is None
    drain(db)
    out = agents.submit_review(db, "rev1", True, "lgtm")
    assert "Only a reviewer" in out or "already" in out
    assert drain(db) == [] and len(runs) == 1


def test_a_late_failing_check_summary_for_a_merged_branch_is_dropped(db, approved, monkeypatch):
    """_deliver-checks was still running the suite in the worktree when the
    branch merged and the worktree started to go: its failure is not news."""
    ws, runs = approved
    keep_worktree(monkeypatch)
    sha = gates.head(ws)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    drain(db)
    agents._late_check_summary(db, ws, sha, "FAIL `pytest`\n83 errors during collection",
                               "rev0", "boss")
    assert drain(db) == []
