"""The app's live editor, through the daemon transport and local replica."""

import asyncio
import base64
import json
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


@pytest.mark.variant("A competing SQLite writer must not block unrelated work on the daemon event loop")
def test_a_contended_draft_write_keeps_the_daemon_event_loop_responsive(db, live_doc, monkeypatch):
    entered = threading.Event()
    progressed = threading.Event()
    receive = DraftReplica.receive

    async def progress():
        progressed.set()

    def receive_with_trace(replica, data):
        def trace(statement):
            if statement == "BEGIN IMMEDIATE":
                entered.set()

        replica.db.conn.set_trace_callback(trace)
        try:
            return receive(replica, data)
        finally:
            replica.db.conn.set_trace_callback(None)

    monkeypatch.setattr(DraftReplica, "receive", receive_with_trace)
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


def test_an_app_window_edit_reaches_another_machine_under_the_number_its_socket_lent(db, live_doc, tmp_path):
    from brindle.pro import live_docs
    from test_live_docs import Clock, FakeRelay, Machine, settle

    relay = FakeRelay(Clock(), author="owner")
    heard = threading.Event()
    receive = relay.receive

    def receive_and_note(pipe, text):
        receive(pipe, text)
        heard.set()

    here = live_docs.Manager(relay.transport("owner"), available=lambda: True, drive=False)
    there = Machine(relay, tmp_path / "there.db", "teammate")
    app = build_app(TOKEN)
    app.state.live_docs = here
    path = f"/v1/visuals/{live_doc}/live"
    with TestClient(app) as client:
        client.portal.call(here.link("doc1").connect)
        client.portal.call(there.link.connect)
        with client.websocket_connect(path) as window:
            window.send_text(TOKEN)
            peer = window.receive_json()["peer"]                # before the snapshot, as a string
            assert window.receive_json()["state"]["lock"] is None
            doc = SharedDoc.load(base64.b64decode(window.receive_json()["update"]))
            doc.set_peer_id(int(peer))
            before = doc.version()
            doc.insert("body", 5, "!")
            relay.receive = receive_and_note
            window.send_json({"update": encoded(doc.export_updates(before))})
            assert heard.wait(FIRST_FRAME_TIMEOUT)              # the socket sent it on to the relay
            client.portal.call(settle, here.link("doc1"), there.link)
        client.portal.call(here.stop)
    assert relay.refusals == []
    assert there.sections() == {"lede": "# Design\n\nHello", "body": "World!"}


def test_the_authors_freeze_and_reopen_frames_lock_the_draft_for_sign_off_and_open_it_again(db, live_doc, monkeypatch):
    """DocPresence's Freeze and Reopen buttons send {"freeze": true} and
    {"reopen": true} on the live socket: the Library makes and withdraws the
    version, and the channel stays open showing the lock. The version holds
    what was typed before Freeze even while the relay hasn't taken it yet (the
    freeze goes over HTTP and could overtake it). An edit made here while the
    Library is being asked waits, even if the link checks its locks meanwhile,
    and so does one typed once it is frozen, until Reopen. A Freeze that can't
    reach the relay first is refused to the window that asked: nothing frozen,
    nothing lost."""
    from brindle.pro import live_docs
    from test_live_docs import Clock, FakeRelay, settle

    relay = FakeRelay(Clock(), author="owner")
    receive = relay.receive
    held: list[tuple] = []
    stashed = threading.Event()
    versions: dict[bool, list[str]] = {False: [], True: []}
    asked, answer = threading.Event(), threading.Event()

    def library(doc_id, reopen):                        # the Library's freeze and reopen, and the text each saw
        versions[reopen].append(relay.sections()["body"])
        if not reopen:
            asked.set()
            answer.wait(FIRST_FRAME_TIMEOUT)            # the Library takes its time answering
        relay.frozen = None if reopen else 2
        return relay.frozen

    def engine_types(text):                             # the supervisor or engine writing into the draft here
        replica = DraftReplica.open(db, "doc1")
        replica.insert("body", len(replica.text("body")), text)

    def slow_socket(pipe, text):                        # the relay takes an update only once time passes
        if json.loads(text)["type"] == "update":
            held.append((pipe, text))
            stashed.set()
        else:
            receive(pipe, text)

    async def time_passes(seconds):                     # Manager.wait: the held updates arrive and are acked
        while held:
            receive(*held.pop(0))
        await settle(here.link("doc1"))

    monkeypatch.setattr(live_docs, "_library_freeze", library, raising=False)
    here = live_docs.Manager(relay.transport("owner"), available=lambda: True, drive=False, wait=time_passes)
    app = build_app(TOKEN)
    app.state.live_docs = here
    with TestClient(app) as client:
        client.portal.call(here.link("doc1").connect)
        relay.receive = slow_socket
        with client.websocket_connect(f"/v1/visuals/{live_doc}/live") as window:
            window.send_text(TOKEN)
            peer = window.receive_json()["peer"]
            window.receive_json()
            doc = SharedDoc.load(base64.b64decode(window.receive_json()["update"]))
            doc.set_peer_id(int(peer))

            def types(text):
                before = doc.version()
                doc.insert("body", len(doc.text("body")), text)
                window.send_json({"update": encoded(doc.export_updates(before))})

            types("!")                                      # typed before Freeze, still on its way to the relay
            window.send_json({"freeze": True})
            assert asked.wait(FIRST_FRAME_TIMEOUT)
            stashed.clear()
            engine_types("#")                               # while the Library is being asked
            client.portal.call(here.changed, "doc1")
            client.portal.call(here.link("doc1").reconcile)
            assert not stashed.is_set() and not held        # nothing went to race the freeze
            with db.tx() as c:                              # the review as last read from the Library
                c.execute("UPDATE plan_shares SET review=? WHERE doc_id='doc1'",
                          (json.dumps({"sign_offs": [{"sub": "u_dana", "rev": 2}, {"sub": "u_old", "rev": 1}]}),))
            answer.set()
            state = window.receive_json()["state"]
            assert state["lock"] == "sign_off"
            assert versions[False] == ["World!"]           # the version has "!", not "#"
            # The window is told the version itself: its number, its text, who wrote and who signed it.
            version = state["version"]
            assert (version["id"], version["signOffs"], version["writers"]) == (2, ["u_dana"], ["you"])
            assert SharedDoc.load(base64.b64decode(version["snapshot"])).text("body") == "World!"
            types("?")
            window.send_json({"reopen": True})
            reopened = window.receive_json()["state"]
            assert (reopened["lock"], reopened["version"]) == (None, None)
            assert stashed.wait(FIRST_FRAME_TIMEOUT)        # what waited goes once it is open
            client.portal.call(time_passes, 0)
            body = relay.sections()["body"]
            assert versions[True] == ["World!"] and sorted(body[len("World!"):]) == ["#", "?"]

            client.portal.call(here.link("doc1").close)     # the relay can't be reached
            types("%")
            window.send_json({"freeze": True})
            assert window.receive_json()["state"]["lock"] is None
            assert window.receive_json()["refused"]["action"] == "freeze"
            assert client.portal.call(here.work, live_docs.unsent, "doc1")     # "%" kept to send
        client.portal.call(here.stop)
    assert len(versions[False]) == 1                        # the refused Freeze never reached the Library
    assert relay.refusals == []
    assert relay.sections()["body"] == body


def test_a_teammate_opens_a_design_shared_with_them_by_its_library_id_and_writes_in_it(db):
    """The app's shared-design reader opens the live socket on the Library doc
    id: the relay supplies its text before the window opens, and its edit
    goes up under the number the socket lent."""
    from brindle.pro import live_docs
    from test_live_docs import REVISION, Clock, FakeRelay

    relay = FakeRelay(Clock(), author="alice")
    heard = threading.Event()
    receive = relay.receive

    def receive_and_note(pipe, text):
        receive(pipe, text)
        heard.set()

    here = live_docs.Manager(relay.transport("bob"), available=lambda: True, drive=False)
    told = threading.Event()
    pass_on = here.awareness

    async def awareness(lease, section, data):          # the socket passed a presence frame on to the relay
        told.set()
        await pass_on(lease, section, data)

    here.awareness = awareness
    app = build_app(TOKEN)
    app.state.live_docs = here
    path = "/v1/visuals/ad_shared1/live"
    with TestClient(app) as client:
        with client.websocket_connect(path) as window:
            window.send_text(TOKEN)
            peer = window.receive_json()["peer"]
            assert window.receive_json()["state"]["lock"] is None
            doc = SharedDoc.load(base64.b64decode(window.receive_json()["update"]))
            assert doc.sections() == REVISION
            presence = loro.EphemeralStore(30_000)
            presence.set("123-cm-user", {"name": "Bob"})
            window.send_json({"section": "__document__", "awareness": encoded(presence.encode_all())})
            assert told.wait(FIRST_FRAME_TIMEOUT)
            doc.set_peer_id(int(peer))
            before = doc.version()
            doc.insert("body", 5, "!")
            relay.receive = receive_and_note
            window.send_json({"update": encoded(doc.export_updates(before))})
            assert heard.wait(FIRST_FRAME_TIMEOUT)
        client.portal.call(here.stop)
    assert relay.refusals == []
    assert relay.sections() == {"lede": "# Design\n\nHello", "body": "World!"}
