"""The app's live editor, through the daemon transport and local replica."""

import asyncio
import base64
import threading

import loro
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brindle import plans, visual_offers
from brindle.crdt import SharedDoc
from brindle.daemon.app import build_app
from brindle.daemon.auth import FIRST_FRAME_TIMEOUT
from brindle.plan_drafts import DraftReplica
from test_plans import TOKEN, session  # noqa: F401 - fixture


@pytest.fixture
def live_doc(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Design\n\nHello", sections=[
        {"id": "body", "title": "Body", "markdown": "World"},
        {"id": "picture", "title": "Picture", "kind": "mermaid", "markdown": "graph LR; A-->B"},
    ])["id"]
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, doc_id, owner) VALUES (?, ?, ?)", (pid, "doc1", "owner"))
    return pid


def encoded(data):
    return base64.b64encode(data).decode("ascii")


def joined(socket):
    socket.send_text(TOKEN)
    return SharedDoc.load(base64.b64decode(socket.receive_json()["update"]))


def test_two_local_sockets_converge_and_rejoin_the_persisted_replica(db, live_doc):
    path = f"/v1/visuals/{live_doc}/live"
    with TestClient(build_app(TOKEN)) as client:
        with client.websocket_connect(path) as first, client.websocket_connect(path) as second:
            a, b = joined(first), joined(second)
            assert a.sections() == b.sections() == {"lede": "# Design\n\nHello", "body": "World"}
            va, vb = a.version(), b.version()
            a.insert("lede", len(a.text("lede")), " 🌍")
            b.insert("lede", 0, ">")
            first.send_json({"update": encoded(a.export_updates(va))})
            second.send_json({"update": encoded(b.export_updates(vb))})
            a.import_updates(base64.b64decode(first.receive_json()["update"]))
            b.import_updates(base64.b64decode(second.receive_json()["update"]))
            assert a.sections() == b.sections()
            assert a.text("lede") == "># Design\n\nHello 🌍"
            before = a.version()
            a.insert("body", 5, "1")
            dependency = a.export_updates(before)
            before = a.version()
            a.insert("body", 6, "2")
            first.send_json({"update": encoded(a.export_updates(before))})
            assert not b.import_updates(base64.b64decode(second.receive_json()["update"]))
            # Join while the second edit is still waiting on the first.
            with client.websocket_connect(path) as late:
                c = joined(late)
                first.send_json({"update": encoded(dependency)})
                b.import_updates(base64.b64decode(second.receive_json()["update"]))
                marker = loro.EphemeralStore(30_000)
                marker.set("marker", True)
                first.send_json({"section": "body", "awareness": encoded(marker.encode_all())})
                # The marker fences bootstrap/relay delivery without assuming
                # how many update frames the bootstrap needed.
                while "update" in (frame := late.receive_json()):
                    c.import_updates(base64.b64decode(frame["update"]))
                assert a.sections() == b.sections() == c.sections()
                assert c.text("body") == "World12"
        assert DraftReplica.open(db, "doc1").sections() == a.sections()
        with client.websocket_connect(path) as again:
            rejoined = joined(again)
            assert rejoined.sections() == a.sections()
            before = rejoined.version()
            rejoined.insert("lede", 0, "must not be saved")
            with db.tx() as c:
                c.execute("UPDATE plans SET state='approved' WHERE id=?", (live_doc,))
            again.send_json({"update": encoded(rejoined.export_updates(before))})
            with pytest.raises(WebSocketDisconnect) as closed:
                again.receive_json()
            assert closed.value.code == 4409
            assert DraftReplica.open(db, "doc1").sections() == a.sections()

        with client.websocket_connect(path) as refused:
            refused.send_text("wrong")
            with pytest.raises(WebSocketDisconnect) as closed:
                refused.receive_json()
            assert closed.value.code == 4401
        with client.websocket_connect("/v1/visuals/missing/live") as missing:
            missing.send_text(TOKEN)
            with pytest.raises(WebSocketDisconnect) as closed:
                missing.receive_json()
            assert closed.value.code == 4404
        with client.websocket_connect(path) as locked:
            locked.send_text(TOKEN)
            with pytest.raises(WebSocketDisconnect) as closed:
                locked.receive_json()
            assert closed.value.code == 4409


def test_carets_are_relayed_and_replayed_without_any_sqlite_write(db, live_doc):
    path = f"/v1/visuals/{live_doc}/live"
    with TestClient(build_app(TOKEN)) as client:
        with client.websocket_connect(path) as first, client.websocket_connect(path) as second:
            joined(first)
            joined(second)
            before = list(db.conn.iterdump())
            presence = loro.EphemeralStore(30_000)
            presence.set("123-cm-user", {"name": "Ada"})
            frame = {"section": "lede", "awareness": encoded(presence.encode_all())}
            first.send_json(frame)
            assert second.receive_json() == frame
            # A later caret-only update must not erase the name on joining.
            presence.set("123-cm-cursor", {"anchor": 2})
            frame = {"section": "lede", "awareness": encoded(presence.encode("123-cm-cursor"))}
            first.send_json(frame)
            assert second.receive_json() == frame
            with client.websocket_connect(path) as third:
                joined(third)
                replay = third.receive_json()
                got = loro.EphemeralStore(30_000)
                got.apply(base64.b64decode(replay["awareness"]))
                assert replay["section"] == "lede"
                assert got.get_all_states() == presence.get_all_states()
            assert list(db.conn.iterdump()) == before


def test_a_contended_draft_write_keeps_the_daemon_event_loop_responsive(db, live_doc, monkeypatch):
    entered = threading.Event()
    progressed = threading.Event()
    apply_remote = DraftReplica.apply_remote

    async def progress():
        progressed.set()

    def apply_with_trace(replica, data):
        def trace(statement):
            if statement == "BEGIN IMMEDIATE":
                entered.set()

        replica.db.conn.set_trace_callback(trace)
        try:
            return apply_remote(replica, data)
        finally:
            replica.db.conn.set_trace_callback(None)

    monkeypatch.setattr(DraftReplica, "apply_remote", apply_with_trace)
    path = f"/v1/visuals/{live_doc}/live"
    with TestClient(build_app(TOKEN)) as client:
        with client.websocket_connect(path) as first, client.websocket_connect(path) as second:
            a, b = joined(first), joined(second)
            loop = client.portal.call(asyncio.get_running_loop)
            before = a.version()
            a.insert("body", 5, "!")
            # Hold a real competing write transaction until an unrelated task
            # progresses on the daemon loop while the draft waits in SQLite.
            with db.tx():
                first.send_json({"update": encoded(a.export_updates(before))})
                assert entered.wait(FIRST_FRAME_TIMEOUT)
                asyncio.run_coroutine_threadsafe(progress(), loop)
                assert progressed.wait(FIRST_FRAME_TIMEOUT)
            b.import_updates(base64.b64decode(second.receive_json()["update"]))
            assert b.text("body") == "World!"
