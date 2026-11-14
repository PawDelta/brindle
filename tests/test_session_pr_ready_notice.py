"""A recorded PR must not silently prevent the next PR's Inbox card."""

from concurrent.futures import Future
from contextlib import contextmanager
from types import SimpleNamespace

from brindle import bus, gates, git, spawn, status_cache, workspaces
from brindle.daemon import routes_actions, session_review as review
from brindle.db import Agent, Workspace
from brindle.service import inbox


def test_closed_recorded_pr_does_not_hide_ready_session(db, tmp_path, monkeypatch):
    head = "a" * 40
    old = "b" * 40
    private = tmp_path / ".git"
    (private / "refs/heads").mkdir(parents=True)
    (private / "HEAD").write_text("ref: refs/heads/session")
    (private / "refs/heads/session").write_text(head)
    (private / "refs/heads/main").write_text(old)
    ws = Workspace("home", str(tmp_path), "session", "worktree", "session", "main",
                   str(tmp_path), None, "session", 0)
    db.add_workspace(ws)
    db.add_agent(Agent("root", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, 0))
    db.add_autopilot("root")
    db.set_milestones("root", [("Delivered", None, None, None, None)])
    db.record_check(db.milestones("root")[0].id, True, "", head)
    db.update_autopilot("root", state="done", audit_sha=head, audit_ok=1)
    url = "https://github.com/o/r/pull/451"
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at)"
                    " VALUES ('root', 'session', ?, ?, 1)", (url, old))
    db.conn.execute("INSERT INTO session_pr_declined (root_id, branch, sha) VALUES ('root', 'session', ?)",
                    (f"{old}|{url}",))
    payload = None

    def submit(fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    @contextmanager
    def checked(*args, **kwargs):
        yield head

    monkeypatch.setattr(spawn, "room", lambda: 4)
    monkeypatch.setattr(review.session_sync, "refresh_idle_sessions", lambda *args: None)
    monkeypatch.setattr(review.session_sync, "refresh_drift_flags", lambda *args: None)
    monkeypatch.setattr(review, "poll_merged", lambda db: None)
    monkeypatch.setattr(review, "_pr_cache", {})
    monkeypatch.setattr(review, "_pr_closed", set(), raising=False)
    monkeypatch.setattr(review, "_head_cache", {})
    monkeypatch.setattr(review, "_head_flights", set())
    monkeypatch.setattr(review._pr_pool, "submit", submit)
    monkeypatch.setattr(workspaces, "pull_request_checks", lambda *args: payload)
    monkeypatch.setattr(workspaces, "open_pull_request_for_commit", lambda *args: {})
    monkeypatch.setattr(workspaces, "refresh_branch", lambda db, ws: ws)
    monkeypatch.setattr(git, "run", lambda *args, **kwargs: SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(git, "out", lambda args, path: head if args[0] == "rev-parse" else "1")
    monkeypatch.setattr(git, "base_ref", lambda *args: "main")
    monkeypatch.setattr(git, "ahead_behind", lambda *args: (0, 1))
    monkeypatch.setattr(git, "head", lambda *args: head)
    monkeypatch.setattr(git, "is_ancestor", lambda *args: False)
    monkeypatch.setattr(status_cache, "tracked_dirty", lambda *args: False)
    monkeypatch.setattr(gates, "pr_checked", checked)
    monkeypatch.setattr(routes_actions, "_spawn", lambda run: run())
    monkeypatch.setattr(routes_actions, "_PR_WORK", set())
    monkeypatch.setattr(routes_actions, "_PR_TRIED", {})
    monkeypatch.setattr(inbox.view, "snapshot", lambda *args, **kwargs: [
        {"id": ws.id, "branch": ws.branch, "agents": [{"id": "root", "status": "idle"}]}])

    def cards():
        return [i for i in inbox.items(db, ws.repo_root, panes={}) if i.kind.startswith("pr_")]

    watch = review.ReadinessWatch()
    watch.sweep(db)
    # An unavailable lookup cannot forget a recorded PR. Neither can the
    # current commit's absence from GitHub's search: the PR may be behind it.
    assert review.open_pr(db, db.get_agent("root"), ws)["url"] == url
    assert cards() == []  # the person dismissed this PR
    payload = {"state": "OPEN"}
    review._read_pr_state(url, ws.path)
    watch.sweep(db)
    assert review.open_pr(db, db.get_agent("root"), ws)["url"] == url
    assert cards() == []

    # GitHub confirms that the old PR closed without merging. All milestone
    # and audit inputs stay unchanged; the cached readiness must still allow
    # real preparation, and the old dismissal must not swallow its new card.
    payload = {"state": "CLOSED"}
    review._read_pr_state(url, ws.path)
    watch.sweep(db)
    [card] = cards()
    assert (card.kind, card.agent_id) == ("pr_ready", "root")
    assert review.open_pr(db, db.get_agent("root"), ws) is None
    assert db.conn.execute("SELECT state FROM session_pr_prep WHERE root_id = 'root'").fetchone()[0] == "passed"
    assert bus.tail(db, kinds=("session.pr_prepared",))
    watch.sweep(db)
    assert len(cards()) == 1
