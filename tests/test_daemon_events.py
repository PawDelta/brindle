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


def _readiness_reuses_unchanged_sessions(db, tmp_path, monkeypatch, gitlink, outcome="none"):
    import time
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
    monkeypatch.setattr(review.session_sync, "refresh_drift_flags", lambda db: None)
    monkeypatch.setattr(review.session_sync, "refresh_idle_sessions", lambda db, fetched_at: None)
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
    # GitHub's look is held: first pending, then failed (unknown, not "none"), and only then "none open".
    monkeypatch.setattr(review._pr_pool, "submit", lambda *a: SimpleNamespace(done=lambda: False))
    review._head_cache.clear()
    key = ("a" * 40, str(tmp_path))
    assert watch.sweep(db) == ["s"]
    assert bus.tail(db, kinds=("session.pr_ready",)) == []     # not announced on a guess
    review._head_cache[key] = (time.monotonic(), None)
    watch.sweep(db)
    assert bus.tail(db, kinds=("session.pr_ready",)) == []
    if outcome == "open":
        # The lookup finishes between the helper starting it and reading the answer: a supervisor's PR is there.
        def finishes_meanwhile(sha, cwd, root_id=None):
            review._head_cache[key] = (time.monotonic(), {"url": "https://github.com/o/r/pull/8", "number": 8})
        monkeypatch.setattr(review, "open_pr_for_head", finishes_meanwhile)
    else:
        if outcome == "none":
            db.conn.execute("INSERT INTO session_prs (root_id, branch, url, opened_at) VALUES ('s', 's', 'https://github.com/o/r/pull/7', 0)")
        review._head_cache[key] = (time.monotonic(), {})
    watch.sweep(db)
    said = [e["agent_id"] for e in bus.tail(db, kinds=("session.pr_ready",))]
    assert said == ([] if outcome == "open" else ["s"])   # said once the look answered "none", never for an open PR
    reads.clear()
    monkeypatch.setattr(spawn, "room", lambda: 0)
    monkeypatch.setattr(review, "_merge_polled", {})
    watch.sweep(db)
    assert reads == [] and watch.ready == {"s": True} and review._merge_polled == {}


@pytest.mark.parametrize("recorded, expected", [
    pytest.param(False, None, marks=pytest.mark.variant("review finding: a missing gh read as confirmed absence, so no PR")),
    pytest.param(True, True, marks=pytest.mark.variant("review finding: a missing gh dropped the PR already recorded")),
])
def test_missing_gh_is_not_confirmed_absence(db, monkeypatch, recorded, expected, real_github_lookups):
    from types import SimpleNamespace
    from brindle import status_cache, workspaces
    from brindle.daemon import session_review as review
    monkeypatch.setattr(review, "session", lambda db, root: (None, SimpleNamespace(path="/p")))
    monkeypatch.setattr(status_cache, "read_head", lambda path: "h")
    monkeypatch.setattr(workspaces.shutil, "which", lambda *a: None)
    monkeypatch.setattr(review._pr_pool, "submit", lambda fn, *a: fn(*a) or SimpleNamespace(done=lambda: True))
    review._head_cache.clear()
    if recorded:
        db.conn.execute("INSERT INTO session_prs (root_id, branch, url, opened_at) VALUES ('s', 'b', 'https://x/pull/1', 0)")
    assert review._pr_open_state(db, "s") is expected    # not "none open": nothing was asked of GitHub


def _readiness_slow_gh_is_parallel_and_bounded(db, monkeypatch, caplog):
    from concurrent.futures import Future
    from contextvars import Context
    from types import SimpleNamespace
    from brindle import spawn, workspaces
    from brindle.daemon import session_review as review
    _open_readiness_prs(db, 2)
    started, scheduled, results = [], [], []
    def submit(check, *args):
        future = Future()
        future.set_running_or_notify_cancel()
        scheduled.append((future, check, args))
        return future
    def end_round(pending, **kwargs):
        # All reads must be submitted before waiting. Keep their futures running
        # across sweeps, without depending on shared threads or wall-clock time.
        assert len(pending) == 2 and all(f.running() for f in pending)
        if not started:
            for _, check, args in scheduled:
                # Executor threads start without the caller's deadline context.
                results.append(Context().run(check, *args))
    # Only the merged-PR polling is under test: the checks refresh has its own pool.
    monkeypatch.setattr(review, "refresh_pr_states", lambda db: None)
    monkeypatch.setattr(review, "_merge_pool", SimpleNamespace(submit=submit))
    monkeypatch.setattr(review, "_merge_checks", {})
    monkeypatch.setattr(review, "_merge_order", {})
    monkeypatch.setattr(review, "wait", end_round)
    # Other sweep steps touch the fake /repo outside the merge-check deadline.
    # In particular, fetch notices resolve its git directory through spawn.read.
    monkeypatch.setattr(review.session_sync, "refresh_drift_flags", lambda *a, **kw: None)
    monkeypatch.setattr(review.session_sync, "refresh_idle_sessions", lambda *a, **kw: None)
    monkeypatch.setattr(review.session_sync, "tell_refused_fetches", lambda db: None)
    def slow(*args, **kwargs):
        assert spawn.current() is not None
        started.append(args)
        raise spawn.Busy("read stopped")
    monkeypatch.setattr(spawn, "read", slow)
    monkeypatch.setattr(workspaces.shutil, "which", lambda *a: "gh")
    monkeypatch.setattr(spawn, "room", lambda: 2)
    monkeypatch.setattr(review, "_merge_polled", {})
    review.ReadinessWatch().sweep(db)
    assert len(started) == 2
    monkeypatch.setattr(review, "_merge_polled", {})
    review.ReadinessWatch().sweep(db)
    assert len(scheduled) == len(started) == 2
    # Finish every controlled read before patches and log capture end.
    for (future, _, _), result in zip(scheduled, results, strict=True):
        future.set_result(result)
    for future in review._merge_checks.values():
        assert future.result() is False
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.variant("a race from a real incident: the pool thread read gh with no slot (main CI run 42855607818)")
def test_slow_gh_read_has_its_slot(monkeypatch):
    """The checks refresh reads on a pool thread, which copies no context: the
    read still runs under a deadline (a slot), never with none."""
    from brindle import spawn, workspaces
    from brindle.daemon import session_review as review
    url = "https://github.com/o/r/pull/9"
    seen = []
    monkeypatch.setattr(review, "_pr_cache", {})
    monkeypatch.setattr(workspaces, "pull_request_checks", lambda *a: seen.append(spawn.current()))
    review._pr_pool.submit(review._read_pr_state, url, "/repo").result()
    assert seen and seen[0] is not None


def test_a_background_pr_lookup_never_outlives_its_test(tmp_path, monkeypatch):
    """The Inbox's and goal's look for a PR on a session's HEAD runs on a pool,
    past the read that asked. In a test it must not ask the machine's gh: that
    read GitHub over the network, outlived the test and left reads counted in
    spawn.running() for the next one (the flaky test_spawn rows)."""
    import os
    from brindle import spawn
    from brindle.daemon import session_review as review
    asked = tmp_path / "gh-asked"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(f"#!/bin/sh\ntouch {asked}\nexec tail -f /dev/null\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")
    flights, submit = [], review._pr_pool.submit
    monkeypatch.setattr(review._pr_pool, "submit", lambda fn, *args: flights.append(submit(fn, *args)) or flights[-1])
    key = ("a" * 40, str(tmp_path))
    review.open_pr_for_head(*key)
    flights[0].result()     # on the base, the hanging gh until its read's deadline stops it
    assert not asked.exists() and spawn.running() == 0


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
        monkeypatch.setattr(review, "_merge_polled", {})
        review.poll_merged(db)
    assert {url.rsplit("/", 1)[1] for url in seen} == {str(n) for n in range(5)}


def _merge_poll_soon_after_boot(db, monkeypatch):
    """A daemon that has never polled this database's PRs polls on its first
    round, even when the clock (time since boot, on Linux) is younger than
    MERGE_POLL_SECONDS: a fresh CI VM skipped it (run 44135119625)."""
    import time
    from concurrent.futures import Future
    from types import SimpleNamespace
    from brindle.daemon import session_review as review
    _open_readiness_prs(db, 1)
    asked = []
    monkeypatch.setattr(review, "time", SimpleNamespace(monotonic=lambda: 30.0, time=time.time))
    monkeypatch.setattr(review._merge_pool, "submit", lambda check, url, cwd: asked.append(url) or Future())
    monkeypatch.setattr(review, "wait", lambda *a, **kw: None)
    monkeypatch.setattr(review, "_merge_checks", {})
    monkeypatch.setattr(review, "_merge_order", {})
    review.poll_merged(db)
    assert asked == ["https://github.com/o/r/pull/0"]


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
    pytest.param(_readiness_reuses_unchanged_sessions, {"gitlink": False, "outcome": "none"}, id="readiness-reuses",
                 marks=pytest.mark.variant("an unchanged tracked file is not re-read: a sweep runs no git at all")),
    pytest.param(_readiness_reuses_unchanged_sessions, {"gitlink": False, "outcome": "open"}, id="readiness-pr-open",
                 marks=pytest.mark.variant("a lookup completing mid-check with a supervisor's PR must not announce the PR as ready")),
    pytest.param(_readiness_slow_gh_is_parallel_and_bounded, {}, id="readiness-slow-gh"),
    pytest.param(_readiness_deferred_prs_get_a_turn, {"queued": "expired"}, id="readiness-deferred-expired",
                 marks=pytest.mark.variant("a queued check starts after deadline expiry before the sweep can cancel it")),
    pytest.param(_merge_poll_soon_after_boot, {}, id="merge-poll-after-boot",
                 marks=pytest.mark.variant("a first poll within a minute of boot was skipped (CI run 44135119625)")),
    pytest.param(_session_changed_tracks_head_and_index_without_status_changes, {"memory": False},
                 id="session_changed-file-db",
                 marks=pytest.mark.variant("two daemons on one database file each announce a change exactly once")),
    pytest.param(_status_fingerprint, {}, id="status_fingerprint",
                 marks=pytest.mark.variant("a cached git status is reused until a file's size or the tree changes")),
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


@pytest.mark.parametrize("since,first", [(1, ["event", "event"])])
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
