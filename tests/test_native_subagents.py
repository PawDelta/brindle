"""Claude Code's own built-in subagents (its Agent tool), surfaced in brindle's
sidebar via the SubagentStart/SubagentStop hooks."""

import sqlite3
import time

import pytest

from brindle import agents, tmux, view, watch
from brindle.db import DB, Agent


def fake_agent(db, ws, status="processing", mode="assign", parent=None, agent_id="a1"):
    a = Agent(agent_id, ws.id, "developer", "claude", parent, mode, status,
              f"%fake-{agent_id}", None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def ws(db, repo):
    from brindle import workspaces

    return workspaces.create(db, str(repo), "feature").workspace


# -- hooks are registered -----------------------------------------------------


# -- hook payloads create/end rows without touching the parent's status ------


# -- kept out of agents-table-backed views -------------------------------------


# -- a stopped parent never shows a subagent as still running -----------------


def test_pausing_the_parent_ends_its_running_subagents(db, ws, monkeypatch):
    monkeypatch.setattr(tmux, "kill_window", lambda w: None)
    monkeypatch.setattr(tmux, "kill_session", lambda s: None)
    monkeypatch.setattr(tmux, "windows", lambda s: [])
    fake_agent(db, ws, agent_id="boss", mode="interactive", status="processing")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})

    agents.pause(db, "boss")
    assert db.get_agent("boss").status == "paused"
    [row] = db.native_subagents("boss")
    assert row.ended_at is not None

    [snap_ws] = view.snapshot(db, ws.repo_root)
    [entry] = [a for a in snap_ws["agents"] if a["id"] == "boss"]
    assert all(s["ended_at"] is not None for s in entry["subagents"])


def test_killing_the_parent_ends_its_running_subagents(db, ws, monkeypatch):
    monkeypatch.setattr(tmux, "kill_window", lambda w: None)
    fake_agent(db, ws, agent_id="boss", mode="assign")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    agents.kill(db, "boss")
    assert db.get_agent("boss") is None  # cascaded away with the parent


def test_agent_entry_drops_running_subs_once_the_parent_is_not_running(db, ws):
    # A parent's process can exit without ever going through agents.pause/kill
    # (e.g. the person closed its terminal), so the view itself must not show
    # a subagent as running once the parent's computed status says otherwise.
    fake_agent(db, ws, agent_id="w1", mode="assign", status="processing")
    agents.handle_hook(db, "w1", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    entry = view.agent_entry(db, db.get_agent("w1"), detail=True)
    assert entry["status"] in view._PARENT_NOT_RUNNING  # no real tmux window: it reads as "exited"
    assert entry["subagents"] == []


# -- sidebar: snapshot and render ---------------------------------------------


def test_snapshot_hides_a_subagent_that_finished_a_while_ago(db, ws, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)  # boss is actually running
    fake_agent(db, ws, agent_id="boss", mode="interactive", status="processing")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    agents.handle_hook(db, "boss", "subagent-stop", {"agent_id": "sub1", "agent_type": "Explore"})
    later = time.time() + 60
    monkeypatch.setattr(time, "time", lambda: later)

    [snap_ws] = view.snapshot(db, ws.repo_root)
    [entry] = [a for a in snap_ws["agents"] if a["id"] == "boss"]
    assert entry["subagents"] == []


def test_render_draws_subagents_one_level_deeper_and_unselectable():
    now = 1000.0
    snap = [{
        "id": "repo/feat", "name": "feat", "branch": "feat", "base_branch": "main",
        "path": "/", "ahead": 0, "behind": 0, "dirty": 0,
        "agents": [{
            "id": "boss", "profile": "supervisor", "provider": "claude", "status": "processing",
            "mode": "interactive", "status_since": now - 30, "pending": 0, "reported": False,
            "window": "@1",
            "subagents": [
                {"id": "sub1", "agent_type": "Explore", "started_at": now - 60, "ended_at": None},
                {"id": "sub2", "agent_type": "Plan", "started_at": now - 90, "ended_at": now - 5},
            ],
        }],
    }]
    lines = watch.render(snap, now=now)
    running = next(ln for ln in lines if "Explore" in ln.text)
    done = next(ln for ln in lines if "Plan" in ln.text)
    assert running.text.strip().startswith("↳ Explore · running")
    assert running.agent is None and running.style == "busy"
    assert done.text.strip() == "↳ Plan · ✓ done"
    assert done.agent is None and done.style == "dim"


# -- migration -----------------------------------------------------------------


def test_migration_adds_the_native_subagents_table_to_an_old_db(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, repo_root TEXT NOT NULL, name TEXT NOT NULL,
          kind TEXT NOT NULL, branch TEXT NOT NULL, base_branch TEXT, path TEXT NOT NULL,
          port_base INTEGER, tmux_session TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE agents (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, profile TEXT NOT NULL,
          provider TEXT NOT NULL, parent_id TEXT, mode TEXT NOT NULL, status TEXT NOT NULL,
          tmux_window TEXT NOT NULL, result TEXT, created_at REAL NOT NULL);
        INSERT INTO workspaces (id, repo_root, name, kind, branch, base_branch, path, port_base,
                                tmux_session, created_at)
        VALUES ('w','/r','n','main','b',NULL,'/p',NULL,'s',0);
        INSERT INTO agents VALUES ('a','w','p','claude',NULL,'assign','idle','@1',NULL,7.0);
    """)
    con.commit()
    con.close()

    db = DB(str(path))
    db.start_native_subagent("sub1", "a", "Explore")
    [row] = db.native_subagents("a")
    assert row.agent_type == "Explore"
