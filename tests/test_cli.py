import json
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, cli, workspaces
from brindle.cli import app
from brindle.db import Agent

from conftest import sh


@pytest.mark.variant("guards the suite against launching the real agent CLI")
@pytest.mark.parametrize("argv", [["claude", "--version"], ["/usr/bin/env", "codex"]])
def test_unmocked_agent_cli_launch_raises(argv):
    import subprocess

    with pytest.raises(AssertionError, match="test_unmocked_agent_cli_launch_raises"):
        subprocess.Popen(argv)


@pytest.mark.parametrize("command", [[], ["new", "feat/provider"], ["start"], ["supervisor"]])
@pytest.mark.parametrize("flag", [
    "--supervisor",
    pytest.param("-s", marks=pytest.mark.variant("short form")),
    pytest.param("--provider", marks=pytest.mark.variant("existing scripts use the hidden alias")),
])
def test_supervisor_provider_rejects_worker_only_cli(db, repo, monkeypatch, command, flag):
    monkeypatch.chdir(repo)
    # Native is a worker provider; either spelling must reach supervisor validation.
    result = CliRunner().invoke(app, [*command, flag, "native"])
    assert result.exit_code == 1, result.output
    assert result.exception is not None and isinstance(result.exception, SystemExit)


def test_ls_json(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    sh("echo change >> app.py && touch new.txt", ws.path)
    db.add_agent(Agent("a1", ws.id, "developer", "claude", None, "interactive", "idle", "", None, time.time()))

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["ls", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert len(data) == 1
    entry = data[0]
    assert set(entry) == {"id", "name", "branch", "base_branch", "path", "ahead", "behind", "dirty", "agents"}
    assert entry["id"] == ws.id and entry["name"] == "feat-x"
    assert entry["branch"] == "feat/x" and entry["base_branch"] == "main"
    assert entry["path"] == ws.path
    assert (entry["ahead"], entry["behind"], entry["dirty"]) == (0, 0, 2)
    # No tmux window, so the agent reads as exited.
    assert entry["agents"] == [
        {"id": "a1", "profile": "developer", "provider": "claude", "status": "exited", "mode": "interactive"}
    ]


def test_rm_survives_a_missing_base_branch(db, repo, monkeypatch):
    sh("git branch tmp-base", repo)
    ws = workspaces.create(db, str(repo), "feat/x", "tmp-base", fetch=False).workspace
    sh("git branch -D tmp-base", repo)  # the base is gone, so git.status raises

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["rm", ws.id])
    assert res.exit_code == 0, res.output
    assert "removed" in res.output
    assert db.get_workspace(ws.id) is None


def test_version_flag_prints_the_installed_version():
    from typer.testing import CliRunner

    from brindle import __version__
    from brindle.cli import app

    out = CliRunner().invoke(app, ["--version"])
    assert out.exit_code == 0 and out.output.strip() == f"brindle {__version__}"
    assert __version__ != "unknown"


def test_continue_from_inside_session(db, repo, monkeypatch, fake_agent_cli):
    ws = workspaces.adopt_root(db, str(repo))
    caller = agents.spawn(db, ws, "developer", mode="interactive")
    target = agents.spawn(db, ws, "developer", mode="interactive")
    db.set_status(target.id, "paused")
    calls = []
    monkeypatch.setattr(agents, "is_alive", lambda a: a.id == caller.id)
    # The caller is the one live session; no tmux state decides that.
    monkeypatch.setattr(cli, "_live_sessions", lambda db_, ws_: [db.get_agent(caller.id)])
    monkeypatch.setattr(agents, "pause", lambda *a, **k: calls.append("pause"))
    monkeypatch.setattr(agents, "resume", lambda *a, **k: calls.append("resume") or [])
    monkeypatch.setenv("BRINDLE_AGENT_ID", caller.id)
    monkeypatch.chdir(repo)

    res = CliRunner().invoke(app, ["continue", target.id, "--no-attach"])
    assert res.exit_code == 1, res.output
    assert calls == []
