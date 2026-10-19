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


def test_overlapping_decision_needs_supersede(db, repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle import cli

    key = knowledge.repo_key(repo)
    first, _ = knowledge.record_decision(db, key, "Secrets", "keychain only", "no env vars",
                                         ["src/config/**"], session="s1")
    refused, existing = knowledge.record_decision(db, key, "secrets ", "env vars", "",
                                                  ["src/config/keys.py"], session="s2")
    assert refused is None and existing["id"] == first["id"]
    elsewhere, clash = knowledge.record_decision(db, key, "secrets", "vault", "", ["docs/**"])
    assert elsewhere and clash is None    # same topic, disjoint scope

    second, _ = knowledge.record_decision(db, key, "secrets", "env vars", "", ["src/config/keys.py"],
                                          supersedes=first["id"][:8], session="s2")
    assert knowledge.get_decision(db, first["id"])["status"] == "superseded"
    assert [d["id"] for d in knowledge.decisions(db, key)] == [elsewhere["id"], second["id"]]
    kinds = db.conn.execute("SELECT kind FROM kb_events WHERE repo_key = ?", (key,)).fetchall()
    assert [k[0] for k in kinds] == [knowledge.DECISION] * 3
    before = knowledge.decisions(db, key, status="superseded") + knowledge.decisions(db, key)
    db.conn.execute("DELETE FROM decisions")
    knowledge.rebuild(db, key)
    assert knowledge.decisions(db, key, status="superseded") + knowledge.decisions(db, key) == before

    monkeypatch.chdir(repo)
    out = repo / "decisions.md"
    assert CliRunner().invoke(cli.app, ["decisions", "export", str(out)]).exit_code == 0
    assert second["id"] in out.read_text() and "keychain only" not in out.read_text()


def test_decision_notifies_matching_session(db, repo, monkeypatch):
    from conftest import full_text

    ws = workspaces.adopt_root(db, str(repo))
    for root, file in (("me", None), ("near", "src/x.py"), ("far", "src/y.py")):
        db.add_agent(Agent(root, ws.id, "supervisor", "claude", None, "interactive", "processing",
                           "@0", None, time.time()))
        if file:
            db.add_agent(Agent(f"w-{root}", ws.id, "developer", "claude", root, "assign",
                               "processing", "", None, time.time()))
            db.add_task(Task(f"t-{root}", ws.repo_root, f"w-{root}", root, ws.id, "developer", "do",
                             "assign", True, f"feat-{root}", None, f'["{file}"]', None, "started",
                             time.time(), time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "me")

    reply = mcp_server.record_decision("parsing", "x.py parses lazily", "startup time", ["src/x.py"])
    [d] = knowledge.decisions(db, knowledge.repo_key(repo))
    assert "near" in reply
    assert d["id"][:8] in full_text(db, "near", db.pop_pending("near").body)
    assert db.pop_pending("far") is None and db.pop_pending("me") is None
    assert d["id"][:8] in mcp_server.decisions(paths=["src/*.py"])
    assert d["id"][:8] not in mcp_server.decisions(paths=["src/y.py"])
    assert d["id"][:8] in mcp_server.record_decision("Parsing", "eager", "", ["src/**"])  # refused


def test_resume_replays_receipt(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    key = knowledge.repo_key(repo)
    launched = []
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(kw["prompt"]))
    first, _ = knowledge.record_decision(db, key, "parsing", "x.py parses lazily", "", ["src/x.py"])

    near = agents.spawn(db, ws, "developer", prompt="do x", mode="assign", files=["src/x.py"])
    agents.spawn(db, ws, "developer", prompt="do y", mode="assign", files=["src/y.py"])
    assert "x.py parses lazily" in launched[0] and "x.py parses lazily" not in launched[1]
    assert knowledge.receipt(db, near.id)[0][:2] == (knowledge.DECISION_ITEM, first["id"])

    knowledge.record_decision(db, key, "parsing", "x.py parses eagerly", "", ["src/x.py"],
                              supersedes=first["id"])
    db.set_status(near.id, "paused")
    agents.resume(db, near.id)
    assert "x.py parses lazily" in launched[2] and "eagerly" not in launched[2]
