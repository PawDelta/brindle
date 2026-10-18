"""Merges into one checkout are serialized by a file lock, and a transient
index-lock failure is retried instead of being reported."""

import os
import subprocess
import threading

import pytest

from brindle import gates, git, pipeline, workspaces
from brindle.db import DB


def _feature(db, repo, name="feature", fname="new.py"):
    ws = workspaces.create(db, str(repo), name).workspace
    open(os.path.join(ws.path, fname), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")
    return ws


def test_checkout_lock_gives_up_on_a_hung_holder(repo):
    held, release = threading.Event(), threading.Event()

    def hang():
        with git.checkout_lock(repo):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hang)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(git.GitError, match="may be hung"):
            with git.checkout_lock(repo, timeout=0.3):
                pass
    finally:
        release.set()
        t.join()


def test_persistent_index_failure_is_reported_after_retries(db, repo, monkeypatch):
    ws = _feature(db, repo)
    monkeypatch.setattr(git, "RETRY_DELAYS", (0, 0))
    real_run = git.run
    calls = {"n": 0}

    def always(args, cwd, check=True):
        if args[:2] == ["merge", "--no-ff"]:
            calls["n"] += 1
            return subprocess.CompletedProcess(args, 128, "", "fatal: index.lock exists")
        return real_run(args, cwd, check)

    monkeypatch.setattr(git, "run", always)
    with pytest.raises(git.GitError, match="index.lock"):
        workspaces.merge_back(db, ws)
    assert calls["n"] == 3  # first try + two retries


def test_merge_refuses_a_target_with_uncommitted_changes(db, repo, monkeypatch):
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "readme"], cwd=repo, check=True)
    ws = _feature(db, repo)
    (repo / "README.md").write_text("supervisor edit\n")
    # tracked uncommitted change blocks the merge outright, untouched
    with pytest.raises(git.GitError, match="uncommitted"):
        workspaces.merge_back(db, ws)
    assert (repo / "README.md").read_text() == "supervisor edit\n"


def test_two_concurrent_merges_both_succeed(db, repo):
    a = _feature(db, repo, "feat-a", "a.py")
    b = _feature(db, repo, "feat-b", "b.py")
    errors = []

    def go(ws):
        try:
            workspaces.merge_back(db, ws)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(w,)) for w in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert (repo / "a.py").exists() and (repo / "b.py").exists()


def test_issue197_a_merge_into_a_base_waits_for_another_ones_gates(db, repo, monkeypatch):
    """#197: a supervisor's merge_workspace and the pipeline's auto-merge of
    another branch into the same base ran at once, each in its own process.
    The second waits for the first's whole sync, gates and merge, so neither
    merges into a base that moved under its gates."""
    a = _feature(db, repo, "feat-a", "a.py")
    b = _feature(db, repo, "feat-b", "b.py")
    in_gates, release = threading.Event(), threading.Event()
    real_run = gates.run

    def slow_run(db_, ws, cfg, review_required):
        if ws.id == a.id:
            in_gates.set()
            assert release.wait(10)
        return real_run(db_, ws, cfg, review_required=review_required)

    monkeypatch.setattr(gates, "run", slow_run)
    out = {}
    first = threading.Thread(target=lambda: out.update(a=pipeline.merge(DB(), None, a)))
    first.start()
    second = threading.Thread(target=lambda: out.update(b=pipeline.merge(DB(), None, b)))
    try:
        assert in_gates.wait(10)
        second.start()
        second.join(0.5)
        assert second.is_alive() and "b" not in out   # waiting for a's merge
    finally:
        release.set()
        first.join(20)
        if second.ident:
            second.join(20)
    assert out["a"].startswith("Merged feat-a") and out["b"].startswith("Merged feat-b")
