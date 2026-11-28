"""The person's own edits to a solo plan (no live draft) survive the
supervisor's next revision: read_plan hands it the current text, and a
revision written without reading it is refused as stale."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from brindle import mcp_server, plan_docs, plans, visual_offers
from brindle.daemon.app import build_app
from brindle.service.errors import ServiceError
from test_plans import AUTH, PLAN, TOKEN, session  # noqa: F401 - fixture

EDIT = "Two workers, one per repo."
CODE = "    brindle plan --dry-run\n    brindle plan"


def _edit(client, pid: str, rev: int, sid: str, before: str, after: str) -> None:
    got = client.post(f"/v1/plans/{pid}/edit", json={"rev": rev, "section": sid, "before": before, "after": after})
    assert got.json()["rev"] == rev + 1, got.json()


def test_a_person_edit_survives_the_supervisors_next_revision(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        split = next(s for s in client.get(f"/v1/plans/{pid}").json()["sections"] if s["markdown"] == "Two workers.")
        _edit(client, pid, 1, split["id"], "Two workers.", EDIT)
        _edit(client, pid, 2, "context", "None.", "")      # a spine section the person cleared
        _edit(client, pid, 3, "risks", "None.", CODE)       # leading spaces that make it a code block
        _edit(client, pid, 4, "lede", "# Org presets", "    # Org presets")
        client.post(f"/v1/plans/{pid}/decide", json={"rev": 5, "approved": False, "note": "Tighten it."})

    # Revised from its own r1, the supervisor would drop the person's edits: refused, naming the sections.
    with pytest.raises(ServiceError) as stale:
        plans.propose(db, boss, PLAN + "\n\nRevised.", plan_id=pid)
    assert stale.value.code == "stale" and split["title"] in stale.value.message
    with pytest.raises(ToolError):
        mcp_server.propose_plan(PLAN + "\n\nRevised.", plan_id=pid)
    assert plans.get(db, pid)["rev"] == 5

    # read_plan gives a solo plan's current text; revising that keeps every edit exactly, whitespace included.
    got = json.loads(mcp_server.read_plan(pid))
    assert got["rev"] == 5
    sections = [{**s, "markdown": s["markdown"] + " Revised."} if s["id"] == "goals" else s
                for s in got["sections"]]
    mcp_server.propose_plan(got["lede"], plan_id=pid, sections=sections, base=got["base"])
    after = plans.get(db, pid)
    now = {s["id"]: s["markdown"] for s in after["sections"]}
    assert after["rev"] == 6 and after["lede"] == got["lede"] and after["lede"].startswith("    ")
    assert after["markdown"].startswith("    # Org presets")      # the stored copy the library gets too
    assert (now[split["id"]], now["context"], now["risks"]) == (EDIT, "", CODE)


@pytest.mark.variant("a revision published between read and propose must not become the base")
def test_a_revision_published_meanwhile_never_becomes_the_base(db, session, monkeypatch):
    """A proposal that found the person's r2 has not read it, even if another
    proposal (one that did read r2) lands r3 before it works out its base."""
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        _edit(client, pid, 1, "s2", "Two workers.", EDIT)
    real, landed = plan_docs.draft, []

    def competing(db_, p):
        # Called between the snapshot (at r2) and the base: the other proposal lands first.
        if p["id"] == pid and p["rev"] == 2 and not landed:
            landed.append(None)
            landed[0] = plans.propose(db, boss, PLAN.replace("Two workers.", EDIT), plan_id=pid, base="r2")["rev"]
        return real(db_, p)

    monkeypatch.setattr(plan_docs, "draft", competing)
    with pytest.raises(ServiceError) as stale:
        plans.propose(db, boss, PLAN + "\n\nRevised.", plan_id=pid)
    assert landed == [3] and stale.value.code == "stale"
    assert plans.get(db, pid)["rev"] == 3
