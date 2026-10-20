import json
import os
import shlex
import subprocess
import threading
from unittest.mock import ANY

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brindle import identity_lock, tmux, workspaces
from brindle.daemon.app import build_app
from brindle.daemon.terminal import WS_NOT_FOUND
from brindle.db import Agent

TOKEN = "t" * 43


class FakeTmux:
    """tmux._tmux with one live pane, %1 (every other target is gone), that
    records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.attached = 0
        self.cond = threading.Condition()

    def __call__(self, *args, input=None, check=True, server=None):
        target = args[args.index("-t") + 1] if "-t" in args else None
        with self.cond:
            self.calls.append(args)
            self.cond.notify_all()
        if target not in (None, "%1"):
            if check:
                raise tmux.TmuxError(f"can't find pane: {target}")
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "display-message":
            out = f"3 1 {self.attached}" if "#{session_attached}" in args[-1] else "0"
        else:
            out = "hello\nworld\n" if args[0] == "capture-pane" else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    def wait_for(self, pred) -> None:
        with self.cond:
            assert self.cond.wait_for(lambda: pred(self.calls), timeout=5)


@pytest.fixture
def fake(db, repo, tmp_path, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/term").workspace
    said = tmp_path / "w9.jsonl"
    said.write_text(json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "all done\nbye"}]}}) + "\n")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", "%1", None, 1000.0))
    db.add_agent(Agent("w9", ws.id, "developer", "claude", None, "assign", "idle", "%9", None, 1000.0,
                       transcript_path=str(said)))
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: None)
    fake = FakeTmux()
    monkeypatch.setattr(tmux, "_tmux", fake)
    return fake


def connect(client, agent):
    sock = client.websocket_connect(f"/v1/terminal/{agent}").__enter__()
    sock.send_text(TOKEN)
    return sock


def test_viewers_share_one_pipe_that_ends_with_the_last(fake):
    with TestClient(build_app(TOKEN)) as client:
        a = connect(client, "w1")
        assert a.receive_json() == {"type": "terminal", "agent": "w1", "shared": False}
        assert a.receive_bytes() == b"\x1bchello\r\nworld\x1b[2;4H"
        b = connect(client, "w1")
        b.receive_json()
        b.receive_bytes()
        command = next(c[-1] for c in fake.calls if c[:2] == ("pipe-pane", "-O"))
        fifo = shlex.split(command)[-1]
        with open(fifo, "wb") as out:
            out.write(b"\x1b[31mlive")
        assert a.receive_bytes() == b.receive_bytes() == b"\x1b[31mlive"
        b.__exit__(None, None, None)
        with open(fifo, "wb") as out:      # still piped for the one viewer left
            out.write(b"more")
        assert a.receive_bytes() == b"more"
        a.__exit__(None, None, None)
        fake.wait_for(lambda calls: ("pipe-pane", "-t", "%1") in calls)
    assert sum(c[:2] == ("pipe-pane", "-O") for c in fake.calls) == 1
    assert not os.path.exists(fifo)


@pytest.mark.parametrize("attached", [0, 1])
def test_control_frames_reach_the_pane(fake, attached):
    fake.attached = attached
    with TestClient(build_app(TOKEN)) as client:
        sock = connect(client, "w1")
        assert sock.receive_json()["shared"] is bool(attached)
        sock.receive_bytes()
        sock.send_text("not json")
        sock.send_json({"type": "key", "text": "-t %2"})
        sock.send_json({"type": "key", "keys": ["C-c"]})
        sock.send_json({"type": "paste", "text": "a\nb"})
        for cols in (80, 100, 120):
            sock.send_json({"type": "resize", "cols": cols, "rows": 40})
        if attached:    # a terminal is attached: its size rules, nothing is resized
            fake.wait_for(lambda calls: sum("#{session_attached}" in c[-1] for c in calls) == 2)
        else:
            fake.wait_for(lambda calls: calls[-1][0] == "resize-window")
        sent = [c for c in fake.calls if c[0] in ("send-keys", "load-buffer", "paste-buffer",
                                                  "set-option", "resize-window")]
        assert sent == [
            ("send-keys", "-t", "%1", "-l", "--", "-t %2"),
            ("send-keys", "-t", "%1", "--", "C-c"),
            ("load-buffer", "-b", ANY, "-"),
            ("paste-buffer", "-p", "-d", "-b", ANY, "-t", "%1"),
            *([] if attached else [("set-option", "-w", "-t", "%1", "window-size", "manual"),
                                   ("resize-window", "-t", "%1", "-x", "120", "-y", "40")]),
        ]
        sock.__exit__(None, None, None)


@pytest.mark.parametrize("agent,locked,hello,shown,code", [
    ("w9", None, {"type": "no_pane", "agent": "w9", "reason": "exited", "source": "transcript"},
     b"\x1bcall done\r\nbye", 1000),
    ("w9", "boss@example.com", {"type": "no_pane", "agent": "w9", "reason": "locked",
                                "locked_by": "boss@example.com"}, None, 1000),
    ("nope", None, {"type": "error", "code": "not_found", "message": ANY}, None, WS_NOT_FOUND),
])
def test_without_a_pane_says_so_and_shows_what_is_left(fake, monkeypatch, agent, locked, hello,
                                                       shown, code):
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: locked)
    with TestClient(build_app(TOKEN)) as client:
        sock = connect(client, agent)
        assert sock.receive_json() == hello
        if shown:
            assert sock.receive_bytes() == shown
        with pytest.raises(WebSocketDisconnect) as closed:
            sock.receive_bytes()
        assert closed.value.code == code
        sock.__exit__(None, None, None)
    assert not any(c[0] == "pipe-pane" for c in fake.calls)


@pytest.mark.serial
def test_a_real_pane_streams_what_it_prints(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/term").workspace
    pane = tmux._tmux("new-session", "-d", "-s", "term", "-P", "-F", "#{pane_id}", "sh").stdout.strip()
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", pane, None, 1000.0,
                       tmux_server=tmux.current_server()))
    monkeypatch.setattr(identity_lock, "locked_by", lambda a: None)
    with TestClient(build_app(TOKEN)) as client:
        sock = connect(client, "w1")
        sock.receive_json()
        sock.receive_bytes()
        sock.send_json({"type": "key", "text": "echo brindle-$((40+2))"})
        sock.send_json({"type": "key", "keys": ["Enter"]})
        seen = b""
        while b"brindle-42" not in seen:
            seen += sock.receive_bytes()
        sock.__exit__(None, None, None)
