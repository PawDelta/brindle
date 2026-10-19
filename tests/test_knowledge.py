import time

from brindle import agents, autopilot, knowledge, sessions, workspaces
from brindle.db import Agent


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
