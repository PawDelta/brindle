"""The tmux follow hook should avoid starting the full CLI."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from copse import sidebar_follow

ENTRY = Path(__file__).parents[1] / "src" / "copse" / "__main__.py"


def _run(code, tmp_path):
    env = {**os.environ, "COPSE_HOME": str(tmp_path / "copse-home")}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


def test_sidebar_follow_entry_does_not_import_heavy_modules(tmp_path):
    code = f"""
import runpy, sys
sys.argv = [{str(ENTRY)!r}, '_sidebar-follow', 'missing-session']
runpy.run_path(sys.argv[0], run_name='__main__')
for name in ('copse.cli', 'copse.mcp_server', 'typer', 'copse.providers', 'copse.workspaces'):
    assert name not in sys.modules, name
"""
    result = _run(code, tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("reported_window", "reported_tag", "should_skip"),
    [("@window", "root", True), ("@other", "root", False),
     ("@window", "different-root", False)],
)
def test_sidebar_follow_fast_skip_requires_matching_window_and_tag(
    monkeypatch, reported_window, reported_tag, should_skip,
):
    ws = SimpleNamespace(id="ws", tmux_session="session")
    agent = SimpleNamespace(id="root", tmux_window="%agent", parent_id=None)

    class FakeDB:
        def workspace_by_tmux_session(self, session):
            return ws

        def list_agents(self, workspace_id):
            return [agent]

        def get_agent(self, agent_id):
            return agent

        def get_sidebar_pane(self, root_id):
            return "%sidebar"

    calls = []
    monkeypatch.setattr(sidebar_follow.tmux, "active_window", lambda session: "@window")
    monkeypatch.setattr(sidebar_follow.tmux, "pane_window", lambda pane: "@window")

    def pane_window_tag(pane, tag):
        calls.append((pane, tag))
        return reported_window, reported_tag

    monkeypatch.setattr(sidebar_follow.tmux, "pane_window_tag", pane_window_tag)
    monkeypatch.setattr(sidebar_follow, "_sidebar_lock", lambda root_id: (_ for _ in ()).throw(
        AssertionError("slow path reached")))
    monkeypatch.setattr(sidebar_follow.tmux, "window_alive", lambda *a: (_ for _ in ()).throw(
        AssertionError("skip path revalidated via tmux")))
    monkeypatch.setattr(sidebar_follow.tmux, "move_pane", lambda *a: (_ for _ in ()).throw(
        AssertionError("skip path moved the sidebar")))

    if should_skip:
        sidebar_follow.sidebar_follow(FakeDB(), "session")
    else:
        with pytest.raises(AssertionError, match="slow path reached"):
            sidebar_follow.sidebar_follow(FakeDB(), "session")
    assert calls == [("%sidebar", "@copse_sidebar")]


def test_main_argv_falls_through_for_normal_command_and_swallows_hook_errors(tmp_path):
    code = f"""
import runpy, sys, types
cli = types.ModuleType('copse.cli')
cli.app = lambda: print('cli-called')
sys.modules['copse.cli'] = cli
sys.argv = [{str(ENTRY)!r}, 'version']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = _run(code, tmp_path)
    assert result.returncode == 0
    assert "cli-called" in result.stdout

    code = f"""
import runpy, sys, types
db = types.ModuleType('copse.db')
db.DB = lambda: object()
follow = types.ModuleType('copse.sidebar_follow')
def fail(db, session): raise RuntimeError('hook failure')
follow.sidebar_follow = fail
sys.modules['copse.db'] = db
sys.modules['copse.sidebar_follow'] = follow
sys.argv = [{str(ENTRY)!r}, '_sidebar-follow', 'session']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = _run(code, tmp_path)
    assert result.returncode == 0
    assert "hook failure" not in result.stderr
