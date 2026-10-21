"""The daemon under repeated use: the read routes, the events feed and the
terminal socket, on a real tmux server, must hand back every fd and SQLite
connection they open, and keystrokes sent after all that must still reach the
pane."""

import os
import sqlite3
import threading
import time

import pytest
from starlette.testclient import TestClient

from brindle import identity_lock, tmux, workspaces
from brindle.daemon.app import build_app
from brindle.db import Agent

TOKEN = "t" * 43
ROUNDS = 12
EVENT_CLIENTS = 120
TERMINAL_CLIENTS = 25

READ_PATHS = [
    "/v1/meta", "/v1/repos", "/v1/sessions?repo={repo}", "/v1/snapshot", "/v1/goal?repo={repo}",
    "/v1/inbox", "/v1/agents/w1", "/v1/agents/missing", "/v1/workspaces/{ws}/diff",
    "/v1/workspaces/{ws}/file", "/v1/decisions?repo={repo}", "/v1/kb/sources?repo={repo}",
    "/v1/kb/search?repo={repo}&q=x", "/v1/profiles", "/v1/profiles/developer", "/v1/config?repo={repo}",
    "/v1/routing?repo={repo}", "/v1/doctor", "/v1/providers/claude/preflight?repo={repo}",
    "/v1/account", "/v1/cost",
]


class Tracked(sqlite3.Connection):
    live: set = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Tracked.live.add(id(self))

    def close(self):
        Tracked.live.discard(id(self))
        super().close()


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def settled(measure, limit: float = 5.0) -> int:
    """``measure()`` once it has stopped changing (worker threads and the
    control client are still winding down for a moment after a socket closes)."""
    deadline, last, pause = time.monotonic() + limit, None, threading.Event()
    while time.monotonic() < deadline:
        now = measure()
        if now == last:
            return now
        last = now
        pause.wait(0.2)
    return measure()


def connect(client, path):
    sock = client.websocket_connect(path).__enter__()
    sock.send_text(TOKEN)
    return sock


@pytest.mark.serial
def test_daemon_leaks_nothing_under_load_and_keys_still_reach_the_pane(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/soak").workspace
    pane = tmux._tmux("new-session", "-d", "-s", "soak", "-P", "-F", "#{pane_id}", "sh").stdout.strip()
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", pane, None, 1000.0,
                       tmux_server=tmux.current_server()))
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: None)
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(*a, factory=Tracked, **kw))
    paths = [p.format(repo=repo, ws=ws.id) for p in READ_PATHS]
    headers = {"Authorization": f"Bearer {TOKEN}"}

    def hammer(client):
        for _ in range(ROUNDS):
            for path in paths:
                client.get(path, headers=headers)
        for _ in range(EVENT_CLIENTS):
            sock = connect(client, "/v1/events")
            sock.receive_json()     # the first liveness sweep: the feed has its DB open
            sock.__exit__(None, None, None)
        for _ in range(TERMINAL_CLIENTS):
            sock = connect(client, "/v1/terminal/w1")
            sock.receive_json()
            sock.receive_bytes()
            sock.__exit__(None, None, None)

    started = time.monotonic()
    with TestClient(build_app(TOKEN)) as client:
        hammer(client)      # warm-up: lazily opened files and thread pools are not leaks
        fds = settled(open_fds)
        conns = settled(lambda: len(Tracked.live))
        hammer(client)
        assert settled(open_fds) <= fds
        assert settled(lambda: len(Tracked.live)) <= conns

        sock = connect(client, "/v1/terminal/w1")
        sock.receive_json()
        sock.receive_bytes()
        sock.send_json({"type": "key", "text": "echo soak-$((6*7))"})
        sock.send_json({"type": "key", "keys": ["Enter"]})
        seen = b""
        while b"soak-42" not in seen.replace(b"echo soak-$((6*7))", b""):
            seen += sock.receive_bytes()
        sock.__exit__(None, None, None)
    assert "soak-42" in tmux.capture(pane, 50, server=tmux.current_server()).replace("echo soak-$((6*7))", "")
    assert time.monotonic() - started < 60
