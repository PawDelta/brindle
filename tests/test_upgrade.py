import asyncio
import shutil
import sys
from types import SimpleNamespace

from mcp import Client

import brindle
from brindle import mcp_server, profiles, upgrade, watch


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
    monkeypatch.setattr(watch.curses, "wrapper", lambda fn, root, sidebar: watch.RESTART)
    monkeypatch.setattr(upgrade.sys, "argv", ["brindle", "watch", "--sidebar"])
    monkeypatch.setattr("brindle.providers.brindle_invocation", lambda: ["/venv/bin/brindle"])
    execs = []
    monkeypatch.setattr(upgrade.os, "execv", lambda path, argv: execs.append(argv))

    watch.run("/repo", sidebar=True)
    assert execs == [["/venv/bin/brindle", "watch", "--sidebar"]]
