"""A plan as a design doc (brindle.plan_docs), through the daemon's routes:
revisions with sections and diagrams, anchored comment threads, suggestions
and direct edits, votes, and attachments, with assigns still held until the
person approves."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from brindle import agents, bus, mcp_server, plans, spawn, visual_offers
from brindle.daemon.app import build_app
from brindle.service.errors import ServiceError
from test_plans import AUTH, DECISIONS, PLAN, TOKEN, messages, session, work  # noqa: F401 - fixture

SUMMARY = "Workers run on a machine the customer controls. Code never passes through PawDelta."
QUOTE = "a machine the customer controls"
REAL_IS_ALIVE = agents.is_alive   # the session fixture replaces it


async def _post_as_uvicorn_serves(app, path: str, body: dict) -> list[dict]:
    """One POST the way uvicorn serves it: once the response is complete,
    ``receive`` answers http.disconnect, so the daemon's Deadlines middleware
    has stopped the request's deadline before a background task runs."""
    done = asyncio.Event()
    received: list[int] = []
    sent: list[dict] = []

    async def receive():
        received.append(1)
        if len(received) == 1:
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()
            loop = asyncio.get_running_loop()
            turn = loop.create_future()
            loop.call_soon(turn.set_result, None)
            await turn   # the listener takes the disconnect first, as it does under uvicorn
            assert spawn.current().stopped

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"authorization", AUTH["Authorization"].encode()),
                         (b"content-type", b"application/json")],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    await app(scope, receive, send)
    return sent


@pytest.mark.variant("drives the real ASGI route with a client disconnect after the reply: the expired-deadline handoff only shows up served this way")
def test_a_question_on_a_live_supervisors_plan_is_delivered_to_it(db, session, tmp_path, monkeypatch):
    """Regression (plan v_69a9d8236420): the question is delivered after the
    response is written, when the request's deadline is already stopped. The
    liveness check's tmux read was refused under it, so a live supervisor's
    plan got "couldn't start on this machine" and the supervisor nothing."""
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Cloud workers\n\nOne page.", sections=[
        {"id": "arch", "title": "Architecture", "kind": "mermaid", "markdown": "graph LR; app-->daemon"}])["id"]
    # The real liveness check, a tmux read through brindle.spawn, answered by a stand-in tmux: pane alive.
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text("#!/bin/sh\necho 0\n")
    fake_tmux.chmod(0o755)
    monkeypatch.setenv("BRINDLE_TMUX_BIN", str(fake_tmux))
    monkeypatch.setattr(agents, "is_alive", REAL_IS_ALIVE)
    before = messages(db)

    sent = asyncio.run(_post_as_uvicorn_serves(build_app(TOKEN), f"/v1/plans/{pid}/comments", {
        "rev": 1, "anchor": {"kind": "section", "section": "arch"}, "text": "was this supposed to render",
        "question": True}))

    assert sent[0]["status"] == 200
    delivered = messages(db)[len(before):]
    assert delivered and "was this supposed to render" in delivered[-1]
    thread = plans.to_dto(db, plans.get(db, pid))["threads"][0]
    assert thread["question"] and not thread["replies"]


@pytest.mark.variant("regression: a revision must publish an event to the open plan reader; failed on base")
def test_supervisor_revision_notifies_the_open_plan_reader(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    cursor = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    plans.decide(db, pid, 1, False, note="Please revise.", person=True)
    mcp_server.propose_plan(PLAN + "\n\nRevised.", plan_id=pid)

    events = bus.tail(db, cursor, kinds=("visual.plan_changed",), root_id="boss")
    assert [(event["payload"]["visual"], event["payload"]["rev"]) for event in events] == [(pid, 2)]


def test_the_person_reviews_a_design_doc_through_the_routes(db, session, tmp_path):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    with pytest.raises(ServiceError) as refused:
        plans.propose(db, boss, "# Cloud", sections=[{"title": "Flow", "kind": "svg",
                                                      "markdown": '<svg onload="steal()"></svg>'}])
    assert (refused.value.code, refused.value.http_status) == ("invalid", 400)
    drawing = '<svg viewBox="0 0 9 9"><use href="#r"/><rect id="r" width="4" fill="url(#g)"/></svg>'
    pid = plans.propose(db, boss, "# Cloud workers\n\nOne page.", DECISIONS, sections=[
        {"id": "summary", "title": "Summary", "markdown": SUMMARY},
        {"id": "arch", "title": "Architecture", "kind": "mermaid", "markdown": "graph LR; app-->daemon",
         "caption": "Figure 1"},
        {"id": "flow", "title": "Flow", "kind": "svg", "markdown": drawing},
    ])["id"]
    asyncio.run(mcp_server.assign("developer", "build it", **work(db)))
    folder =tmp_path / ".brindle" / "attachments"
    folder.mkdir(parents=True)
    (folder / "costs.png").write_bytes(b"png")
    costs = str(folder / "costs.png")

    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        def post(path, **body):
            return client.post(f"/v1/plans/{pid}{path}", json=body)

        got = client.get(f"/v1/plans/{pid}").json()
        assert [(s["id"], s["kind"]) for s in got["sections"]][:3] == [
            ("summary", "markdown"), ("arch", "mermaid"), ("flow", "svg")]
        assert got["sections"][1]["caption"] == "Figure 1" and got["can_edit"]
        # An SVG edit that would run or load something is refused, however it is spelled
        # (reviewer's probes: an animated href to an external image, an entity-encoded javascript URL).
        for probe in ('<svg xmlns="http://www.w3.org/2000/svg"><image><set attributeName="href" '
                      'to="https://example.invalid/pixel.png"/></image></svg>',
                      '<svg><a><animate attributeName="href" to="&#106;avascript:alert(1)"/></a></svg>',
                      '<svg><rect fill="u&#114;l(https://example.invalid/x)"/></svg>',
                      r'<svg><rect fill="u\72l(https://example.invalid/paint.svg#g)"/></svg>'):   # a CSS escape
            r = post("/edit", rev=1, section="flow", before=drawing, after=probe)
            assert r.status_code == 400, probe
        assert [r["rev"] for r in client.get(f"/v1/plans/{pid}").json()["revisions"]] == [1]

        # Comments on a text range, a diagram and a decision (with a file); a quote not in the text is refused.
        start = SUMMARY.index(QUOTE)
        post("/comments", rev=1, anchor={"kind": "text", "section": "summary", "start": start, "quote": QUOTE},
             text="Does a rented VM count?")
        post("/comments", rev=1, anchor={"kind": "diagram", "section": "arch"}, text="Label the arrows.")
        post("/comments", rev=1, anchor={"kind": "decision", "decision": "d1", "option": "repo"}, text="Repo first.",
             attachments=[costs])
        assert post("/comments", rev=1, anchor={"kind": "text", "section": "summary", "quote": "not there"},
                    text="?").status_code == 409
        text_t, diagram_t, decision_t = (t["id"] for t in client.get(f"/v1/plans/{pid}").json()["threads"])
        post("/comments", parent=text_t, text="Any machine they hold the keys to.")
        post(f"/threads/{diagram_t}/resolve")

        # Suggestions and a direct edit: each change the person makes is a new revision.
        # Insertions are placed by the text around them: one whose surroundings the edit
        # changes conflicts, one past the edit moves with its text.
        post("/suggestions", rev=1, section="summary", start=0, before="", after="Today, ")
        post("/suggestions", rev=1, section="summary", start=SUMMARY.index("."), before="", after=" at all",
             text="Clearer.")
        post("/suggestions", rev=1, section="summary", start=0, before="Workers", after="Hosts")
        assert post("/edit", rev=1, section="summary", start=0, before="Workers run",
                    after="Workers always run").json()["rev"] == 2
        assert post("/edit", rev=1, section="summary", start=0, before="Workers", after="W").status_code == 409
        conflict, keep, drop = (t["id"] for t in client.get(f"/v1/plans/{pid}").json()["threads"][3:])
        assert post(f"/threads/{conflict}/accept", rev=2).status_code == 409
        post(f"/threads/{drop}/reject")
        got = post(f"/threads/{keep}/accept", rev=2).json()
        assert got["rev"] == 3 and got["sections"][0]["markdown"] == (
            "Workers always run on a machine the customer controls at all. Code never passes through PawDelta.")
        assert [(r["rev"], r["author"], r["source"]) for r in got["revisions"]] == [
            (1, "supervisor", "proposal"), (2, "person", "edit"), (3, "person", "suggestion")]
        assert client.get(f"/v1/plans/{pid}/revisions/1").json()["sections"][0]["markdown"] == SUMMARY
        threads = {t["id"]: t for t in got["threads"]}
        assert threads[text_t]["anchor"]["state"] == "moved"
        assert threads[text_t]["anchor"]["start"] == got["sections"][0]["markdown"].index(QUOTE)
        assert threads[text_t]["replies"][0]["text"] == "Any machine they hold the keys to."
        assert [threads[i]["status"] for i in (diagram_t, keep, drop)] == ["resolved", "accepted", "rejected"]
        assert threads[decision_t]["attachments"] == ["costs.png"]
        assert threads[text_t]["replies"][0]["sent"] is False

        # Confirming a decision shapes the verdict; Request changes carries the doc to the supervisor.
        got = post("/confirm", rev=3, decision="d1", option="repo").json()
        assert (got["decisions"][0]["votes"], got["decisions"][0]["confirmed"]) == ({"repo": ["person"]}, "repo")
        got = post("/decide", rev=3, approved=False, note="See the numbers.", attachments=[costs]).json()
        assert got["state"] == "redirected" and got["verdict"]["attachments"] == ["costs.png"]
        assert got["verdict"]["picks"] == {"d1": "repo"}
        said = messages(db)[-1]
        assert "Does a rented VM count?" in said and "Repo first." in said and costs in said
        assert "Label the arrows." not in said             # resolved: not carried
        assert [t["sent"] for t in got["threads"]][:3] == [True, False, True]
        assert got["threads"][0]["replies"][0]["sent"] is True   # the verdict carried the reply too
        assert post("/edit", rev=3, section="summary", start=0, before="Workers", after="W").status_code == 409
        assert started == []

        # The supervisor's next revision; approving it releases the held assign.
        plans.propose(db, boss, "# Cloud workers", plan_id=pid, sections=[
            {"id": "summary", "title": "Summary", "markdown": got["sections"][0]["markdown"]}])
        assert post("/decide", rev=4, approved=True).json()["state"] == "approved"
    assert started == ["build it"]


def test_a_plan_from_before_revisions_stays_readable_and_keeps_its_history(db, session):
    """A plan with no revision rows (an older brindle's) reads, takes an edit
    and a supervisor revision, and each keeps the revision it replaced."""
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    db.conn.execute("DELETE FROM plan_revisions")          # as an older brindle left it
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        def section(rev, n):
            return client.get(f"/v1/plans/{pid}/revisions/{rev}").json()["sections"][n]["markdown"]

        got = client.get(f"/v1/plans/{pid}").json()
        assert [s["id"] for s in got["sections"]][:2] == ["s1", "s2"] and [r["rev"] for r in got["revisions"]] == [1]
        got = client.post(f"/v1/plans/{pid}/edit", json={"rev": 1, "section": "s2", "before": "Two workers.",
                                                           "after": "Three workers."}).json()
        assert [r["rev"] for r in got["revisions"]] == [1, 2]
        assert (section(1, 1), section(2, 1)) == ("Two workers.", "Three workers.")

        db.conn.execute("DELETE FROM plan_revisions")      # legacy again, now at r2
        earliest = client.get(f"/v1/plans/{pid}/revisions/2/changes")
        assert earliest.status_code == 200
        retained = earliest.json()
        assert retained["revision"]["sections"][1]["markdown"] == "Three workers."
        assert retained["previous"] is None and not retained["baseline_available"]
        assert retained["changes"] == []
        client.post(f"/v1/plans/{pid}/decide", json={"rev": 2, "approved": False, "note": "Fewer."})
        mcp_server.propose_plan(PLAN.replace("Two workers.", "One worker."), plan_id=pid)
        got = client.get(f"/v1/plans/{pid}").json()
        assert [r["rev"] for r in got["revisions"]] == [2, 3]
        assert (section(2, 1), section(3, 1)) == ("Three workers.", "One worker.")
        assert client.get(f"/v1/plans/{pid}/revisions/2/changes").json()["revision"]["rev"] == 2
        assert client.get(f"/v1/plans/{pid}/revisions/3/changes").json()["previous"]["rev"] == 2
        db.conn.execute("UPDATE plan_revisions SET rev=4 WHERE plan_id=? AND rev=3", (pid,))
        db.conn.execute("UPDATE plans SET rev=4 WHERE id=?", (pid,))
        assert client.get(f"/v1/plans/{pid}/revisions/4/changes").json()["previous"]["rev"] == 2
