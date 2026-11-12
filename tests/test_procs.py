"""brindle.procs and the stops that use it: nothing an ended agent started
outlives it."""

import json
import os
import signal
import time

import pytest

from brindle import agents, cull, procs, sidebar_follow, tmux
from brindle.db import Agent, Workspace


class _World:
    """A process table that a window's close changes the way the real one
    does: the CLI in the pane ends on the hangup, and the Bash tool's shell
    it started in a session of its own (with the test run under it) is left
    with launchd as its parent. Each process has a birth, as lineage reads it."""

    def __init__(self, agent_id: str):
        config = json.dumps({"mcpServers": {"brindle": {"env": {
            "BRINDLE_AGENT_ID": agent_id, "BRINDLE_HOME": os.environ.get("BRINDLE_HOME", "")}}}})
        self.rows: dict[int, procs.Proc] = {}
        self.stopped: set[int] = set()
        self.after_read = None     # what happens just after the next lineage read
        self.start(5001, 900, f"/usr/local/bin/claude --mcp-config {config}")
        self.start(5002, 5001, "/bin/zsh -c source snapshot.sh && eval 'uv run pytest'")
        self.start(5003, 5002, "python -m pytest")

    def table(self) -> dict[int, procs.Proc]:
        return dict(self.rows)

    def lineage(self) -> dict[int, procs.Proc]:
        read = {pid: procs.Proc(p.pid, p.ppid, p.text, p.cmd, p.birth) for pid, p in self.rows.items()}
        if self.after_read:
            happen, self.after_read = self.after_read, None
            happen()
        return read

    def start(self, pid: int, ppid: int, cmd: str) -> None:
        again = pid in self.rows or any(p.birth.startswith(f"born {pid}") for p in self.rows.values())
        self.rows[pid] = procs.Proc(pid, ppid, cmd, cmd, f"born {pid} again" if again else f"born {pid}")

    def close_window(self, *targets, **batch) -> None:
        if targets or batch.get("windows") or batch.get("sessions"):
            self.rows.pop(5001, None)
            if 5002 in self.rows:
                self.rows[5002].ppid = 1

    def terminate(self, pids, grace: float = 3.0, owned=None) -> int:
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
    monkeypatch.setattr(procs, "lineage", world.lineage)
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


def _nothing(world):
    pass


def _closed(world):
    world.close_window("%7")


def _pid_reused(world):
    """The test run ended and its pid came round again, for the same command,
    to a process of the person's own that launchd adopted."""
    world.close_window("%7")
    world.start(5003, 1, "python -m pytest")


def _replaced_as_noted(world):
    """The test run's pid came round again just after the note's read."""
    world.after_read = lambda: world.start(5003, 1, "python -m pytest")


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


def _daemon_adopted(world):
    world.close_window("%7")
    world.rows[5005].ppid = 1


NOTED = {   # (before the note, after it, why it's a variant, what is ended)
    "its parent still runs": (_nothing, _nothing, None, set()),
    "its pid came round again": (_nothing, _pid_reused, None, {5002}),
    "its pid came round again as the note was taken": (
        _replaced_as_noted, _closed, "a race: a pid reused between reading the tree and the births", {5002}),
    "it exec'd another program": (_nothing, _execed, "a process keeps its birth across exec", {5002, 5003}),
    "it started a child since the note": (_nothing, _child_since, None, {5002, 5003, 5004}),
    "the pane had started Claude's daemon": (_daemon_under_the_pane, _daemon_adopted, None, {5002, 5003}),
}


@pytest.mark.parametrize("case", [
    pytest.param(case, marks=pytest.mark.variant(why)) if why else case
    for case, (_, _, why, _) in NOTED.items()])
def test_what_a_close_left_behind_is_ended_and_nothing_else(monkeypatch, case):
    """Only what a close left without its CLI is ended (procs.hand_off,
    handed_orphans): told by its birth, not its pid or command, with what it
    started since, and never Claude Code's daemon or what it hosts."""
    before, after, _, ended = NOTED[case]
    world = _World("a1")
    monkeypatch.setattr(procs, "lineage", world.lineage)
    before(world)
    procs.hand_off([5001])
    after(world)
    assert set(procs.handed_orphans()) == ended


def _reused_before_any_signal(world, sig):
    pass


def _reused_after_sigterm(world, sig):
    if sig == signal.SIGTERM:      # it ends at once; its pid comes round again before the SIGKILL
        world.start(5002, 1, world.rows[5002].cmd)


@pytest.mark.parametrize("when", [_reused_before_any_signal, _reused_after_sigterm])
def test_a_pid_that_came_round_again_is_never_signalled(monkeypatch, when):
    """terminate with the births it was given looks again before each
    signal: another's process on the pid gets neither SIGTERM nor SIGKILL."""
    world = _World("a1")
    owned = {5002: world.rows[5002].birth}
    if when is _reused_before_any_signal:
        world.start(5002, 1, world.rows[5002].cmd)
    sent = []

    def kill(pid, sig):
        if sig:
            sent.append((pid, sig))
            when(world, sig)

    monkeypatch.setattr(procs, "lineage", world.lineage)
    monkeypatch.setattr(procs, "alive", lambda pid: pid in world.rows)
    monkeypatch.setattr(procs.os, "kill", kill)
    procs.terminate([5002], grace=0, owned=owned)
    assert sent == ([] if when is _reused_before_any_signal else [(5002, signal.SIGTERM)])
