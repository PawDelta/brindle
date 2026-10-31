import pytest
from conftest import run_scenario
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brindle import bus
from brindle.daemon.app import build_app
from brindle.daemon.auth import WS_UNAUTHORIZED

TOKEN = "t" * 43


def _open_readiness_prs(db, count):
    from brindle.db import Agent, Workspace
    for n in range(count):
        key = str(n)
        db.add_workspace(Workspace(key, "/repo", key, "worktree", key, "main", "/repo", None, key, 0))
        db.add_agent(Agent(key, key, "supervisor", "claude", None, "interactive", "idle", "", None, 0))
        db.conn.execute("INSERT INTO session_prs (root_id, branch, url, opened_at) VALUES (?, ?, ?, 0)",
                        (key, key, f"https://github.com/o/r/pull/{n}"))


def _readiness_reuses_unchanged_sessions(db, tmp_path, monkeypatch, gitlink):
    from types import SimpleNamespace
    from brindle import git, spawn
    from brindle.daemon import routes_actions, session_review as review
    from brindle.db import Agent, Workspace
    private = tmp_path / ".git"
    (private / "refs/heads").mkdir(parents=True)
    (private / "HEAD").write_text("ref: refs/heads/s")
    (private / "refs/heads/s").write_text("a" * 40)
    (private / "refs/heads/main").write_text("b" * 40)
    tracked = tmp_path / "src" / "file.py"
    tracked.parent.mkdir()
    tracked.write_text("original")
    db.add_workspace(Workspace("w", str(tmp_path), "s", "worktree", "s", "main", str(tmp_path), None, "s", 0))
    db.add_agent(Agent("s", "w", "supervisor", "claude", None, "interactive", "idle", "", None, 0))
    db.add_autopilot("s")
    db.update_autopilot("s", state="done")
    reads = []
    mode, name = ("160000", "src") if gitlink else ("100644", "src/file.py")
    def git_read(args, *a, **kw):
        reads.append(args)
        files = f"{mode} {'d' * 40} 0\t{name}\0" if "--stage" in args else f"{name}\0"
        return SimpleNamespace(stdout=files if args[0] == "ls-files" else "s", returncode=0)
    monkeypatch.setattr(spawn, "room", lambda: 1)
    monkeypatch.setattr(git, "run", git_read)
    monkeypatch.setattr(git, "dirty_files", lambda *a, **kw: reads.append("dirty") or (
        [] if tracked.read_text() == "original" else ["src/file.py"]))
    monkeypatch.setattr(git, "base_ref", lambda *a: "main")
    monkeypatch.setattr(git, "ahead_behind", lambda *a: (0, 1))
    monkeypatch.setattr(routes_actions, "prepare_pr", lambda *a: None)
    watch = review.ReadinessWatch()
    watch.sweep(db)
    reads.clear()
    watch.sweep(db)
    if not gitlink:
        assert reads == []
    tracked.write_text("unstaged")
    watch.sweep(db)
    assert watch.ready == {"s": False}
    tracked.write_text("original")
    assert watch.sweep(db) == ["s"]
    reads.clear()
    monkeypatch.setattr(spawn, "room", lambda: 0)
    monkeypatch.setattr(review, "_merge_polled", 0)
    watch.sweep(db)
    assert reads == [] and watch.ready == {"s": True} and review._merge_polled == 0


def _readiness_slow_gh_is_parallel_and_bounded(db, monkeypatch):
    import threading
    from brindle import spawn, workspaces
    from brindle.daemon import session_review as review
    from brindle.db import DB
    _open_readiness_prs(db, 2)
    release, both, finished = threading.Event(), threading.Event(), threading.Event()
    started, lock = [], threading.Lock()
    def slow(*args, **kwargs):
        assert spawn.current() is not None
        with lock:
            started.append(args)
            if len(started) == 2:
                both.set()
        release.wait()
        raise spawn.Busy("read stopped")
    monkeypatch.setattr(spawn, "read", slow)
    monkeypatch.setattr(workspaces.shutil, "which", lambda *a: "gh")
    monkeypatch.setattr(spawn, "room", lambda: 2)
    monkeypatch.setattr(review, "_merge_polled", 0)
    monkeypatch.setattr(review, "MERGE_CHECK_SECONDS", 0.05, raising=False)
    def sweep():
        connection = DB(db.path)
        try:
            review.ReadinessWatch().sweep(connection)
        finally:
            connection.conn.close()
            finished.set()
    worker = threading.Thread(target=sweep)
    worker.start()
    try:
        assert both.wait(1)
        assert finished.wait(1)
        monkeypatch.setattr(review, "_merge_polled", 0)
        review.ReadinessWatch().sweep(db)
        assert len(started) == 2
    finally:
        release.set()
        worker.join(1)


def _readiness_deferred_prs_get_a_turn(db, monkeypatch, queued):
    from concurrent.futures import Future
    from brindle import spawn, workspaces
    from brindle.daemon import session_review as review
    _open_readiness_prs(db, 5)
    seen, scheduled = [], []
    def submit(check, url, cwd):
        future = Future()
        if len(scheduled) < 4:
            future.set_running_or_notify_cancel()
            check(url, cwd)
        scheduled.append((future, lambda: check(url, cwd)))
        return future
    def end_round(futures, **kw):
        spawn.current().at = 0  # queued work can be dequeued after the round's window
        for future, run in scheduled:
            if future.running():
                future.set_result(False)
            elif queued == "expired":
                future.set_running_or_notify_cancel()
                future.set_result(run())
            else:
                future.cancel()
        scheduled.clear()
    def read(argv, **kw):
        if spawn.current().left() <= 0:
            raise spawn.Busy("deadline passed before gh started")
        seen.append(argv[3])
        return workspaces.subprocess.CompletedProcess(argv, 0, "OPEN", "")
    monkeypatch.setattr(spawn, "read", read)
    monkeypatch.setattr(workspaces.shutil, "which", lambda *a: "gh")
    monkeypatch.setattr(review._merge_pool, "submit", submit)
    monkeypatch.setattr(review, "wait", end_round)
    monkeypatch.setattr(review, "_merge_checks", {})
    monkeypatch.setattr(review, "_merge_order", {})
    for _ in range(2):
        monkeypatch.setattr(review, "_merge_polled", 0)
        review.poll_merged(db)
    assert {url.rsplit("/", 1)[1] for url in seen} == {str(n) for n in range(5)}


def _session_changed_tracks_head_and_index_without_status_changes(db, tmp_path, memory):
    from brindle import git
    from brindle.daemon.events import Feed
    from brindle.db import Agent, DB, Workspace

    if memory:
        db = DB(":memory:")
    private = tmp_path / ".git"
    private.mkdir()
    head = private / "HEAD"
    head.write_text("a" * 40)
    index = private / "index"
    index.write_bytes(b"index")
    db.add_workspace(Workspace("w", "/repo", "s", "worktree", "s", "main", str(tmp_path), None, "s", 0))
    db.add_agent(Agent("s", "w", "supervisor", "claude", None, "interactive", "paused", "", None, 0))
    feed = Feed(db, panes=lambda: [], git_status=lambda *_: git.Status("s", "main", 1, 0, [], 0))
    other_db = db if memory else DB(db.path)
    other = Feed(other_db, panes=lambda: [], git_status=lambda *_: git.Status("s", "main", 1, 0, [], 0))
    feed.tick(0)
    other.tick(0)
    assert not bus.tail(db, kinds=("session.changed",))
    for n, update in enumerate((lambda: head.write_text("b" * 40), lambda: index.write_bytes(b"new index"), index.unlink), 1):
        update()
        first = feed.tick(n * 5)
        second = other.tick(n * 5)
        events = [m for m in first + feed.tick(n * 5 + 1) if m.get("kind") == "session.changed"]
        received = [m for m in second + other.tick(n * 5 + 1) if m.get("kind") == "session.changed"]
        assert len(events) == len(received) == 1
        assert events == received
        assert events[0]["root_id"] == "s"
        assert events[0]["workspace_id"] == "w"
        assert len(bus.tail(db, kinds=("session.changed",))) == n
        assert not [m for m in feed.tick(n * 5 + 2) if m.get("kind") == "session.changed"]
    other_db.conn.close()


def _status_fingerprint(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from brindle import git, status_cache

    (tmp_path / ".git").mkdir()
    source = tmp_path / "main.py"
    source.write_text("original")
    nested = tmp_path / "src"
    nested.mkdir()
    calls = []
    monkeypatch.setattr(git, "base_ref", lambda *a: "main")
    monkeypatch.setattr(git, "ahead_behind", lambda *a: (0, 0))
    monkeypatch.setattr(git, "run", lambda *a: (calls.append(a), SimpleNamespace(stdout="## main\n"))[1])
    status_cache.clear()
    try:
        status_cache.cached_status(tmp_path, "main", now=0)
        calls.clear()
        for now in (5, 10, 15, 30):
            status_cache.cached_status(tmp_path, "main", now=now)
        assert calls == []
        source.write_text("modified and longer")
        status_cache.cached_status(tmp_path, "main", now=31)
        status_cache.cached_status(tmp_path, "main", now=32)
        assert len(calls) == 1
        (nested / "new.py").write_text("new")
        status_cache.cached_status(tmp_path, "main", now=33)
        assert len(calls) == 2
        status_cache.cached_status(tmp_path, "main", now=1000)
        assert len(calls) == 3
    finally:
        status_cache.clear()


@pytest.mark.parametrize("scenario, params", [
    pytest.param(_readiness_reuses_unchanged_sessions, {"gitlink": False}, id="readiness-reuses"),
    pytest.param(_readiness_reuses_unchanged_sessions, {"gitlink": True}, id="readiness-reuses-gitlink"),
    pytest.param(_readiness_slow_gh_is_parallel_and_bounded, {}, id="readiness-slow-gh"),
    pytest.param(_readiness_deferred_prs_get_a_turn, {"queued": "cancelled"}, id="readiness-deferred-cancelled"),
    pytest.param(_readiness_deferred_prs_get_a_turn, {"queued": "expired"}, id="readiness-deferred-expired",
                 marks=pytest.mark.variant("a queued check starts after deadline expiry before the sweep can cancel it")),
    pytest.param(_session_changed_tracks_head_and_index_without_status_changes, {"memory": False},
                 id="session_changed-file-db"),
    pytest.param(_session_changed_tracks_head_and_index_without_status_changes, {"memory": True},
                 id="session_changed-memory-db"),
    pytest.param(_status_fingerprint, {}, id="status_fingerprint"),
])
def test_readiness_and_status_fingerprint(request, scenario, params):
    run_scenario(request, scenario, params)


@pytest.mark.variant("a wrong first-frame token must close the socket with WS_UNAUTHORIZED")
def test_missing_token_is_rejected(db):
    with TestClient(build_app(TOKEN)) as client, \
            client.websocket_connect("/v1/events") as ws:
        ws.send_text("wrong")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == WS_UNAUTHORIZED


@pytest.mark.parametrize("since,first", [(1, ["event", "event"]), (99, ["resnapshot"])])
def test_since_replays_then_tails(db, since, first):
    for n in range(3):
        bus.publish(db, "note", n=n)
    with TestClient(build_app(TOKEN)) as client, \
            client.websocket_connect(f"/v1/events?since={since}") as ws:
        ws.send_text(TOKEN)
        got = [ws.receive_json() for _ in first]
        assert [m["type"] for m in got] == first
        if since == 1:
            assert [m["payload"]["n"] for m in got] == [1, 2]
            bus.publish(db, "note", n=3)
            live = next(m for m in iter(ws.receive_json, None) if m["type"] == "event")
            assert live["payload"] == {"n": 3}
