"""The app's first snapshot after the daemon starts answers from what is known:
no git status per workspace on the request path (with recovery competing for
the daemon, that took 30 s), counts stored by the last daemon while their
commits still stand, and the rest counted afterwards and published."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from brindle import bus, git, status_cache, tmux, view
from brindle.daemon import routes_read
from brindle.db import Agent, Workspace

MAIN, OLD, NEW = "a" * 40, "b" * 40, "c" * 40
UNKNOWN = {"ahead": None, "behind": None, "dirty": None}


def _worktree(tmp_path, db, name, head):
    """A worktree whose git files say HEAD is ``feat-<name>`` at ``head`` and
    main is at MAIN (plain files: nothing here can run git), with one worker
    that has reported."""
    path = tmp_path / name
    (path / ".git" / "refs" / "heads").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text(f"ref: refs/heads/feat-{name}\n")
    (path / ".git" / "refs" / "heads" / f"feat-{name}").write_text(head + "\n")
    (path / ".git" / "refs" / "heads" / "main").write_text(MAIN + "\n")
    ws = Workspace(id=f"ws-{name}", repo_root=str(tmp_path), name=name, kind="worktree",
                   branch=f"feat-{name}", base_branch="main", path=str(path), port_base=None,
                   tmux_session="s", created_at=time.time() - 3600)
    db.add_workspace(ws)
    db.add_agent(Agent(id=f"ag-{name}", workspace_id=ws.id, profile="developer", provider="shell",
                       parent_id=None, mode="assign", status="done", tmux_window="", result="done",
                       created_at=time.time() - 3600))
    return ws


class FakeGit:
    """``git status`` and the counts for every worktree (status_cache's own
    caching runs for real around it): ``status`` is what it reports, each
    look is in ``asked``, and a look waits for ``release`` once ``hold``."""

    def __init__(self):
        self.status = git.Status("feat", "main", 0, 3, ["x"], None)
        self.asked: list[str] = []
        self.hold = False
        self.release = threading.Event()

    def compute(self, path, base, ref, ahead_behind=None):
        self.asked.append(str(path))
        if self.hold:
            self.release.wait()
        status_cache._reported[str(path)] = (None, [])
        return self.status


@pytest.fixture
def fake_git(monkeypatch):
    """A fresh daemon's caches, over fake git."""
    status_cache.clear()
    fake = FakeGit()
    monkeypatch.setattr(status_cache, "_compute_status", fake.compute)
    monkeypatch.setattr(git, "base_ref", lambda path, base: base)
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: tmux.PaneSnapshot(None))
    yield fake
    fake.release.set()
    status_cache.clear()


def _route_snapshot(monkeypatch=None):
    """GET /v1/snapshot's handler; with ``monkeypatch``, its background count
    is held back and returned."""
    later = []
    if monkeypatch is not None:
        monkeypatch.setattr(view, "warm_later", lambda pending: later.append(list(pending)), raising=False)
    request = SimpleNamespace(query_params={}, app=SimpleNamespace(state=SimpleNamespace()))
    rows = routes_read.snapshot(request)
    return {w["id"]: w for w in json.loads(rows.body)}, later


def _counts(row):
    return row and {k: row[k] for k in ("ahead", "behind", "dirty")}


@pytest.mark.parametrize("stored_head, shown", [
    (None, UNKNOWN),    # nothing stored: unknown until counted, never counted on the request path
    (OLD, None),        # stored for these commits, ahead 0: merged, so hidden at once
    (NEW, UNKNOWN),     # HEAD moved since: the stored counts no longer apply
])
def test_cold_snapshot_runs_no_git_and_uses_stored_counts(db, tmp_path, monkeypatch, fake_git, stored_head, shown):
    ws = _worktree(tmp_path, db, "one", OLD)
    if stored_head:
        db.set_git_counts(ws.id, "main", (stored_head, MAIN, None), 0, 3, 1)

    rows, later = _route_snapshot(monkeypatch)

    assert fake_git.asked == []                    # nothing counted on the request path
    assert _counts(rows.get(ws.id)) == shown
    assert [w.id for w, _ in later[0]] == [ws.id]    # counted afterwards


def test_a_count_gone_stale_before_the_first_snapshot_is_shown_and_recounted_later(
        db, tmp_path, monkeypatch, fake_git):
    ws = _worktree(tmp_path, db, "one", OLD)
    fake_git.status = git.Status("feat", "main", 2, 0, [], None)
    status_cache.cached_status(ws.path, "main")     # the events feed's sweep got there first
    (tmp_path / "one" / "new.txt").write_text("edited since\n")    # then the worktree changed
    fake_git.asked.clear()

    rows, later = _route_snapshot(monkeypatch)

    assert fake_git.asked == []
    assert _counts(rows[ws.id]) == {"ahead": 2, "behind": 0, "dirty": 0}
    assert [w.id for w, _ in later[0]] == [ws.id]


@pytest.mark.parametrize("ahead, told", [
    (2, True),      # not what was shown: the app reads again
    (0, False),     # as shown: nothing to read again
])
def test_background_count_stores_counts_the_next_daemon_shows(db, tmp_path, monkeypatch, fake_git, ahead, told):
    ws = _worktree(tmp_path, db, "two", OLD)
    fake_git.status = git.Status("feat", "main", ahead, 3, ["x"], None)
    fake_git.hold = True

    first = view.warm_later([(ws, view.GitCounts(0, 3, 1))])
    again = view.warm_later([(ws, view.GitCounts(0, 3, 1))])    # the inbox's read, meanwhile
    fake_git.release.set()
    first.join()

    assert again is None and fake_git.asked == [ws.path]
    kinds = [e["kind"] for e in bus.tail(db, repo_root=ws.repo_root)]
    assert (view.GIT_KNOWN_EVENT in kinds) is told
    # A restarted daemon: nothing in memory; the route reads the database afresh.
    status_cache.clear()
    fake_git.asked.clear()
    rows, _ = _route_snapshot(monkeypatch)
    assert fake_git.asked == []
    assert _counts(rows.get(ws.id)) == (None if ahead == 0 else {"ahead": 2, "behind": 3, "dirty": 1})
