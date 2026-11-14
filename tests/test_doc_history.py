"""Document history keeps actions after mutable review state moves on."""

from starlette.testclient import TestClient

from brindle import plan_docs, plans, visual_offers
from brindle.daemon.app import build_app
from brindle.pro import live_docs, shared_docs
from test_plans import AUTH, DECISIONS, TOKEN, session  # noqa: F401


def test_timeline_and_revision_changes_through_daemon(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Design", DECISIONS, sections=[
        {"id": "summary", "title": "Summary", "markdown": "The old text."},
        {"id": "removed", "title": "Removed", "markdown": "Keep in history."},
    ])["id"]
    plan_docs.edit(db, pid, 1, "summary", 4, "old", "new")
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, owner, doc_id, remote_rev, local_rev) VALUES (?,?,?,?,?)",
                  (pid, "owner", "shared-doc", 8, 2))
    shared_docs.record_freeze(db, "shared-doc", 8, False, 20)
    view = {"rev": 8, "sign_offs": [{"sub": "dana", "rev": 8, "at": 21}]}
    shared_docs._remember(db, pid, view)
    shared_docs._remember(db, pid, view)
    live_docs._frozen_version(db, "shared-doc", 8, {
        "rev": 8, "sign_offs": [{"sub": "lee", "rev": 8, "at": 21.5}]})
    shared_docs.record_freeze(db, "shared-doc", 8, True, 22)
    shared_docs._remember(db, pid, {"rev": 8, "sign_offs": []})
    shared_docs._remember(db, pid, {"rev": 9, "draft": {"frozen": 9}})
    shared_docs._remember(db, pid, {"rev": 9, "draft": {"frozen": None}})
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        events = client.get(f"/v1/plans/{pid}/history").json()["events"]
        observed = [e for e in events if e["version"] == 9]
        assert {e["kind"] for e in observed} == {"freeze", "reopen"}
        assert all(e["rev"] is None and e["observed"] and e["at"] > 0 for e in observed)
        actions = [e for e in events if e["version"] == 8]
        assert [(e["kind"], e["actor"], e["at"], e["rev"], e["version"]) for e in actions] == [
            ("reopen", "person", 22, 2, 8), ("sign_off", "teammate:lee", 21.5, 2, 8),
            ("sign_off", "teammate:dana", 21, 2, 8),
            ("freeze", "person", 20, 2, 8)]
        assert len([e for e in events if e["kind"] == "revision"]) == 2
        comparison = client.get(f"/v1/plans/{pid}/revisions/2/changes").json()
        change = comparison["changes"][0]
        assert change["section"] == "summary"
        assert change["spans"] == [{"kind": "replace", "before_start": 4, "before_end": 7,
                                     "start": 4, "end": 7, "before": "old", "after": "new"}]
        assert comparison["previous"]["sections"][0]["markdown"] == "The old text."
        assert comparison["revision"]["sections"][0]["markdown"] == "The new text."
        first = client.get(f"/v1/plans/{pid}/revisions/1/changes").json()
        assert first["previous"] is None
        assert "decision:d1" in {c["section"] for c in first["changes"]}
        assert client.get(f"/v1/plans/{pid}/revisions/999/changes").status_code == 404
        assert client.get("/v1/plans/missing/history").status_code == 404
        assert plans.get(db, pid)["rev"] == 2
        visual_offers.person_spoke(db, boss, "Make a planning doc.")
        plans.propose(db, boss, "# Design", [{**DECISIONS[0], "mine": "repo"}], plan_id=pid,
                      sections=[{"id": "summary", "title": "Renamed", "markdown": "The new text."}])
        structural = client.get(f"/v1/plans/{pid}/revisions/3/changes").json()
        changed = {c["section"]: c for c in structural["changes"]}
        assert changed["removed"]["after"] is None
        assert changed["summary"]["before"]["title"] != changed["summary"]["after"]["title"]
        assert changed["decision:d1"]["spans"]
        assert "removed" not in {s["id"] for s in structural["document"]}


def test_relay_records_confirmed_actions_and_not_refusals(db, session, monkeypatch):
    import asyncio

    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Design", sections=[{"title": "Summary", "markdown": "Text"}])["id"]
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, owner, doc_id, remote_rev, local_rev) VALUES (?,?,?,?,?)",
                  (pid, "owner", "shared-doc", 5, 1))
    state = {"frozen": None, "refuse": False}

    def library(doc_id, reopen):
        if state["refuse"]:
            raise RuntimeError("refused")
        state["frozen"] = None if reopen else 5
        return state["frozen"]

    monkeypatch.setattr(live_docs, "_library_freeze", library)

    async def run():
        manager = live_docs.Manager(db_factory=lambda: db, drive=False, clock=lambda: 30,
                                   review=lambda doc_id: {"rev": 5, "sign_offs": []})
        # Use the actual dispatch while keeping the test database on its owning thread.
        async def work(fn, *args):
            return fn(db, *args)

        manager.work = work
        link = manager.link("shared-doc")
        assert await manager.freeze("shared-doc") is None
        assert await manager.freeze("shared-doc") is None
        state["refuse"] = True
        assert await manager.freeze("shared-doc", reopen=True) is not None
        assert link.frozen == 5
        state["refuse"] = False
        assert await manager.freeze("shared-doc", reopen=True) is None
        manager._pool.shutdown(wait=True)

    asyncio.run(run())
    shared_docs.remember_live_state(db, "shared-doc", 5, "owner", 40)
    shared_docs.remember_live_state(db, "shared-doc", None, "owner", 41)
    actions = [e for e in plan_docs.timeline(db, pid)["events"] if e["kind"] != "revision"]
    assert sorted(e["kind"] for e in actions) == ["freeze", "reopen"]
    assert all(e["at"] == 30 and not e["observed"] for e in actions)


def test_live_join_and_reconcile_archive_remote_freeze_and_reopen_without_sync(db, session):
    import asyncio

    from test_live_docs import Clock, FakeRelay, REVISION

    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, REVISION["lede"], sections=[
        {"id": "body", "title": "Body", "markdown": REVISION["body"]}])["id"]
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, owner, doc_id, remote_rev, local_rev) VALUES (?,?,?,?,?)",
                  (pid, "owner", "doc1", 8, 1))
    clock = Clock()

    async def run():
        relay = FakeRelay(clock, author="remote-author")
        relay.frozen = 8
        manager = live_docs.Manager(relay.transport("owner"), db_factory=lambda: db, drive=False, clock=clock,
                                   review=lambda doc_id: {"rev": 8, "sign_offs": [
                                       {"sub": "dana", "rev": 8, "at": clock() - 1}]})

        async def work(fn, *args):
            return fn(db, *args)

        manager.work = work
        try:
            link = manager.link("doc1")
            await link.connect()
            await link.note_version()
            clock.now += 10
            relay.frozen = None
            await link.reconcile()
            assert link.frozen is None
            await link.note_version()
        finally:
            await manager.stop()

    asyncio.run(run())
    events = [e for e in plan_docs.timeline(db, pid)["events"] if e["kind"] != "revision"]
    assert [(e["kind"], e["actor"], e["at"], e["observed"]) for e in events] == [
        ("reopen", "teammate:remote-author", clock(), True),
        ("freeze", "teammate:remote-author", clock() - 10, True),
        ("sign_off", "teammate:dana", clock() - 11, False)]
