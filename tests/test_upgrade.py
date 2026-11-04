import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

import brindle
from brindle import agents, mcp_server, procs, profiles, upgrade, watch


@pytest.mark.parametrize("exit_kind", ["broken_pipe", "eof"])
def test_mcp_transport_failure_delivers_recovery_without_mcp(db, monkeypatch, exit_kind):
    from brindle.db import Agent, Workspace

    db.add_workspace(Workspace("ws", "/repo", "root", "root", "main", None, "/repo", None, "", 0))
    db.add_agent(Agent("boss", "ws", "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, 0, headless=1))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(agents, "is_alive", lambda *a: True)
    monkeypatch.setattr(agents, "pulls_messages", lambda *a: True)
    stopped = []
    monkeypatch.setattr(mcp_server, "start_azure_refresher",
                        lambda: SimpleNamespace(stop=lambda: stopped.append(True)))
    notification_db = mcp_server.DB()
    monkeypatch.setattr(mcp_server, "DB", lambda: notification_db)

    async def disconnect(handover):
        raise BrokenPipeError("client pipe closed")

    if exit_kind == "broken_pipe":
        monkeypatch.setattr(mcp_server, "_serve_stdio", disconnect)
        with pytest.raises(BrokenPipeError):
            mcp_server.main()
    else:
        stdin, client = os.pipe()
        os.close(client)
        monkeypatch.setattr("brindle.mcp_stdio.claim_stdio", lambda: (stdin, os.open(os.devnull, os.O_WRONLY)))
        mcp_server.main()
    assert stopped == [True]
    assert db.pop_pending("boss") is not None
    assert db.unread_count("boss") == 0
    with pytest.raises(sqlite3.ProgrammingError):
        notification_db.conn.execute("SELECT 1")


def test_an_upgrade_under_the_mcp_server_leaves_it_serving_the_version_it_started_with(monkeypatch, tmp_path):
    install = tmp_path / "site-packages" / "brindle"    # the built-ins, where an upgrade can replace them
    for package in profiles._BUILTIN_PACKAGES:
        shutil.copytree(profiles.resources.files(package), install / package.rsplit(".", 1)[-1])
    monkeypatch.setattr(profiles, "resources", SimpleNamespace(files=lambda p: install / p.rsplit(".", 1)[-1]))
    monkeypatch.setattr(profiles, "_frozen", None)
    # A module the server only imports lazily, not loaded yet when it starts.
    monkeypatch.delitem(sys.modules, "brindle.doctor", raising=False)
    monkeypatch.delattr(brindle, "doctor", raising=False)
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, SimpleNamespace(repo_root=None)))
    monkeypatch.setattr("brindle.providers.unusable", lambda provider, env: None)
    exits = []
    monkeypatch.setattr(upgrade.os, "_exit", exits.append)

    def upgrade_install():
        monkeypatch.setattr(upgrade, "_stamp", lambda: None)     # brindle/__init__.py being rewritten
        (install / "builtin_agents" / "developer-heavy.md").unlink()
        (install / "builtin_agents" / "developer.md").write_text("---\nprovider: half-writ")
        shutil.rmtree(install / "builtin_rules")

    async def session():
        async with Client(mcp_server.mcp) as client:
            await client.list_tools()
            assert "brindle.doctor" in sys.modules     # loaded before an upgrade could replace it
            upgrade_install()
            return [await client.call_tool("list_agent_profiles", {}) for _ in range(2)]

    for result in asyncio.run(session()):
        assert not result.is_error
        assert "developer-heavy (claude)" in result.content[0].text
        assert "developer (claude)" in result.content[0].text
    assert exits == []


def test_an_upgraded_sidebar_runs_again_in_its_pane(monkeypatch):
    monkeypatch.setattr(watch, "_log_to_file", lambda: None)
    monkeypatch.setattr("brindle.spawn.exit_on_hangup", lambda: None)
    monkeypatch.setattr(watch.curses, "wrapper", lambda fn, root, sidebar, *_: watch.RESTART)
    monkeypatch.setattr(upgrade.sys, "argv", ["brindle", "watch", "--sidebar"])
    monkeypatch.setattr("brindle.providers.brindle_invocation", lambda: ["/venv/bin/brindle"])
    execs = []
    monkeypatch.setattr(upgrade.os, "execv", lambda path, argv: execs.append(argv))

    watch.run("/repo", sidebar=True)
    assert execs == [["/venv/bin/brindle", "watch", "--sidebar"]]


def test_sidebar_upgrade_waits_for_the_new_install_instead_of_exiting(monkeypatch):
    """uv removes the old venv before the new one is in place: an exec in that
    window fails, and the sidebar must wait for it rather than die with its pane."""
    monkeypatch.setattr(upgrade.sys, "argv", ["brindle", "watch", "--sidebar"])
    monkeypatch.setattr("brindle.providers.brindle_invocation", lambda: ["/venv/bin/brindle"])
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("shutil.which", lambda command: None)
    attempts = []

    def execv(path, argv):
        attempts.append(argv)
        if len(attempts) < 3:
            raise FileNotFoundError(path)

    monkeypatch.setattr(upgrade.os, "execv", execv)

    upgrade.reexec()
    assert len(attempts) == 3

    monkeypatch.setattr(watch, "_log_to_file", lambda: None)
    monkeypatch.setattr("brindle.spawn.exit_on_hangup", lambda: None)
    monkeypatch.setattr(watch.curses, "wrapper", lambda *a: watch.RESTART)
    daemon = SimpleNamespace(command=None, clock=0)
    monkeypatch.setattr(upgrade.time, "monotonic", lambda: daemon.clock)
    monkeypatch.setattr("shutil.which", lambda command: daemon.command)

    def replaced_engine(path, argv):
        if path != "/replacement/bin/brindle":
            assert daemon.clock == 0, "sidebar kept using the removed engine"
            daemon.clock += 180
            daemon.command = "/replacement/bin/brindle"
            raise FileNotFoundError(path)
        attempts.append(argv)

    monkeypatch.setattr(upgrade.os, "execv", replaced_engine)
    watch.run("/repo", sidebar=True)
    assert attempts[-1] == ["/replacement/bin/brindle", "watch", "--sidebar"]


@pytest.mark.variant("real incident: a same-version uv reinstall hardlinks the cached file, so sidebars never restarted")
def test_sidebar_notices_an_upgrade_that_relinks_the_same_cached_files(monkeypatch, tmp_path):
    """Reinstalling the same build links the same cached file: its inode, mtime
    and size don't change, only the package directory is made again."""
    cache = tmp_path / "cache.py"
    cache.write_text("")
    pkg = tmp_path / "brindle"
    pkg.mkdir()
    (pkg / "__init__.py").hardlink_to(cache)
    monkeypatch.setattr(brindle, "__file__", str(pkg / "__init__.py"))
    watch_ = upgrade.InstallWatch()

    (pkg / "__init__.py").unlink()
    pkg.rmdir()
    pkg.mkdir()
    (pkg / "__init__.py").hardlink_to(cache)
    assert watch_.changed()


@pytest.mark.variant("a module hijack attempt; what it guards runs in the child process")
def test_a_brindle_package_in_the_cwd_is_not_what_brindle_runs_again(monkeypatch, tmp_path):
    """A sidebar re-execs, and hooks run brindle, from inside a worktree: a
    `brindle/` a worker wrote there must not shadow the install."""
    (tmp_path / "brindle").mkdir()
    (tmp_path / "brindle" / "__init__.py").write_text("raise SystemExit('decoy imported')")
    monkeypatch.setattr("brindle.providers._in_worktree", lambda path: False)
    from brindle.providers import brindle_invocation

    run = subprocess.run([*brindle_invocation(), "--version"], cwd=tmp_path, capture_output=True, text=True)
    assert "decoy" not in run.stderr
    assert run.returncode == 0


def test_a_newer_daemon_stops_the_mcp_servers_an_update_left_on_old_code(monkeypatch, tmp_path):
    """Claude Code starts a stdio server that exited again, on the install as
    it is now. Stopped: one on a replaced executable. Left: one that reloads
    itself, one busy with a process of its own (to look at again), one on
    the install as it is, another brindle home's."""
    python = tmp_path / "python3"
    python.write_text("")
    monkeypatch.setattr(upgrade.sys, "executable", str(python))
    monkeypatch.setattr(procs, "_running_executable", lambda pid: os.stat(python) if pid == 4 else procs._DELETED)
    cmd = f"{python} -P -m brindle mcp"
    table = {pid: procs.Proc(pid, 0, f"{cmd} BRINDLE_HOME={os.environ['BRINDLE_HOME']}", cmd) for pid in (1, 2, 3, 4)}
    table |= {30: procs.Proc(30, 3, "git", "git"), 5: procs.Proc(5, 0, f"{cmd} BRINDLE_HOME={tmp_path}", cmd)}
    monkeypatch.setattr(procs, "table", lambda: table)
    marks = upgrade._reloading_dir()
    marks.mkdir(parents=True)
    (marks / "2").touch()
    (marks / "99").touch()      # a server that died without unmarking itself
    stopped = []
    monkeypatch.setattr(procs, "terminate", stopped.append)

    assert upgrade.stop_stale_mcp_servers() is True
    assert stopped == [{1}] and [m.name for m in marks.iterdir()] == ["2"]


def test_a_process_left_on_a_moved_or_deleted_executable_is_told_apart(tmp_path):
    """What the app updater does to a running engine: its .app is moved away
    (then deleted) and a new one put at its path."""
    real = os.path.realpath(sys.executable)
    me = procs._running_executable(os.getpid())
    if me is None or me is procs._DELETED or (me.st_dev, me.st_ino) != (os.stat(real).st_dev, os.stat(real).st_ino):
        pytest.skip("this Python runs through a launcher that execs another file (a framework build), "
                    "so a copy of it can't stand in for the app's engine")
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    shutil.copy(real, python)
    (tmp_path / "lib").symlink_to(os.path.join(os.path.dirname(os.path.dirname(real)), "lib"))
    engine = subprocess.Popen([python, "-c", "print(flush=True); input()"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        engine.stdout.readline()        # it runs
        running = procs.Proc(engine.pid, 0, str(python), str(python))
        assert not procs.runs_replaced_executable(running)
        python.rename(tmp_path / "moved")
        shutil.copy(real, python)
        assert procs.runs_replaced_executable(running)
        (tmp_path / "moved").unlink()
        assert procs.runs_replaced_executable(running)
    finally:
        engine.stdin.close()
        engine.wait()


def test_a_stale_install_is_told_to_clients_and_stops_the_daemon_before_a_launch(monkeypatch):
    fake = SimpleNamespace(changed=lambda: True)
    stops = []
    monkeypatch.setattr(upgrade, "install", fake)
    monkeypatch.setattr(upgrade, "on_stale_launch", lambda: stops.append(1))
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, SimpleNamespace(repo_root=None)))

    with pytest.raises(agents.AgentError):
        agents._launch(None, None, None, prompt=None, resume=None, watch_pane=False)
    assert stops == [1]
    compared = []
    monkeypatch.setattr(upgrade, "install", SimpleNamespace(changed=lambda: compared.append(1) or True))
    monkeypatch.setattr(upgrade, "on_stale_launch", None)
    with pytest.raises(AttributeError):     # past the check, into launch setup (ws is None)
        agents._launch(None, None, None, prompt=None, resume=None, watch_pane=False)
    assert compared == [1]
    ok = asyncio.run(mcp_server.mcp.call_tool("list_repos", {}))
    assert mcp_server.STALE_NOTICE in [c.text for c in ok.content]
    with pytest.raises(ToolError, match="run /mcp to reconnect"):
        asyncio.run(mcp_server.mcp.call_tool("no_such_tool", {}))
