"""The New session page opens at once, and a new session's start overlaps what it can.

The page's reads (GET /repos, GET /sessions per repo, GET /providers/usable)
run no git and no provider CLI on the request path; a start in a new worktree
fetches origin while its checks run."""

import asyncio
import json
import math
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from brindle import bus, git, providers, scratch, session_sync, spawn, tmux, workspaces
from brindle.daemon import routes_actions, routes_read
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.service import sessions as svc

TOKEN = "t" * 43


def _get(path: str, **query: str) -> tuple[int, object]:
    async def ask():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": urlencode(query).encode(), "root_path": "", "server": ("127.0.0.1", 8123),
                 "client": ("127.0.0.1", 50000), "headers": [(b"authorization", f"Bearer {TOKEN}".encode())]}
        await build_app(TOKEN)(scope, receive, send)
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return sent[0]["status"], json.loads(body)

    return asyncio.run(ask())


def _chat(db, ws: Workspace, agent_id: str, status: str = "running", created: float = 0.0,
          pane: str = "%1") -> Agent:
    agent = Agent(agent_id, ws.id, "supervisor", "claude", None, "interactive", status, pane, None,
                  created or time.time(), tmux_server=tmux.current_server())
    db.add_agent(agent)
    return agent


def _panes(up: set[str]):
    snap = tmux.PaneSnapshot(tmux.current_server())
    snap.update({p: True for p in up})
    return snap


def test_project_list_leaves_out_gone_and_temp_repos_unless_a_session_runs_there(db, tmp_path, monkeypatch):
    temp = tmp_path / "T"
    monkeypatch.setattr(routes_read, "TEMP_ROOTS", (f"{temp}/",), raising=False)
    up = {"%2", "%3", "%5"}
    monkeypatch.setattr(tmux, "list_panes", lambda server=None: _panes(up))

    def project(path, used, *, on_disk=True, chat=None) -> str:
        if on_disk:
            (path / ".git").mkdir(parents=True)
        ws = Workspace(f"{path.name}/root", str(path), "root", "root", "main", "main", str(path), None,
                       f"brindle-{path.name}", used)
        db.add_workspace(ws)
        if chat:
            _chat(db, ws, f"chat-{path.name}", status=chat[0], created=used, pane=chat[1])
        return str(path)

    old = project(tmp_path / "projects" / "old", 1.0)
    new = project(tmp_path / "projects" / "new", 3.0)
    project(tmp_path / "projects" / "gone", 5.0, on_disk=False)       # deleted since
    project(temp / "bench-idle", 6.0)                                 # a benchmark's, nothing running
    bench = project(temp / "bench-live", 2.0, chat=("running", "%2"))   # a temp repo with a session running
    # Its chat marked failed (a brief not confirmed) while its pane still runs: running.
    failing = project(temp / "bench-failing", 2.5, chat=("failed", "%3"))
    project(temp / "bench-ended", 2.7, chat=("running", "%4"))        # its pane is gone, its row not yet updated
    project(scratch.scratch_dir() / "idle", 7.0, on_disk=False)       # a scratch session, paused
    live_scratch = project(scratch.scratch_dir() / "live", 0.5, on_disk=False, chat=("running", "%5"))

    def listed() -> list[str]:
        code, rows = _get("/v1/repos")
        assert code == 200
        return [r["repo_root"] for r in rows]

    # First opened first (created 0.5, 1.0, 2.0, 2.5, 3.0).
    assert listed() == [live_scratch, old, bench, failing, new]
    up.discard("%3")        # that session's chat exits
    assert listed() == [live_scratch, old, bench, new]
    # New work in an older repo doesn't move it: the order is when each was first opened.
    db.add_workspace(Workspace("old/later", old, "later", "worktree", "later", "main", old, None, "brindle-old-later", 9.0))
    assert listed() == [live_scratch, old, bench, new]


def test_new_session_page_reads_run_no_git_or_cli_and_share_one_tmux_look(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    _chat(db, ws, "chat1")
    ran = []

    def no_process(argv, **kw):
        ran.append(argv)
        return SimpleNamespace(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(spawn, "read", no_process)
    monkeypatch.setattr(spawn, "action", no_process)
    looks = []

    def list_panes(server=None):
        looks.append(1)
        snap = tmux.PaneSnapshot(tmux.current_server())
        snap["%1"] = True
        return snap

    monkeypatch.setattr(tmux, "list_panes", list_panes)
    monkeypatch.setattr(routes_read, "PANE_VIEW_MAX_AGE", math.inf, raising=False)
    answers, asked, refreshes = [[]], [], []

    def usable(root):
        asked.append((root, providers.probing()))
        return list(answers[-1])

    monkeypatch.setattr(svc, "usable_providers", usable)
    monkeypatch.setattr(routes_read, "_start_usable_refresh", refreshes.append, raising=False)
    root = str(repo)

    # Every repo's sessions, as the app reads them after each event: one look at tmux for them all.
    for _ in range(3):
        code, rows = _get("/v1/sessions", repo=root)
        assert code == 200 and [r["id"] for r in rows] == ["chat1"]
    assert len(looks) == 1
    bus.publish(db, "agent.status", agent_id="chat1", repo_root=root)     # something happened: look again
    assert [r["id"] for r in _get("/v1/sessions", repo=root)[1]] == ["chat1"] and len(looks) == 2

    # The CLIs: answered without running one, then worked out behind the reply.
    def usable_now():
        return _get("/v1/providers/usable", repo=root)[1]["usable"]

    def published():
        return [e["payload"]["usable"] for e in bus.tail(db, kinds=("providers.usable",))]

    assert usable_now() == [] and asked == [(root, False)] and refreshes == [root]
    routes_read._run_usable_refresh(root)        # what the background thread runs: nothing changed
    assert asked[-1] == (root, True) and published() == []
    # Signed in to Codex in Settings, back on New session a moment later: told at once from
    # the last answer, and checked again behind it.
    answers.append(["codex"])
    assert usable_now() == [] and refreshes == [root, root]
    assert usable_now() == [] and refreshes == [root, root]     # asked again meanwhile: one more waits
    routes_read._run_usable_refresh(root)
    assert published() == [["codex"]]            # the page hears it and asks again
    assert refreshes == [root, root, root]       # and the waiting check follows the one that ran
    routes_read._run_usable_refresh(root)
    assert published() == [["codex"]]            # the same answer again says nothing
    assert _get("/v1/providers/usable", repo=root) == (200, {"usable": ["codex"], "default": "codex"})
    assert ran == []


@pytest.mark.variant("every test reopens the DB, but none notices the setup re-run on each open (a burst's queueing)")
def test_each_request_opens_the_db_without_setting_it_up_again(db, monkeypatch):
    """The daemon opens a DB per request: one this process set up, its schema
    unchanged, opens without the setup (whose reads queued a burst of
    requests), with its foreign keys still on; a changed schema is set up again."""
    from brindle import db as dbm

    runs, real = [], dbm.DB._migrate
    monkeypatch.setattr(dbm.DB, "_migrate", lambda self: runs.append(1) or real(self))
    for _ in range(2):
        again = dbm.DB()
        assert again.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        again.conn.close()
    assert runs == []
    db.conn.execute("CREATE TABLE added_by_another_brindle (x)")     # another process's migration
    dbm.DB().conn.close()
    assert runs == [1]


class _Stop(Exception):
    """Where the test stops a start: once its worktree would be made."""


def test_new_worktree_start_fetches_while_its_checks_run(db, repo, monkeypatch):
    checks_on, fetch_on = threading.Event(), threading.Event()
    overlapped = []

    def blockers(db_, repo_, provider):
        checks_on.set()
        overlapped.append(fetch_on.wait(5))     # the fetch is under way while the checks run
        return None

    def fresh_start(root):
        fetch_on.set()
        checks_on.wait(5)
        return git.out(["rev-parse", "HEAD"], root)

    def create(*a, **k):
        raise _Stop

    monkeypatch.setattr(routes_actions, "session_blockers", blockers)
    monkeypatch.setattr(git, "missing_user_identity", lambda path: [])
    monkeypatch.setattr(svc, "supervisor_provider", lambda root, provider=None: "claude")
    monkeypatch.setattr(session_sync, "fresh_start", fresh_start)
    monkeypatch.setattr(workspaces, "create", create)
    with pytest.raises(_Stop):
        routes_actions.start_session(db, {}, {"repo": str(repo), "choice": "n"})
    assert overlapped == [True]
