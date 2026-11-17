"""brindle.pro.live_docs against an in-process relay that follows the protocol
of pawdelta-web's analysis_live.py (and the checks analysis_drafts makes on a
batch): no network, the lock's 24 hours on an injected clock, every frame
handled in turn so each test is one deterministic run."""

import asyncio
import base64
import json
import secrets
import sqlite3
import threading

import loro
import pytest
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, CloseConnection, Request, TextMessage

from brindle import plans, visual_offers
from brindle.crdt import SharedDoc
from brindle.db import DB
from brindle.plan_drafts import DraftReplica, retype
from brindle.pro import live_docs
from test_plans import session  # noqa: F401 - fixture

SEED_PEER = 1               # the relay's own replica: the revision's text
LOCK_HOLDS = 24 * 3600      # analysis_drafts.AUTHOR_OFFLINE_TTL
NO_WAIT = 0                 # a connection that meets a held write lock fails at once
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
        self.marks: dict[int, str] = {}     # whom each number writes for: "person" or "supervisor"
        self.retired: set[int] = set()
        self.lock: dict | None = None
        self.frozen: int | None = None      # a version out for sign-off (freeze and reopen are REST calls)
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
        for mark in said["register"]:
            assert mark in ("person", "supervisor")
            n = (1 << 16) + secrets.randbelow((1 << 63) - (1 << 16))
            self.ledger[n] = pipe.sub
            self.marks[n] = mark
            writers.add(n)
        pipe.writers, pipe.author = writers, pipe.sub == self.author
        self.pipes.append(pipe)
        pipe.deliver({"type": "state", "conn": str(id(pipe)), "replicas": sorted(writers),
                      "snapshot": base64.b64encode(self.doc.export(loro.ExportMode.Snapshot())).decode(),
                      "seq": 0, "version": "", "rev": 1, "frozen": self.frozen, "author": self.author,
                      "author_offline": self._lock_view(), "present": []})

    def _refuse(self, pipe: Pipe, said: dict, error: str, **extra) -> None:
        self.refusals.append(error)
        pipe.deliver({"type": "refused", "id": said.get("id"), "error": error, **extra})

    def _update(self, pipe: Pipe, said: dict) -> None:
        blob = base64.b64decode(said["batch"])
        if self.frozen:
            return self._refuse(pipe, said, "frozen", rev=self.frozen)
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
                                         drive=False, clock=relay.clock)

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
        """A positional edit of the person's into the replica, as the engine makes one."""
        self.replica().insert(section, offset, text)
        await self.manager.changed("doc1")

    async def supervise_in(self, section: str, offset: int, text: str, delete: int = 0) -> None:
        """The supervisor's edit, the way plan_docs.supervise writes it: under its own peer id."""
        db = self.db()
        replica = DraftReplica.open(db, "doc1")
        with db.tx() as c:
            replica.supervise(c, lambda doc: doc.replace(section, offset, delete, text))
        await self.manager.changed("doc1")

    def supervisor_peer(self) -> int | None:
        return self.db().conn.execute("SELECT supervisor_peer FROM plan_drafts").fetchone()["supervisor_peer"]

    def generation(self) -> float:
        return self.db().conn.execute("SELECT created_at FROM plan_drafts").fetchone()["created_at"]


def edits_by(relay: FakeRelay, peer: int) -> tuple[int, int]:
    """How many letters the relay holds as typed, and as deleted, by ``peer``."""
    log = json.loads(relay.doc.export_json_updates(loro.VersionVector(), relay.doc.oplog_vv))
    peers = [int(p) for p in log["peers"]]
    ops = [op["content"] for change in log["changes"] if peers[int(change["id"].split("@")[1])] == peer
           for op in change["ops"]]
    return (sum(len(op["text"]) for op in ops if op.get("type") == "insert"),
            sum(abs(op["len"]) for op in ops if op.get("type") == "delete"))


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
        relay = FakeRelay(Clock(), author="bob")
        a, b = Machine(relay, tmp_path / "a.db", "alice"), Machine(relay, tmp_path / "b.db", "bob")
        await b.link.connect()
        await b.type_in("lede", 0, ">")
        await settle(b.link)
        # A's window seeded the replica from the revision before any join, as the local socket
        # does, and A wrote in it under a number of its own before joining.
        seeded = a.db()
        replica = DraftReplica.open(seeded, "doc1", SharedDoc.new(REVISION).export_snapshot())
        live_docs.note_seed(seeded, "doc1", REVISION, replica.version())
        replica.insert("body", 5, " here")
        # The supervisor writes and deletes before any join too.
        await a.supervise_in("lede", len(REVISION["lede"]), " (sv)")
        for at in reversed(range(len("# Design\n\n"), len("# Design\n\nHello"))):  # backwards, a letter at a time
            await a.supervise_in("lede", at, "", delete=1)
        await a.link.connect()
        await settle(a.link, b.link)
        # The relay's seed only, never a second one; A's early edits re-typed onto it, B's kept.
        assert a.sections() == b.sections() == {"lede": "># Design\n\n (sv)", "body": "World here"}
        # The supervisor's letters, and its deletion, went in under the number the relay issued it.
        supervisor = a.supervisor_peer()
        assert relay.marks[supervisor] == "supervisor"
        assert edits_by(relay, supervisor) == (len(" (sv)"), len("Hello"))
        assert edits_by(relay, a.replica().peer_id)[1] == 0       # none of its deletions credited to the person

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
        assert a.sections() == b.sections() == relay.sections() == {"lede": "#># Design\n\n (sv)", "body": "World here!"}

        relay.drop("alice")                                   # A is away a while; its window stays open
        await b.type_in("body", 0, "Remote ")
        await settle(b.link)
        await a.link.connect()                                # the relay replays nothing to a returning machine
        await settle(a.link, b.link)
        for frame in window.frames:
            if "update" in frame:
                app.import_updates(base64.b64decode(frame["update"]))
        assert app.sections() == relay.sections() == {"lede": "#># Design\n\n (sv)", "body": "Remote World here!"}

        # A change under a number the relay never issued goes up with the supervisor's next ones:
        # the batch is refused, and all are re-typed, each still under its own writer.
        stray = SharedDoc.load(a.replica().export_bootstrap()[0])
        before = stray.version()
        stray.insert("body", 0, "?")
        a.replica().apply_remote(stray.export_updates(before))
        await a.supervise_in("lede", 0, "Also: ")
        for at in reversed(range(1, 1 + len("Remote "))):
            await a.supervise_in("body", at, "", delete=1)
        await settle(a.link, b.link)
        assert relay.refusals == ["batch_refused"]
        await a.link.connect()
        await settle(a.link, b.link)
        assert a.sections() == b.sections() == relay.sections() == {"lede": "Also: #># Design\n\n (sv)",
                                                                       "body": "?World here!"}
        assert a.supervisor_peer() == supervisor
        assert edits_by(relay, supervisor) == (len(" (sv)Also: "), len("HelloRemote "))
        assert edits_by(relay, a.replica().peer_id)[1] == 0

    asyncio.run(run())


def test_a_window_edit_racing_the_first_join_is_carried_onto_the_relays_draft(tmp_path, monkeypatch):
    async def run():
        relay = FakeRelay(Clock(), author="bob")
        a = Machine(relay, tmp_path / "a.db", "alice")
        seeded = a.db()
        DraftReplica.open(seeded, "doc1", SharedDoc.new(REVISION).export_snapshot())
        live_docs.note_seed(seeded, "doc1", REVISION)
        # A window opened before this machine had relay numbers writes under its own, through
        # the local socket's connection to the database.
        window = DraftReplica.open(a.db(), "doc1")
        window.db.conn.execute(f"PRAGMA busy_timeout={NO_WAIT}")
        app = SharedDoc.load(window.export_bootstrap()[0])
        # Another window's change reaches the replica and this app, but not this socket's holder.
        other = DraftReplica.open(a.db(), "doc1")
        twin = SharedDoc.load(other.export_bootstrap()[0])
        before = twin.version()
        twin.insert("body", 5, " remote")
        other.apply_remote(twin.export_updates(before))
        app.import_updates(twin.export_updates(before))
        before = app.version()
        app.insert("body", 12, " local")
        first = app.export_updates(before)
        before = app.version()
        app.insert("body", 18, " again")
        second = app.export_updates(before)
        blocked = []
        loop = asyncio.get_running_loop()

        async def frame_arrives():
            try:
                window.apply_remote(first)
            except sqlite3.OperationalError:
                blocked.append(True)                          # the rebuild holds the write lock

        def racing(*args):
            # The window's frame arrives (on the socket's own thread) while the first join rebuilds the replica.
            asyncio.run_coroutine_threadsafe(frame_arrives(), loop).result()
            retype(*args)

        monkeypatch.setattr(live_docs, "retype", racing)
        await a.link.connect()
        assert blocked                                        # it couldn't land between the read and the write
        reloaded = SharedDoc.load(a.replica().export_bootstrap()[0])    # a window on the new replica
        # Its frames land after, on the new replica, the second first: it waits for the first.
        assert window.receive(second) == (False, None)
        merged, forward = window.receive(first)
        assert merged and reloaded.import_updates(forward)   # what other windows get is the new replica's
        await a.manager.changed("doc1")
        await settle(a.link)

        assert relay.refusals == []
        assert reloaded.text("body") == a.sections()["body"] == relay.sections()["body"] == "World remote local again"

    asyncio.run(run())


def test_an_older_review_read_answering_last_never_takes_back_a_newer_sign_off(tmp_path):
    """Two refreshes of a frozen version overlap: the first one's Library read
    is answered last, without the sign-off the second one already showed. The
    windows keep the sign-off, and the link's version too."""
    asked, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def review(doc_id):
        calls.append(doc_id)
        if len(calls) == 1:             # the older read: held until the newer one has answered
            asked.set()
            release.wait(5)
            return {"sign_offs": []}
        return {"sign_offs": [{"sub": "reviewer", "rev": 2}]}

    async def run():
        manager = live_docs.Manager(FakeRelay(Clock()).transport("bob"), db_factory=lambda: DB(str(tmp_path / "b.db")),
                                    available=lambda: True, drive=False, review=review)
        link = manager.link("doc1")
        link.frozen = 2
        window = Window()
        link.windows.append(live_docs.Lease("doc1", window))
        older = asyncio.create_task(link.refresh())
        assert await asyncio.to_thread(asked.wait, 5)
        await link.refresh()
        release.set()
        await older
        told = [f["state"]["version"]["signOffs"] for f in window.frames if "state" in f]
        assert told == [["reviewer"]] and link.version["signOffs"] == ["reviewer"]

    asyncio.run(run())


def test_a_held_outbox_goes_once_the_author_lock_lapses_and_once_a_frozen_version_is_reopened(tmp_path):
    async def run():
        clock = Clock()
        relay = FakeRelay(clock, author="alice")
        a, b = Machine(relay, tmp_path / "a.db", "alice"), Machine(relay, tmp_path / "b.db", "bob")
        window = Window()
        lease = await b.manager.open_window("doc1", window)
        await a.link.connect()
        await b.link.connect()
        # B held no text of the doc: its first join stores the relay's, and the window opens again on it.
        assert window.closed == live_docs.RELOAD
        await b.manager.close_window(lease)
        window = Window()
        await b.manager.open_window("doc1", window)
        await b.link.connect()                                # what the link's task does once a window is open again
        await a.type_in("body", 5, " now")
        await settle(a.link, b.link)
        relay.drop("alice")                                   # mid-edit: the draft is locked to its author
        await b.type_in("lede", 0, "> ")
        await settle(b.link)
        assert relay.refusals == ["author_offline"] and window.frames[-1]["state"]["lock"] == "author_offline"

        clock.now += LOCK_HOLDS + 1                           # it lapses; the relay says so to nobody
        await b.link.reconcile()
        await settle(b.link)
        assert relay.sections()["lede"] == "> # Design\n\nHello" and window.frames[-1]["state"]["lock"] is None

        relay.frozen = 2                                      # the author froze a version over REST
        await b.type_in("body", 0, "Draft: ")
        await settle(b.link)
        assert window.frames[-1]["state"]["lock"] == "sign_off"
        relay.frozen = None                                   # and reopened it, over REST too
        await b.link.reconcile()
        await settle(b.link)
        assert relay.sections()["body"] == "Draft: World now" and window.frames[-1]["state"]["lock"] is None

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
        await a.supervise_in("lede", 0, "Note: ")             # the supervisor writes too, under its own number
        await settle(a.link, b.link)
        supervisor = a.supervisor_peer()
        assert (relay.ledger[supervisor], relay.marks[supervisor]) == ("alice", "supervisor")

        await a.manager.close_window(lease)
        # The last window closed and nothing is unsent: a clean close, so the author is not locked out.
        assert relay.closes[-1] == ("alice", 1000) and relay.lock is None and a.link.conn is None

        a.replica().insert("body", 0, "Offline: ")            # typed while disconnected
        await a.supervise_in("lede", 0, "Again: ")
        generation = a.generation()
        await a.manager.stop()                                # the daemon restarts
        assert live_docs.docs_with_unsent(a.db()) == ["doc1"]
        again = Machine(relay, tmp_path / "a.db", "alice")
        await again.link.connect()                            # claims the numbers it stored
        await settle(again.link, b.link)

        assert relay.refusals == []
        assert b.sections() == relay.sections() == {"lede": "Again: Note: # Design\n\nHello", "body": "Offline: World now"}
        assert live_docs.docs_with_unsent(again.db()) == []
        # Nothing was re-typed: the same replica, and the supervisor's letters are still its own.
        assert (again.generation(), again.supervisor_peer()) == (generation, supervisor)
        assert relay.doc.oplog_vv.get_last(supervisor) == len("Note: Again: ") - 1

    asyncio.run(run())


@pytest.fixture
def shared_plan(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, REVISION["lede"], sections=[{"id": "body", "title": "Body", "markdown": "World"}])["id"]
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, doc_id, owner) VALUES (?, ?, ?)", (pid, "doc1", "alice"))
    return pid


def test_offline_edits_refused_as_stale_after_the_lock_lapsed_are_kept_until_they_stand_as_suggestions(
        db, shared_plan, tmp_path):
    async def run():
        clock = Clock()
        relay = FakeRelay(clock, author="alice")
        a, b = Machine(relay, None, "alice"), Machine(relay, tmp_path / "b.db", "bob")
        await a.link.connect()
        await b.link.connect()
        await a.type_in("body", 5, " now")                   # in the live draft, not in the plan's revision
        await settle(a.link, b.link)

        relay.drop("alice")                                   # mid-edit, no clean close: the author-offline lock
        a.replica().replace("body", 6, 3, "later")            # typed offline: "now" becomes "later"
        clock.now += LOCK_HOLDS + 1                           # the lock lapsed: A's numbers are retired
        state = db.conn.execute("SELECT state FROM plans WHERE id=?", (shared_plan,)).fetchone()["state"]
        with db.tx() as c:                                    # the plan can't take a suggestion just now
            c.execute("UPDATE plans SET state='approved' WHERE id=?", (shared_plan,))
        await a.link.connect()
        await settle(a.link, b.link)
        assert relay.refusals == ["stale_offline"]
        assert live_docs.docs_with_offline(db) == ["doc1"]    # kept, not dropped with the replica

        await a.link.connect()                                # starts again under new numbers
        await settle(a.link, b.link)
        assert a.sections() == relay.sections() == {"lede": REVISION["lede"], "body": "World now"}

        with db.tx() as c:
            c.execute("UPDATE plans SET state=? WHERE id=?", (state, shared_plan))
        await a.manager.sweep()
        threads = db.conn.execute("SELECT anchor, after, status FROM plan_threads WHERE plan_id=? AND kind='suggestion'",
                                  (shared_plan,)).fetchall()
        # The revision has no "now": "later" goes where it would stand in the revision's text.
        assert [(json.loads(t["anchor"])["start"], t["after"], t["status"]) for t in threads] == [(5, "later", "open")]
        assert live_docs.docs_with_offline(db) == []

    asyncio.run(run())


def test_each_offline_edit_is_filed_once_whole_deletions_included_and_a_crash_files_nothing_twice(
        db, shared_plan, monkeypatch):
    from brindle import plan_docs

    def keep(base, start, before, after, section="body"):
        with db.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM live_doc_offline").fetchone()[0]
            c.execute("INSERT INTO live_doc_offline (doc_id, section, base, start, before, after, key)"
                      " VALUES ('doc1', ?, ?, ?, ?, ?, ?)", (section, base, start, before, after, f"edit-{n}-{after}"))

    def threads():
        return [(t["kind"], json.loads(t["anchor"]).get("quote"), json.loads(t["anchor"]).get("section"), t["after"])
                for t in db.conn.execute("SELECT * FROM plan_threads WHERE plan_id=? AND parent_id IS NULL ORDER BY id",
                                         (shared_plan,))]

    async def run():
        machine = Machine(FakeRelay(Clock()), None, "alice")
        long_text = " ".join(f"word{i}" for i in range(500))  # past what one comment holds
        keep("World now", 0, "World", "")                    # removes words the revision has
        keep("World now", 6, "now", "")                      # removes words only the live draft had
        keep("World now", 6, "now", long_text, section="gone")  # in a section the revision doesn't have
        keep("World now", 6, "now", "later")
        forget = live_docs._forget_offline
        crashed = []

        def crash_once(db_, n):
            if not crashed:
                crashed.append(n)
                raise RuntimeError("the daemon stopped here")
            forget(db_, n)

        monkeypatch.setattr(live_docs, "_forget_offline", crash_once)
        with pytest.raises(RuntimeError):
            await machine.manager.sweep()                     # filed the first, then crashed before forgetting it
        p = plans.get(db, shared_plan)                        # meanwhile the plan moves on
        plan_docs.edit(db, shared_plan, p["rev"], "body", 0, "", "Prefix ")
        await machine.manager.sweep()                         # the same edit again: already filed, nothing remapped
        assert threads() == [("suggestion", "World", "body", ""),
                             ("comment", None, "body", None),
                             ("comment", None, "lede", None),
                             ("suggestion", "", "body", "later")]
        assert live_docs.docs_with_offline(db) == []
        # The edit in a missing section is there whole, across its comment and replies.
        head = db.conn.execute("SELECT id FROM plan_threads WHERE json_extract(anchor, '$.section')='lede'").fetchone()
        parts = [r["body"] for r in db.conn.execute(
            "SELECT body FROM plan_threads WHERE id=? OR parent_id=? ORDER BY id", (head["id"], head["id"]))]
        assert len(parts) > 1 and long_text in "".join(part[part.index("«") + 1:-1] for part in parts)

        # A later recovery makes the very same change at the same place: a different edit, filed anew.
        plan_docs.reject(db, shared_plan, db.conn.execute(
            "SELECT id FROM plan_threads WHERE after='later'").fetchone()["id"])
        keep("World now", 6, "now", "later")
        await machine.manager.sweep()
        assert [t for t in threads() if t[3] == "later"] == [("suggestion", "", "body", "later")] * 2

        # A secret across the boundary between two parts passes each part's own scan: none is written.
        lead = len(live_docs._recovery_text({"section": "gone", "before": "now", "after": "x"})) - 1
        token = "ghp_" + "A1b2C3d4E5" * 4                 # a made-up token in GitHub's format
        keep("World now", 6, "now", "x" * (live_docs.PART - lead - 20) + " " + token + " end", section="gone")
        before = db.conn.execute("SELECT COUNT(*) FROM plan_threads").fetchone()[0]
        await machine.manager.sweep()
        assert db.conn.execute("SELECT COUNT(*) FROM plan_threads").fetchone()[0] == before
        assert live_docs.docs_with_offline(db) == ["doc1"]    # kept, for its writer to take out

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
