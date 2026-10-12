"""An agent's argv (its whole system prompt and first message) can exceed
what tmux accepts in one command ("command too long"): such a command runs
through a private launch script instead."""
import time

import pytest

from brindle import tmux


@pytest.fixture
def session(tmp_path):
    name = "brindle_longcmd"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_the_launch_script_is_private_and_gone_once_started(session, tmp_path, brindle_home):
    out = tmp_path / "done"
    tmux.new_window(session, "long", str(tmp_path), ["/bin/sh", "-c", 'touch "$1"', "sh", str(out),
                                                     "y" * 20_000], {})
    deadline = time.time() + 10
    while time.time() < deadline and not out.exists():
        time.sleep(0.1)
    assert out.exists() and list((brindle_home / "launch").iterdir()) == []


