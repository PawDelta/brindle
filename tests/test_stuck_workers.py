"""A worker waiting on its supervisor is an open item the supervisor can't ignore."""

import os

import pytest

from brindle import agents, autopilot, cull, mcp_server, open_items, tasks, tmux, workspaces
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
    mcp_server.send_message("boss", "Should the cache be per user?")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    open_items.track(db, db.get_agent("w1"), "stopped without calling report_result", T0)

    first_line = autopilot.on_stop(db, boss, {})["reason"].splitlines()[0]
    assert "w1" in first_line and "Should the cache be per user?" in first_line

    if act == "send_message":
        mcp_server.send_message("w1", "Yes, per user.")
    else:
        monkeypatch.setattr(workspaces, "remove", lambda *a, **k: workspaces.Removed(False, "branch kept", None))
        mcp_server.remove_workspace("w1", delete_branch=False)
    assert db.open_items("boss") == []
    assert "w1" not in (autopilot.on_stop(db, boss, {}) or {"reason": ""})["reason"]


def test_the_stop_block_is_capped_so_the_takeover_can_come(db, boss, worker, told):
    """brindle's takeover waits for an idle supervisor, which a block with no end would rule out."""
    open_items.track(db, worker, "stopped without calling report_result", T0)
    for _ in range(open_items.MAX_STOP_BLOCKS):
        assert "w1" in autopilot.on_stop(db, boss, {})["reason"]
    assert "w1" not in (autopilot.on_stop(db, boss, {}) or {"reason": ""})["reason"]
    assert [i.worker_id for i in db.open_items("boss")] == ["w1"]


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


@pytest.mark.variant("a race: the worker wakes between the sweep's read and its retirement")
def test_a_worker_that_came_back_is_not_retired(db, boss, takeover, monkeypatch):
    real = db.claim_open_item_retire

    def wakes_during_claim(worker_id, now, expired_before):
        db.update_agent("w1", status="processing")
        return real(worker_id, now, expired_before)

    monkeypatch.setattr(db, "claim_open_item_retire", wakes_during_claim)
    open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER, panes())
    assert takeover["removed"] == [] and takeover["started"] == []
    assert db.list_tasks() == []


def test_a_plan_waiting_for_approval_is_not_a_stuck_worker(db, boss, worker, takeover):
    db.update_agent("w1", plan_state="proposed")
    held = db.get_agent("w1")
    open_items.sweep(db, T0 + open_items.AUTO_REASSIGN_AFTER, panes())
    assert takeover["removed"] == []
    db.close_open_item("w1")
    assert open_items.track(db, held, "stopped without calling report_result", T0) is False


def test_requeue_of_another_supervisors_worker_is_refused(db, boss, takeover, monkeypatch):
    db.add_agent(Agent("other", boss.workspace_id, "supervisor", "claude", None, "interactive", "idle", "@2", None, T0))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "other")
    assert mcp_server.requeue("w1").startswith("Error")
    assert takeover["removed"] == []
