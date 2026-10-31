"""``WS /v1/pty``: the app's pane view, run by the daemon (brindle.daemon.pty), against
a real tmux server of the test's own (conftest's BRINDLE_TMUX_SOCKET)."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brindle import tmux
from brindle.daemon import pty
from brindle.daemon.app import build_app
from brindle.daemon.auth import WS_UNAUTHORIZED

TOKEN = "t" * 43
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "view_session.json").read_text())
needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


def test_the_view_session_is_the_apps():
    """What the app's pty_view.rs makes, as tests/fixtures/view_session.json records it
    (pty_view.rs's the_view_session_is_the_engines checks the same file)."""
    f = FIXTURE
    assert (pty.FEATURES, pty.KEY_TABLE, pty.VIEW_PREFIX, list(pty.MIN_TMUX)) == \
        (f["features"], f["key_table"], f["view_prefix"], f["min_tmux"])
    assert (pty.LAYOUT_FORMAT, pty.CLIENT_FORMAT) == (f["layout_format"], f["client_format"])
    p = f["prepare"]
    assert pty.prepare_args(p["view"], p["target"], p["cols"], p["rows"]) == p["args"]
    assert pty.attach_args(f["attach"]["view"]) == f["attach"]["args"]
    assert {v: pty.view_bindings(tuple(map(int, v.split(".")))) for v in f["bindings"]} == f["bindings"]
    for case in f["server_args"]:
        try:
            got = pty.server_args(case["server"])
        except ValueError:
            got = None
        assert got == case["args"], case
    assert [[t, pty.valid_target(t)] for t, _ in f["valid_target"]] == f["valid_target"]
    newest = lambda v: v and [min(v[0], 9999), v[1]]  # noqa: E731 - `master`, as the fixture writes it
    assert [[v, newest(pty.parse_version(v))] for v, _ in f["parse_version"]] == f["parse_version"]
    assert [[v, pty.check_version(v, "The agents' tmux server")] for v, _ in f["check_version"]] == f["check_version"]
    assert [[s, pty.view_owner(s)] for s, _ in f["view_owner"]] == f["view_owner"]
    for case in f["crop_of"]:
        client = tuple(case["client"]) if case["client"] else None
        assert pty.crop_of(case["panes"], case["visible"], client).to_json() == case["crop"], case


def test_a_redraw_goes_whole_and_a_release_waits_behind_the_motion(monkeypatch):
    """The client's bytes a redraw at a time, a synchronized update whole; a mouse
    release written RELEASE_GAP after the motion before it."""
    import queue

    reads: queue.Queue = queue.Queue()
    for chunk in [pty.SYNC_START + b"half", b" of it", pty.SYNC_END, 0]:
        reads.put(chunk)
    sent: list[bytes] = []
    assert pty.forward(reads, sent.append) == 0
    assert sent == [pty.SYNC_START + b"half of it" + pty.SYNC_END]
    r, w = os.pipe()
    stamps: list[float] = []
    real_write = os.write
    monkeypatch.setattr(pty.os, "write", lambda fd, data: (stamps.append(time.monotonic()), real_write(fd, data))[1])
    pty.write_input(w, b"\x1b[<32;5;5M\x1b[<0;5;5m", [])
    assert os.read(r, 100) == b"\x1b[<32;5;5M\x1b[<0;5;5m"
    assert len(stamps) == 2 and stamps[1] - stamps[0] >= pty.RELEASE_GAP * 0.9


def _agents(cmd: str = "/bin/sh") -> str:
    tmux._tmux("-f", os.devnull, "new-session", "-d", "-s", "agents", "-n", "a1", "-x", "80", "-y", "24", cmd)
    return tmux.current_server()


def _sessions() -> list[str]:
    return tmux._tmux("list-sessions", "-F", "#{session_name}").stdout.split()


def _until(what: str, done, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def _read_until(ws, marker: bytes) -> bytes:
    seen = b""
    while marker not in seen:
        message = ws.receive()
        if message.get("bytes"):
            seen += message["bytes"]
        elif message.get("text") and "exit" in json.loads(message["text"]):
            pytest.fail(f"the client exited before {marker!r}: {seen[-300:]!r}")
    return seen


@needs_tmux
def test_a_view_attaches_streams_both_ways_resizes_and_leaves_only_the_agents_window(db):
    server = _agents()
    with TestClient(build_app(TOKEN)) as client, \
            client.websocket_connect(f"/v1/pty?server={server}&target=agents:a1&cols=90&rows=25") as ws:
        ws.send_text(TOKEN)
        assert ws.receive_json() == {"crop": {"cols": 0, "left": 0, "width": None}}
        view = next(s for s in _sessions() if s.startswith(pty.VIEW_PREFIX))
        # The app's options, on the view session only: the agent's keeps tmux's own.
        shown = tmux._tmux("show-options", "-t", f"={view}:", "status").stdout.strip()
        assert shown == "status off"
        assert tmux._tmux("show-options", "-t", "=agents:", "status").stdout.strip() == ""
        ws.send_text(json.dumps({"type": "input", "data": "printf 'MARK%s\\n' 42\r"}))
        assert b"MARK42" in _read_until(ws, b"MARK42")
        ws.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        size = lambda: tmux._tmux("display-message", "-p", "-t", "agents:a1", "#{window_width}x#{window_height}").stdout.strip()  # noqa: E731
        _until("the window takes the view's size", lambda: size() == "100x30")
        ws.send_text(json.dumps({"type": "focus"}))
        ws.send_text(json.dumps({"type": "input", "data": "printf 'AFTER%s\\n' 1\r"}))
        _read_until(ws, b"AFTER1")
    _until("the view session goes with its socket", lambda: view not in _sessions())
    assert "agents" in _sessions()
    assert tmux._tmux("list-windows", "-t", "=agents", "-F", "#{window_name}").stdout.split() == ["a1"]


@needs_tmux
def test_the_clients_exit_is_reported_and_a_view_that_cant_open_says_why(db):
    server = _agents()
    with TestClient(build_app(TOKEN)) as client:
        with client.websocket_connect(f"/v1/pty?server={server}&target=agents:a1&cols=90&rows=25") as ws:
            ws.send_text(TOKEN)
            ws.receive_json()
            view = next(s for s in _sessions() if s.startswith(pty.VIEW_PREFIX))
            tmux._tmux("kill-session", "-t", f"={view}")
            said = [json.loads(m["text"]) for m in iter(ws.receive, None) if m.get("text")]
            assert said[-1] == {"exit": 0}
        with client.websocket_connect(f"/v1/pty?server={server}&target=nowhere:9&cols=90&rows=25") as ws:
            ws.send_text(TOKEN)
            assert "tmux couldn't make the view" in ws.receive_json()["error"]
    assert not [s for s in _sessions() if s.startswith(pty.VIEW_PREFIX)]


@needs_tmux
def test_the_status_says_whether_the_engines_tmux_can_show_a_session(db, tmp_path, monkeypatch):
    with TestClient(build_app(TOKEN)) as client:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/v1/pty/status", headers=auth).json()["kind"] == "ready"
        old = tmp_path / "tmux"
        old.write_text("#!/bin/sh\necho 'tmux 3.0a'\n")
        old.chmod(0o755)
        monkeypatch.setenv("BRINDLE_TMUX_BIN", str(old))
        assert client.get("/v1/pty/status", headers=auth).json() == {
            "kind": "unusable",
            "message": "The engine's tmux is tmux 3.0a, and brindle needs tmux 3.2 or newer to show a session. "
                       "Update tmux, then reopen the session."}


@pytest.mark.variant("the guard's token check, on this route: no pane for a wrong token")
def test_a_wrong_token_opens_no_view(db):
    with TestClient(build_app(TOKEN)) as client, \
            client.websocket_connect("/v1/pty?server=x&target=agents:a1&cols=90&rows=25") as ws:
        ws.send_text("wrong")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == WS_UNAUTHORIZED
