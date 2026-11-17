"""The app's first snapshot after the daemon starts answers from what is known:
no git status per workspace on the request path (with recovery competing for
the daemon, that took 30 s), counts stored by the last daemon while their
commits still stand, and the rest counted afterwards and published."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
        if not (path / ".git" / "refs" / "heads" / ref).exists():
            raise git.GitError(f"fatal: bad revision '{ref}'")
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


@pytest.mark.variant("SQL fanout regression: adding workspaces must not add per-row queries")
def test_snapshot_query_count_is_independent_of_workspace_count(db, tmp_path, fake_git):
    queries = []
    counts = []
    previous = 0
    for total in (1, 8):
        for i in range(previous, total):
            ws = _worktree(tmp_path, db, str(i), OLD)
            db.update_agent(f"ag-{i}", parent_id="ag-0" if i else None)
            db.add_review(ws.id, OLD, None, approved=True, summary="")
        if total == 1:
            db.add_autopilot("ag-0")
            db.update_autopilot("ag-0", state="blocked")
        db.conn.set_trace_callback(queries.append)
        try:
            rows = view.snapshot(db, str(tmp_path), read_screens=False, known_git=view.KnownGit(db))
        finally:
            db.conn.set_trace_callback(None)
        counts.append(len(queries))
        queries.clear()
        assert len(rows) == total
        assert all(row["autopilot"] and row["review"] == "approved" for row in rows)
        assert rows[0]["asking"] == "ag-0"
        assert all(row["asking"] is None for row in rows[1:])
        previous = total
    assert counts[1] <= counts[0] <= 25, counts


@pytest.mark.variant("concurrent inbox and snapshot reads must share a render and invalidate it after events")
def test_inbox_reuses_snapshot_and_refreshes_after_an_event(db, tmp_path, monkeypatch, fake_git):
    ws = _worktree(tmp_path, db, "one", OLD)
    calls = []
    render = view.snapshot

    def snapshot(*args, **kwargs):
        rows = render(*args, **kwargs)
        calls.append(rows)
        return rows

    monkeypatch.setattr(view, "snapshot", snapshot)
    monkeypatch.setattr(view, "warm_later", lambda pending: None)
    clock = [100.0]
    monkeypatch.setattr(routes_read, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    state = SimpleNamespace()
    if hasattr(routes_read, "SnapshotView"):
        state.snapshot_view = routes_read.SnapshotView()
    request = SimpleNamespace(query_params={}, app=SimpleNamespace(state=state))
    ready = threading.Barrier(2)

    def read(route):
        ready.wait()
        return route(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot_response, first = list(pool.map(read, (routes_read.snapshot, routes_read.inbox_)))
    assert snapshot_response.status_code == 200
    assert first.status_code == 200
    assert any(row["agent_id"] == "ag-one" for row in json.loads(first.body))
    assert len(calls) == 1
    db.update_agent("ag-one", dismissed_at=time.time())
    bus.publish_ws(db, "agent.closed", ws, agent_id="ag-one")
    assert not any(row.get("agent_id") == "ag-one" for row in json.loads(routes_read.inbox_(request).body))
    assert len(calls) == 2
    clock[0] += 1
    routes_read.snapshot(request)
    assert len(calls) == 3
    request.query_params = {"repo": "another-repo"}
    assert json.loads(routes_read.snapshot(request).body) == []
    assert len(calls) == 4


@pytest.mark.parametrize("stored_head, shown", [
    pytest.param(None, UNKNOWN, marks=pytest.mark.variant(
        "a daemon with no stored counts must defer git and show unknown counts")),
    pytest.param(OLD, None, marks=pytest.mark.variant(
        "stored zero-ahead counts for unchanged commits must hide a merged workspace immediately")),
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


@pytest.mark.variant("an edit after the events feed counts must preserve the last count and schedule a refresh")
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


def test_a_deleted_base_shows_unknown_once_counted_and_stops_recounting(db, tmp_path, fake_git):
    ws = _worktree(tmp_path, db, "one", OLD)
    fake_git.status = git.Status("feat", "main", 2, 0, [], None)
    status_cache.cached_status(ws.path, "main")                  # counted while main existed
    (tmp_path / "one" / ".git" / "refs" / "heads" / "main").unlink()   # then the base branch went

    first = view.KnownGit(db)
    assert first(ws) == view.GitCounts(2, 0, 0) and first.pending   # the last count, recounted later
    view.warm_later(first.pending).join()
    events = len(bus.tail(db, repo_root=ws.repo_root))

    again = view.KnownGit(db)
    assert again(ws) == view.GitCounts() and again.pending == []    # the failure stands: nothing to redo
    assert view.warm_later(again.pending) is None and len(bus.tail(db, repo_root=ws.repo_root)) == events


@pytest.mark.parametrize("ahead, told", [
    pytest.param(2, True, marks=pytest.mark.variant(
        "changed background counts must notify the app and survive a daemon restart")),
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
