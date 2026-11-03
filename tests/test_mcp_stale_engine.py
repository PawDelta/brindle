import asyncio
import json
import os
import sys
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brindle import db as brindle_db
from brindle import mcp_server, mcp_stdio, upgrade
from brindle.db import DB, SCHEMA_VERSION


@pytest.mark.parametrize("migration", [
    "ALTER TABLE workspaces ADD COLUMN from_a_newer_engine TEXT",
    f"PRAGMA user_version={SCHEMA_VERSION + 1}",
])
def test_a_server_upgraded_under_refuses_tools_once_the_new_engine_migrated_the_database(monkeypatch, migration):
    """The app's update migrated the database while a session's `brindle mcp`
    kept its old code loaded: no tool runs that code against the migrated
    database, the client is told to reconnect."""
    monkeypatch.setattr(upgrade, "install", SimpleNamespace(changed=lambda: True))
    ran = []
    monkeypatch.setattr(mcp_server, "_caller",
                        lambda db: ran.append(1) or (None, SimpleNamespace(repo_root="/repo")))
    DB().conn.execute(migration)

    with pytest.raises(ToolError, match="/mcp → brindle → Reconnect"):
        asyncio.run(mcp_server.mcp.call_tool("list_repos", {}))
    assert ran == []


def _wire(monkeypatch):
    """A client's pipes to a server, and the server's (faked) exec of the new
    install: it records that the process was replaced, as execv would."""
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


@pytest.mark.parametrize("replacement_knows_the_column", [True, False])
def test_after_a_schema_bump_the_next_call_goes_to_the_reloaded_server(monkeypatch, replacement_knows_the_column):
    """The update migrated the database under a session's `brindle mcp`: the
    server execs the installed brindle on the same stdio and that one answers
    the next call, already initialized. It serves the call only if its code
    declares the new schema; a replacement as old as the database's writer
    refuses, as the old server would have."""
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, SimpleNamespace(repo_root="/repo")))
    old, update, server_fds, (from_client, from_server), execs = _wire(monkeypatch)
    declared = brindle_db._declared_columns()
    newer = {**declared, "workspaces": declared["workspaces"] | {"from_a_newer_engine"}}

    async def scenario():
        async with anyio.create_task_group() as tg:
            async def client():
                answers.extend(await _answers(from_server, 0))
                update.installed = True
                DB().conn.execute("ALTER TABLE workspaces ADD COLUMN from_a_newer_engine TEXT")
                _send(from_client, {"id": 1, "method": "tools/call",
                                    "params": {"name": "list_repos", "arguments": {}}})
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
    assert [a["id"] for a in answers] == [0, 1]     # initialize answered once, by the old server
    call = answers[1]["result"]
    refused = bool(call.get("isError")) and "Reconnect" in json.dumps(call)
    assert refused is not replacement_knows_the_column


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
