"""Merges into one checkout are serialized by a file lock, and a transient
index-lock failure is retried instead of being reported."""

import subprocess
import threading

import pytest

from brindle import git, spawn, workspaces
from test_merge_order import _repo_template, repo, worktree  # noqa: F401 - fixture


def _feature(db, repo, name="feature", fname="new.py"):
    return worktree(db, repo, name, files={fname: "x = 1\n"})


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




@pytest.mark.variant("race: two merges into one checkout at once, with a git read outlasting spawn's first poll")
def test_two_concurrent_merges_both_succeed(db, repo, monkeypatch):
    a = _feature(db, repo, "feat-a", "a.py")
    b = _feature(db, repo, "feat-b", "b.py")
    errors = []
    # On a busy machine a git read outlasts spawn's first poll; it polls again
    # and the merge carries on. Every read here outlasts one poll.
    real_wait, polled_again = spawn._wait_read, []

    def outlasts_first_poll(p, argv, input, *a, **k):
        if input is None:
            real = p.communicate

            def first_poll(input=None, timeout=None):
                p.communicate = real
                polled_again.append(argv)
                raise subprocess.TimeoutExpired(argv, timeout)

            p.communicate = first_poll
        return real_wait(p, argv, input, *a, **k)

    monkeypatch.setattr(spawn, "_wait_read", outlasts_first_poll)

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
    assert any(args[0] == "git" for args in polled_again)


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
