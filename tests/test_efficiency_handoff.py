"""Keep enough quota to finish work and preserve progress across worker takeovers."""

import json
from pathlib import Path

import pytest

from brindle import agents, autopilot, git, open_items, tasks, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent
from test_merge_order import fake_spawn, quiet_removal, repo, worktree
from test_routing import everything_available
from test_stuck_workers import T0, boss, told


def test_routing_reserves_headroom_for_task_size(db, repo, boss, everything_available, monkeypatch):
    from brindle import quota

    (repo / ".brindle" / "config.json").write_text(json.dumps({
        "usage_limit": 90, "limit_headroom": 12,
        "routing": {w: ["developer-gemini", "developer-codex"] for w in ("heavy", "medium", "light")},
    }))
    room = 22
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: room if provider == "antigravity" else 100)

    def choose(weight):
        return autopilot.choose_profile(db, boss.id, str(repo), None, "task", None, weight)[0]

    assert choose("heavy") == "developer-codex"
    assert choose("medium") == "developer-gemini"
    room = 16
    assert choose("medium") == "developer-codex"
    assert choose("light") == "developer-gemini"
    cfg = load_repo_config(repo)
    cfg.limit_headroom = 0
    assert autopilot._unavailable("developer-gemini", cfg, str(repo), weight="heavy") is None


@pytest.mark.slow("Exercises real git branch creation, retirement and re-staffing")
def test_restaffed_worker_continues_commits_and_notes(db, repo, boss, told, monkeypatch):
    (repo / ".brindle" / "config.json").write_text(json.dumps({"branch_prefix": "feat/"}))
    quiet_removal(monkeypatch)
    monkeypatch.setattr(agents, "_stop", lambda *a, **k: None)
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "model_problem", lambda *a: None)
    monkeypatch.setattr(tasks, "room_wait", lambda *a: None)
    monkeypatch.setattr(tasks, "_claim_fair", lambda db_, t: (db_.claim_task(t.id), None, None))
    ws = worktree(db, repo, "feat/progress", files={"progress.py": "implemented = True\n"})
    tip = git.out(["rev-parse", "HEAD"], ws.path)
    db.add_agent(Agent("old", ws.id, "developer", "claude", boss.id, "assign", "failed",
                       "@999", None, T0, task="Finish the implementation"))
    old_task = tasks.enqueue(db, boss, db.get_workspace(boss.workspace_id), "developer",
                             "Finish the implementation", "assign", isolate=True, branch=ws.branch,
                             done_when=None, files=["progress.py"], depends_on=[])
    db.update_task(old_task.id, agent_id="old", state="started")
    report = "Parser implemented; integration remains"
    note = "Integration remains.\nThe remaining integration must preserve identity checks"
    db.update_agent("old", result=report, pipeline="fixing")
    note_id = db.enqueue_held(boss.id, note, sender_id="old")
    db.claim_notice(boss.id)
    db.read_held(boss.id)
    # Push delivery also keeps a full held body, followed by a condensed pointer.
    pointer = db.enqueue(boss.id, agents.one_line(db, note_id, note, "old"), sender_id="old")
    db.mark_delivered(pointer)
    open_items.track(db, db.get_agent("old"), "worker died during fixes", T0)

    item = db.get_open_item("old")
    open_items.reassign(db, item, T0 + open_items.AUTO_REASSIGN_AFTER, actor=boss)

    [replacement] = db.list_tasks(state="started")
    worker = db.get_agent(replacement.agent_id)
    resumed = db.get_workspace(worker.workspace_id)
    assert resumed.branch == ws.branch
    assert git.out(["rev-parse", "HEAD"], resumed.path) == tip
    assert (Path(resumed.path) / "progress.py").read_text() == "implemented = True\n"
    assert report in worker.task and note in worker.task
    # A repeated trigger must respect the replacement already in flight.
    assert open_items.reassign(db, item,
                               T0 + open_items.AUTO_REASSIGN_AFTER, actor=boss) is None
    assert len(db.list_tasks(state="started")) == 1
