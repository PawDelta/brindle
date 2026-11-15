"""A merged branch whose base advanced is not "unreported"; a new commit still is."""
import subprocess
import time

import pytest

from brindle import agents, cull, tmux
from brindle.db import Agent, Workspace


def _git(path, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=path,
                          check=True, capture_output=True, text=True).stdout.strip()


def _commit(path, name):
    (path / name).write_text(name)
    _git(path, "add", name)
    _git(path, "commit", "-m", name)
    return _git(path, "rev-parse", "HEAD")


@pytest.mark.variant("catches the false unreported-commits alarm on a merged branch whose base was merged back")
def test_base_merged_back_after_report_is_not_unreported(db, tmp_path, monkeypatch):
    repo = tmp_path / "repo"  # apart from the test database, which lives in tmp_path
    repo.mkdir()
    _git(repo, "init", "-b", "base")
    _commit(repo, "a")
    _git(repo, "checkout", "-b", "work")
    reported = _commit(repo, "w1")
    # The pipeline merges the branch into the base, the base moves on, and
    # the branch takes the base back in.
    _git(repo, "checkout", "base")
    _git(repo, "merge", "--no-ff", "work", "-m", "merge work")
    _commit(repo, "b1")
    _commit(repo, "b2")
    _git(repo, "checkout", "work")
    _git(repo, "merge", "base")

    ws = Workspace("w", str(repo), "work", "worktree", "work", "base", str(repo), None, "fake", 1)
    db.add_workspace(ws)
    now = time.time()
    common = dict(profile="developer", provider="claude", result=None, created_at=now - 7200)
    db.add_agent(Agent("boss", ws.id, parent_id=None, mode="interactive", status="processing",
                       headless=1, tmux_window="%boss", **common))
    db.add_agent(Agent("worker", ws.id, parent_id="boss", mode="assign", status="idle",
                       status_since=1, tmux_window="%worker", **common))
    row = db.outbox_receive("worker", "digest", "done")
    db.outbox_bind(row["id"], reported)
    db.outbox_accept(row["id"], "worker", "done")
    db.outbox_deliver(row["id"], nothing=True)
    panes = {"%worker": True, "%boss": True}
    monkeypatch.setattr(tmux, "window_activity", lambda *a, **k: now - 1000)
    monkeypatch.setattr(agents, "is_alive", lambda *a: True)
    monkeypatch.setattr(agents, "command_running", lambda *a: False)

    cull.note_unreported_commits(db, now, panes)
    assert db.pending_count("boss") == 0

    _commit(repo, "w2")
    cull.note_unreported_commits(db, now, panes)
    assert db.pending_count("boss") == 1
