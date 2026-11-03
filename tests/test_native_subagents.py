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




def test_killing_the_parent_ends_its_running_subagents(db, ws, monkeypatch):
    monkeypatch.setattr(tmux, "kill_window", lambda w: None)
    fake_agent(db, ws, agent_id="boss", mode="assign")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    agents.kill(db, "boss")
    assert db.get_agent("boss") is None  # cascaded away with the parent




# -- sidebar: snapshot and render ---------------------------------------------






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
