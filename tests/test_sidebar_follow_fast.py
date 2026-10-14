"""The tmux follow hook should avoid starting the full CLI."""

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle import sidebar_follow
from brindle.db import Agent, Workspace

ENTRY = Path(__file__).parents[1] / "src" / "brindle" / "__main__.py"


def _run(code, tmp_path):
    env = {**os.environ, "BRINDLE_HOME": str(tmp_path / "brindle-home")}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


def test_sidebar_follow_entry_does_not_import_heavy_modules(tmp_path):
    code = f"""
import runpy, sys
sys.argv = [{str(ENTRY)!r}, '_sidebar-follow', 'missing-session']
runpy.run_path(sys.argv[0], run_name='__main__')
for name in ('brindle.cli', 'brindle.mcp_server', 'typer', 'brindle.providers', 'brindle.workspaces'):
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
    agent = SimpleNamespace(id="root", tmux_window="%agent", parent_id=None, status="idle")

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
    assert calls == [("%sidebar", "@brindle_sidebar")]


def test_root_for_session_skips_an_older_paused_root_in_the_same_workspace(db, tmp_path, monkeypatch):
    """Two roots share one workspace: an old paused one whose stale pane id
    now resolves to the active window, and the live one. `brindle sidebar`
    must find the live root, not refuse with "paused"."""
    ws = Workspace(id="root-ws", repo_root=str(tmp_path), name="root", kind="worktree",
                   branch="main", base_branch="main", path=str(tmp_path), port_base=None,
                   tmux_session="brindle_root", created_at=time.time())
    db.add_workspace(ws)
    db.add_agent(Agent("old", ws.id, "supervisor", "claude", None, "interactive", "paused",
                       "%stale", None, 1.0))
    db.add_agent(Agent("live", ws.id, "supervisor", "claude", None, "interactive", "idle",
                       "%live", None, 2.0))
    monkeypatch.setattr(sidebar_follow.tmux, "active_window", lambda session: "@here")
    monkeypatch.setattr(sidebar_follow.tmux, "pane_window",
                        {"%stale": "@here", "%live": "@elsewhere"}.get)

    assert sidebar_follow.root_for_session(db, "brindle_root") == "live"
