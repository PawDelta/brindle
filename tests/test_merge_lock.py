"""Merges into one checkout are serialized by a file lock, and a transient
index-lock failure is retried instead of being reported."""

import os
import subprocess
import threading
import time

import pytest

from copse import git, workspaces


def _feature(db, repo, name="feature", fname="new.py"):
    ws = workspaces.create(db, str(repo), name).workspace
    open(os.path.join(ws.path, fname), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")
    return ws


def test_checkout_lock_serializes_holders(repo):
    events = []

    def hold(tag):
        with git.checkout_lock(repo):
            events.append(f"in-{tag}")
            time.sleep(0.15)
            events.append(f"out-{tag}")

    threads = [threading.Thread(target=hold, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # never interleaved: every "in" is immediately followed by its own "out"
    for i in range(0, len(events), 2):
        assert events[i].startswith("in-")
        assert events[i + 1] == events[i].replace("in-", "out-")


def test_transient_index_failure_is_retried(db, repo, monkeypatch):
    ws = _feature(db, repo)
    monkeypatch.setattr(git, "RETRY_DELAYS", (0, 0, 0))
    real_run = git.run
    calls = {"n": 0}

    def flaky(args, cwd, check=True):
        if args[:1] == ["merge"] and args[1:2] == ["--no-ff"]:
            calls["n"] += 1
            if calls["n"] < 3:
                return subprocess.CompletedProcess(args, 128, "", "error: Unable to write index.")
        return real_run(args, cwd, check)

    monkeypatch.setattr(git, "run", flaky)
    target = workspaces.merge_back(db, ws)

    assert calls["n"] == 3
    assert (repo / "new.py").read_text() == "x = 1\n"
    assert target == str(repo)


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


def test_non_transient_failure_is_not_retried(db, repo, monkeypatch):
    ws = _feature(db, repo)
    real_run = git.run
    calls = {"n": 0}

    def bad(args, cwd, check=True):
        if args[:2] == ["merge", "--no-ff"]:
            calls["n"] += 1
            return subprocess.CompletedProcess(args, 1, "", "CONFLICT (content)")
        return real_run(args, cwd, check)

    monkeypatch.setattr(git, "run", bad)
    with pytest.raises(git.GitError, match="CONFLICT"):
        workspaces.merge_back(db, ws)
    assert calls["n"] == 1


def test_uncommitted_changes_in_target_survive_a_retried_merge(db, repo, monkeypatch):
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
