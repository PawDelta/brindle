"""The person's own edits to a solo plan (no live draft) survive the
supervisor's next revision: read_plan hands it the current text, and a
revision written without reading it is refused as stale."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from brindle import mcp_server, plans, visual_offers
from brindle.daemon.app import build_app
from brindle.service.errors import ServiceError
from test_plans import AUTH, PLAN, TOKEN, session  # noqa: F401 - fixture

EDIT = "Two workers, one per repo."


def test_a_person_edit_survives_the_supervisors_next_revision(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        split = next(s for s in client.get(f"/v1/plans/{pid}").json()["sections"] if s["markdown"] == "Two workers.")
        edited = client.post(f"/v1/plans/{pid}/edit", json={"rev": 1, "section": split["id"],
                                                              "before": "Two workers.", "after": EDIT})
        assert edited.json()["rev"] == 2, edited.json()
        client.post(f"/v1/plans/{pid}/decide", json={"rev": 2, "approved": False, "note": "Tighten it."})

    # Revised from its own r1, the supervisor would drop the person's r2: refused, naming the section.
    with pytest.raises(ServiceError) as stale:
        plans.propose(db, boss, PLAN + "\n\nRevised.", plan_id=pid)
    assert stale.value.code == "stale" and split["title"] in stale.value.message
    with pytest.raises(ToolError):
        mcp_server.propose_plan(PLAN + "\n\nRevised.", plan_id=pid)
    assert plans.get(db, pid)["rev"] == 2

    # read_plan gives a solo plan's current text; revising that keeps the edit.
    got = json.loads(mcp_server.read_plan(pid))
    assert got["rev"] == 2
    sections = [{**s, "markdown": s["markdown"] + " Revised."} if s["id"] != split["id"] else s
                for s in got["sections"]]
    mcp_server.propose_plan(got["lede"], plan_id=pid, sections=sections, base=got["base"])
    now = plans.get(db, pid)
    assert now["rev"] == 3
    assert next(s for s in now["sections"] if s["id"] == split["id"])["markdown"] == EDIT
