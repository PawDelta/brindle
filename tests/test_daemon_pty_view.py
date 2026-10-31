import os
import pty
import shutil
import socket
import subprocess
import sys
import threading

import pytest
from starlette.testclient import TestClient

from brindle import tmux
from brindle.daemon import pty_view as pv
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace

TOKEN = "t" * 43
WATCHDOG_SECONDS = 60.0
# list-panes rows (LAYOUT_FORMAT): sidebar, left, right, top, bottom, width, height, zoomed, active, cursor_x
LEFT = "s\t0\t19\t0\t23\t80\t24\t0\t0\t0\n\t21\t79\t0\t23\t80\t24\t0\t1\t50"
RIGHT = "\t0\t58\t0\t23\t80\t24\t0\t1\t0\ns\t60\t79\t0\t23\t80\t24\t0\t0\t0"


@pytest.mark.parametrize("call,args,want", [
    (pv.server_args, (None,), []),
    (pv.server_args, ("Default",), []),
    (pv.server_args, ("brindle-x",), ["-L", "brindle-x"]),
    (pv.server_args, ("/tmp/s",), ["-S", "/tmp/s"]),
    (pv.server_args, ("a;kill-server",), pv.ViewError),
    (pv.valid_target, ("s:@1",), True),
    (pv.valid_target, ("-t",), False),
    (pv.parse_version, ("tmux 3.2a",), (3, 2)),
    (pv.parse_version, ("master",), (sys.maxsize, 0)),
    (pv.parse_version, ("tmux ???",), None),
    (pv.check_version, ("tmux 3.1c", "x"), pv.ViewError),
    (pv.check_version, ("tmux ???", "x"), pv.ViewError),
    (pv.check_version, ("tmux 3.2", "x"), None),
    (lambda v: pv.view_bindings(v).count("\n"), ((3, 2),), 8),
    (lambda v: pv.view_bindings(v).count("\n"), ((3, 7),), 12),
    (pv.view_owner, ("brindle-view-123-4",), 123),
    (pv.view_owner, ("agent-123-4",), None),
    (pv.crop_of, (LEFT, 59, None), pv.Crop(21, 21, None)),
    (pv.crop_of, (LEFT, 70, None), pv.Crop(21, 21, 59)),          # a narrower terminal has the window
    (pv.crop_of, (LEFT, 40, (60, 10)), pv.Crop(21, 20, None)),    # panned: the cursor kept in view
    (pv.crop_of, (RIGHT, 59, None), pv.Crop(21, 0, None)),
    (pv.crop_of, (LEFT.replace("\t0\t0\t0\n", "\t1\t0\t0\n"), 59, None), pv.Crop()),   # zoomed
    (pv.crop_of, ("s\t0\t79\t0\t23\t80\t24\t0\t1\t0", 59, None), pv.Crop()),            # a sidebar alone
    (pv.sync_open_after, (False, b"\x1b[?20", b"26h"), True),     # a marker split between reads
    (pv.sync_open_after, (True, b"", b"text"), True),
    (pv.sync_open_after, (True, b"", pv.SYNC_END), False),
    (pv.sgr_releases, (b"\x1b[<0;1;1M\x1b[<0;1;1m\x1b[<0",), [9]),
])
def test_pure_parts(call, args, want):
    if want is pv.ViewError:
        with pytest.raises(pv.ViewError):
            call(*args)
    else:
        assert call(*args) == want


@pytest.mark.parametrize("server,admitted", [
    ("agent-srv", True),
    (str(tmux.socket_dir() / "agent-srv"), True),
    ("someone-else", False),
    pytest.param(None, False, marks=pytest.mark.variant("no server must not mean the default one")),
])
def test_only_brindles_own_servers_are_shown(db, server, admitted):
    db.add_workspace(Workspace("ws", "/r", "root", "main", "main", None, "/r", None, "brindle-root", 1.0))
    db.add_agent(Agent("w1", "ws", "developer", "claude", None, "assign", "idle", "%1", None, 1.0,
                       tmux_server="agent-srv"))
    if admitted:
        assert pv.admit(server) == pv.server_args(server)
    else:
        with pytest.raises(pv.ViewError):
            pv.admit(server)


@pytest.mark.parametrize("ask", [
    {"target": "s:@1"},
    {"server": "someone-else", "target": "s:@1", "cols": 80, "rows": 24},
])
def test_a_view_that_cant_open_says_why_and_closes(db, ask):
    with TestClient(build_app(TOKEN)) as client, client.websocket_connect("/v1/pty_view") as ws:
        ws.send_text(TOKEN)
        ws.send_json(ask)
        assert list(ws.receive_json()) == ["error"]
        assert ws.receive()["type"] == "websocket.close"


def _size(window: str) -> str:
    return tmux._tmux("display-message", "-p", "-t", window, "#{window_width}x#{window_height}").stdout.strip()


def _views() -> list[str]:
    names = tmux._tmux("list-sessions", "-F", "#{session_name}").stdout.split()
    return [n for n in names if n.startswith(pv.VIEW_PREFIX)]


def _until(ws, done) -> bytes:
    """The view's bytes, frame by frame, until ``done(bytes so far)``: each
    change the test waits on is redrawn, so it comes with a frame."""
    seen = b""
    while not done(seen):
        frame = ws.receive()
        assert frame["type"] == "websocket.send" and "exit" not in (frame.get("text") or ""), frame
        seen += frame.get("bytes") or b""
    return seen


@pytest.mark.parametrize("matches", [True, False])
def test_an_older_servers_client_is_the_executable_listening_on_its_socket(tmp_path, monkeypatch, matches):
    # The "server" is this test's own socket, so the kernel names this Python as its
    # peer: its executable is picked only if it says it is the server's version.
    monkeypatch.chdir(tmp_path)     # a short socket path (macOS caps them at 104 bytes)
    # Whether this host's Python sits in a folder only root or this user can write to
    # varies (a CI runner's toolcache may be group-writable); this test is about the peer lookup.
    monkeypatch.setattr(pv, "_trusted_exe", lambda exe: True)
    listening = socket.socket(socket.AF_UNIX)
    listening.bind("server")
    listening.listen()
    theirs = pv.client_version(sys.executable) if matches else "tmux 9.9"
    with listening:
        if matches:
            picked = pv.client_for("tmux", "tmux 3.8", pv.Path("server"), theirs)
            # This process's own executable (a framework Python runs Python.app's, not sys.executable).
            assert pv.Path(picked).resolve() == pv._exe_of(os.getpid()).resolve()
        else:
            with pytest.raises(pv.ViewError):
                pv.client_for("tmux", "tmux 3.8", pv.Path("server"), theirs)


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_a_view_that_fails_once_its_session_is_made_leaves_no_session(db, monkeypatch):
    tmux._tmux("new-session", "-d", "-s", "agent", "cat")
    real = pv._tmux

    def prepared_then_hung(server, *args, **kw):
        out = real(server, *args, **kw)
        if "new-session" in args:   # the view session made, then tmux took too long to answer
            raise pv.ViewError("couldn't run tmux: timed out")
        return out

    monkeypatch.setattr(pv, "_tmux", prepared_then_hung)
    with pytest.raises(pv.ViewError):
        pv.PtyView.open(tmux.current_server(), "=agent:", 80, 24)
    assert _views() == []


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_a_view_shows_the_pane_types_into_it_and_goes_when_closed(db):
    tmux._tmux("new-session", "-d", "-s", "agent", "-x", "80", "-y", "24", "cat")
    window = tmux._tmux("display-message", "-p", "-t", "=agent:", "#{window_id}").stdout.strip()
    tmux._tmux("set-hook", "-g", "session-closed", "wait-for -S view-closed")
    # A person's own terminal on the agent's session, 100x30.
    master, slave = pty.openpty()
    pv._set_size(slave, (100, 30), pv.Crop())
    env = {k: v for k, v in os.environ.items() if k != "TMUX"} | {"TERM": "xterm-256color"}
    other = None
    try:
        with TestClient(build_app(TOKEN)) as client:
            with client.websocket_connect("/v1/pty_view") as ws:
                ws.send_text(TOKEN)
                ws.send_json({"server": tmux.current_server(), "target": window, "cols": 60, "rows": 20})
                assert ws.receive_json() == {"crop": {"cols": 0, "left": 0, "width": None}}
                [view] = _views()
                # A view that stops drawing fails the test (its exit comes down) instead of hanging it.
                watchdog = threading.Timer(WATCHDOG_SECONDS, tmux._tmux, ("kill-session", "-t", f"={view}"))
                watchdog.start()
                try:
                    ws.send_json({"type": "input", "data": "typed-in-the-view\r"})
                    _until(ws, lambda seen: b"typed-in-the-view" in seen)

                    # The person's terminal typed in takes the window; the view focused takes it back.
                    other = subprocess.Popen([*tmux._argv(tmux.current_server()), "attach", "-t", "=agent"],
                                             stdin=slave, stdout=slave, stderr=slave, env=env,
                                             start_new_session=True)
                    os.write(master, b"x")
                    _until(ws, lambda _: _size(window) == "100x29")     # its status line takes a row
                    ws.send_json({"type": "focus"})
                    _until(ws, lambda _: _size(window) == "60x20")
                    ws.send_json({"type": "resize", "cols": 70, "rows": 22})
                    _until(ws, lambda _: _size(window) == "70x22")
                finally:
                    watchdog.cancel()
            tmux._tmux("wait-for", "view-closed")
    finally:
        if other is not None:
            other.kill()
            other.wait()
        os.close(master)
        os.close(slave)
    assert _views() == []
    assert tmux._tmux("display-message", "-p", "-t", window, "#{window_id}").stdout.strip() == window
