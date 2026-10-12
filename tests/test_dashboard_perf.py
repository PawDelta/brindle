import os
import shutil
import time
from pathlib import Path

import pytest

from brindle import agents, git, status_cache, tmux, view, workspaces

from conftest import sh


def make_workspaces(db, repo, names):
    return [workspaces.create(db, str(repo), name).workspace for name in names]


def count_calls(monkeypatch, target, name):
    calls = []
    orig = getattr(target, name)

    def counting(*args, **kwargs):
        calls.append(args)
        return orig(*args, **kwargs)

    monkeypatch.setattr(target, name, counting)
    return calls


# -- parity with the old git.status for several scenarios -------------------


def test_status_matches_old_status_when_behind(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]

    other = repo.parent / "other"
    sh(f"git clone -q {repo.parent / 'origin.git'} {other}", repo.parent)
    (other / "b.txt").write_text("b")
    sh("git add -A && git commit -qm upstream && git push -q", other)
    sh("git fetch -q origin main", ws.path)

    old = git.status(ws.path, "main")
    status_cache.clear()
    new = status_cache.cached_status(ws.path, "main")
    assert (new.ahead, new.behind) == (old.ahead, old.behind) == (0, 1)


def test_status_matches_old_status_for_missing_base(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]

    with pytest.raises(git.GitError):
        git.status(ws.path, "ghost")
    status_cache.clear()
    with pytest.raises(git.GitError):
        status_cache.cached_status(ws.path, "ghost")


def test_base_ref_recomputed_when_packed_refs_change(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]
    wt_gitdir = status_cache._worktree_gitdir(Path(ws.path))
    common = status_cache._common_gitdir(wt_gitdir)

    other = repo.parent / "other"
    sh(f"git clone -q {repo.parent / 'origin.git'} {other}", repo.parent)
    (other / "b.txt").write_text("b")
    sh("git add -A && git commit -qm upstream && git push -q", other)
    sh("git fetch -q origin main", ws.path)

    first = status_cache._cached_base_ref(Path(ws.path), "main", common)
    assert first == "origin/main"

    sh("git pull -q origin main", repo)
    sh("git pack-refs --all", repo)  # packs refs/heads/main away into packed-refs

    second = status_cache._cached_base_ref(Path(ws.path), "main", common)
    assert second == "main"


def test_soft_reset_is_caught_by_logs_head_in_fingerprint(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]
    with open(os.path.join(ws.path, "f.txt"), "w") as f:
        f.write("f")
    git.commit_all(ws.path, "feature work")

    first = status_cache.cached_status(ws.path, "main")
    assert first.ahead == 1

    # Undoes the local commit while leaving the index, HEAD, the base ref and
    # the worktree root all untouched -- only logs/HEAD records it.
    sh("git reset --soft HEAD~1", Path(ws.path))

    second = status_cache.cached_status(ws.path, "main")
    assert second.ahead == 0


def test_list_panes_empty_when_tmux_not_installed(monkeypatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)
    assert tmux.list_panes() == {}


# -- benchmark: subprocess counts for 3 workspaces x 2 agents ----------------


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_snapshot_subprocess_counts_cold_and_warm(db, repo, monkeypatch, capsys):
    wss = make_workspaces(db, repo, ["one", "two", "three"])
    spawned = []
    try:
        for ws in wss:
            for _ in range(2):
                spawned.append(agents.spawn(db, ws, "developer", provider_name="shell"))

        deadline = time.time() + 5
        while time.time() < deadline and not all(agents.is_alive(a) for a in spawned):
            time.sleep(0.1)
        assert all(agents.is_alive(a) for a in spawned)

        git_calls = count_calls(monkeypatch, git, "run")
        tmux_calls = count_calls(monkeypatch, tmux, "_tmux")
        view.snapshot(db, str(repo))
        cold_git, cold_tmux = len(git_calls), len(tmux_calls)

        git_calls.clear()
        tmux_calls.clear()
        view.snapshot(db, str(repo))
        warm_git, warm_tmux = len(git_calls), len(tmux_calls)

        print(f"\ndashboard perf: cold git={cold_git} tmux={cold_tmux}; "
              f"warm git={warm_git} tmux={warm_tmux}")

        assert warm_git == 0
        assert cold_git > warm_git
        assert warm_tmux == 1  # one shared list-panes call per snapshot, even warm
    finally:
        for ws in wss:
            tmux.kill_session(ws.tmux_session)
