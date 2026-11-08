"""A worker waiting on its supervisor is an open item the supervisor can't ignore."""

import os
import time

import pytest

from brindle import agents, autopilot, cull, disk, gates, mcp_server, open_items, pipeline, tasks, tmux, workspaces
from brindle.db import Agent, Workspace

T0 = 1_000_000.0


def panes():
    live = tmux.PaneSnapshot(tmux.current_server())
    live.update({"@998": True, "@999": True})
    return live


@pytest.fixture
def told(monkeypatch):
    """What each supervisor was sent; every agent runs, and has nothing else going."""
    sent = []

    def send(db, to, body, sender_id=None, person=False):
        sent.append((to, body))
        return "delivered"

    monkeypatch.setattr(agents, "send_message", send)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "owns_pane", lambda db, a, owners=None: True)
    monkeypatch.setattr(agents, "same_server", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "command_running", lambda db, a: False)
    monkeypatch.setattr(agents, "transcript_active", lambda a, now=None: False)
    monkeypatch.setattr(cull, "_last_edit", lambda ws: 0)
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes())
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "")
    return sent


@pytest.fixture
def boss(db, repo, monkeypatch):
    home = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", home.id, "supervisor", "claude", None, "interactive", "idle", "@998", None, T0))
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Ship it", [("Done", "test -f done.txt", None)])
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return db.get_agent("boss")


@pytest.fixture
def worker(db, boss, repo, tmp_path):
    ws = Workspace("w", str(repo), "w", "worktree", "feat/stuck", "main", str(tmp_path / "gone"), None, "s", T0)
    db.add_workspace(ws)
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "idle", "@999", None, T0 - 7200,
                       status_since=T0 - 7200, task="Build the thing"))
    return db.get_agent("w1")


@pytest.mark.parametrize("act", ["send_message", "remove_workspace"])
def test_the_supervisor_cannot_stop_until_it_deals_with_the_worker(db, boss, worker, told, monkeypatch, act):
    monkeypatch.setenv("BRINDLE_AGENT_ID", "w1")
    mcp_server.send_message("bos", "Should the cache be per user?")   # ids go by unambiguous prefix
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    open_items.track(db, db.get_agent("w1"), "stopped without calling report_result", time.time())

    first_line = autopilot.on_stop(db, boss, {})["reason"].splitlines()[0]
    assert "w1" in first_line and "Should the cache be per user?" in first_line

    if act == "send_message":
        mcp_server.send_message("w", "Yes, per user.")
    else:
        monkeypatch.setattr(workspaces, "remove", lambda *a, **k: workspaces.Removed(False, "branch kept", None))
        mcp_server.remove_workspace("w1", delete_branch=False)
    assert db.open_items("boss") == []
    assert "w1" not in (autopilot.on_stop(db, boss, {}) or {"reason": ""})["reason"]


def test_stops_are_refused_until_the_takeover_is_due_and_again_if_it_fails(db, boss, worker, told, monkeypatch):
    """Within the hour every stop is refused. Past it the supervisor may go
    idle, which is what brindle's takeover waits for; a takeover that can't
    happen leaves the item the supervisor's alone, so stops are refused again."""
    open_items.track(db, worker, "stopped without calling report_result", time.time())
    for _ in range(8):
        assert "w1" in autopilot.on_stop(db, boss, {})["reason"]

    db.close_open_item("w1")
    open_items.track(db, worker, "stopped without calling report_result", time.time() - open_items.AUTO_REASSIGN_AFTER)
    assert "w1" not in (autopilot.on_stop(db, boss, {}) or {"reason": ""})["reason"]

    monkeypatch.setattr(disk, "live_in", lambda *a, **k: True)   # someone is attached to its worktree
    open_items.sweep(db, time.time(), panes())
    assert "didn't reassign" in told[-1][1]
    for _ in range(3):
        assert "w1" in autopilot.on_stop(db, boss, {})["reason"]


def test_a_worker_that_died_in_a_fix_round_stays_open(db, boss, worker, told, monkeypatch):
    """A dead worker with a result is marked done (cull.note_dead_panes), but
    its fix round is still unfinished, so its item is not settled."""
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    monkeypatch.setattr(gates, "head", lambda ws: "abc")
    db.update_agent("w1", result="done", pipeline="fixing", status="done", status_since=T0)
    db.conn.execute("INSERT INTO reviews (workspace_id, sha, reviewer_id, approved, summary, created_at) "
                    "VALUES ('w', 'abc', 'rev', 0, 'fix it', ?)", (T0 + 60,))
    db.conn.commit()
    os.makedirs(db.get_workspace("w").path)

    assert cull.note_stalled(db, time.time(), panes())
    assert "w1" in autopilot.on_stop(db, boss, {})["reason"]

    # Closed on purpose, though (agents.close keeps its worktree and branch): nothing to chase.
    db.update_agent("w1", dismissed_at=time.time())
    open_items.sweep(db, time.time() + open_items.AUTO_REASSIGN_AFTER, panes())
    assert db.open_items("boss") == [] and db.list_tasks() == []   # and no takeover
    assert os.path.isdir(db.get_workspace("w").path)


def test_a_stuck_worker_in_a_fix_round_is_reported_once_then_escalates(db, boss, worker, told, monkeypatch):
    """The incident: the idle notice and the fix-round notice overwrote each
    other's de-dupe mark, so both went out again and again for 14 hours."""
    monkeypatch.setattr(agents, "liveness", lambda *a, **k: "running")
    db.update_agent("w1", result="done", pipeline="fixing", status_since=T0)
    db.conn.execute("INSERT INTO reviews (workspace_id, sha, reviewer_id, approved, summary, created_at) "
                    "VALUES ('w', 'abc', 'rev', 0, 'fix it', ?)", (T0 + 60,))
    db.conn.commit()
    os.makedirs(db.get_workspace("w").path)   # a folder that's gone is cleared away by the sweep

    start = T0 + 1900
    for offset in (0, 700, 1100):
        cull.sweep(db, now=start + offset)
    assert len([b for to, b in told if to == "boss" and "w1" in b]) == 1

    for offset in (1300, 1400):   # past the first escalation, twice
        cull.sweep(db, now=start + offset)
    about_it = [b for to, b in told if to == "boss" and "w1" in b]
    assert len(about_it) == 2 and about_it[1].startswith("Still open after")


@pytest.fixture
def takeover(db, boss, worker, told, monkeypatch):
    """The two ends of a takeover that touch real processes, recorded."""
    calls = {"removed": [], "started": []}

    def remove(db_, ws, **kw):
        calls["removed"].append((ws.id, kw))
        return workspaces.Removed(False, "branch kept", None)

    def delegate(db_, caller, caller_ws, profile, task, mode, **kw):
        calls["started"].append((caller_ws.id, task))
        db_.add_agent(Agent("fresh", caller_ws.id, profile, "claude", caller.id, mode, "starting", "@1", None, T0))
        return db_.get_agent("fresh"), caller_ws

    monkeypatch.setattr(workspaces, "remove", remove)
    monkeypatch.setattr(agents, "delegate", delegate)
    monkeypatch.setattr(tasks, "_claim_fair", lambda db_, t: (True, None, None))
    open_items.track(db, worker, "stopped without calling report_result", T0)
    return calls


@pytest.mark.parametrize("via", ["sweep", "requeue"])
def test_a_stuck_worker_is_taken_over_after_the_hour(db, boss, takeover, told, via):
    if via == "sweep":
        open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER - 1, panes())
        assert takeover["removed"] == []
        open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER, panes())
        assert "branch `feat/stuck` kept" in told[-1][1]
    else:
        assert "feat/stuck" in mcp_server.requeue("w1")

    assert takeover["removed"] == [("w", {"force": True, "delete_branch": False})]
    assert [ws for ws, _ in takeover["started"]] == [boss.workspace_id]   # cut from the supervisor's branch
    assert "Build the thing" in takeover["started"][0][1] and "feat/stuck" in takeover["started"][0][1]
    assert db.open_items("boss") == []


@pytest.mark.parametrize("wakes", [
    pytest.param("claim", marks=pytest.mark.variant("the worker wakes between the sweep's read and the claim")),
    "preparing",
])
def test_a_worker_that_came_back_is_not_retired(db, boss, takeover, monkeypatch, wakes):
    """Checked once more after everything that can take a while, and what
    was prepared for the replacement is undone."""
    waiting = tasks.enqueue(db, boss, db.get_workspace(boss.workspace_id), "developer", "after it", "assign",
                            isolate=True, branch=None, done_when=None, files=None, depends_on=["w1"])

    def wake(*a, **k):
        db.update_agent("w1", status="processing")
        return True

    if wakes == "claim":
        real = db.claim_open_item_retire
        monkeypatch.setattr(db, "claim_open_item_retire", lambda *a: wake() and real(*a))
    else:   # runs the repo's event hooks, after the first look at the worker
        monkeypatch.setattr(pipeline, "note_removed_unmerged", wake)
    open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER, panes())
    assert takeover["removed"] == [] and takeover["started"] == []
    assert tasks._loads(db.get_task(waiting.id).depends_on) == ["w1"]
    assert {t.state for t in db.list_tasks() if t.id != waiting.id} <= {"cancelled"}


def test_a_skipped_takeover_restarts_the_hour_and_the_stop_refusal(db, boss, worker, takeover, told, monkeypatch):
    """One edit made after the item opened counts as life once, not forever:
    the supervisor is held to it again, and the next hour's takeover goes ahead."""
    now = time.time()
    opened = now - open_items.AUTO_REASSIGN_AFTER
    db.close_open_item("w1")
    open_items.track(db, worker, "stopped without calling report_result", opened)
    monkeypatch.setattr(cull, "_last_edit", lambda ws: opened + 10)

    open_items.sweep(db, now, panes())
    assert takeover["removed"] == [] and "held off" in told[-1][1]
    assert "w1" in autopilot.on_stop(db, boss, {})["reason"]

    open_items.sweep(db, now + open_items.AUTO_REASSIGN_AFTER, panes())
    assert [ws for ws, _ in takeover["removed"]] == ["w"]


def test_a_plan_waiting_for_approval_is_not_a_stuck_worker(db, boss, worker, takeover, monkeypatch):
    # Another supervisor can't requeue this worker while its open item exists.
    db.add_agent(Agent("other", boss.workspace_id, "supervisor", "claude", None, "interactive", "idle", "@2", None, T0))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "other")
    assert db.open_items("boss") != []
    assert mcp_server.requeue("w1").startswith("Error")   # without the ownership check this would take it over
    assert takeover["removed"] == [] and db.open_items("boss") != []
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    db.update_agent("w1", plan_state="proposed")
    held = db.get_agent("w1")
    open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER, panes())
    assert takeover["removed"] == []
    db.close_open_item("w1")
    assert open_items.track(db, held, "stopped without calling report_result", T0) is False
