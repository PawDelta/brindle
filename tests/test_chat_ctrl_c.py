"""Ctrl+C in a brindle chat pane must reach the program in it (a raw-mode TUI
such as Claude Code reads it as a key, not a signal)."""

import fcntl
import os
import pty
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest

from brindle import agents, tmux

RAW_PROGRAM = """
import os, sys, tty, signal
out = sys.argv[1]
def log(s):
    with open(out, "a") as f:
        f.write(s + "\\n")
signal.signal(signal.SIGINT, lambda *a: log("SIGINT"))
tty.setraw(0)
log("ready")
while True:
    b = os.read(0, 1)
    log("byte %d" % b[0])
    if b == b"q":
        break
"""


COOKED_PROGRAM = """
import sys, signal, time
out = sys.argv[1]
def log(s):
    with open(out, "a") as f:
        f.write(s + "\\n")
signal.signal(signal.SIGINT, lambda *a: log("SIGINT"))
log("ready")
while True:
    time.sleep(0.1)
"""


@pytest.fixture
def session(tmp_path):
    name = f"cc-{uuid.uuid4().hex[:6]}"
    tmux.ensure_session(name, str(tmp_path), {})
    tmux.apply_theme(name)
    yield name
    tmux.kill_session(name)


def _wait(path, text, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and text in path.read_text().split("\n"):
            return True
        time.sleep(0.1)
    return False


def test_ctrl_c_reaches_a_raw_mode_program_in_a_brindle_chat_pane(session, tmp_path):
    out = tmp_path / "out.log"
    prog = tmp_path / "raw.py"
    prog.write_text(RAW_PROGRAM)
    argv = agents._pause_when_done("none", [sys.executable, str(prog), str(out)])
    chat = tmux.new_window(session, "chat", str(tmp_path), argv, {})
    side = tmux.split_left(chat, str(tmp_path), ["sleep", "60"], {})
    tmux._tmux("select-pane", "-t", chat)
    assert _wait(out, "ready")
    tmux.send_keys(chat, "C-c")
    assert _wait(out, "byte 3"), out.read_text()
    assert "SIGINT" not in out.read_text()
    assert side


def test_ctrl_c_reaches_the_chat_after_a_drag_left_it_in_copy_mode(session, tmp_path):
    """Root cause of the dead Ctrl+C: a drag in the chat pane enters copy mode
    and, when the release lands outside the pane, never leaves it; there tmux
    binds C-c to cancel, so the chat program never saw the key. Driven through a
    real attached client (mouse and key bytes on a pty), as a terminal would."""
    out = tmp_path / "out.log"
    prog = tmp_path / "raw.py"
    prog.write_text(RAW_PROGRAM)
    argv = agents._pause_when_done("none", [sys.executable, str(prog), str(out)])
    chat = tmux.new_window(session, "chat", str(tmp_path), argv, {})
    tmux.split_left(chat, str(tmp_path), ["sleep", "60"], {})
    tmux._tmux("select-pane", "-t", chat)
    tmux._tmux("select-window", "-t", chat)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("TMUX", None)
    client = subprocess.Popen([*tmux._argv(tmux.current_server()), "attach", "-t", f"={session}"],
                              stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    try:
        assert _wait(out, "ready")
        time.sleep(1)

        def in_mode() -> str:
            return tmux._tmux("display-message", "-p", "-t", chat, "#{pane_in_mode}").stdout.strip()

        os.write(master, b"\x1b[<0;60;10M")    # press in the chat pane
        time.sleep(0.2)
        os.write(master, b"\x1b[<32;62;10M")   # drag: copy mode
        time.sleep(0.2)
        os.write(master, b"\x1b[<32;10;10M")   # drag into the sidebar
        time.sleep(0.2)
        os.write(master, b"\x1b[<0;10;10m")    # release outside the chat pane
        time.sleep(0.5)
        assert in_mode() == "1"  # the premise: the chat pane is stuck in copy mode
        os.write(master, b"\x03")
        assert _wait(out, "byte 3"), out.read_text()
        assert in_mode() == "0"
    finally:
        client.kill()
        client.wait()
        os.close(master)
        os.close(slave)


def test_ctrl_c_signals_a_cooked_mode_program_in_a_brindle_chat_pane(session, tmp_path):
    out = tmp_path / "out.log"
    prog = tmp_path / "cooked.py"
    prog.write_text(COOKED_PROGRAM)
    argv = agents._pause_when_done("none", [sys.executable, str(prog), str(out)])
    chat = tmux.new_window(session, "chat", str(tmp_path), argv, {})
    tmux.split_left(chat, str(tmp_path), ["sleep", "60"], {})
    tmux._tmux("select-pane", "-t", chat)
    assert _wait(out, "ready")
    tmux.send_keys(chat, "C-c")
    assert _wait(out, "SIGINT"), out.read_text()
    assert tmux.window_alive(chat)  # the wrapper survives; the program decides
