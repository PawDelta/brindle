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
    # An older paused root in the same workspace whose stale pane id also
    # resolves to the active window: the live root is the one followed.
    stale = SimpleNamespace(id="stale", tmux_window="%stale", parent_id=None, status="paused")

    class FakeDB:
        def workspace_by_tmux_session(self, session):
            return ws

        def list_agents(self, workspace_id):
            return [stale, agent]

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
    sent = []
    monkeypatch.setattr(sidebar_follow.tmux, "send_keys", lambda pane, *keys: sent.append((pane, keys)))
    monkeypatch.setattr(sidebar_follow, "_sidebar_lock", lambda root_id: (_ for _ in ()).throw(
        AssertionError("slow path reached")))
    monkeypatch.setattr(sidebar_follow.tmux, "window_alive", lambda *a: (_ for _ in ()).throw(
        AssertionError("skip path revalidated via tmux")))
    monkeypatch.setattr(sidebar_follow.tmux, "move_pane", lambda *a: (_ for _ in ()).throw(
        AssertionError("skip path moved the sidebar")))

    if should_skip:
        sidebar_follow.sidebar_follow(FakeDB(), "session")
        # A session switch leaves iTerm2 showing a blank sidebar: it must redraw.
        assert sent == [("%sidebar", ("C-l",))]
    else:
        with pytest.raises(AssertionError, match="slow path reached"):
            sidebar_follow.sidebar_follow(FakeDB(), "session")
    assert calls == [("%sidebar", "@brindle_sidebar")]


def test_sidebar_survives_a_second_client_changing_a_workers_window(db, monkeypatch):
    """The person's terminal shows the root's session, sidebar and all, while
    a second client (the app's) is attached to a worker's session. A window
    change there fires that session's follow hook: the sidebar must stay
    where the person is looking, not vanish into the worker's window."""
    for ws_id, session in (("rootws", "root_s"), ("workerws", "worker_s")):
        db.add_workspace(Workspace(ws_id, "/repo", ws_id, "worktree", ws_id, "main", f"/{ws_id}",
                                   None, session, time.time()))
    db.add_agent(Agent("root1", "rootws", "supervisor", "claude", None, "interactive", "idle",
                       "%root", None, time.time()))
    db.add_agent(Agent("w1", "workerws", "developer", "claude", "root1", "assign", "idle",
                       "%worker", None, time.time()))
    db.set_sidebar_pane("root1", "%sidebar")
    windows = {"%root": "@r", "%worker": "@w", "%sidebar": "@r"}
    tm = sidebar_follow.tmux
    monkeypatch.setattr(tm, "active_window", {"root_s": "@r", "worker_s": "@w"}.get)
    monkeypatch.setattr(tm, "pane_window", windows.get)
    monkeypatch.setattr(tm, "pane_window_tag", lambda pane, tag: (windows[pane], "root1"))
    monkeypatch.setattr(tm, "window_alive", lambda pane: True)
    monkeypatch.setattr(tm, "get_pane_tag", lambda pane, tag: "root1")
    monkeypatch.setattr(tm, "pane_session", {"%sidebar": "root_s", "%worker": "worker_s"}.get)
    monkeypatch.setattr(tm, "session_attached", lambda session: True)   # both clients
    monkeypatch.setattr(tm, "agent_pane_in_window", lambda window, sidebar: "%worker")
    monkeypatch.setattr(tm, "send_keys", lambda *a: None)
    moved = []
    monkeypatch.setattr(tm, "move_pane", lambda pane, target, *a: moved.append((pane, target)))

    sidebar_follow.sidebar_follow(db, "worker_s")

    assert moved == []
