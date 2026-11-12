"""brindle.procs and the stops that use it: nothing an ended agent started
outlives it."""

import json
import os
import time

import pytest

from brindle import agents, procs, sidebar_follow, tmux
from brindle.db import Agent, Workspace


class _World:
    """A process table that a window's close changes the way the real one
    does: the CLI in the pane ends on the hangup, and the Bash tool's shell
    it started in a session of its own (with the test run under it) is left
    with launchd as its parent."""

    def __init__(self, agent_id: str):
        config = json.dumps({"mcpServers": {"brindle": {"env": {
            "BRINDLE_AGENT_ID": agent_id, "BRINDLE_HOME": os.environ.get("BRINDLE_HOME", "")}}}})
        cli = f"/usr/local/bin/claude --mcp-config {config}"
        self.rows = {5001: procs.Proc(5001, 900, cli, cli),
                     5002: procs.Proc(5002, 5001, "/bin/zsh -c source snapshot.sh && eval 'uv run pytest'",
                                      "/bin/zsh -c source snapshot.sh && eval 'uv run pytest'"),
                     5003: procs.Proc(5003, 5002, "python -m pytest", "python -m pytest")}
        self.stopped: set[int] = set()

    def table(self) -> dict[int, procs.Proc]:
        return dict(self.rows)

    def close_window(self, target: str) -> None:
        self.rows.pop(5001, None)
        if 5002 in self.rows:
            self.rows[5002].ppid = 1

    def terminate(self, pids, grace: float = 3.0) -> int:
        for pid in pids:
            self.rows.pop(pid, None)
        self.stopped.update(pids)
        return len(pids)


@pytest.mark.parametrize("how", ["session pause", "worker stop"])
def test_a_stop_leaves_nothing_the_agents_cli_started(db, monkeypatch, how):
    db.add_workspace(Workspace("w", "/repo", "w", "worktree", "main", "main", "/repo", None, "s", 1.0))
    db.add_agent(Agent("a1", "w", "developer", "claude", None, "interactive", "idle", "%7", None, time.time()))
    world = _World("a1")
    monkeypatch.setattr(procs, "table", world.table)
    monkeypatch.setattr(procs, "terminate", world.terminate)
    monkeypatch.setattr(tmux, "kill_window", world.close_window)
    monkeypatch.setattr(tmux, "window_pids", lambda target, server=None: [5001])
    monkeypatch.setattr(tmux, "windows", lambda session: [])
    monkeypatch.setattr(tmux, "agent_panes", lambda *a, **k: [])
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes=None: {"%7": "a1"})
    monkeypatch.setattr(agents, "same_server", lambda agent, panes=None: True)
    monkeypatch.setattr(agents, "owns_pane", lambda db, agent, owners=None: True)
    monkeypatch.setattr(sidebar_follow, "rescue_sidebar_from_window", lambda db, agent: None)
    if how == "session pause":
        agents.pause(db, "a1", stop_local_models=False)
    else:
        agents.pause_worker(db, db.get_agent("a1"))
    assert world.stopped >= {5002, 5003}
    assert set(world.rows) <= {5001}
