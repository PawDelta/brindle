import asyncio
import json
import os
import select
import shutil
import subprocess
import sys
import sysconfig
import time
import venv
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brindle import db as brindle_db
from brindle import mcp_server, mcp_stdio, upgrade
from brindle.db import DB, SCHEMA_VERSION


@pytest.mark.parametrize("migration, incompatible", [
    ("ALTER TABLE workspaces ADD COLUMN from_a_newer_engine TEXT", None),
    ("ALTER TABLE workspaces ADD COLUMN from_a_newer_engine TEXT NOT NULL DEFAULT ''", None),
    ("ALTER TABLE workspaces ADD COLUMN from_a_newer_engine TEXT NOT NULL", "workspaces: from_a_newer_engine"),
    pytest.param("CREATE TABLE dev_only (required TEXT NOT NULL)", None,
                 marks=pytest.mark.variant("an undeclared dev table must not block this version's writes")),
    (f"PRAGMA user_version={SCHEMA_VERSION + 1}", "user_version"),
    ("ALTER TABLE workspaces DROP COLUMN session_root", "workspaces: session_root"),
])
def test_a_server_upgraded_under_refuses_tools_once_the_new_engine_migrated_the_database(
        monkeypatch, migration, incompatible):
    """Only schema changes that prevent correct writes block tools and reload probes."""
    from brindle import cli

    monkeypatch.setattr(upgrade, "install", SimpleNamespace(changed=lambda: False))
    ran = []
    monkeypatch.setattr(mcp_server, "_caller",
                        lambda db: ran.append(1) or (None, SimpleNamespace(repo_root="/repo")))
    db = DB()
    db.conn.execute(migration)
    db.conn.close()

    if incompatible:
        with pytest.raises(ToolError) as error:
            asyncio.run(mcp_server.mcp.call_tool("list_repos", {}))
        assert incompatible in str(error.value)
        assert ran == []
    else:
        asyncio.run(mcp_server.mcp.call_tool("list_repos", {}))
        assert ran == [1]
    assert brindle_db.migrated_by_newer() is bool(incompatible)
    assert mcp_server._wants_reload(False) is bool(incompatible)
    assert mcp_server._wants_reload(True) is False
    with pytest.raises(cli.typer.Exit) as exit:
        cli.mcp(check=True)
    assert exit.value.exit_code == int(bool(incompatible))


def _wire(monkeypatch, modern=False):
    """A client's pipes to a server, and the server's (faked) exec of the new
    install: it records that the process was replaced, as execv would.
    ``modern``: the client opens as Claude Code 2.1.318 does (see _modern)."""
    monkeypatch.setattr(upgrade, "preload", lambda: None)
    update = SimpleNamespace(installed=False)
    monkeypatch.setattr(upgrade, "install", SimpleNamespace(changed=lambda: update.installed))
    monkeypatch.setattr(upgrade, "stamp", lambda: update.installed)
    monkeypatch.delenv(mcp_stdio.HANDOVER_ENV, raising=False)
    to_server, from_client = os.pipe()
    from_server, to_client = os.pipe()
    execs = []
    old = mcp_stdio.Connection(to_server, to_client, wants_reload=mcp_server._wants_reload,
                               replace=lambda: execs.append(1) or True)
    if modern:
        _send(from_client, _modern(0, "server/discover"), _modern(**LISTEN))
    else:
        _send(from_client, INIT, {"method": "notifications/initialized"})
    return old, update, (to_server, to_client), (from_client, from_server), execs


def _send(fd, *messages):
    os.write(fd, b"".join(json.dumps({"jsonrpc": "2.0", **m}).encode() + b"\n" for m in messages))


async def _answers(fd, until_id) -> list[dict]:
    got, buf = [], b""
    while until_id not in [r.get("id") for r in got]:
        await anyio.wait_readable(fd)
        *lines, buf = (buf + os.read(fd, 65536)).split(b"\n")
        got += [json.loads(line) for line in lines if line]
    return got


INIT = {"id": 0, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def _modern(id, method, **params):
    """A request as Claude Code 2.1.318 sends it (MCP 2026-07-28): there is
    no initialize, every request carries its own envelope, and the client
    opens with server/discover, then a subscriptions/listen it keeps open."""
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "version": "2.1.318"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    return {"id": id, "method": method, "params": {**params, "_meta": meta}}


# The client's standing request for server events: answered only when the stream ends.
LISTEN = {"id": "listen:0", "method": "subscriptions/listen", "notifications": {"toolsListChanged": True}}


@pytest.mark.parametrize("replacement_knows_the_column, modern", [(True, False), (False, False), (True, True)])
def test_after_a_schema_bump_the_next_call_goes_to_the_reloaded_server(monkeypatch, replacement_knows_the_column,
                                                                       modern):
    """The update migrated the database under a session's `brindle mcp`: the
    server execs the installed brindle on the same stdio and that one answers
    the next call, already initialized. It serves the call only if its code
    declares the new schema; a replacement as old as the database's writer
    refuses, as the old server would have. A modern client's listen stream,
    open throughout, isn't waited for: it goes on in the new server, which
    doesn't acknowledge it a second time."""
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, SimpleNamespace(repo_root="/repo")))
    old, update, server_fds, (from_client, from_server), execs = _wire(monkeypatch, modern)
    call = {"id": 1, "method": "tools/call", "params": {"name": "list_repos", "arguments": {}}}
    declared = brindle_db._declared_columns()
    newer = {**declared, "workspaces": declared["workspaces"] | {"from_a_newer_engine"}}

    async def scenario():
        if modern:      # the listen's acknowledgement is still being written when the reload is wanted
            wanted, write = anyio.Event(), old._write

            async def held_ack(data):
                if b"acknowledged" in data:
                    await wanted.wait()
                await write(data)
            old._write = held_ack
            old._wants_reload = lambda reloaded: mcp_server._wants_reload(reloaded) and (wanted.set() or True)
        async with anyio.create_task_group() as tg:
            async def client():
                answers.extend(await _answers(from_server, 0))
                update.installed = True
                DB().conn.execute("ALTER TABLE workspaces ADD COLUMN from_a_newer_engine TEXT NOT NULL")
                _send(from_client, _modern(1, "tools/call", **call["params"]) if modern else call)
                answers.extend(await _answers(from_server, 1))
                os.close(from_client)

            tg.start_soon(client)
            with anyio.fail_after(30):
                await old.serve(mcp_server._run_server)
                assert execs == [1]
                # The new process, on the install as it is now.
                update.installed = False
                if replacement_knows_the_column:
                    monkeypatch.setattr(brindle_db, "_declared_columns", lambda: newer)
                new = mcp_stdio.Connection(*server_fds, mcp_stdio.take_handover(),
                                           wants_reload=mcp_server._wants_reload, replace=lambda: False)
                await new.serve(mcp_server._run_server)

    answers = []
    anyio.run(scenario)
    # initialize (or discover) answered once, by the old server; the listen never is
    assert [a["id"] for a in answers if "id" in a] == [0, 1]
    if select.select([from_server], [], [], 0)[0]:     # and whatever the new server wrote after
        answers.extend(json.loads(line) for line in os.read(from_server, 65536).splitlines() if line)
    result = next(a for a in answers if a.get("id") == 1)["result"]
    refused = bool(result.get("isError")) and "Reconnect" in json.dumps(result)
    assert refused is not replacement_knows_the_column
    acks = [a for a in answers if a.get("method") == "notifications/subscriptions/acknowledged"]
    assert len(acks) == int(modern)


def test_an_idle_server_reloads_once_its_install_changes(monkeypatch):
    """No call has to come in, and no daemon has to start: an idle server
    that sees its install change on its next look execs the new one."""
    old, update, _, (from_client, from_server), execs = _wire(monkeypatch)
    next_look = anyio.Event()
    looks = [next_look]     # then none: the server waits for its next look forever

    async def wait(seconds):
        await (looks.pop(0) if looks else anyio.Event()).wait()

    async def scenario():
        async with anyio.create_task_group() as tg:
            async def update_lands():
                await _answers(from_server, 0)      # the session is up, and idle
                update.installed = True
                next_look.set()

            tg.start_soon(update_lands)
            with anyio.fail_after(30):
                await mcp_server._serve(old, wait=wait)

    anyio.run(scenario)
    assert execs == [1]
    handover = mcp_stdio.take_handover()
    assert json.loads(handover.initialize)["method"] == "initialize" and handover.initialized


def test_an_update_that_completes_during_a_failed_reload_is_reloaded_onto(monkeypatch):
    """The first attempt found the install half-written; by the time its
    check failed the update had finished. That finished install is tried
    next, with no further request or install change needed."""
    old, update, _, (from_client, from_server), _ = _wire(monkeypatch)
    attempts = []

    def replace():
        attempts.append(update.installed)
        if update.installed == "half-written":
            update.installed = "complete"   # lands while the check runs
            return False
        return True

    old._replace = replace

    async def scenario():
        async with anyio.create_task_group() as tg:
            async def client():
                await _answers(from_server, 0)
                update.installed = "half-written"
                _send(from_client, {"id": 1, "method": "ping"})
                await _answers(from_server, 1)      # served here, since the reload failed

            tg.start_soon(client)
            with anyio.fail_after(30):
                await old.serve(mcp_server._run_server)

    anyio.run(scenario)
    assert attempts == ["half-written", "complete"] and old.replaced


_WHICH_BUILD = '''
@mcp.tool()
def which_build(hold: str = "") -> str:
    if hold:    # test fifos: say it started, then answer once released
        with open(hold + ".started", "w") as started: started.write("x")
        open(hold).read()
    return {build!r}
'''


def _app_install(at, build):
    """An app's engine at ``at``: its own python, brindle in its site-packages
    with a ``which_build`` tool answering ``build``, and brindle's
    dependencies from this environment."""
    import brindle

    venv.create(at, with_pip=False, symlinks=True)
    site = next(at.glob("lib/python*/site-packages"))
    shutil.copytree(os.path.dirname(brindle.__file__), site / "brindle", ignore=shutil.ignore_patterns("__pycache__"))
    with open(site / "brindle" / "mcp_server.py", "a") as f:
        f.write(_WHICH_BUILD.format(build=build))
    (site / "deps.pth").write_text(sysconfig.get_path("purelib") + "\n")


class _Client:
    """Claude Code 2.1.318's end of a stdio MCP server: one connection, never reopened."""

    def __init__(self, argv, cwd, log):
        self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        self.buf, self.frames = b"", []

    def send(self, id, method, **params):
        _send(self.proc.stdin.fileno(), _modern(id, method, **params))

    def answer(self, id):
        end = time.monotonic() + 90
        while not (got := [f for f in self.frames if f.get("id") == id]):
            assert select.select([self.proc.stdout], [], [], max(0, end - time.monotonic()))[0], f"no answer to {id}"
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            assert chunk, f"the server closed the connection before answering {id}"
            *lines, self.buf = (self.buf + chunk).split(b"\n")
            self.frames += [json.loads(line) for line in lines if line]
        return got[0]

    def build(self, id, send=True, **args):
        if send:
            self.send(id, "tools/call", name="which_build", arguments=args)
        result = self.answer(id)["result"]
        assert not result.get("isError"), result
        return result["content"][0]["text"]


@pytest.mark.variant("real incident: the app updater moved the running engine's bundle away mid-session")
@pytest.mark.parametrize("mid_call, old_deleted", [(False, False), (True, False), (False, True)])
def test_an_app_update_is_served_by_the_new_install_on_the_same_connection(tmp_path, mid_call, old_deleted):
    """The app updater renames the running app away (to a tauri_updated_app
    temp dir, later deleted) and puts the new one at its path. The server a
    session started from the old app answers the next call from the new
    install, on the same stdio: no reconnect, and no request lost, even one
    in flight while the update landed. Claude Code's subscriptions/listen,
    open for the whole connection, isn't waited for, and carries on in the
    new process (acknowledged once, as the client saw)."""
    app, staged = tmp_path / "Applications" / "brindle.app", tmp_path / "download" / "brindle.app"
    _app_install(app, "old")
    _app_install(staged, "new")
    moved = tmp_path / "T" / "tauri_updated_appXYZ"
    log = open(tmp_path / "server.log", "wb")
    client = _Client([str(app / "bin" / "python"), "-P", "-m", "brindle", "mcp"], tmp_path, log)
    try:
        client.send(0, "server/discover")
        client.answer(0)
        client.send(**LISTEN)
        client.send(1, "tools/list")
        assert "which_build" in [t["name"] for t in client.answer(1)["result"]["tools"]]
        assert client.build(2) == "old"
        if mid_call:
            hold = str(tmp_path / "hold")
            os.mkfifo(hold)
            os.mkfifo(hold + ".started")
            started = os.open(hold + ".started", os.O_RDONLY | os.O_NONBLOCK)
            os.open(hold + ".started", os.O_WRONLY)     # kept open: no EOF before the tool writes
            client.send(3, "tools/call", name="which_build", arguments={"hold": hold})
            assert select.select([started], [], [], 60)[0], "the held call never started"
        moved.parent.mkdir()
        os.rename(app, moved)       # what the updater does
        os.rename(staged, app)
        if old_deleted:
            shutil.rmtree(moved)
        if mid_call:
            client.send(4, "tools/call", name="which_build", arguments={})
            open(hold, "w").close()                 # the held call may finish now
            assert client.build(3, send=False) == "old"     # finished where it started
        assert client.build(4, send=not mid_call) == "new"
        assert client.build(5) == "new" and client.proc.poll() is None
        acks = [f for f in client.frames if f.get("method") == "notifications/subscriptions/acknowledged"]
        assert len(acks) == 1 and not [f for f in client.frames if f.get("id") == "listen:0"]
    finally:
        client.proc.stdin.close()
        client.proc.wait(30)
        log.close()


def test_a_reload_never_execs_an_install_whose_brindle_is_missing(monkeypatch):
    """Mid-update the interpreter can be there while its brindle isn't yet:
    exec'ing it would kill the server, so it isn't exec'd and the server
    keeps serving (and falls back to refusing, if the database moved on)."""
    monkeypatch.setattr(sys, "argv", ["brindle", "mcp"])
    monkeypatch.setattr("brindle.providers.brindle_invocation",
                        lambda: [sys.executable, "-P", "-m", "brindle_not_installed_yet"])
    monkeypatch.setattr("shutil.which", lambda command: None)
    execs = []
    monkeypatch.setattr(upgrade.os, "execv", lambda path, argv: execs.append(argv))

    assert mcp_stdio.replace() is False
    assert execs == []
