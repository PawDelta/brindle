"""A workspace merges once: a second verdict or merge for a branch that has
merged (or whose worktree is gone, or going) runs no gate, merges nothing and
reports no failure. And the pipeline won't merge into the default branch
unless told to."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from conftest import full_text, sh
from brindle import agents, gates, pipeline, workspaces
from brindle.db import DB
from test_merge_order import add


@pytest.fixture
def approved(db, repo, monkeypatch):
    """A piped worker's branch with two reviewers that both approved its
    commit (what a duplicate review looks like), and a count of gate runs."""
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps(
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
        bodies.append(full_text(db, agent_id, msg.body))
    return bodies


def keep_worktree(monkeypatch):
    def boom(*a, **k):
        raise workspaces.WorkspaceError("teardown failed")

    monkeypatch.setattr(workspaces, "remove", boom)


def test_a_second_verdict_or_merge_of_a_merged_branch_runs_nothing(db, approved, monkeypatch):
    ws, runs = approved
    keep_worktree(monkeypatch)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    assert sum("Merged feat" in b for b in drain(db)) == 1
    assert pipeline.on_review(db, db.get_agent("rev1"), ws, True, "lgtm") is True
    assert drain(db) == []
    assert pipeline.merge(db, db.get_agent("boss"), ws).startswith("Already merged")
    assert len(runs) == 1


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


def test_submitting_the_same_review_twice_records_it_once(db, approved, monkeypatch):
    ws, runs = approved
    keep_worktree(monkeypatch)
    assert "brindle takes it from here" in agents.submit_review(db, "rev0", True, "lgtm")
    merged = [b for b in drain(db) if "Merged feat" in b]
    assert len(merged) == 1
    reviews = db.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert "already" in agents.submit_review(db, "rev0", True, "lgtm")
    assert drain(db) == [] and len(runs) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == reviews


@pytest.mark.variant("live incident: a reviewer's submit_review retry closed it mid-merge, so queued workers never started")
def test_a_slow_merge_neither_holds_up_the_reviewer_nor_is_cut_off_by_its_retry(db, approved, monkeypatch):
    """The merge after an approval runs the repo's checks, which can take
    minutes. submit_review returns at once (a reviewer left waiting calls it
    again), the merge runs apart from the reviewer, and a repeated call while
    it runs doesn't close the reviewer under it: the verdict's own run does,
    once the merge is done."""
    ws, runs = approved
    in_gates, release = threading.Event(), threading.Event()
    real_run = gates.run

    def slow_run(*a, **k):
        in_gates.set()
        assert release.wait(10)
        return real_run(*a, **k)

    monkeypatch.setattr(gates, "run", slow_run)
    closed, helpers, out = [], [], []
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: closed.append(agent_id))

    def detached(reviewer_id, ws_id):   # a separate process, in production
        helpers.append(threading.Thread(target=lambda: agents.finish_review(DB(), reviewer_id, ws_id)))
        helpers[-1].start()

    monkeypatch.setattr(agents, "finish_review_later", detached, raising=False)
    first = threading.Thread(target=lambda: out.append(agents.submit_review(DB(), "rev0", True, "lgtm")))
    first.start()
    try:
        first.join(5)
        assert out and in_gates.wait(10)   # answered while the merge waits on the checks
        assert "already" in agents.submit_review(db, "rev0", True, "lgtm")
        assert closed == []
    finally:
        release.set()
        first.join(20)
        for t in helpers:
            t.join(20)
    assert any("Merged feat into main" in b for b in drain(db))
    assert closed == ["rev0"] and len(runs) == 1


@pytest.mark.variant("regression for #172: main blocks the verdict on the merge lock")
def test_a_verdict_is_recorded_at_once_while_a_merge_holds_the_branch(db, approved, monkeypatch):
    """#172: a merge holds the branch's lock while the repo's checks run. A
    reviewer's submit_review waited on it, so the call hung and the verdict
    was never recorded; it is recorded at once, and acted on apart from it."""
    ws, runs = approved
    db.conn.execute("DELETE FROM reviews WHERE reviewer_id = 'rev1'")
    acted = []
    monkeypatch.setattr(agents, "finish_review_later", lambda rid, ws_id: acted.append(rid))
    with pipeline.merge_lock(ws.id):   # held by a merge, in another process in production
        t = threading.Thread(target=lambda: agents.submit_review(DB(), "rev1", False, "fix it"))
        t.start()
        t.join(5)
        answered = not t.is_alive()
        rejected = [r.reviewer_id for r in db.reviews_at(ws.id, gates.head(ws)) if not r.approved]
    t.join(5)
    assert answered and rejected == ["rev1"] and acted == ["rev1"]


def test_a_second_reviewer_submitting_after_the_removal_is_told_it_is_done(db, approved):
    ws, runs = approved
    agents.submit_review(db, "rev0", True, "lgtm")
    assert db.get_workspace(ws.id) is None
    drain(db)
    out = agents.submit_review(db, "rev1", True, "lgtm")
    # The workspace row is gone, so submit_review can't tell it from a non-reviewer.
    assert out.startswith("Only a reviewer")
    assert drain(db) == [] and len(runs) == 1


@pytest.mark.parametrize("in_use", ["new turn", "unread message", "uncommitted edits"])
def test_a_merge_leaves_a_worker_still_at_work_and_its_worktree_alone(db, approved, monkeypatch, in_use):
    """#170: report_result reports a result, not that the worker is done.
    One that took on more since then keeps its worktree and its process."""
    ws, runs = approved
    stopped = []
    monkeypatch.setattr(agents, "_stop", lambda db_, a: stopped.append(a.id))
    if in_use == "new turn":   # the supervisor's next round arrived and it started on it
        db.update_agent("w1", reviewing_since=time.time() - 60)
    elif in_use == "unread message":
        db.enqueue("w1", "round 3: ...", "boss")
    else:
        real_merge_back = workspaces.merge_back

        def edits_meanwhile(db_, ws_, squash=False):
            target = real_merge_back(db_, ws_, squash=squash)
            (Path(ws_.path) / "wip.py").write_text("z = 3\n")
            return target

        monkeypatch.setattr(workspaces, "merge_back", edits_meanwhile)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    assert any("Merged feat into main" in b for b in drain(db))
    assert db.get_workspace(ws.id) is not None and db.get_agent("w1") is not None
    assert os.path.isdir(ws.path) and "w1" not in stopped


@pytest.mark.variant("safety: an approval never merges into the default branch on its own")
def test_default_branch_is_not_merged_into_without_opt_in(db, repo, approved):
    ws, runs = approved
    (repo / ".brindle" / "config.json").write_text(json.dumps({"review": True}))
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    assert "new.py" not in sh("git ls-tree --name-only HEAD", repo)
    body = db.pop_pending("boss").body
    assert "needs you" in body and "merge_workspace" in body
    assert db.get_workspace(ws.id) is not None and db.get_agent("w1").pipeline is None


@pytest.mark.variant("the only test asserting the merge_into setting")
def test_merge_into_puts_workers_on_that_branch(db, repo, monkeypatch):
    sh("git branch integration", repo)
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"merge_into": "integration"}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        agents.delegate(db, db.get_agent("boss"), root, "developer", "do it", "assign")
    [ws] = [w for w in db.find_workspaces() if w.kind == "worktree"]
    assert ws.base_branch == "integration"
