import time

from brindle import agents, autopilot, knowledge, mcp_server, sessions, workspaces
from brindle.db import Agent, Task


def test_intents_projection_rebuilds(db):
    key = "github.com/acme/app"
    knowledge.append(db, key, knowledge.INTENT, {"summary": "auth", "paths": ["src/auth/**"]},
                     session="s1", ts=1.0)
    knowledge.append(db, key, knowledge.INTENT, {"summary": "billing", "paths": ["src/bill.py"]},
                     session="s2", ts=2.0)
    knowledge.append(db, key, knowledge.PAUSE, {}, session="s1", ts=3.0)
    before = knowledge.intents(db, key)
    assert [(i["session_root"], i["status"], i["paths"]) for i in before] == [
        ("s2", "live", ["src/bill.py"]), ("s1", "paused", ["src/auth/**"])]

    db.conn.execute("DELETE FROM intents")
    knowledge.rebuild(db, key)
    assert knowledge.intents(db, key) == before


def test_set_goal_writes_live_intent(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_autopilot("boss")
    key = knowledge.repo_key(ws.repo_root)

    autopilot.set_goal(db, "boss", "Settings page", [("API", "true", None), ("UI", "true", None)])
    [intent] = knowledge.intents(db, key)
    assert (intent["session_root"], intent["status"]) == ("boss", "live")
    assert intent["summary"].splitlines() == ["Settings page", "API", "UI"]

    agents.pause(db, "boss", stop_procs=False, stop_local_models=False)
    assert [i["status"] for i in knowledge.intents(db, key)] == ["paused"]

    agents.resume(db, "boss", watch_pane=False)
    assert [i["status"] for i in knowledge.intents(db, key)] == ["live"]

    agents.pause(db, "boss", stop_procs=False, stop_local_models=False)
    [session] = sessions.paused(db, ws.repo_root)
    sessions._forget(db, session)
    assert knowledge.intents(db, key) == []


def test_other_sessions_block_and_who_is_working(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    key = knowledge.repo_key(ws.repo_root)

    def add(aid, status="idle", mode="interactive", parent=None, wsid=None):
        db.add_agent(Agent(aid, wsid or ws.id, "supervisor" if mode == "interactive" else "developer",
                           "claude", parent, mode, status, "", None, time.time(), time.time(),
                           f"goal of {aid}", f"sess-{aid}"))

    for sid in ("me", "intent", "tasked", "neither"):
        add(sid)
    add("worker", "processing", "assign", "tasked")
    knowledge.append(db, key, knowledge.INTENT, {"summary": "auth", "paths": ["src/a.py"]},
                     session="intent")
    db.add_task(Task("t1", ws.repo_root, "worker", "tasked", ws.id, "developer", "do x", "assign",
                     True, "feat-x", None, '["src/x.py"]', None, "started", time.time(), time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)

    block = knowledge.other_sessions_block(db, key, "me")
    assert "goal of intent" in block and "goal of tasked" in block
    assert "goal of neither" not in block and "goal of me" not in block

    monkeypatch.setenv("BRINDLE_AGENT_ID", "me")
    assert mcp_server.who_is_working(paths=["src/x.py"]).count("session ") == 1
    assert "tasked" in mcp_server.who_is_working(paths=["src/x.py"])
    assert "intent" in mcp_server.who_is_working(query="a.py")
