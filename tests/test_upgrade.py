import asyncio
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
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
    attempts = []

    def execv(path, argv):
        attempts.append(argv)
        if len(attempts) < 3:
            raise FileNotFoundError(path)

    monkeypatch.setattr(upgrade.os, "execv", execv)

    upgrade.reexec()
    assert len(attempts) == 3


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
