import json
import time

import pytest
from typer.testing import CliRunner

from brindle import workspaces
from brindle.cli import app
from brindle.db import Agent

from conftest import sh


@pytest.mark.parametrize("command", [[], ["start", "--no-attach"], ["supervisor", "--no-attach"]])
@pytest.mark.parametrize("flag", ["--no-autopilot", pytest.param("--autopilot", marks=pytest.mark.variant("deprecated positive spelling"))])
def test_deprecated_driver_flags_still_start_a_goal_session(db, repo, monkeypatch, command, flag):
    from brindle import agents, cli, config

    monkeypatch.chdir(repo)
    config.set_local(str(repo), "autopilot", False)
    monkeypatch.setattr(cli, "_resolve_supervisor", lambda *a, **k: "claude")
    for name in ("_preflight", "_trust_codex_hook", "_pause_transferred", "_check_updates",
                 "_cull_detached", "_local_models_detached", "_settings_sync_detached", "_attach"):
        monkeypatch.setattr(cli, name, lambda *a, **k: None)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    monkeypatch.setattr(agents, "_launch", lambda *a, **k: None)
    result = CliRunner().invoke(app, [*command, flag, "--yes"])
    assert result.exit_code == 0, result.output
    roots = db.conn.execute("SELECT root_id FROM autopilot").fetchall()
    assert len(roots) == 1
    assert db.get_agent(roots[0][0]).profile == "supervisor"
    assert db.get_autopilot(roots[0][0]).enabled
    assert config.load_repo_config(str(repo)).autopilot


@pytest.mark.variant("guards the suite against launching the real agent CLI")
@pytest.mark.parametrize("argv, override", [
    (["claude", "--version"], None),
    (["/usr/bin/env", "-u", "X", "A=b", "--", "codex"], None),
    (["/usr/bin/true"], "/usr/bin/true"),
    (["brindle", "_headless", "abc"], "/usr/bin/true"),
])
def test_unmocked_agent_cli_launch_raises(argv, override, monkeypatch, tmp_path):
    import subprocess

    from brindle import tmux

    if override:
        monkeypatch.setenv("BRINDLE_CLAUDE_BIN", override)
    launch = ((lambda: tmux.new_window("s", "w", str(tmp_path), argv)) if "_headless" in argv
              else (lambda: subprocess.Popen(argv)))
    with pytest.raises(AssertionError, match="test_unmocked_agent_cli_launch_raises"):
        launch()


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


@pytest.mark.parametrize("check,pushed,said", [
    ("echo boom; false", False, "checks failed: echo boom; false"),
    ("git -c user.name=a -c user.email=a@a commit --allow-empty -qm moved", False, "changed while its checks ran"),
    ("grep -q fixed app.py; r=$?; git checkout -q app.py; exit $r", False, "uncommitted changes"),   # passes only dirty
    ("true", True, ""),
], ids=["fails", "head-moves", "dirty-start", "passes"])
def test_pr_runs_the_checks_before_pushing(db, repo, monkeypatch, check, pushed, said):
    from brindle import config

    ws = workspaces.create(db, str(repo), "feat/x").workspace
    config.set_local(str(repo), "checks", [check])
    if "dirty" in said or "fixed" in check:
        sh("echo fixed >> app.py", ws.path)
    verified = sh("git rev-parse HEAD", ws.path)
    opened = []

    def fake_pull_request(ws, title, draft, **kw):
        opened.append(kw["sha"])    # a commit landing now must not be what is pushed
        sh("git -c user.name=a -c user.email=a@a commit --allow-empty -qm late", ws.path)
        return "https://gh.test/pr/1"

    monkeypatch.setattr(workspaces, "pull_request", fake_pull_request)
    monkeypatch.setattr(workspaces, "keeps_merged_branches", lambda root: False)
    monkeypatch.chdir(ws.path)
    res = CliRunner().invoke(app, ["pr"])
    assert (opened == [verified], res.exit_code == 0) == (pushed, pushed) and said in res.output


def test_version_flag_prints_the_installed_version():
    from typer.testing import CliRunner

    from brindle import __version__
    from brindle.cli import app

    out = CliRunner().invoke(app, ["--version"])
    assert out.exit_code == 0 and out.output.strip() == f"brindle {__version__}"
    assert __version__ != "unknown"


def test_app_command_opens_the_app_or_prints_the_download_url(monkeypatch):
    import subprocess
    import sys

    from brindle import cli

    monkeypatch.setattr(sys, "platform", "linux")
    runs = []
    no_opener = []

    def run(cmd, **kw):
        runs.append(cmd)
        if no_opener:
            raise OSError("no opener")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", run)
    installed = CliRunner().invoke(cli.app, ["app"])
    assert runs == [["xdg-open", "brindle://"]]
    assert cli.APP_DOWNLOAD_URL not in installed.output

    no_opener.append(True)
    missing = CliRunner().invoke(cli.app, ["app"])
    assert cli.APP_DOWNLOAD_URL in missing.output
