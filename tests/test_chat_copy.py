"""Select-then-copy in a brindle chat pane (Apple Terminal, Cmd+C): Terminal's
Cmd+C only copies its own native selection, never tmux's, so brindle puts a
drag on the clipboard itself, the moment the mouse is released, says so on screen and leaves copy mode, so
the next key goes straight to the chat. Driven through a
real attached client (SGR mouse bytes on a pty) with a mouse-tracking program
in the pane and a stub clip command."""

import fcntl
import os
import pty
import stat
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest

from brindle import agents, tmux

# What Claude Code does: asks for mouse tracking and reads raw keys.
TRACKING_PROGRAM = """
import os, sys, tty
out = sys.argv[1]
def log(s):
    with open(out, "a") as f:
        f.write(s + "\\n")
tty.setraw(0)
sys.stdout.write("\\x1b[2J\\x1b[H\\x1b[?1000h\\x1b[?1002h\\x1b[?1006hCOPYME-123 tail")
sys.stdout.flush()
log("ready")
while True:
    b = os.read(0, 1)
    log("byte %d" % b[0])
    if b == b"q":
        break
"""


@pytest.fixture
def session(tmp_path):
    name = f"cp-{uuid.uuid4().hex[:6]}"
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


def _wait_file(path, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.1)
    return ""


def test_copy_is_confirmed_on_screen_only_when_there_is_a_clipboard_tool():
    for key, cmd in tmux.chat_mouse_bindings("pbcopy").items():
        assert ("copied to the clipboard" in cmd) == (key in ("DoubleClick1Pane", "TripleClick1Pane")), key
    assert "display-message" not in tmux.chat_mouse_bindings(None)["DoubleClick1Pane"]


def test_drag_select_lands_on_the_clipboard_and_the_next_key_reaches_the_chat(session, tmp_path, monkeypatch):
    clipped = tmp_path / "clipboard.txt"
    stub = tmp_path / "clip.sh"
    stub.write_text(f"#!/bin/sh\ncat > {clipped}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(tmux, "clipboard_command", lambda: str(stub))
    tmux.bind_session_keys(session)

    out = tmp_path / "out.log"
    prog = tmp_path / "track.py"
    prog.write_text(TRACKING_PROGRAM)
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
        left, top = (int(x) for x in tmux._tmux(
            "display-message", "-p", "-t", chat, "#{pane_left} #{pane_top}").stdout.split())
        col, row = left + 1, top + 1  # SGR coordinates are 1-based

        def in_mode() -> str:
            return tmux._tmux("display-message", "-p", "-t", chat, "#{pane_in_mode}").stdout.strip()

        os.write(master, f"\x1b[<0;{col};{row}M".encode())          # press on the first character
        time.sleep(0.2)
        os.write(master, f"\x1b[<32;{col + 4};{row}M".encode())     # drag
        time.sleep(0.2)
        os.write(master, f"\x1b[<32;{col + 10};{row}M".encode())
        time.sleep(0.2)
        os.write(master, f"\x1b[<0;{col + 10};{row}m".encode())     # release
        text = _wait_file(clipped)
        assert text.startswith("COPYME-123"), repr(text)
        # The selection is the program's mouse, not forwarded to it.
        assert not any(ln.startswith("byte") for ln in out.read_text().split("\n")), out.read_text()

        os.write(master, b"x")                                      # the next key
        assert _wait(out, "byte 120"), out.read_text()
        assert in_mode() == "0"
    finally:
        client.kill()
        client.wait()
        os.close(master)
        os.close(slave)
