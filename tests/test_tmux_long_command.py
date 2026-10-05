"""An agent's argv (its whole system prompt and first message) can exceed
what tmux accepts in one command ("command too long"): such a command runs
through a private launch script instead."""
import time

import pytest

from copse import tmux


@pytest.fixture
def session(tmp_path):
    name = "copse_longcmd"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_a_command_too_long_for_tmux_still_runs(session, tmp_path):
    out = tmp_path / "out.txt"
    big = "x" * 20_000                       # well past tmux's ~16 KB message limit
    cmd = ["/bin/sh", "-c", 'printf "%s" "$1" > "$2"', "sh", big, str(out)]
    tmux.new_window(session, "long", str(tmp_path), cmd, {})
    deadline = time.time() + 10
    while time.time() < deadline and not (out.exists() and out.stat().st_size == len(big)):
        time.sleep(0.1)
    assert out.read_text() == big            # the argument arrived intact


def test_the_launch_script_is_private_and_gone_once_started(session, tmp_path, copse_home):
    out = tmp_path / "done"
    tmux.new_window(session, "long", str(tmp_path), ["/bin/sh", "-c", 'touch "$1"', "sh", str(out),
                                                     "y" * 20_000], {})
    deadline = time.time() + 10
    while time.time() < deadline and not out.exists():
        time.sleep(0.1)
    assert out.exists() and list((copse_home / "launch").iterdir()) == []


def test_short_commands_are_passed_as_they_are():
    assert tmux._short(["claude", "--model", "x"]) == ["claude", "--model", "x"]
