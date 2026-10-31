import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from brindle import mcp_server, upgrade
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
