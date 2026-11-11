import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from brindle import agents, bus, git, mcp_server, visual_offers, visuals
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.service import inbox
from brindle.service.errors import ServiceError

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OPTIONS = [{"id": "A", "label": "Tag first"}, {"id": "B", "label": "Gate on CI"}]
SECRET = "api_key = " + "Zq8vR2mK7pX4tL9w" + "B3nY6cH1"   # secret-shaped, not a real one
REAL_CONSUME = visual_offers.consume


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
        monkeypatch.setenv("BRINDLE_VISUALS", "0")
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


def test_publish_visual_from_a_file(db, session, tmp_path, monkeypatch):
    ws, boss, worker = session
    worktree = tmp_path / "worker"
    worktree.mkdir()
    db.conn.execute("UPDATE workspaces SET path=? WHERE id=?", (str(worktree), ws.id))
    db.conn.commit()
    monkeypatch.setenv("BRINDLE_AGENT_ID", boss.id)
    # Platform temp roots may be aliases, such as macOS /tmp -> /private/tmp.
    temp_alias = tmp_path / "temp-alias"
    temp_alias.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(temp_alias))
    scratchpad = (tmp_path / f"claude-{os.getuid()}" / re.sub(r"[^A-Za-z0-9]", "-", str(worktree))
                  / "session" / "scratchpad")
    scratchpad.mkdir(parents=True)
    page = worktree / "page.html"
    html = '<p>café</p><img src="data:image/png;base64,aGk=">'
    page.write_text(html, encoding="utf-8")
    scratch_page = scratchpad / "page.html"
    scratch_page.write_text(html, encoding="utf-8")
    inside_link = worktree / "inside.html"
    inside_link.symlink_to(page)

    monkeypatch.setattr(visual_offers, "consume", REAL_CONSUME)
    for source in (page, scratch_page, inside_link.relative_to(worktree)):
        visual_offers.person_spoke(db, boss, "show this page", typed=True)
        mcp_server.publish_visual("Page", html_path=str(source), asked="show this page")
        v = visuals.for_session(db, boss.id)[-1]
        assert visuals.page_path(ws.repo_root, boss.id, v["id"], 1).read_text(encoding="utf-8") == html

    outside = tmp_path / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    repo_page = Path(ws.repo_root) / "page.html"
    repo_page.write_text("other checkout", encoding="utf-8")
    escaped = worktree / "escaped.html"
    escaped.symlink_to(outside)
    scratch_escape = scratchpad / "escaped.html"
    scratch_escape.symlink_to(outside)
    outside_session = tmp_path / "outside-session"
    outside_scratchpad = outside_session / "scratchpad"
    outside_scratchpad.mkdir(parents=True)
    outside_scratch_page = outside_scratchpad / "page.html"
    outside_scratch_page.write_text("outside scratchpad", encoding="utf-8")
    linked_session = scratchpad.parent.parent / "linked-session"
    linked_session.symlink_to(outside_session, target_is_directory=True)
    real_read = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path.resolve() not in (outside.resolve(), repo_page.resolve(), outside_scratch_page.resolve(),
                                     tmp_path / "outside-project" / "session" / "scratchpad" / "page.html"), \
            "outside file must never be read"
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    for source in (outside, escaped, scratch_escape, repo_page, linked_session / "scratchpad" / "page.html"):
        with pytest.raises(ToolError):
            mcp_server.publish_visual("Page", html_path=str(source), asked="show this page")
    # The project ancestor can redirect scratchpad discovery too.
    project = scratchpad.parent.parent
    moved_project = tmp_path / "outside-project"
    project.rename(moved_project)
    project.symlink_to(moved_project, target_is_directory=True)
    with pytest.raises(ToolError):
        mcp_server.publish_visual("Page", html_path=str(scratch_page), asked="show this page")
    project.unlink()
    moved_project.rename(project)
    for kwargs in ({}, {"html": html, "html_path": str(page)}):
        with pytest.raises(ToolError):
            mcp_server.publish_visual("Page", **kwargs)
    for source in (worktree / "missing.html", worktree):
        with pytest.raises(ToolError):
            mcp_server.publish_visual("Page", html_path=str(source))
    page.write_bytes(b"\xff")
    with pytest.raises(ToolError):
        mcp_server.publish_visual("Page", html_path=str(page))
    for content, code in (("x" * (visuals.MAX_BYTES + 1), "too_big"), (SECRET, "secret")):
        page.write_text(content, encoding="utf-8")
        with pytest.raises(ToolError) as refused:
            mcp_server.publish_visual("Page", html_path=str(page))
        assert refused.value.__cause__.code == code
    page.write_text(html, encoding="utf-8")
    with pytest.raises(ToolError) as refused:
        mcp_server.publish_visual("Page", html_path=str(page))
    assert refused.value.__cause__.code == "visual_request_required"
    monkeypatch.delenv("BRINDLE_AGENT_ID")
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, ws))
    with pytest.raises(ToolError):
        mcp_server.publish_visual("Page", html_path=str(page))
    assert len(visuals.for_session(db, boss.id)) == 3


def test_note_only_answers_reach_the_supervisor(db, session, client):
    _, boss, worker = session
    for via_route in (False, True):
        v = visuals.publish(db, worker, "Release flow", "<p>flow</p>", "Which flow?", OPTIONS)
        for option, note in ((None, "  "), ("unknown", "Another flow"), (None, SECRET)):
            with pytest.raises(ServiceError):
                visuals.answer(db, v["id"], 1, option, note)
        if via_route:
            response = client.post(f"/v1/visuals/{v['id']}/answer", json={"rev": 1, "note": "  Stage it first  "})
            assert response.status_code == 200
            answered = response.json()
        else:
            answered = visuals.answer(db, v["id"], 1, None, "  Stage it first  ")
        assert answered["answer"]["option"] is None
        assert answered["answer"]["note"] == "Stage it first"
        assert not answered.get("unread", False)
    bodies = [r["body"] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (boss.id,))]
    assert len(bodies) == 2 and all("Stage it first" in body for body in bodies)
    assert not [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]


def test_dismiss_tells_the_agent_once_clears_the_inbox_and_a_new_revision_asks_again(db, session, client, monkeypatch):
    _, boss, worker = session
    v = visuals.publish(db, worker, "Release flow", "<p>flow</p>", "Which flow?", OPTIONS)
    answered = visuals.publish(db, worker, "Answered", "<p>x</p>", "Which?", OPTIONS)
    visuals.answer(db, answered["id"], 1, "A")
    assert [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]
    def notices(agent):
        return db.conn.execute("SELECT COUNT(*) FROM inbox WHERE agent_id=?", (agent,)).fetchone()[0]

    before = (notices(worker.id), notices(boss.id))

    response = client.post(f"/v1/visuals/{v['id']}/dismiss", json={"rev": 1})
    assert response.status_code == 200
    # The worker that published it stops waiting; its supervisor's session hears too.
    assert (notices(worker.id), notices(boss.id)) == (before[0] + 1, before[1] + 1)
    assert not [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]
    assert v["id"] in [x["id"] for x in visuals.for_session(db, "boss")]

    # A repeat, and an already answered revision, are safe no-ops that tell nobody.
    assert client.post(f"/v1/visuals/{v['id']}/dismiss", json={"rev": 1}).status_code == 200
    assert client.post(f"/v1/visuals/{answered['id']}/dismiss", json={"rev": 1}).status_code == 200
    assert (notices(worker.id), notices(boss.id)) == (before[0] + 1, before[1] + 1)
    assert visuals.get(db, answered["id"])["answer"]["option"] == "A"

    visuals.publish(db, worker, "Release flow", "<p>flow 2</p>", "Which flow?", OPTIONS, visual_id=v["id"])
    assert [i.visual_id for i in inbox.items(db, None, panes={}) if i.kind == "visual"] == [v["id"]]

    # A supervisor that can't be told doesn't undo the publisher's dismissal or make a repeat re-tell it.
    real = agents.send_message

    def send(db_, to, *args, **kwargs):
        if to == "boss":
            raise ServiceError("paused", "the session is paused", 409)
        return real(db_, to, *args, **kwargs)

    monkeypatch.setattr(agents, "send_message", send)
    w = notices(worker.id)
    for _ in range(2):
        assert client.post(f"/v1/visuals/{v['id']}/dismiss", json={"rev": 2}).status_code == 200
    assert notices(worker.id) == w + 1
    assert not [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]


def test_answer_attachments_reach_receiver_and_kept_visual(db, session, client, tmp_path, monkeypatch):
    ws, boss, worker = session
    receiving = tmp_path / "receiving"
    receiving.mkdir()
    db.conn.execute("UPDATE workspaces SET path=? WHERE id=?", (str(receiving), ws.id))
    db.conn.commit()
    monkeypatch.setattr(git, "exclude", lambda *args: None)
    v = visuals.publish(db, worker, "Flow", "<p>flow</p>", "Which?", OPTIONS)
    [dto] = client.get(f"/v1/sessions/{boss.id}/visuals").json()
    assert (dto["answer_agent_id"], dto["accepts_files"]) == (boss.id, True)
    paths = []
    for name in ("diagram.png", "notes.txt"):
        uploaded = client.post(f"/v1/agents/{dto['answer_agent_id']}/attachments",
                               json={"name": name, "data_base64": "aGk="})
        assert uploaded.status_code == 200, uploaded.text
        paths.extend(uploaded.json()["paths"])
    client.post(f"/v1/visuals/{v['id']}/keep")
    response = client.post(f"/v1/visuals/{v['id']}/answer",
                           json={"rev": 1, "option": "A", "attachments": paths})
    assert response.status_code == 200
    names = [path.rsplit("/", 1)[-1] for path in paths]
    assert response.json()["answer"]["attachments"] == names
    assert visuals.get(db, v["id"])["answer"]["attachments"] == names
    assert client.get(f"/v1/kept-visuals/{v['id']}").json()["answer"]["attachments"] == names
    client.post(f"/v1/visuals/{v['id']}/keep")
    assert client.get("/v1/kept-visuals").json()[0]["answer"]["attachments"] == names
    [body] = [r["body"] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (boss.id,))]
    assert all('@"' + path + '"' in body for path in paths)


def test_answer_attachments_refused_before_claim(db, session, client, tmp_path):
    _, boss, worker = session
    folder = tmp_path / "repo" / ".brindle" / "attachments"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    path = folder / "file.txt"
    path.write_text("uploaded")
    v = visuals.publish(db, worker, "Flow", "<p>flow</p>", "Which?", OPTIONS)

    def refused(paths):
        response = client.post(f"/v1/visuals/{v['id']}/answer",
                               json={"rev": 1, "option": "A", "attachments": paths})
        assert response.status_code == 400, paths
        assert visuals.get(db, v["id"])["answer"] is None
        assert db.conn.execute("SELECT COUNT(*) FROM inbox WHERE agent_id=?", (boss.id,)).fetchone()[0] == 0

    # Outside, traversal, a directory, missing, a scalar in place of the list, a non-path item.
    for paths in ([str(outside)], [str(folder / ".." / ".." / ".." / outside.name)], [str(folder)],
                  [str(folder / "absent")], str(path), [42]):
        refused(paths)
    # An uploaded path linked to an outside file.
    path.unlink()
    path.symlink_to(outside)
    refused([str(path)])
    path.unlink()
    path.write_text("uploaded")
    # The attachments folder itself points outside.
    folder.rename(tmp_path / "moved")
    folder.symlink_to(tmp_path / "moved", target_is_directory=True)
    refused([str(path)])
    folder.unlink()
    (tmp_path / "moved").rename(folder)
    # A provider that takes no files.
    db.update_agent(boss.id, provider="shell")
    [dto] = client.get(f"/v1/sessions/{boss.id}/visuals").json()
    assert dto["accepts_files"] is False
    refused([str(path)])


def test_visual_ready_notice_and_answers_for_the_revision_on_screen(db, session, client,
                                                                  monkeypatch):
    ws, boss, worker = session
    ready = visuals.publish(db, worker, "Release flow", "<p>flow</p>")
    for rev in (1, 2):
        if rev == 2:
            visuals.publish(db, worker, "Revised flow", "<p>revision</p>", visual_id=ready["id"])
        assert [i.visual_id for i in inbox.items(db, ws.repo_root, panes={}) if i.kind == "visual"] == [ready["id"]]
        assert not [i for i in inbox.items(db, "other-repo", panes={}) if i.kind == "visual"]
        assert client.post(f"/v1/visuals/{ready['id']}/read").json()["unread"] is False
        assert not [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]
    assert len(bus.tail(db, kinds=("visual.read",), repo_root=ws.repo_root)) == 2
    # r1's read lands after r2 was published: r2 hasn't been seen, so its notice stands.
    late = visuals.publish(db, worker, "Late", "<p>1</p>")
    visuals.publish(db, worker, "Late", "<p>2</p>", visual_id=late["id"])
    assert client.post(f"/v1/visuals/{late['id']}/read", json={"rev": 0}).status_code == 400
    assert client.post(f"/v1/visuals/{late['id']}/read", json={"rev": 1}).json()["unread"] is True
    assert [i.visual_id for i in inbox.items(db, None, panes={}) if i.kind == "visual"] == [late["id"]]
    assert client.post(f"/v1/visuals/{late['id']}/read", json={"rev": 2}).json()["unread"] is False
    monkeypatch.setenv("BRINDLE_AGENT_ID", worker.id)
    mcp_server.publish_visual("Release flow", "<p>r1</p>", "Which flow?", OPTIONS)
    [v] = [v for v in client.get(f"/v1/sessions/{boss.id}/visuals").json() if v["id"] not in (ready["id"], late["id"])]
    assert (v["rev"], v["unread"], v["by"]) == (1, True, "w1")
    [item] = [i for i in inbox.items(db, None, panes={}) if i.kind == "visual"]
    assert (item.visual_id, item.root_id) == (v["id"], boss.id)
    assert client.post(f"/v1/visuals/{v['id']}/read").json()["unread"] is False
    assert [i.visual_id for i in inbox.items(db, None, panes={}) if i.kind == "visual"] == [v["id"]]
    # Each read says whether its visual still waits in the Inbox: the app drops the card at once if not.
    assert [e["payload"]["pending"] for e in bus.tail(db, kinds=("visual.read",))] == [False, False, True, False, True]
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


@pytest.mark.variant("real incident: a status board the person asked for was refused on every update (0.6.3, Nov 10)")
def test_asked_visual_updates_without_a_new_request(db, session, monkeypatch):
    ws, boss, worker = session
    monkeypatch.setattr(visual_offers, "consume", REAL_CONSUME)
    visual_offers.person_spoke(db, boss, "render as you go", typed=True)
    v = visuals.publish(db, boss, "Status", "<p>r1</p>", asked="render as you go")
    # The board the person asked for stays the supervisor's to update, with no new quote.
    assert visuals.publish(db, boss, "Status", "<p>r2</p>", visual_id=v["id"])["rev"] == 2
    assert visuals.publish(db, boss, "Status", "<p>r3</p>", visual_id=v["id"])["rev"] == 3
    for who, vid in ((boss, None), (worker, v["id"])):
        with pytest.raises(ServiceError) as refused:
            visuals.publish(db, who, "Other", "<p>no</p>", visual_id=vid)
        assert refused.value.code == "visual_request_required"


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


def test_failed_answer_removes_the_claim_from_a_concurrently_kept_copy(db, session, monkeypatch):
    _, boss, _ = session
    v = visuals.publish(db, boss, "Flow", "<p>flow</p>", "Which?", OPTIONS)

    def fail(*args, **kwargs):
        visuals.keep(db, v["id"])
        raise ServiceError("paused", "session paused", 409)

    monkeypatch.setattr(agents, "send_message", fail)
    with pytest.raises(ServiceError):
        visuals.answer(db, v["id"], 1, "A")
    assert visuals.get(db, v["id"])["answer"] is None
    assert visuals.get_kept(db, v["id"])["answer"] is None


@pytest.mark.variant("Keep races answers, revisions and Unkeep at the writer transaction boundary")
@pytest.mark.parametrize("interleaved", ["answer", "revision", "unkeep", "late_keep"])
def test_keep_reads_and_copies_after_acquiring_the_write_lock(db, session, monkeypatch, interleaved):
    from brindle.db import DB

    _, boss, _ = session
    v = visuals.publish(db, boss, "Flow", "<p>first</p>", "Which?", OPTIONS)
    visuals.keep(db, v["id"])
    other = DB()
    tx = db.tx

    @contextmanager
    def preempted():
        monkeypatch.setattr(db, "tx", tx)
        if interleaved == "answer":
            visuals.answer(other, v["id"], 1, "A")
        elif interleaved == "revision":
            visuals.publish(other, boss, "New flow", "<p>second</p>", "Which?", OPTIONS, visual_id=v["id"])
        elif interleaved == "unkeep":
            visuals.unkeep(other, v["id"])
        with tx() as c:
            yield c
        if interleaved == "late_keep":
            visuals.keep(other, v["id"])

    monkeypatch.setattr(db, "tx", preempted)
    try:
        current = (visuals.unkeep if interleaved == "late_keep" else visuals.keep)(db, v["id"])
        kept = visuals.get_kept(db, v["id"])
        assert kept["rev"] == current["rev"]
        assert visuals.kept_to_dto(kept)["answer"] == visuals.to_dto(db, current)["answer"]
        assert visuals.kept_page(db, v["id"]).read_bytes() == visuals.page(db, v["id"], str(current["rev"])).read_bytes()
    finally:
        other.conn.close()


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


@pytest.mark.variant("incident: staff build 96 shipped visuals hidden because nothing set the old opt-in flag")
def test_visuals_are_on_unless_switched_off(monkeypatch):
    # The staff build shipped with visuals hidden: nothing set the old opt-in flag.
    monkeypatch.delenv("BRINDLE_VISUALS", raising=False)
    assert visuals.enabled()
    monkeypatch.setenv("BRINDLE_VISUALS", "0")
    assert not visuals.enabled()


def test_the_visuals_setting_flips_the_feature_and_every_running_agents_tools_live(monkeypatch):
    import asyncio

    from mcp import Client

    from brindle import config

    monkeypatch.delenv("BRINDLE_VISUALS", raising=False)
    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    async def tool_names(client):
        return {t.name for t in (await client.list_tools()).tools} & set(mcp_server.VISUAL_TOOLS)

    async def flow():
        async with Client(mcp_server.mcp) as client, client.listen(tools_list_changed=True) as events:
            assert await tool_names(client) == set(mcp_server.VISUAL_TOOLS)
            ticks = asyncio.Queue()

            waiting = asyncio.Event()

            async def wait(_):
                waiting.set()
                await ticks.get()
            watcher = asyncio.create_task(mcp_server._watch_visuals(wait))
            await waiting.wait()                  # the watcher has read the setting: on
            config.set_user("visuals_enabled", "off")
            assert not visuals.enabled()          # same process, no restart
            ticks.put_nowait(None)
            await asyncio.wait_for(anext(aiter(events)), 5)    # tools/list_changed arrived
            assert await tool_names(client) == set()
            config.set_user("visuals_enabled", "on")
            assert await tool_names(client) == set(mcp_server.VISUAL_TOOLS)
            watcher.cancel()
        monkeypatch.setenv("BRINDLE_VISUALS", "0")      # the override beats the setting
        assert not visuals.enabled()

    asyncio.run(flow())
