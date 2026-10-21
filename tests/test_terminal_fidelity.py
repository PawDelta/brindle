"""The app's terminal shows exactly what tmux shows: a real pane running a
small TUI, streamed through the daemon's terminal route and rendered by a
headless terminal (pyte) as the app's xterm.js renders it, matches tmux's
screen and cursor, and keys land at that cursor, whether the app sizes the
pane or a terminal of another size shares it."""

import fcntl
import json
import os
import queue
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

import pyte
import pytest
from starlette.testclient import TestClient

from brindle import tmux
from brindle.daemon import terminal
from brindle.daemon.app import build_app

TOKEN = "t" * 43
APP = (80, 24)      # the app's xterm, as its fit addon sizes it

# Like Claude Code's TUI: a long line that wraps, a rule as wide as the
# terminal, a box with the cursor put in it by cursor addressing, keys
# echoed there; all redrawn on SIGWINCH.
TUI = r'''
import os, signal, tty
tty.setraw(0)
typed, out = "", lambda s: os.write(1, s.encode())
def draw(*_):
    cols, rows = os.get_terminal_size(0)
    out("\x1b[2J\x1b[H" + "a long line that wraps at the edge of a narrow terminal " * 3)
    out(f"\x1b[{rows - 3};1H" + "─" * cols + "╭" + "─" * (cols - 2) + "╮")
    out(f"\x1b[{rows - 1};1H│ > {typed}\x1b[{rows - 1};{cols}H│╰" + "─" * (cols - 2) + "╯")
    out(f"\x1b[{rows - 1};{5 + len(typed)}H")
out("".join(f"history {i}\r\n" for i in range(40)))
signal.signal(signal.SIGWINCH, draw)
draw()
while True:
    key = os.read(0, 64).decode()
    typed += key
    out(key)
'''


class Client:
    """``tmux attach`` in a terminal window of ``cols`` x ``rows``."""

    def __init__(self, session: str, cols: int, rows: int) -> None:
        self.master, slave = os.openpty()
        self.size(cols, rows)
        self.proc = subprocess.Popen(
            [*tmux._base(), "attach-session", "-t", f"={session}"], stdin=slave, stdout=slave,
            stderr=slave, env={**tmux.control_env(), "TERM": "xterm-256color"}, start_new_session=True)
        os.close(slave)
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        try:
            while os.read(self.master, 1 << 16):
                pass
        except OSError:     # the client is gone with the test's tmux server
            os.close(self.master)

    def size(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if hasattr(self, "proc"):
            os.kill(self.proc.pid, signal.SIGWINCH)


class App:
    """The app's Terminal.tsx: an xterm of the app's size that sends that
    size once the hello comes and shows the pane at the size the daemon
    says it is."""

    def __init__(self, sock) -> None:
        self.sock, self.frames = sock, queue.Queue()
        self.screen = pyte.Screen(*APP)
        self.stream = pyte.ByteStream(self.screen)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                self.frames.put(self.sock.receive())
        except Exception:   # the socket closed
            pass

    def view(self) -> tuple:
        """What it shows, once the frames that came (within a moment) are in."""
        while True:
            try:
                m = self.frames.get(timeout=0.05)
            except queue.Empty:
                break
            if m.get("bytes") is not None:
                self.stream.feed(m["bytes"])
                continue
            m = json.loads(m["text"])
            if "cols" in m:
                self.screen.resize(m["rows"], m["cols"])
            if m["type"] == "terminal":
                self.sock.send_json({"type": "resize", "cols": APP[0], "rows": APP[1]})
        s = self.screen
        return [r.rstrip() for r in s.display], (s.cursor.x, s.cursor.y), (s.columns, s.lines)


def tmux_view(pane: str) -> tuple:
    rows = tmux._tmux("capture-pane", "-p", "-t", pane).stdout.removesuffix("\n").split("\n")
    x, y, w, h = map(int, tmux._tmux("display-message", "-p", "-t", pane,
                                     "#{cursor_x} #{cursor_y} #{pane_width} #{pane_height}").stdout.split())
    return [r.rstrip() for r in rows], (x, y), (w, h)


def wait(pred) -> None:
    """Until ``pred`` holds; each look asks tmux, which paces the loop."""
    deadline = time.monotonic() + 10
    while not pred():
        assert time.monotonic() < deadline


def see(app: App, pane: str, size: tuple[int, int], typed: str = "") -> None:
    """Until the app shows exactly what tmux shows: the TUI at ``size`` with
    ``typed`` in its box and the cursor right after it."""
    deadline = time.monotonic() + 10
    while True:
        ours, theirs = app.view(), tmux_view(pane)
        rows, (x, y), at = theirs
        if ours == theirs and at == size and "╰" in rows[-1] and rows[y][:x] == "│ > " + typed:
            return
        assert time.monotonic() < deadline, "\n".join(["app:", *ours[0], str(ours[1:]),
                                                       "tmux:", *theirs[0], str(theirs[1:])])


@pytest.mark.serial
@pytest.mark.parametrize("shared,control", [(None, True), ((120, 30), True), ((120, 30), False)])
def test_the_apps_terminal_shows_what_tmux_shows_and_keys_land_at_its_cursor(tmp_path, monkeypatch,
                                                                             shared, control):
    monkeypatch.setattr(terminal, "TAKE_SECONDS", 0.1, raising=False)
    if not control:     # no tmux control mode: the pane is polled
        async def refused(argv):
            raise OSError("no control mode")
        monkeypatch.setattr(terminal, "spawn", refused)
    (tmp_path / "tui.py").write_text(TUI)
    tid = terminal.open_action("fidelity", [sys.executable, str(tmp_path / "tui.py")])["terminal"]
    pane = terminal._find_action(tid)["target"]
    if shared:      # a terminal of another size is attached, showing the pane
        client = Client(terminal.ACTION_SESSION, *shared)
        wait(lambda: tmux._tmux("select-window", "-t", pane, check=False).returncode == 0
             and tmux_view(pane)[2] == (shared[0], shared[1] - 1))
    with TestClient(build_app(TOKEN)) as http:
        sock = http.websocket_connect(f"/v1/terminal/{tid}").__enter__()
        sock.send_text(TOKEN)
        try:
            app = App(sock)
            see(app, pane, APP)     # the app's size, from its resize, shared or not
            if shared:
                # The terminal types: like tmux's latest client, it takes the
                # window, and the app shows it at the terminal's size. tmux
                # keeps input times to the second, so that is a second later.
                threading.Event().wait(1.05 - time.time() % 1)
                os.write(client.master, b"t")
                see(app, pane, (shared[0], shared[1] - 1), "t")
                client.size(100, 35)
                see(app, pane, (100, 34), "t")
            # Keys typed in the app land at the cursor (and take the window back).
            sock.send_json({"type": "key", "text": "hi"})
            see(app, pane, APP, "thi" if shared else "hi")
        finally:
            sock.__exit__(None, None, None)
        # The daemon lets its control client go before the loop does.
        wait(lambda: "control-mode" not in tmux._tmux("list-clients", "-F", "#{client_flags}").stdout)
