"""A plan as a design doc (brindle.plan_docs), through the daemon's routes:
revisions with sections and diagrams, anchored comment threads, suggestions
and direct edits, votes, and attachments, with assigns still held until the
person approves."""

import asyncio

import pytest
from starlette.testclient import TestClient

from brindle import mcp_server, plans, visual_offers
from brindle.daemon.app import build_app
from brindle.service.errors import ServiceError
from test_plans import AUTH, DECISIONS, PLAN, TOKEN, messages, session  # noqa: F401 - fixture

SUMMARY = "Workers run on a machine the customer controls. Code never passes through PawDelta."
QUOTE = "a machine the customer controls"


def test_the_person_reviews_a_design_doc_through_the_routes(db, session, tmp_path):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    with pytest.raises(ServiceError, match="plain <svg>"):
        plans.propose(db, boss, "# Cloud", sections=[{"title": "Flow", "kind": "svg",
                                                      "markdown": '<svg onload="steal()"></svg>'}])
    pid = plans.propose(db, boss, "# Cloud workers\n\nOne page.", DECISIONS, sections=[
        {"id": "summary", "title": "Summary", "markdown": SUMMARY},
        {"id": "arch", "title": "Architecture", "kind": "mermaid", "markdown": "graph LR; app-->daemon",
         "caption": "Figure 1"},
        {"id": "flow", "title": "Flow", "kind": "svg", "markdown": '<svg viewBox="0 0 9 9"><rect width="4"/></svg>'},
    ])["id"]
    asyncio.run(mcp_server.assign("developer", "build it"))
    folder = tmp_path / ".brindle" / "attachments"
    folder.mkdir(parents=True)
    (folder / "costs.png").write_bytes(b"png")
    costs = str(folder / "costs.png")

    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        def post(path, **body):
            return client.post(f"/v1/plans/{pid}{path}", json=body)

        got = client.get(f"/v1/plans/{pid}").json()
        assert [(s["id"], s["kind"]) for s in got["sections"]] == [
            ("summary", "markdown"), ("arch", "mermaid"), ("flow", "svg")]
        assert got["sections"][1]["caption"] == "Figure 1" and got["can_edit"]

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
        at = SUMMARY.index("PawDelta")
        post("/suggestions", rev=1, section="summary", start=at, before="PawDelta", after="our servers",
             text="Clearer.")
        post("/suggestions", rev=1, section="summary", start=0, before="Workers", after="Hosts")
        assert post("/edit", rev=1, section="summary", start=0, before="Workers run",
                    after="Workers always run").json()["rev"] == 2
        assert post("/edit", rev=1, section="summary", start=0, before="Workers", after="W").status_code == 409
        keep, drop = (t["id"] for t in client.get(f"/v1/plans/{pid}").json()["threads"][3:])
        post(f"/threads/{drop}/reject")
        got = post(f"/threads/{keep}/accept", rev=2).json()
        assert got["rev"] == 3 and got["sections"][0]["markdown"] == (
            "Workers always run on a machine the customer controls. Code never passes through our servers.")
        assert [(r["rev"], r["author"], r["source"]) for r in got["revisions"]] == [
            (1, "supervisor", "proposal"), (2, "person", "edit"), (3, "person", "suggestion")]
        assert client.get(f"/v1/plans/{pid}/revisions/1").json()["sections"][0]["markdown"] == SUMMARY
        threads = {t["id"]: t for t in got["threads"]}
        assert threads[text_t]["anchor"]["state"] == "moved"
        assert threads[text_t]["anchor"]["start"] == got["sections"][0]["markdown"].index(QUOTE)
        assert threads[text_t]["replies"][0]["text"] == "Any machine they hold the keys to."
        assert [threads[i]["status"] for i in (diagram_t, keep, drop)] == ["resolved", "accepted", "rejected"]
        assert threads[decision_t]["attachments"] == ["costs.png"]

        # Confirming a decision shapes the verdict; Request changes carries the doc to the supervisor.
        got = post("/confirm", rev=3, decision="d1", option="repo").json()
        assert (got["decisions"][0]["votes"], got["decisions"][0]["confirmed"]) == ({"repo": ["person"]}, "repo")
        got = post("/decide", rev=3, approved=False, note="See the numbers.", attachments=[costs]).json()
        assert got["state"] == "redirected" and got["verdict"]["attachments"] == ["costs.png"]
        said = messages(db)[-1]
        assert "chose repo" in said and "Does a rented VM count?" in said and "Repo first." in said
        assert "Label the arrows." not in said and "edited Summary (r2)" in said and costs in said
        assert [t["sent"] for t in got["threads"]][:3] == [True, False, True]
        assert post("/edit", rev=3, section="summary", start=0, before="Workers", after="W").status_code == 409
        assert started == []

        # The supervisor's next revision; approving it releases the held assign.
        plans.propose(db, boss, "# Cloud workers", plan_id=pid, sections=[
            {"id": "summary", "title": "Summary", "markdown": got["sections"][0]["markdown"]}])
        assert post("/decide", rev=4, approved=True).json()["state"] == "approved"
    assert started == ["build it"]


def test_a_plan_from_before_revisions_stays_readable_and_editable(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    db.conn.execute("DELETE FROM plan_revisions")          # as an older brindle left it
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        got = client.get(f"/v1/plans/{pid}").json()
        assert [s["id"] for s in got["sections"]] == ["s1", "s2"] and [r["rev"] for r in got["revisions"]] == [1]
        got = client.post(f"/v1/plans/{pid}/edit", json={"rev": 1, "section": "s2", "before": "Two workers.",
                                                           "after": "Three workers."}).json()
        assert got["sections"][1]["markdown"] == "Three workers." and [r["rev"] for r in got["revisions"]] == [2]
        assert "Three workers." in plans.get(db, pid)["markdown"]
