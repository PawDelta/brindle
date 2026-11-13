"""Starting a supervisor on another branch/worktree, and handing a session over."""

import time

import pytest

from brindle import agents, autopilot, bus, git, sessions, workspaces
from brindle.db import Agent, Task

from conftest import sh


def add(db, ws, aid, mode="interactive", parent=None, status="running"):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", None, time.time(), time.time(), "task", f"sess-{aid}")
    db.add_agent(a)
    return a


def test_checkout_for_adopts_a_worktree_the_user_made(db, repo, tmp_path):
    mine = tmp_path / "mine"
    sh(f"git worktree add -q -b topic {mine}", repo)
    ws = workspaces.checkout_for(db, str(repo), branch="topic")
    assert ws.path == str(mine.resolve()) and ws.repo_root == str(repo)
    assert workspaces.checkout_for(db, str(repo), worktree=str(mine)).id == ws.id
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.checkout_for(db, str(repo), branch="other", worktree=str(mine))
    assert git.current_branch(mine) == "topic"
    assert db.get_workspace(ws.id) == ws and mine.is_dir()


def test_checkout_for_worktree_path_creates_it_with_the_branch(db, repo, tmp_path):
    path = tmp_path / "wt" / "feature"
    ws = workspaces.checkout_for_target(db, str(repo), str(path))
    assert ws.path == str(path.resolve()) and git.current_branch(path) == "feature"
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.checkout_for(db, str(repo), worktree=str(tmp_path / "nope"))


def test_handover_moves_goal_workers_tasks_and_carries_the_note(db, repo, monkeypatch):
    from brindle import knowledge

    root = workspaces.adopt_root(db, str(repo))
    dest = workspaces.checkout_for(db, str(repo), branch="integration")
    add(db, root, "old")
    add(db, root, "onlooker")
    db.update_agent("old", profile="developer")   # launched before launches were supervisor-only
    worker = workspaces.create(db, str(repo), "w-branch").workspace
    add(db, worker, "w1", mode="assign", parent="old")
    db.add_autopilot("old")
    autopilot.set_goal(db, "old", "ship it", [("one", "true", None), ("two", "false", "d", "reviewer")], "why", summary="Release")
    m1 = db.milestones("old")[0]
    db.record_check(m1.id, True, "ok", "abc")
    db.add_task(Task("t1", str(repo), None, "old", root.id, "developer", "later", "assign", 1, None,
                     None, None, '["w1"]', "pending", time.time()))
    autopilot.wait_on(db, "old", until=time.time() + 60, note="training")
    db.add_open_item("w1", "old", "question", "which db?", time.time(), time.time())

    spawned = {}

    def fake_spawn(db_, ws, profile, **kw):
        spawned.update(kw, ws=ws, profile=profile)
        a = add(db_, ws, "new")
        if kw["autopilot"]:
            db_.add_autopilot("new")
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    paused = []
    monkeypatch.setattr(agents, "pause", lambda db_, rid, **kw: paused.append(rid))

    new = sessions.handover(db, "old", dest, "rule: never push to main")

    assert new.id == "new" and spawned["ws"].id == dest.id and paused == ["old"]
    assert [i.worker_id for i in db.open_items("new")] == ["w1"] and db.open_items("old") == []
    assert spawned["profile"] == "supervisor"
    assert "rule: never push to main" in spawned["prompt"] and "w1" in spawned["prompt"]
    ap = db.get_autopilot("new")
    assert ap.goal == "ship it" and ap.detail == "why"
    assert ap.summary == "Release"
    ms = db.milestones("new")
    assert [(m.title, m.status, m.profile) for m in ms] == [("one", "passed", None), ("two", "pending", "reviewer")]
    assert db.get_agent("w1").parent_id == "new"
    task = db.get_task("t1")
    assert task.caller_id == "new" and task.caller_ws_id == dest.id
    assert autopilot.on_stop(db, db.get_agent("old"), {}) is None
    assert [(e["root_id"], e["payload"]) for e in bus.tail(db, kinds=("session.handover",))] == [
        ("new", {"from_root": "old"})]
    # The old session, still up, stops advertising the goal it handed over.
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    found = knowledge.who_is_working(db, knowledge.repo_key(str(repo)), "onlooker")
    assert [s["session"] for s in found] == ["new"]
    # The wait went with the session: once it ends, one notice, to the new supervisor.
    for root_id in ("old", "new"):
        autopilot.wait_ended(db, db.get_autopilot(root_id), now=time.time() + 120)
    told = db.conn.execute("SELECT agent_id FROM inbox WHERE instr(body, ?) > 0",
                           (autopilot.WAIT_ENDED_HEAD,)).fetchall()
    assert [r["agent_id"] for r in told] == ["new"]

@pytest.mark.parametrize("own_goal", [
    pytest.param(None, marks=pytest.mark.variant("Handover must reuse a live supervisor with no goal and migrate its worker and queued task")),
    pytest.param("done", marks=pytest.mark.variant("A completed destination goal must allow reuse of its supervisor without starting another")),
    "running", "handed_away", "send_fails",
])
def test_handover_to_a_live_supervisor_delivers_to_it_instead_of_starting_another(db, repo, monkeypatch, own_goal):
    from brindle import tmux

    root = workspaces.adopt_root(db, str(repo))
    dest = workspaces.checkout_for(db, str(repo), branch="integration")
    add(db, root, "old")
    add(db, dest, "gone", status="paused")      # a paused chat there is not the one taking over
    add(db, dest, "there")
    spawned, made = [], []

    def fake_spawn(db_, ws, profile, **kw):
        spawned.append(ws.id)
        made.append(ws.id)
        a = add(db_, ws, f"new{len(made)}")
        db_.add_autopilot(a.id)
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "there")
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    if own_goal in ("done", "running", "handed_away"):
        db.add_autopilot("there")
        autopilot.set_goal(db, "there", "its own work", [("mine", "true", None)])
    if own_goal in ("done", "running"):
        db.update_autopilot("there", state=own_goal)
    elif own_goal == "handed_away":
        # A round trip: "there" handed its goal on, and stays up ending its turn.
        away = workspaces.checkout_for(db, str(repo), branch="elsewhere")
        sessions.handover_to(db, "there", away, pause_old=False)
        spawned.clear()
    worker = workspaces.create(db, str(repo), "w-branch").workspace
    add(db, worker, "w1", mode="assign", parent="old")
    db.add_autopilot("old")
    autopilot.set_goal(db, "old", "ship it", [("one", "true", None)])
    db.add_task(Task("t1", str(repo), None, "old", root.id, "developer", "later", "assign", 1, None,
                     None, None, '["w1"]', "pending", time.time()))
    told = []

    def fake_send(db_, to, body, *a, **kw):
        if own_goal == "send_fails":
            raise tmux.TmuxError("paste failed")
        told.append((to, body))

    monkeypatch.setattr(agents, "send_message", fake_send)
    paused = []
    monkeypatch.setattr(agents, "pause", lambda db_, rid, **kw: paused.append(rid))

    h = sessions.handover_to(db, "old", dest, "rule: never push to main")

    # An unfinished goal of its own stays the live supervisor's: a new one takes the session, and says why.
    to = "new1" if own_goal == "running" else "there"
    assert h.agent.id == to and db.get_autopilot(to).goal == "ship it" and paused == ["old"]
    assert db.get_agent("w1").parent_id == to and db.get_task("t1").caller_id == to
    assert [e["root_id"] for e in bus.tail(db, kinds=("session.handover",))][-1] == to
    if own_goal == "running":
        assert spawned == [dest.id] and told == [] and "unfinished goal" in h.why
        assert db.get_autopilot("there").goal == "its own work"
    elif own_goal == "send_fails":
        # The paste failed after the move was saved: the note waits in its inbox instead.
        queued = db.conn.execute("SELECT body FROM inbox WHERE agent_id='there'").fetchall()
        assert spawned == [] and h.why is None and "rule: never push to main" in queued[-1]["body"]
    else:
        assert spawned == [] and h.why is None
        assert [t for t, _ in told] == ["there"] and "rule: never push to main" in told[0][1]
