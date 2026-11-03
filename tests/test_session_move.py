"""Starting a supervisor on another branch/worktree, and handing a session over."""

import json
import time

import pytest

from brindle import agents, autopilot, bus, config, git, sessions, workspaces
from brindle.db import Agent, Task

from conftest import sh


def add(db, ws, aid, mode="interactive", parent=None, status="running"):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", None, time.time(), time.time(), "task", f"sess-{aid}")
    db.add_agent(a)
    return a


def test_config_is_found_from_a_linked_worktree_via_the_main_one(repo, tmp_path):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.local.json").write_text(json.dumps({"max_agents": 7}))
    linked = tmp_path / "linked"
    sh(f"git worktree add -q -b other {linked}", repo)
    assert not (linked / ".brindle").exists()
    assert config.load_repo_config(linked).max_agents == 7
    assert config.config_root(linked) == repo


def test_checkout_for_adopts_a_worktree_the_user_made(db, repo, tmp_path):
    mine = tmp_path / "mine"
    sh(f"git worktree add -q -b topic {mine}", repo)
    ws = workspaces.checkout_for(db, str(repo), branch="topic")
    assert ws.path == str(mine.resolve()) and ws.repo_root == str(repo)
    assert workspaces.checkout_for(db, str(repo), worktree=str(mine)).id == ws.id


def test_checkout_for_worktree_path_creates_it_with_the_branch(db, repo, tmp_path):
    path = tmp_path / "wt" / "feature"
    ws = workspaces.checkout_for_target(db, str(repo), str(path))
    assert ws.path == str(path.resolve()) and git.current_branch(path) == "feature"
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.checkout_for(db, str(repo), worktree=str(tmp_path / "nope"))


def test_checkout_for_rejects_a_worktree_on_another_branch(db, repo, tmp_path):
    mine = tmp_path / "mine"
    sh(f"git worktree add -q -b topic {mine}", repo)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.checkout_for(db, str(repo), branch="other", worktree=str(mine))


def test_handover_moves_goal_workers_tasks_and_carries_the_note(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    dest = workspaces.checkout_for(db, str(repo), branch="integration")
    add(db, root, "old")
    db.update_agent("old", profile="developer")   # launched before launches were supervisor-only
    worker = workspaces.create(db, str(repo), "w-branch").workspace
    add(db, worker, "w1", mode="assign", parent="old")
    db.add_autopilot("old")
    autopilot.set_goal(db, "old", "ship it", [("one", "true", None), ("two", "false", "d", "reviewer")], "why")
    m1 = db.milestones("old")[0]
    db.record_check(m1.id, True, "ok", "abc")
    db.add_task(Task("t1", str(repo), None, "old", root.id, "developer", "later", "assign", 1, None,
                     None, None, '["w1"]', "pending", time.time()))
    autopilot.wait_on(db, "old", until=time.time() + 60, note="training")

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
    assert spawned["profile"] == "supervisor"
    assert "rule: never push to main" in spawned["prompt"] and "w1" in spawned["prompt"]
    ap = db.get_autopilot("new")
    assert ap.goal == "ship it" and ap.detail == "why"
    ms = db.milestones("new")
    assert [(m.title, m.status, m.profile) for m in ms] == [("one", "passed", None), ("two", "pending", "reviewer")]
    assert db.get_agent("w1").parent_id == "new"
    task = db.get_task("t1")
    assert task.caller_id == "new" and task.caller_ws_id == dest.id
    assert autopilot.on_stop(db, db.get_agent("old"), {}) is None
    assert [(e["root_id"], e["payload"]) for e in bus.tail(db, kinds=("session.handover",))] == [
        ("new", {"from_root": "old"})]
    # The wait went with the session: once it ends, one notice, to the new supervisor.
    for root_id in ("old", "new"):
        autopilot.wait_ended(db, db.get_autopilot(root_id), now=time.time() + 120)
    told = db.conn.execute("SELECT agent_id FROM inbox WHERE instr(body, ?) > 0",
                           (autopilot.WAIT_ENDED_HEAD,)).fetchall()
    assert [r["agent_id"] for r in told] == ["new"]


def test_checkout_for_target_reads_a_slashed_name_as_a_branch(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    ws = workspaces.checkout_for_target(db, str(repo), "fix/handover")
    assert ws.branch == "fix/handover" and not (repo / "fix").exists()


def test_handover_to_a_live_supervisor_delivers_to_it_instead_of_starting_another(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    dest = workspaces.checkout_for(db, str(repo), branch="integration")
    add(db, root, "old")
    add(db, dest, "gone", status="paused")      # a paused chat there is not the one taking over
    add(db, dest, "there")
    worker = workspaces.create(db, str(repo), "w-branch").workspace
    add(db, worker, "w1", mode="assign", parent="old")
    db.add_autopilot("old")
    autopilot.set_goal(db, "old", "ship it", [("one", "true", None)])
    db.add_task(Task("t1", str(repo), None, "old", root.id, "developer", "later", "assign", 1, None,
                     None, None, '["w1"]', "pending", time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "there")
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    monkeypatch.setattr(agents, "spawn", lambda *a, **kw: pytest.fail("started a second supervisor"))
    told = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, *a, **kw: told.append((to, body)))

    new = sessions.handover(db, "old", dest, "rule: never push to main", pause_old=False)

    assert new.id == "there" and db.get_autopilot("there").goal == "ship it"
    assert db.get_agent("w1").parent_id == "there" and db.get_task("t1").caller_id == "there"
    assert [to for to, _ in told] == ["there"] and "rule: never push to main" in told[0][1]


def test_handed_over_session_stops_advertising_its_goal(db, repo, monkeypatch):
    from brindle import knowledge

    root = workspaces.adopt_root(db, str(repo))
    dest = workspaces.checkout_for(db, str(repo), branch="integration")
    for aid in ("old", "onlooker"):
        add(db, root, aid)
    db.add_autopilot("old")
    autopilot.set_goal(db, "old", "ship it", [("one", "true", None)])

    def fake_spawn(db_, ws, profile, **kw):
        a = add(db_, ws, "new")
        db_.add_autopilot("new")
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "new")
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    # The MCP tool's handover: the old supervisor is still up, ending its own turn.
    sessions.handover(db, "old", dest, pause_old=False)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)

    found = knowledge.who_is_working(db, knowledge.repo_key(str(repo)), "onlooker")
    assert [s["session"] for s in found] == ["new"]
