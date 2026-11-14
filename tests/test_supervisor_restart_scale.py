"""Large goals must not feed their completed history into a restart loop."""

import json
from dataclasses import replace

import pytest

from brindle import bus, supervisor_state
from brindle.db import Task
from fast_repo import repo, repo_template  # noqa: F401
from test_efficiency_supervisor import restarting, session, transcript  # noqa: F401
from test_merge_order import add


def test_large_seed_keeps_current_work_before_completed_history(db, session, tmp_path):
    session(supervisor_state=True)
    root_ws = db.get_workspace(db.get_agent("boss").workspace_id)
    db.set_milestones("boss", [(f"milestone-{i}: " + "acceptance detail " * 40, "true", None)
                               for i in range(48)])
    for milestone in db.milestones("boss")[:36]:
        db.record_check(milestone.id, True, "verified")
    # A previously passed check that regressed is still essential work.
    regressed = db.milestones("boss")[35]
    db.record_check(regressed.id, False, "regression", state="regressed")
    for i in range(180):
        wid, branch = f"worker-{i}", f"feat/scale-{i}"
        ws = replace(root_ws, id=f"ws-{i}", name=branch, branch=branch,
                     path=str(tmp_path / branch), kind="worktree")
        db.add_workspace(ws)
        add(db, ws, wid, "assign", parent="boss", status="processing" if i >= 170 else "idle")
        db.set_result(wid, f"result-{i}: " + "long findings and implementation notes " * 100)
        bus.publish_agent(db, "pipeline.merged" if i < 170 else "pipeline.reported", wid, branch=branch)
    # Workers sharing a branch have separate in-flight work and no results yet.
    for i in range(2):
        add(db, root_ws, f"shared-{i}", "assign", parent="boss", status="processing")
    for i in range(12):
        db.add_task(Task(f"queued-{i}", root_ws.repo_root, None, "boss", root_ws.id,
                         "developer", f"Implement queued work {i}", "assign", 1,
                         f"feat/queued-{i}", None, None, None,
                         ("pending", "held", "starting")[i % 3], float(i)))
    for i in range(50):
        bus.publish_agent(db, "autopilot.audit_decision", "boss",
                          decision=f"decision-{i}: " + "background rationale " * 60)
    bus.publish_agent(db, "autopilot.need_user", "boss", ask=True, question="Which deployment region?")
    path = tmp_path / "many-messages.jsonl"
    path.write_text("".join(json.dumps({"type": "user", "message": {
        "content": f"latest-{i}: " + "instructions " * 100}}) + "\n" for i in range(80)))

    seed = supervisor_state.seed_prompt(db, "boss", path)
    assert "shared-0" in seed and "shared-1" in seed
    assert len(seed.encode()) < 32_000  # comfortably below a 120k-token context, even at one byte/token
    for milestone in db.milestones("boss")[35:]:
        assert milestone.title in seed
    for i in range(170, 180):
        assert f"feat/scale-{i}" in seed and f"worker-{i}" in seed and f"result-{i}:" in seed
    for i in range(12):
        assert f"queued-{i}" in seed and f"Implement queued work {i}" in seed
    assert "Which deployment region?" in seed
    for _, message in supervisor_state.latest_messages(path):
        assert message in seed
    assert "result-0:" not in seed and db.milestones("boss")[0].title not in seed
    assert all(tool in seed for tool in ("get_progress", "list_agents", "read_messages"))
    # Compaction is a view: the detailed state is still available after a restart.
    full = supervisor_state.render(db, "boss")
    assert "result-0:" in full and db.milestones("boss")[0].title in full


def test_fresh_supervisor_does_not_immediately_restart_again(db, restarting, tmp_path, monkeypatch):
    calls = restarting()
    [argv] = calls.detached
    calls.helper(argv)
    db.update_agent("boss", session_ref="fresh-chat")
    db.set_status("boss", "idle")
    path = transcript(tmp_path, 60_000, "Continue the goal.")
    payload = {"transcript_path": str(path), "session_id": "fresh-chat"}
    now = supervisor_state.time.time()
    monkeypatch.setattr(supervisor_state.time, "time", lambda: now)
    assert not supervisor_state.on_turn_end(db, db.get_agent("boss"), payload)
    assert len(calls.detached) == 1 and not db.restart_holding("boss")
    # Suppression survives a new connection, and expires without blocking later restarts.
    from brindle.db import DB

    reopened = DB()
    try:
        assert not supervisor_state.on_turn_end(reopened, reopened.get_agent("boss"), payload)
        monkeypatch.setattr(supervisor_state.time, "time", lambda: now + 3600)
        assert supervisor_state.on_turn_end(reopened, reopened.get_agent("boss"), payload)
        calls.helper(calls.detached[-1])
        assert [item["session_ref"] for item in supervisor_state.previous_sessions(reopened, "boss")] == [
            "old-long-chat", "fresh-chat"]
    finally:
        reopened.conn.close()


def test_oversized_current_work_leaves_the_old_session_running(db, restarting):
    calls = restarting()
    db.update_autopilot("boss", detail="Unverified requirements must be retained. " * 2000)
    calls.helper(calls.detached[0])
    assert calls.stopped == [] and calls.launched == []
    assert db.get_agent("boss").status == "idle"
    assert db.get_agent("boss").session_ref == "old-long-chat"
    assert not db.restart_holding("boss")
    assert supervisor_state.previous_sessions(db, "boss") == []
    assert db.pending_count("boss") == 1
    with pytest.raises(supervisor_state.SeedTooLarge):
        supervisor_state.seed_prompt(db, "boss")
