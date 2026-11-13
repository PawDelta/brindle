"""brindle.pro.live_docs against an in-process relay that follows the protocol
of pawdelta-web's analysis_live.py (and the checks analysis_drafts makes on a
batch): no network, the lock's 24 hours on an injected clock, every frame
handled in turn so each test is one deterministic run."""

import asyncio
import base64
import json
import secrets

import loro
import pytest
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, CloseConnection, Request, TextMessage

from brindle import plans, visual_offers
from brindle.crdt import SharedDoc
from brindle.db import DB
from brindle.plan_drafts import DraftReplica
from brindle.pro import live_docs
from test_plans import session  # noqa: F401 - fixture

SEED_PEER = 1               # the relay's own replica: the revision's text
LOCK_HOLDS = 24 * 3600      # analysis_drafts.AUTHOR_OFFLINE_TTL
REVISION = {"lede": "# Design\n\nHello", "body": "World"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Pipe:
    """One relay socket: the daemon's end (send/recv/close) and the relay's (deliver/end)."""

    def __init__(self, relay: "FakeRelay", sub: str) -> None:
        self.relay, self.sub = relay, sub
        self.inbox: list[str] = []
        self.closed: int | None = None
        self.arrived = asyncio.Event()
        self.writers: set[int] = set()
        self.author = self.edited = False

    def deliver(self, frame: dict) -> None:
        if self.closed is None:
            self.inbox.append(json.dumps(frame))
            self.arrived.set()

    def end(self, code: int) -> None:
        if self.closed is None:
            self.closed = code
            self.arrived.set()
        self.relay.leave(self, code)

    def take(self) -> str | None:
        return self.inbox.pop(0) if self.inbox else None

    async def send(self, text: str) -> None:
        if self.closed is not None:
            raise OSError("the relay socket is closed")
        self.relay.receive(self, text)

    async def recv(self) -> str | None:
        while not self.inbox:
            if self.closed is not None:
                return None
            self.arrived.clear()
            await self.arrived.wait()
        return self.inbox.pop(0)

    async def close(self, code: int) -> None:
        self.end(code)


class FakeRelay:
    """One shared design's draft as the relay holds it: seeded with the
    revision under SEED_PEER, a ledger of the numbers it issued, and the
    author-offline lock."""

    def __init__(self, clock: Clock, author: str = "alice") -> None:
        self.clock, self.author = clock, author
        self.doc = loro.LoroDoc()
        self.doc.peer_id = SEED_PEER
        for name, text in REVISION.items():
            self.doc.get_text(name).insert(0, text)
        self.doc.commit()
        self.ledger: dict[int, str] = {}
        self.retired: set[int] = set()
        self.lock: dict | None = None
        self.pipes: list[Pipe] = []
        self.closes: list[tuple[str, int]] = []
        self.refusals: list[str] = []

    def transport(self, sub: str):
        relay = self

        class Transport:
            async def connect(self, doc_id: str) -> Pipe:
                return Pipe(relay, sub)

        return Transport()

    def sections(self) -> dict[str, str]:
        return {k: v for k, v in self.doc.get_deep_value().items() if isinstance(v, str)}

    def _lock_view(self) -> dict | None:
        if self.lock and self.clock() < self.lock["at"] + LOCK_HOLDS:
            return {"by": self.author, "at": self.lock["at"], "until": self.lock["at"] + LOCK_HOLDS}
        return None

    def _retire_lapsed(self) -> None:
        if self.lock and not self._lock_view():
            self.retired |= set(self.lock["replicas"])

    def _broadcast(self, frame: dict, skip: Pipe | None = None) -> None:
        for p in self.pipes:
            if p is not skip:
                p.deliver(frame)

    def receive(self, pipe: Pipe, text: str) -> None:
        said = json.loads(text)
        if said["type"] == "join":
            self._join(pipe, said)
        elif said["type"] == "update":
            self._update(pipe, said)
        elif said["type"] == "awareness":
            self._broadcast({"type": "awareness", "sub": pipe.sub, "state": said["state"]}, skip=pipe)
        elif said["type"] == "ping":
            pipe.deliver({"type": "pong"})

    def _join(self, pipe: Pipe, said: dict) -> None:
        if any(self.ledger.get(n) != pipe.sub for n in said["replicas"]):
            pipe.deliver({"type": "error", "error": "writer"})
            pipe.end(1008)
            return
        writers = set(said["replicas"])
        for _ in said["register"]:
            n = (1 << 16) + secrets.randbelow((1 << 63) - (1 << 16))
            self.ledger[n] = pipe.sub
            writers.add(n)
        pipe.writers, pipe.author = writers, pipe.sub == self.author
        self.pipes.append(pipe)
        pipe.deliver({"type": "state", "conn": str(id(pipe)), "replicas": sorted(writers),
                      "snapshot": base64.b64encode(self.doc.export(loro.ExportMode.Snapshot())).decode(),
                      "seq": 0, "version": "", "rev": 1, "frozen": None, "author": self.author,
                      "author_offline": self._lock_view(), "present": []})

    def _refuse(self, pipe: Pipe, said: dict, error: str, **extra) -> None:
        self.refusals.append(error)
        pipe.deliver({"type": "refused", "id": said.get("id"), "error": error, **extra})

    def _update(self, pipe: Pipe, said: dict) -> None:
        blob = base64.b64decode(said["batch"])
        lock = self._lock_view()
        if lock and not pipe.author:
            return self._refuse(pipe, said, "author_offline", **lock)
        meta = loro.LoroDoc.decode_import_blob_meta(blob, True)
        carried = set(loro.VersionRange.from_vv(meta.partial_end_vv).inner())
        if carried - pipe.writers:
            return self._refuse(pipe, said, "batch_refused", reason="writer")
        self._retire_lapsed()
        if carried & self.retired:
            return self._refuse(pipe, said, "stale_offline", reason="stale")
        status = self.doc.import_(blob)
        if status.pending is not None:
            return self._refuse(pipe, said, "batch_refused", reason="pending")
        pipe.edited = True
        pipe.deliver({"type": "ack", "id": said["id"], "seq": 1, "version": ""})
        self._broadcast({"type": "update", "seq": 1, "batch": said["batch"], "sub": pipe.sub}, skip=pipe)
        if pipe.author and self.lock:
            self.lock = None
            self._broadcast({"type": "lock", "author_offline": None})

    def leave(self, pipe: Pipe, code: int) -> None:
        if pipe not in self.pipes:
            return
        self.pipes.remove(pipe)
        self.closes.append((pipe.sub, code))
        if code != 1000 and pipe.author and pipe.edited and not any(p.author for p in self.pipes):
            self._retire_lapsed()
            self.lock = {"at": self.clock(), "replicas": sorted(n for n, s in self.ledger.items() if s == pipe.sub)}
            self._broadcast({"type": "lock", "author_offline": self._lock_view()})

    def drop(self, sub: str) -> None:
        """The network takes ``sub``'s connection: no clean close."""
        for p in [p for p in self.pipes if p.sub == sub]:
            p.end(1006)


class Window:
    """An app window's local socket, as the relay link writes to it."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.closed: int | None = None

    async def send(self, frame: dict) -> None:
        self.frames.append(frame)

    async def close(self, code: int) -> None:
        self.closed = code


class Machine:
    """One daemon: its database and its relay links (driven by the test, not by tasks)."""

    def __init__(self, relay: FakeRelay, path, sub: str) -> None:
        self.path = None if path is None else str(path)
        self.manager = live_docs.Manager(relay.transport(sub), db_factory=self.db, available=lambda: True,
                                         drive=False)

    def db(self) -> DB:
        return DB(self.path)

    @property
    def link(self) -> live_docs.DocLink:
        return self.manager.link("doc1")

    def replica(self) -> DraftReplica:
        return DraftReplica.open(self.db(), "doc1")

    def sections(self) -> dict[str, str]:
        return self.replica().sections()

    async def type_in(self, section: str, offset: int, text: str) -> None:
        """The supervisor's edit (or any engine write) into the replica."""
        self.replica().insert(section, offset, text)
        await self.manager.changed("doc1")


async def settle(*links: live_docs.DocLink) -> None:
    """Hand each link the frames the relay sent it, until none are left."""
    busy = True
    while busy:
        busy = False
        for link in links:
            text = link.conn.take() if link.conn is not None else None
            if text is not None:
                busy = True
                await link.handle(text)


def test_two_machines_converge_and_an_app_window_writes_under_a_number_the_relay_issued(tmp_path):
    async def run():
        relay = FakeRelay(Clock())
        a, b = Machine(relay, tmp_path / "a.db", "alice"), Machine(relay, tmp_path / "b.db", "bob")
        await b.link.connect()
        await b.type_in("lede", 0, ">")
        await settle(b.link)
        # A's window seeded the replica from the revision before any join, as the local socket
        # does, and A wrote in it under a number of its own before joining.
        seeded = a.db()
        DraftReplica.open(seeded, "doc1", SharedDoc.new(REVISION).export_snapshot()).insert("body", 5, " here")
        live_docs.note_seed(seeded, "doc1", REVISION)
        await a.link.connect()
        await settle(a.link, b.link)
        # The relay's seed only, never a second one; A's early edit re-typed onto it, B's kept.
        assert a.sections() == b.sections() == {"lede": "># Design\n\nHello", "body": "World here"}

        window = Window()
        lease = await a.manager.open_window("doc1", window)
        app = SharedDoc.load(a.replica().export_bootstrap()[0])
        app.set_peer_id(lease.peer)                          # as the app does on {"peer": n}
        before = app.version()
        app.insert("body", 10, "!")
        a.replica().apply_remote(app.export_updates(before))  # what the local socket does with its frame
        await a.manager.changed("doc1")
        await b.type_in("lede", 0, "#")
        await settle(a.link, b.link)

        assert relay.refusals == []
        assert a.sections() == b.sections() == relay.sections() == {"lede": "#># Design\n\nHello", "body": "World here!"}
        assert any("update" in f for f in window.frames)     # B's change reached A's window

    asyncio.run(run())


def test_edits_made_while_disconnected_are_sent_from_the_outbox_on_reconnect_after_a_clean_close(tmp_path):
    async def run():
        relay = FakeRelay(Clock())
        a, b = Machine(relay, tmp_path / "a.db", "alice"), Machine(relay, tmp_path / "b.db", "bob")
        window = Window()
        lease = await a.manager.open_window("doc1", window)
        await a.link.connect()
        await b.link.connect()
        await a.type_in("body", 5, " now")
        await settle(a.link, b.link)

        await a.manager.close_window(lease)
        # The last window closed and nothing is unsent: a clean close, so the author is not locked out.
        assert relay.closes == [("alice", 1000)] and relay.lock is None and a.link.conn is None

        a.replica().insert("body", 0, "Offline: ")            # typed while disconnected
        await a.manager.stop()                                # the daemon restarts
        assert live_docs.docs_with_unsent(a.db()) == ["doc1"]
        again = Machine(relay, tmp_path / "a.db", "alice")
        await again.link.connect()                            # claims the numbers it stored
        await settle(again.link, b.link)

        assert relay.refusals == []
        assert b.sections()["body"] == relay.sections()["body"] == "Offline: World now"
        assert live_docs.docs_with_unsent(again.db()) == []

    asyncio.run(run())


@pytest.fixture
def shared_plan(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, REVISION["lede"], sections=[{"id": "body", "title": "Body", "markdown": "World"}])["id"]
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, doc_id, owner) VALUES (?, ?, ?)", (pid, "doc1", "alice"))
    return pid


def test_offline_edits_refused_as_stale_after_the_lock_lapsed_become_suggestions(db, shared_plan, tmp_path):
    async def run():
        clock = Clock()
        relay = FakeRelay(clock, author="alice")
        a, b = Machine(relay, None, "alice"), Machine(relay, tmp_path / "b.db", "bob")
        b_window = Window()
        await b.manager.open_window("doc1", b_window)
        await a.link.connect()
        await b.link.connect()
        await a.type_in("body", 5, " now")
        await settle(a.link, b.link)

        relay.drop("alice")                                   # mid-edit, no clean close
        await settle(b.link)
        assert b_window.frames[-1]["state"]["lock"] == "author_offline"
        a.replica().insert("body", 0, "Late ")               # typed offline
        clock.now += LOCK_HOLDS + 1                           # the lock lapsed: A's numbers are retired
        await a.link.connect()
        await settle(a.link, b.link)
        assert relay.refusals == ["stale_offline"]

        await a.link.connect()                                # starts again under new numbers
        await settle(a.link, b.link)
        assert a.sections() == relay.sections() == {"lede": REVISION["lede"], "body": "World now"}
        thread = db.conn.execute("SELECT anchor, after, status FROM plan_threads WHERE plan_id=? AND kind='suggestion'",
                                 (shared_plan,)).fetchone()
        assert (json.loads(thread["anchor"])["section"], thread["after"], thread["status"]) == ("body", "Late ", "open")

    asyncio.run(run())


def test_the_relay_socket_signs_in_sends_text_frames_and_closes_cleanly(monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_DEV", "1")              # plain http to a loopback relay

    async def run():
        seen: dict = {}
        closed = asyncio.Event()

        async def relay(reader, writer):
            ws = WSConnection(ConnectionType.SERVER)
            while not closed.is_set():
                ws.receive_data(await reader.read(65536))
                for event in ws.events():
                    if isinstance(event, Request):
                        seen["auth"], seen["target"] = dict(event.extra_headers)[b"authorization"], event.target
                        writer.write(ws.send(AcceptConnection()))
                    elif isinstance(event, TextMessage):
                        seen["said"] = event.data
                        writer.write(ws.send(TextMessage(data='{"type":"pong"}')))
                    elif isinstance(event, CloseConnection):
                        seen["code"] = event.code
                        writer.write(ws.send(event.response()))
                        closed.set()
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(relay, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = live_docs.RelayTransport(lambda: (f"http://127.0.0.1:{port}/api/brindle/v1", "access-token"))
        conn = await transport.connect("doc 1")
        await conn.send('{"type":"ping"}')
        assert await conn.recv() == '{"type":"pong"}'
        await conn.close(1000)
        await closed.wait()
        server.close()
        assert seen == {"auth": b"Bearer access-token", "target": "/api/brindle/v1/analysis-docs/doc%201/design/live",
                        "said": '{"type":"ping"}', "code": 1000}

    asyncio.run(run())
