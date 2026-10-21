"""The daemon under repeated use: the read routes, the events feed and the
terminal socket, on a real tmux server, must hand back every fd and SQLite
connection they open, and keystrokes sent after all that must still reach the
pane."""

import contextlib
import json
import os
import sqlite3
import threading
import time

import anyio
import pytest
from starlette.testclient import TestClient

from brindle import identity_lock, tmux, workspaces
from brindle.daemon.app import build_app
from brindle.db import Agent

TOKEN = "t" * 43
ROUNDS = 5
EVENT_CLIENTS = 60
TERMINAL_CLIENTS = 10

DEADLINE = 50.0     # seconds the whole test may wait on the daemon before it fails
pause = threading.Event()   # never set: pause.wait(n) is a plain timed wait

# path -> the status it must answer; the 404s and the 402 are deliberate error paths
READ_PATHS = {
    "/v1/meta": 200, "/v1/repos": 200, "/v1/sessions?repo={repo}": 200, "/v1/snapshot": 200,
    "/v1/goal?repo={repo}": 200, "/v1/inbox": 200, "/v1/agents/w1": 200, "/v1/agents/missing": 404,
    "/v1/workspaces/{ws}/diff": 200, "/v1/workspaces/{ws}/file?path=app.py": 200,
    "/v1/workspaces/{ws}/file?path=nope.py": 404, "/v1/decisions?repo={repo}": 200,
    "/v1/kb/sources?repo={repo}": 200, "/v1/kb/search?repo={repo}&q=x": 200, "/v1/profiles": 200,
    "/v1/profiles/developer": 200, "/v1/config?repo={repo}": 200, "/v1/routing?repo={repo}": 200,
    "/v1/doctor": 200, "/v1/providers/claude/preflight?repo={repo}": 200, "/v1/account": 200,
    "/v1/cost": 402,    # Pro-gated: refused after the route opened its DB
}


class Tracked(sqlite3.Connection):
    """A connection that says whether it is still open."""

    live: set = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Tracked.live.add(id(self))

    def close(self):
        super().close()
        Tracked.live.discard(id(self))


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def settled(measure, limit: float = 5.0) -> int:
    """``measure()`` once it has stopped changing for a second (worker threads
    and the control client are still winding down after a socket closes)."""
    deadline, last, same = time.monotonic() + limit, None, 0
    while time.monotonic() < deadline:
        now = measure()
        same = same + 1 if now == last else 0
        if same >= 5:
            break
        last = now
        pause.wait(0.2)
    return measure()


class Sockets:
    """WebSockets whose receives give up at ``deadline`` (a monotonic time):
    a daemon that never answers fails the test instead of hanging it."""

    def __init__(self, client, deadline: float) -> None:
        self.client, self.deadline = client, deadline

    @contextlib.contextmanager
    def connect(self, path):
        with self.client.websocket_connect(path) as sock:
            sock.send_text(TOKEN)
            yield sock

    def receive(self, sock):
        """The next frame's payload (text parsed as JSON, else bytes)."""
        async def next_message():
            with anyio.fail_after(max(0.0, self.deadline - time.monotonic())):
                return await sock._send_rx.receive()    # what sock.receive() waits on, with a timeout

        try:
            message = sock.portal.call(next_message)
        except TimeoutError:
            pytest.fail("no frame from the daemon before the deadline")
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"]) if message.get("text") is not None else message["bytes"]


@pytest.mark.serial
def test_daemon_leaks_nothing_under_load_and_keys_still_reach_the_pane(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/soak").workspace
    pane = tmux._tmux("new-session", "-d", "-s", "soak", "-P", "-F", "#{pane_id}", "sh").stdout.strip()
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", pane, None, 1000.0,
                       tmux_server=tmux.current_server()))
    # The daemon is the file's only user, as in production: SQLite keeps the fd
    # of a connection closed beside another open one, which is not a leak.
    db.conn.close()
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: None)
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=Tracked, **kw))
    paths = {p.format(repo=repo, ws=ws.id): status for p, status in READ_PATHS.items()}
    headers = {"Authorization": f"Bearer {TOKEN}"}

    def hammer(client, socks):
        for _ in range(ROUNDS):
            for path, status in paths.items():
                got = client.get(path, headers=headers).status_code
                assert got == status, f"{path}: {got}, expected {status}"
        for _ in range(EVENT_CLIENTS):
            with socks.connect("/v1/events") as sock:
                socks.receive(sock)     # the replay: the feed has its DB open
        for _ in range(TERMINAL_CLIENTS):
            with socks.connect("/v1/terminal/w1") as sock:
                socks.receive(sock)     # hello
                socks.receive(sock)     # seed

    deadline = time.monotonic() + DEADLINE
    with TestClient(build_app(TOKEN)) as client:
        socks = Sockets(client, deadline)
        hammer(client, socks)   # warm-up: lazily opened files and thread pools are not leaks
        fds = settled(open_fds)
        conns = settled(lambda: len(Tracked.live))
        hammer(client, socks)
        assert settled(open_fds) <= fds
        assert settled(lambda: len(Tracked.live)) <= conns

        with socks.connect("/v1/terminal/w1") as sock:
            socks.receive(sock)
            socks.receive(sock)
            sock.send_json({"type": "key", "text": "echo soak-$((6*7))"})
            sock.send_json({"type": "key", "keys": ["Enter"]})
            # Polled, not read off the socket: dropped input times out here instead of blocking.
            while "soak-42" not in tmux.capture(pane, 50, server=tmux.current_server()).replace(
                    "echo soak-$((6*7))", ""):
                assert time.monotonic() < deadline, "the keystrokes never reached the pane"
                pause.wait(0.1)
