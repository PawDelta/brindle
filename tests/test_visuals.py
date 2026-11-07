import time

import pytest
from starlette.testclient import TestClient

from brindle import agents, mcp_server, visual_offers, visuals
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.service import inbox
from brindle.service.errors import ServiceError

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OPTIONS = [{"id": "A", "label": "Tag first"}, {"id": "B", "label": "Gate on CI"}]
SECRET = "api_key = " + "Zq8vR2mK7pX4tL9w" + "B3nY6cH1"   # secret-shaped, not a real one


@pytest.fixture
def session(db, tmp_path, monkeypatch):
    """A supervisor (headless, so a message is queued in the db) and its worker."""
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    # These tests exercise page storage and revisions; consent is covered in test_visual_offers.
    monkeypatch.setattr(visual_offers, "consume", lambda c, root, kind: None)
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: True)
    repo = tmp_path / "repo"
    ws = Workspace("ws1", str(repo), "main", "main", "main", None, str(repo), None, "brindle-ws1", time.time())
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, time.time(),
                 headless=1)
    worker = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "%2", None, time.time())
    db.add_agent(boss)
    db.add_agent(worker)
    return ws, boss, worker


@pytest.fixture
def client():
    with TestClient(build_app(TOKEN), headers=AUTH) as c:
        yield c


@pytest.mark.parametrize("caller,html,options,enabled,refused", [
    ("w1", "<p>ok</p>", OPTIONS, True, None),
    ("w1", "x" * (visuals.MAX_BYTES + 1), None, True, "too_big"),
    ("w1", "<p>ok</p>", [{"id": "A", "label": "a"}, {"id": "A", "label": "b"}], True, "invalid"),
    ("w1", f"<p>{SECRET}</p>", None, True, "secret"),
    (None, "<p>ok</p>", None, True, "invalid"),
    ("w1", "<p>ok</p>", None, False, "not_enabled"),
])
def test_publish_refuses_each_bad_page(db, session, monkeypatch, caller, html, options, enabled, refused):
    ws, boss, worker = session
    if not enabled:
        monkeypatch.delenv("BRINDLE_VISUALS")
    who = db.get_agent(caller) if caller else None
    if refused:
        with pytest.raises(ServiceError) as e:
            visuals.publish(db, who, "Release flow", html, "Which flow?" if options else None, options)
        assert e.value.code == refused
        assert db.conn.execute("SELECT COUNT(*) FROM visuals").fetchone()[0] == 0
        return
    v = visuals.publish(db, who, "Release flow", html, "Which flow?", options)
    # A worker publishes into its supervisor's session, in a folder git ignores.
    assert (v["root_id"], v["by_agent"], v["rev"]) == ("boss", "w1", 1)
    assert visuals.page_path(ws.repo_root, "boss", v["id"], 1).read_text() == html
    assert (visuals.store_root(ws.repo_root) / ".gitignore").exists()


def test_an_answer_reaches_the_supervisor_once_and_only_for_the_revision_on_screen(db, session, client,
                                                                                 monkeypatch):
    ws, boss, worker = session
    monkeypatch.setenv("BRINDLE_AGENT_ID", worker.id)
    mcp_server.publish_visual("Release flow", "<p>r1</p>", "Which flow?", OPTIONS)
    [v] = client.get(f"/v1/sessions/{boss.id}/visuals").json()
    assert (v["rev"], v["unread"], v["by"]) == (1, True, "w1")
    [item] = [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]
    assert (item.visual_id, item.root_id) == (v["id"], boss.id)
    assert client.post(f"/v1/visuals/{v['id']}/read").json()["unread"] is False
    assert client.post(f"/v1/visuals/{v['id']}/keep").json()["kept"] is True     # r1 is kept

    # A new revision arrives while r1 is on screen: r1's answer is refused, not read against r2.
    mcp_server.publish_visual("Release flow", "<p>r2</p>", "Which flow now?",
                              [{"id": "B", "label": "Gate"}, {"id": "C", "label": "Two"}], v["id"])
    stale = client.post(f"/v1/visuals/{v['id']}/answer", json={"rev": 1, "option": "B"})
    assert stale.status_code == 409 and stale.json()["error"]["rev"] == 2

    # A note that looks like a secret is refused before anything is recorded or sent.
    leak = client.post(f"/v1/visuals/{v['id']}/answer", json={"rev": 2, "option": "C", "note": SECRET})
    assert leak.status_code == 400 and leak.json()["error"]["code"] == "secret"
    assert SECRET not in leak.text
    got = client.post(f"/v1/visuals/{v['id']}/answer", json={"rev": 2, "option": "C", "note": "ship it"})
    assert got.status_code == 200 and got.json()["answer"]["option"] == "C"
    again = client.post(f"/v1/visuals/{v['id']}/answer", json={"rev": 2, "option": "B"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "answered"
    [body] = [r["body"] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (boss.id,))]
    assert f"answered visual {v['id']} (r2): option C" in body and "ship it" in body
    assert not [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]
    assert "answered: option C" in mcp_server.list_visuals()
    # The kept copy is still r1, so r2's answer isn't stamped on it.
    assert client.get(f"/v1/kept-visuals/{v['id']}").json()["answer"] is None


@pytest.mark.variant("publication race: another publisher writes r2 between the first publisher's checks and write")
def test_a_revision_published_meanwhile_keeps_its_page(db, session, monkeypatch):
    ws, boss, worker = session
    v = visuals.publish(db, boss, "First", "<p>r1</p>")
    real = visuals._private_dir
    interleaved = []

    def winner_publishes_first(path):
        # Between the loser's checks and its write, another publisher's r2 lands.
        if not interleaved:
            interleaved.append(None)
            interleaved[0] = visuals.publish(db, worker, "Winner", "<p>winner</p>", visual_id=v["id"])
        return real(path)

    monkeypatch.setattr(visuals, "_private_dir", winner_publishes_first)
    loser = visuals.publish(db, boss, "Later", "<p>later</p>", visual_id=v["id"])
    assert (interleaved[0]["rev"], loser["rev"]) == (2, 3)
    assert visuals.page_path(ws.repo_root, boss.id, v["id"], 2).read_text() == "<p>winner</p>"
    assert visuals.page_path(ws.repo_root, boss.id, v["id"], 3).read_text() == "<p>later</p>"


def test_removing_a_session_deletes_its_visuals_but_not_the_kept_one(db, session, client):
    ws, boss, worker = session
    kept = visuals.publish(db, boss, "Keep me", "<p>kept</p>")
    gone = visuals.publish(db, boss, "Drop me", "<p>gone</p>")
    # A session named with something secret-shaped: Keep refuses before writing anything.
    db.update_agent(boss.id, name=SECRET)
    leak = client.post(f"/v1/visuals/{kept['id']}/keep")
    assert leak.status_code == 400 and leak.json()["error"]["code"] == "secret" and SECRET not in leak.text
    assert client.get("/v1/kept-visuals").json() == []
    assert not visuals.kept_path(ws.repo_root, kept["id"]).exists()
    db.update_agent(boss.id, name=None)
    assert client.post(f"/v1/visuals/{kept['id']}/keep").json()["kept"] is True
    client.post(f"/v1/visuals/{gone['id']}/keep")
    assert client.delete(f"/v1/visuals/{gone['id']}/keep").json()["kept"] is False

    db.delete_workspace(ws.id)

    assert db.conn.execute("SELECT COUNT(*) FROM visuals").fetchone()[0] == 0
    assert not visuals.page_path(ws.repo_root, boss.id, gone["id"], 1).exists()
    assert client.get(f"/v1/sessions/{boss.id}/visuals").status_code == 404
    [listed] = client.get("/v1/kept-visuals").json()
    assert (listed["id"], listed["title"], listed["from_session_name"]) == (kept["id"], "Keep me", "boss")
    assert client.get(f"/v1/kept-visuals/{kept['id']}").json() == listed
    page = client.get(f"/v1/kept-visuals/{kept['id']}/html")
    assert page.text == "<p>kept</p>" and "default-src 'none'" in page.headers["content-security-policy"]


@pytest.mark.parametrize("ref,rev,status", [
    ("own", "1", 200),
    ("own", "2", 404),                 # a revision the store doesn't have
    ("v_000000000000", "1", 404),      # another, unknown id
    pytest.param("%2e%2e", "1", 404, marks=pytest.mark.variant("a path step out of the store")),
    pytest.param("own", "1.html", 404, marks=pytest.mark.variant("a file name instead of a revision")),
])
def test_a_page_is_served_only_for_known_revisions_and_with_the_csp(db, session, client, ref, rev, status):
    ws, boss, worker = session
    v = visuals.publish(db, boss, "Page", "<p>page</p>")
    got = client.get(f"/v1/visuals/{v['id'] if ref == 'own' else ref}/r/{rev}")
    assert got.status_code == status
    if status == 200:
        assert got.text == "<p>page</p>"
        assert got.headers["content-security-policy"] == visuals.CSP
