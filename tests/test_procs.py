"""brindle.procs and the stops that use it: nothing an ended agent started
outlives it."""

import json
import os
import time

import pytest

from brindle import agents, cull, procs, sidebar_follow, tmux
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
        shell = "/bin/zsh -c source snapshot.sh && eval 'uv run pytest'"
        self.rows = {5001: procs.Proc(5001, 900, cli, cli),
                     5002: procs.Proc(5002, 5001, shell, shell),
                     5003: procs.Proc(5003, 5002, "python -m pytest", "python -m pytest")}
        self.stopped: set[int] = set()
        self.born = {pid: f"born {pid}" for pid in self.rows}

    def table(self) -> dict[int, procs.Proc]:
        return dict(self.rows)

    def births(self, pids) -> dict[int, str]:
        return {pid: self.born[pid] for pid in pids if pid in self.rows}

    def start(self, pid: int, ppid: int, cmd: str) -> None:
        self.rows[pid] = procs.Proc(pid, ppid, cmd, cmd)
        self.born[pid] = f"born {pid} again" if pid in self.born else f"born {pid}"

    def close_window(self, *targets, **batch) -> None:
        if targets or batch.get("windows") or batch.get("sessions"):
            self.rows.pop(5001, None)
            if 5002 in self.rows:
                self.rows[5002].ppid = 1

    def terminate(self, pids, grace: float = 3.0) -> int:
        for pid in pids:
            self.rows.pop(pid, None)
        self.stopped.update(pids)
        return len(pids)


def _batched_pause(db):
    agents.close_paused(db, agents.record_pauses(db, ["a1"]))


CLOSES = {
    "session pause": lambda db: agents.pause(db, "a1", stop_local_models=False),
    "worker stop": lambda db: agents.pause_worker(db, db.get_agent("a1")),
}
LEFT_TO_A_CULL = {      # these close the window and leave the processes to the next cull
    "session pause left to a cull": lambda db: agents.pause(db, "a1", stop_procs=False, stop_local_models=False),
    "the daemon's batched pause on its way out": _batched_pause,
    "a stale pane settled": lambda db: agents.settle_panes(db, [db.get_agent("a1")], keep=False),
}


@pytest.mark.parametrize("how", [*CLOSES, *LEFT_TO_A_CULL])
def test_a_stop_leaves_nothing_the_agents_cli_started(db, monkeypatch, how):
    db.add_workspace(Workspace("w", "/repo", "w", "worktree", "main", "main", "/repo", None, "s", 1.0))
    db.add_agent(Agent("a1", "w", "developer", "claude", None, "interactive", "idle", "%7", None, time.time()))
    world = _World("a1")
    monkeypatch.setattr(procs, "table", world.table)
    monkeypatch.setattr(procs, "births", world.births)
    monkeypatch.setattr(procs, "terminate", world.terminate)
    monkeypatch.setattr(tmux, "kill_window", world.close_window)
    monkeypatch.setattr(tmux, "close", world.close_window)
    monkeypatch.setattr(tmux, "window_pids", lambda target, server=None: [5001])
    monkeypatch.setattr(tmux, "pane_pids", lambda: {"%7": 5001})
    monkeypatch.setattr(tmux, "pane_places", lambda: {"%7": tmux.Place("@1", "s", "agent")})
    monkeypatch.setattr(tmux, "windows", lambda session: [])
    shown = iter([[tmux.AgentPane("%7", "a1", True, True)]])
    monkeypatch.setattr(tmux, "agent_panes", lambda *a, **k: next(shown, []))
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes=None: {"%7": "a1"})
    monkeypatch.setattr(agents, "same_server", lambda agent, panes=None: True)
    monkeypatch.setattr(agents, "owns_pane", lambda db, agent, owners=None: True)
    monkeypatch.setattr(sidebar_follow, "rescue_sidebar_from_window", lambda db, agent: None)
    if how in CLOSES:
        CLOSES[how](db)
    else:
        LEFT_TO_A_CULL[how](db)
        cull.sweep(db)
    assert world.stopped >= {5002, 5003}
    assert set(world.rows) <= {5001}


def _not_closed(world):
    """The pane's CLI still runs: what it started still has its parent."""


def _pid_reused(world):
    """The test run ended and its pid came round again, for the same command,
    to a process of the person's own that launchd adopted."""
    world.close_window("%7")
    world.start(5003, 1, "python -m pytest")


def _execed(world):
    """The shell exec'd the program it ran: the same process, another command."""
    world.close_window("%7")
    world.rows[5002].cmd = world.rows[5002].text = "uv run pytest"


def _child_since(world):
    """The shell started another command after the note, before the cull."""
    world.start(5004, 5002, "node dev-server")
    world.close_window("%7")


def _daemon_under_the_pane(world):
    """The pane's CLI had started Claude Code's daemon, which hosts another
    agent's session; the daemon outlives the close."""
    world.start(5005, 5001, "/usr/local/bin/claude daemon run")
    world.start(5006, 5005, "/usr/local/bin/claude --resume other-session")


NOTED = {
    "its parent still runs": (_not_closed, None, set()),
    "its pid came round again": (_pid_reused, None, {5002}),
    "it exec'd another program": (_execed, "a process keeps its birth across exec", {5002, 5003}),
    "it started a child since the note": (_child_since, None, {5002, 5003, 5004}),
    "the pane had started Claude's daemon": (_daemon_under_the_pane, None, {5002, 5003}),
}


@pytest.mark.parametrize("case", [
    pytest.param(case, marks=pytest.mark.variant(why)) if why else case
    for case, (_, why, _) in NOTED.items()])
def test_what_a_close_left_behind_is_ended_and_nothing_else(monkeypatch, case):
    """Only what a close left without its CLI is ended (procs.hand_off,
    handed_orphans): told by its birth, not its pid or command, with what it
    started since, and never Claude Code's daemon or what it hosts."""
    happen, _, ended = NOTED[case]
    world = _World("a1")
    monkeypatch.setattr(procs, "table", world.table)
    monkeypatch.setattr(procs, "births", world.births)
    if case == "the pane had started Claude's daemon":
        happen(world)
        procs.hand_off([5001])
        world.close_window("%7")
        world.rows[5005].ppid = 1
    else:
        procs.hand_off([5001])
        happen(world)
    assert procs.handed_orphans() == ended
