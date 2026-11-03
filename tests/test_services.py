"""Per-worktree Docker services, against a fake `docker` that records its argv."""

import json
import os
import stat

import pytest

from brindle import services, workspaces
from brindle.pro import license


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    exe = bindir / "docker"
    exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1" = ps ] && echo c0ffee\nexit 0\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return calls


@pytest.fixture
def entitled(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "services")


def configure(repo, services_cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"services": services_cfg}))


def test_started_on_create_and_in_workspace_env(db, repo, fake_docker, entitled):
    configure(repo, [{"name": "db", "preset": "postgres"}])
    ws = workspaces.create(db, str(repo), "feat-a").workspace
    clear, call = fake_docker()
    assert clear == f"rm -f {services.container_name(ws, 'db')}"  # a leftover never blocks `up`
    assert call.startswith("run -d --rm --name brindle-")
    assert f"--label brindle.workspace={ws.id}" in call
    assert f"-p 127.0.0.1:{ws.port_base + 1}:5432" in call
    assert "-e POSTGRES_PASSWORD=brindle" in call
    env = workspaces.workspace_env(ws)
    assert env["DATABASE_URL"].endswith(f":{ws.port_base + 1}/app")
    assert env["BRINDLE_SVC_DB_PORT"] == str(ws.port_base + 1)


def test_removed_on_remove(db, repo, fake_docker, entitled):
    configure(repo, [{"name": "db", "preset": "postgres"}])
    ws = workspaces.create(db, str(repo), "feat-a").workspace
    configure(repo, [])  # removal goes by label, even after the config changed
    workspaces.remove(db, ws)
    assert fake_docker()[-2:] == [f"ps -aq --filter label=brindle.workspace={ws.id}", "rm -f c0ffee"]


def test_no_containers_means_nothing_to_remove(monkeypatch):
    from types import SimpleNamespace

    def docker(args):
        assert args[0] == "ps", f"nothing to remove, yet ran docker {args}"
        return SimpleNamespace(returncode=0, stdout="", stderr="")     # lists no containers

    monkeypatch.setattr(services, "docker_path", lambda: "/fake/docker")
    monkeypatch.setattr(services, "_docker", docker)
    assert services.down(SimpleNamespace(id="ws-1")) == []


def test_not_entitled_runs_nothing(db, repo, fake_docker, monkeypatch, capsys):
    monkeypatch.setattr(license, "has", lambda feature: False)
    configure(repo, [{"name": "db", "preset": "postgres"}])
    ws = workspaces.create(db, str(repo), "feat-a").workspace
    assert fake_docker() == []
    assert "need brindle Pro" in capsys.readouterr().err
    assert "DATABASE_URL" not in workspaces.workspace_env(ws)

