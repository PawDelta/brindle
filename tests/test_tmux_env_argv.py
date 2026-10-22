"""An agent's environment (a profile's API key, the federation proxy's token)
must never be a tmux ``-e NAME=VALUE`` argument: any user can read those in
``ps``. It reaches the pane through a private launch script instead."""
import time

import pytest

from brindle import tmux


def test_a_stored_key_never_reaches_argv_or_the_tmux_environment(db, repo, monkeypatch, brindle_home):
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


def test_tmux_runs_in_utf8_mode_even_without_a_locale(monkeypatch):
    # launchd starts the engine with no LANG/LC_*; tmux then mangles non-ASCII
    # output unless -u is passed, so every call (new-session included) gets it.
    argvs = []

    class Done:
        stdout, stderr, returncode = "", "", 0

    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(tmux.subprocess, "run", lambda argv, **k: argvs.append(argv) or Done())
    monkeypatch.delenv("BRINDLE_TMUX_SOCKET", raising=False)
    monkeypatch.delenv("BRINDLE_TMUX_BIN", raising=False)
    tmux._tmux("new-session", "-d", "-s", "x")
    assert argvs[-1][:2] == ["tmux", "-u"]
    assert tmux.attach_command("x")[:2] == ["tmux", "-u"]


def test_a_bad_variable_name_is_refused(brindle_home):
    with pytest.raises(tmux.TmuxError):
        tmux._short(["true"], {"A B; rm -rf /": "x"})
