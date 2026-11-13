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


@pytest.mark.parametrize("case", [
    pytest.param("gave_up", marks=pytest.mark.variant("A worker whose takeover gave up must not block its supervisor again on later stops")),
    "budget",
])
def test_gave_up_stops_blocking(db, boss, worker, told, monkeypatch, case):
    now = T0
    monkeypatch.setattr(open_items.time, "time", lambda: now)
    open_items.track(db, worker, "stopped without calling report_result", now)
    if case == "gave_up":
        monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == boss.id)
        now += open_items.AUTO_REASSIGN_AFTER
        assert open_items.stop_block(db, boss) is None
        monkeypatch.setattr(disk, "live_in", lambda *a, **k: True)
        open_items.sweep(db, now, panes())
        assert db.get_open_item(worker.id).level == open_items.GAVE_UP
        assert open_items.stop_block(db, boss) is None
        now += 2 * 86400
        assert open_items.stop_block(db, boss) is None
        return
    for _ in range(autopilot.MAX_NUDGES):
        assert open_items.stop_block(db, boss)["decision"] == "block"
        assert open_items.stop_block(db, boss) is None
        now += autopilot.NUDGE_BACKOFF
    assert open_items.stop_block(db, boss) is None
    assert open_items.stop_block(db, boss) is None
    db.update_autopilot(boss.id, state="running", progress=1)
    assert open_items.stop_block(db, boss)["decision"] == "block"


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
    pytest.param("preparing", marks=pytest.mark.variant("Issue 384: a worker waking during replacement preparation must keep its work and dependencies")),
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


def test_an_item_nothing_can_close_does_not_refuse_stops(db, boss, worker, told, monkeypatch):
    """Dead, so no message reaches it; in a shared checkout, so neither a
    takeover nor remove_workspace applies."""
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    db.update_agent("w1", workspace_id=boss.workspace_id, status="failed")
    open_items.track(db, db.get_agent("w1"), "stopped without calling report_result", time.time())
    assert "w1" in autopilot.on_stop(db, boss, {})["reason"]
    assert "shared checkout" in mcp_server.requeue("w1")
    assert open_items.stop_block(db, boss) is None


@pytest.mark.variant("A crash during worktree removal must reuse the prepared replacement rather than enqueue another")
def test_a_takeover_that_died_part_way_reuses_its_replacement(db, boss, takeover, monkeypatch):
    def die(*a, **k):
        raise KeyboardInterrupt   # as a process dying: nothing below handles it

    finish = workspaces.remove
    monkeypatch.setattr(workspaces, "remove", die)
    with pytest.raises(KeyboardInterrupt):
        open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER, panes())
    monkeypatch.setattr(workspaces, "remove", finish)
    open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER + open_items.CLAIM_EXPIRES + 1, panes())
    assert [ws for ws, _ in takeover["removed"]] == ["w"] and len(takeover["started"]) == 1
    assert len(db.list_tasks()) == 1 and db.list_tasks(state=tasks.UNANSWERED) == []


@pytest.mark.parametrize("after", [
    pytest.param("answered", marks=pytest.mark.variant("An answer after a crash during worktree removal cancels the prepared takeover and restores dependencies")),
    pytest.param("working", marks=pytest.mark.variant("back at work with its open item still there")), "retired",
])
def test_a_takeover_that_died_is_settled_without_its_open_item(db, boss, worker, takeover, told, monkeypatch, after):
    """Whatever became of the worker, the sweep finishes or takes back what was prepared."""
    waiting = tasks.enqueue(db, boss, db.get_workspace(boss.workspace_id), "developer", "after it", "assign",
                            isolate=True, branch=None, done_when=None, files=None, depends_on=["w1"])

    def die(*a, **k):
        if after == "retired":   # it had finished removing the worktree
            db.delete_workspace("w")
            db.delete_agent("w1")
        raise KeyboardInterrupt

    finish = workspaces.remove
    monkeypatch.setattr(workspaces, "remove", die)
    with pytest.raises(KeyboardInterrupt):
        open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER, panes())
    monkeypatch.setattr(workspaces, "remove", finish)
    if after == "answered":
        open_items.close(db, "w1")
    elif after == "working":
        db.update_agent("w1", status="processing")

    open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER + open_items.CLAIM_EXPIRES + 1, panes())
    assert db.list_tasks(state=tasks.UNANSWERED) == [] and db.marks_of_kind(open_items.TAKEOVER) == []
    if after == "retired":
        assert len(takeover["started"]) == 1 and "took it over" in told[-1][1]
    else:
        assert takeover["started"] == [] and takeover["removed"] == []
        assert tasks._loads(db.get_task(waiting.id).depends_on) == ["w1"]


@pytest.mark.parametrize("dies", [
    pytest.param("after its dependency write", marks=pytest.mark.variant("A crash immediately after rewriting dependencies must leave enough state to restore the original worker")),
    pytest.param("while resuming", marks=pytest.mark.variant("a second kill, during the resume")),
])
def test_a_killed_takeover_can_always_restore_the_dependencies(db, boss, worker, takeover, monkeypatch, dies):
    waiting = tasks.enqueue(db, boss, db.get_workspace(boss.workspace_id), "developer", "after it", "assign",
                            isolate=True, branch=None, done_when=None, files=None, depends_on=["w1"])
    write = tasks.set_dependencies

    def wrote_then_died(*a):
        write(*a)
        raise KeyboardInterrupt

    def die(*a, **k):
        raise KeyboardInterrupt

    at = T0 + open_items.AUTO_REASSIGN_AFTER
    with monkeypatch.context() as killed, pytest.raises(KeyboardInterrupt):
        killed.setattr(tasks, "set_dependencies", wrote_then_died)
        open_items.sweep(db, at, panes())
    assert tasks._loads(db.get_task(waiting.id).depends_on) != ["w1"]
    if dies == "while resuming":
        at += open_items.CLAIM_EXPIRES + 1
        with monkeypatch.context() as killed, pytest.raises(KeyboardInterrupt):
            killed.setattr(open_items, "_unstarted", die)
            open_items.sweep(db, at, panes())

    open_items.close(db, "w1")   # its supervisor answers
    open_items.sweep(db, at + open_items.CLAIM_EXPIRES + 1, panes())
    assert tasks._loads(db.get_task(waiting.id).depends_on) == ["w1"]
    assert db.list_tasks(state=tasks.UNANSWERED) == [] and db.marks_of_kind(open_items.TAKEOVER) == []


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
