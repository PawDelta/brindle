import asyncio
import json
import os
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

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


def test_after_a_schema_bump_the_next_call_is_served_by_the_reloaded_server(monkeypatch):
    """The update migrated the database under a session's `brindle mcp`: the
    next call isn't refused, the server execs the new install on the same
    stdio and that one answers it, already initialized."""
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, SimpleNamespace(repo_root="/repo")))
    old, update, server_fds, (from_client, from_server), execs = _wire(monkeypatch)

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
                # The new process: its own install, which declares the new column.
                update.installed = False
                new = mcp_stdio.Connection(*server_fds, mcp_stdio.take_handover(),
                                           wants_reload=mcp_server._wants_reload, replace=lambda: False)
                await new.serve(mcp_server._run_server)

    answers = []
    anyio.run(scenario)
    assert [a["id"] for a in answers] == [0, 1]     # initialize answered once, by the old server
    call = answers[1]["result"]
    assert not call.get("isError") and "Reconnect" not in json.dumps(call)


def test_an_idle_server_reloads_when_a_new_daemon_starts(monkeypatch):
    """No call has to come in: a daemon starting after the update wakes every
    live server, and one on the old install execs the new one at once."""
    old, update, _, (from_client, from_server), execs = _wire(monkeypatch)

    async def scenario():
        async with anyio.create_task_group() as tg:
            async def daemon_starts():
                await _answers(from_server, 0)      # the session is up, and idle
                update.installed = True
                woken.append(upgrade.notify_mcp_servers())

            tg.start_soon(daemon_starts)
            with anyio.fail_after(30):
                await mcp_server._serve(old)

    woken = []
    anyio.run(scenario)
    assert woken == [1] and execs == [1]
    handover = mcp_stdio.take_handover()
    assert json.loads(handover.initialize)["method"] == "initialize" and handover.initialized
