"""An agent's environment (a profile's API key, the federation proxy's token)
must never be a tmux ``-e NAME=VALUE`` argument: any user can read those in
``ps``. It reaches the pane through a private launch script instead."""
import os
import stat
import time

import pytest

from brindle import tmux


@pytest.fixture
def session(tmp_path):
    name = "brindle_envargv"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_env_values_never_reach_tmuxs_argv(tmp_path, monkeypatch):
    seen = []

    class Done:
        stdout, stderr, returncode = "%1\n", "", 0

    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: seen.append(a) or Done())
    monkeypatch.setattr(tmux, "inherited_names", lambda s: set())
    tmux.new_window("s", "w", str(tmp_path), ["true"], {"ANTHROPIC_AUTH_TOKEN": "sekrit-token"})
    argv = [a for call in seen for a in call]
    assert not any("sekrit-token" in a for a in argv) and "-e" not in argv


def test_a_stored_key_never_reaches_argv_or_the_tmux_environment(db, repo, monkeypatch, brindle_home):
    import time

    from brindle import agents, keystore, workspaces
    from brindle.db import Agent

    fake = "sk-ant-test-stored-9999"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    keystore.set_key("ANTHROPIC_API_KEY", fake)
    seen = []

    class Done:
        stdout, stderr, returncode = "%1\n", "", 0

    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: seen.append(a) or Done())
    monkeypatch.setattr(tmux, "inherited_names", lambda s: set())
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "apply_theme", lambda *a, **k: None)
    ws = workspaces.create(db, str(repo), "argv").workspace
    a = Agent("c1", ws.id, "developer", "claude", None, "assign", "starting", "", None, time.time())
    db.add_agent(a)
    agents._open_window(db, a, ws, "c1", ["true"], watch_pane=False)
    argv = [x for call in seen for x in call]
    assert not any(fake in x for x in argv) and "-e" not in argv
    assert "set-environment" not in argv
    # it travels in the private launch script, which the shell deletes as it starts
    [script] = (brindle_home / "launch").iterdir()
    assert f"export ANTHROPIC_API_KEY={fake}" in script.read_text()


def test_the_launch_script_is_private_and_carries_the_env(tmp_path, brindle_home):
    path = tmux._short(["true"], {"K": "it's a 'secret' $x"})[1]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path) as f:
        assert "export K=" in f.read()


def test_a_pane_gets_its_env_and_the_script_is_gone(session, tmp_path, brindle_home):
    out = tmp_path / "out.txt"
    tmux.new_window(session, "e", str(tmp_path),
                    ["/bin/sh", "-c", 'printf "%s" "$BRINDLE_TEST_KEY" > "$1"', "sh", str(out)],
                    {"BRINDLE_TEST_KEY": "it's a 'secret' $x"})
    deadline = time.time() + 10
    while time.time() < deadline and not out.exists():
        time.sleep(0.1)
    assert out.read_text() == "it's a 'secret' $x"
    assert list((brindle_home / "launch").iterdir()) == []


def test_a_bad_variable_name_is_refused(brindle_home):
    with pytest.raises(tmux.TmuxError):
        tmux._short(["true"], {"A B; rm -rf /": "x"})
