"""The last-moment checkpoint (brindle.checkpoint): a provider about to cut
off has its workers commit and leave a handover note mid-turn, and nearing
the limit no longer stops anyone. On a fake clock and fake panes, never the
wall clock or tmux.

brindle.checkpoint is imported inside the tests, so they run (and fail on
their assertions) against a tree without it."""

import time

import pytest

from brindle import agents, autopilot, mcp_server, quota, subscription_usage, tmux, workspaces
from brindle.db import Agent

NOW = 1_800_000_000.0
MINUTE = 60.0
HOUR = 3600.0


@pytest.fixture
def clock(monkeypatch):
    now = [NOW]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(subscription_usage, "identity", lambda: "test-subscription")
    return now


@pytest.fixture
def pane(monkeypatch):
    """The worker's pane: what it shows (None: it can't be read), and what
    brindle typed into it."""
    state = {"screen": "", "typed": [], "cut_after_enter": False}

    def capture(target, *a, **k):
        if state["screen"] is None:
            raise tmux.TmuxError("pane unavailable")
        return state["screen"]

    def paste(target, text, progress=None, **k):
        if state["cut_after_enter"]:   # the connection drops once Enter is pressed
            state["cut_after_enter"] = False
            for stage in ("lead", "body", "submitted"):
                progress(stage)
            raise tmux.TmuxError("connection lost")
        state["typed"].append(text)

    monkeypatch.setattr(tmux, "capture", capture)
    monkeypatch.setattr(tmux, "paste", paste)
    monkeypatch.setattr(agents, "_paste_blocked", lambda agent: False)
    return state


def _worker(db, repo, provider):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("w", ws.id, "developer", provider, None, "assign", "processing", "@1", None, NOW))
    return ws


def _claude_hook(db):
    reply = agents.handle_hook(db, "w", "tool-done", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    return reply and reply["hookSpecificOutput"]["additionalContext"]


def test_closing_asks_a_claude_worker_at_most_twice_and_keeps_its_note(db, repo, clock, pane, monkeypatch):
    _worker(db, repo, "claude")
    quota.record("claude", [quota.Window(98, NOW + 2 * HOUR, 300)])

    first = _claude_hook(db)
    assert first and "handover_note" in first
    assert not _claude_hook(db)              # once, not on every tool call
    clock[0] += 11 * MINUTE
    assert _claude_hook(db)                  # still closing ten minutes on: asked once more
    clock[0] += 11 * MINUTE
    assert not _claude_hook(db)              # and never a third time

    from brindle import checkpoint

    monkeypatch.setenv("BRINDLE_AGENT_ID", "w")
    mcp_server.handover_note("done: parser\nleft: tests\ntraps: none")
    assert checkpoint.note(db, "w") == "done: parser\nleft: tests\ntraps: none"


def test_typed_checkpoint_waits_for_a_usable_pane_and_its_turn(db, repo, clock, pane):
    from brindle import checkpoint

    _worker(db, repo, "antigravity")
    db.enqueue("w", "older message", None)
    quota.record("antigravity", [quota.Window(98, NOW + 2 * HOUR, 300)])

    pane["screen"] = None                    # can't be typed into yet: both stay queued
    checkpoint.sweep(db)
    clock[0] += 5 * MINUTE
    checkpoint.sweep(db)
    assert pane["typed"] == [] and db.pending_count("w") == 2
    pane["screen"], pane["cut_after_enter"] = "", True
    checkpoint.sweep(db)                     # the older message goes in, but its delivery stops partway
    assert pane["typed"] == []
    checkpoint.sweep(db)                     # finished, then the ask, mid-turn and once
    assert len(pane["typed"]) == 1 and "handover_note" in pane["typed"][0]
    assert db.pending_count("w") == 0
    clock[0] += 9 * MINUTE                   # 14 minutes after the claim, 9 after it went in
    checkpoint.sweep(db)
    assert len(pane["typed"]) == 1           # ten minutes from when it went in, not from the claim
    clock[0] += 2 * MINUTE
    checkpoint.sweep(db)
    assert len(pane["typed"]) == 2
    clock[0] += 11 * MINUTE
    checkpoint.sweep(db)
    assert len(pane["typed"]) == 2           # never a third time


@pytest.mark.parametrize("shown", [
    pytest.param("API Error (429 rate_limit_error) · trying again in 5 seconds… (attempt 2/10)",
                 id="a 429 waited out mid-turn"),
    pytest.param("You're approaching your usage limit", id="the CLI's approaching-limit notice",
                 marks=pytest.mark.variant("a notice on screen with no Notification hook is the other pane trigger")),
])
def test_92_percent_pauses_no_worker_and_asks_only_once_its_pane_shows_the_limit(
        db, repo, clock, pane, monkeypatch, shown):
    ws = _worker(db, repo, "claude")
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "@0", None, NOW))
    db.update_agent("w", parent_id="boss")
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Goal", [("M1", "true", None)])
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status not in ("paused", "done"))
    monkeypatch.setattr(agents, "_stop", lambda db, a: None)
    quota.record("claude", [quota.Window(92, NOW + 2 * HOUR, 300)])

    autopilot.usage_sweep(db)
    autopilot.on_stop(db, db.get_agent("boss"), {})
    assert db.get_agent("w").status == "processing"
    assert db.get_autopilot("boss").state == "running"
    assert _claude_hook(db) is None

    from brindle import checkpoint

    pane["screen"] = f"working on it\n{shown}\n"
    checkpoint.sweep(db)
    assert _claude_hook(db)
