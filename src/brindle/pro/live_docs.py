"""This machine's connection to the Library's live relay for shared design docs.

The relay (pawdelta-web ``backend/pro_api/analysis_live.py``) holds one room
per shared design's draft. While a doc is open in the app here (the daemon's
local live socket, :mod:`brindle.daemon.routes_visuals`) or its replica holds
edits the relay has not acknowledged, the daemon keeps one relay connection
for it, signed in with the account's Library credentials. Over it:

- **Numbers.** The relay accepts a change only under a replica number it
  issued to this account in this doc ("writer" refusal otherwise). The
  replica (:mod:`brindle.plan_drafts`) writes under one, and each app window
  is lent one of its own (a ``{"peer": n}`` frame before the window's
  snapshot), so two writers never share a number. Every number is claimed
  again on every join; new ones are registered at a join (two at most).
- **Outbox.** Everything the replica holds past ``live_docs.relay_version``
  (what the relay is known to hold) is unsent: it goes up as one batch at a
  time, and the version moves on with each ack. Since the replica is in
  SQLite, unsent edits outlive a dropped connection and a restart.
- **Remote changes** are merged into the replica and passed to the app's
  windows. Lock and freeze states go to them as the app contract's
  ``{"state": {...}}`` (``app/src/api/client.ts``).
- **Re-typing.** A replica the relay cannot take as it is (seeded here before
  its first join, which would put the revision's text in twice, or under a
  number that is no longer this account's) is rebuilt: the relay's draft,
  with what changed here since the base re-typed onto it under the
  replica's number (:func:`_retype`). App windows on the old text are told
  to open the channel again (``RELOAD``).
- **Stale offline edits.** After an author-offline lock lapsed (24 hours),
  the relay refuses the author's old replicas (``stale_offline``). Their
  edits become suggestions (:func:`plan_docs.suggest`) and the replica
  starts again from the relay's draft under new numbers.
- **Closing.** Only an unclean drop mid-edit sets the author-offline lock,
  so once the last window closes and nothing is unsent the connection is
  closed cleanly (1000), and on the daemon's way out too.

The daemon starts the :class:`Manager` and stops it (``daemon/lifecycle.py``).
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import itertools
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any, Protocol

from brindle import plan_drafts
from brindle.crdt import CrdtError, SharedDoc, merge_versions, update_version
from brindle.db import DB

log = logging.getLogger(__name__)

PING_EVERY = 30.0           # the relay's protocol: a daemon pings every 30 s
SWEEP_EVERY = 15.0          # how often docs with unsent edits are looked for
BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)   # between connection attempts, in turn
MAX_CLAIMS = 8              # analysis_live.MAX_REPLICAS: numbers one connection may claim
MAX_REGISTER = 2            # analysis_live: "register" lists at most two marks
SPARE_WINDOWS = 1           # numbers kept free, so a window opened offline can still type
MAX_AWARENESS = 4000        # under the relay's 4096-character awareness state
MARK = "person"             # whom a number writes for (T8 adds the supervisor's)

CLOSE_CLEAN = 1000
RELOAD = 4205               # to the app: the live text here was replaced; open the channel again


class RelayError(RuntimeError):
    """The relay refused a join, or the connection broke."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# -- what this machine keeps (sync; one DB connection, the manager's thread) ------------------------


def note_seed(db: DB, doc_id: str, sections: dict[str, str]) -> None:
    """A replica was seeded here from the doc's revision before any join:
    keep that text as the base its local edits are re-typed from."""
    with db.tx() as c:
        c.execute("INSERT INTO live_docs (doc_id, relay_version, seed, updated_at) VALUES (?, NULL, ?, ?)"
                  " ON CONFLICT(doc_id) DO NOTHING", (doc_id, json.dumps(sections), time.time()))


def _link(db: DB, doc_id: str):
    return db.conn.execute("SELECT relay_version, seed FROM live_docs WHERE doc_id=?", (doc_id,)).fetchone()


def _set_relay_version(db: DB, doc_id: str, version: bytes) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO live_docs (doc_id, relay_version, seed, updated_at) VALUES (?, ?, NULL, ?)"
                  " ON CONFLICT(doc_id) DO UPDATE SET relay_version=excluded.relay_version, seed=NULL,"
                  " updated_at=excluded.updated_at", (doc_id, version, time.time()))


def _peers(db: DB, doc_id: str) -> dict[int, str]:
    return {r["peer"]: r["role"] for r in db.conn.execute(
        "SELECT peer, role FROM live_doc_peers WHERE doc_id=? ORDER BY rowid", (doc_id,))}


def _forget_peers(db: DB, doc_id: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM live_doc_peers WHERE doc_id=?", (doc_id,))


def _replica(db: DB, doc_id: str) -> plan_drafts.DraftReplica | None:
    """The stored replica, never a new one."""
    if db.conn.execute("SELECT 1 FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone() is None:
        return None
    return plan_drafts.DraftReplica.open(db, doc_id)


def _base(replica: plan_drafts.DraftReplica, row) -> dict[str, str] | None:
    """The text the relay last held of this replica, or None if unknown."""
    if row is not None and row["relay_version"] is not None:
        return replica.sections_at(row["relay_version"])
    if row is not None and row["seed"] is not None:
        return json.loads(row["seed"])
    return None


def unsent(db: DB, doc_id: str) -> bool:
    """Whether the replica holds edits the relay has not acknowledged."""
    replica, row = _replica(db, doc_id), _link(db, doc_id)
    if replica is None or row is None:
        return False
    if row["relay_version"] is None:
        return row["seed"] is not None and replica.sections() != json.loads(row["seed"])
    return not replica.holds(row["relay_version"])


def docs_with_unsent(db: DB) -> list[str]:
    return [r["doc_id"] for r in db.conn.execute("SELECT doc_id FROM live_docs ORDER BY doc_id")
            if unsent(db, r["doc_id"])]


def _edits(base: str, ours: str, theirs: str) -> list[tuple[int, int, str]]:
    """What changed from ``base`` to ``ours``, as (start, length, text) in
    ``theirs``, another descendant of ``base``. A change of ours whose range
    they also changed deletes nothing of theirs: ours goes in beside it."""
    mapping = difflib.SequenceMatcher(None, base, theirs, autojunk=False).get_opcodes()

    def at(pos: int) -> int:
        for tag, i1, i2, j1, _ in mapping:
            if tag == "equal" and i1 <= pos <= i2:
                return j1 + pos - i1
        return next((j2 for _, i1, i2, _, j2 in mapping if i1 <= pos <= i2), len(theirs))

    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, ours, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        start = at(i1)
        touched = any(t != "equal" and i1 < b2 and b1 < i2 for t, b1, b2, _, _ in mapping)
        out.append((start, 0 if touched else max(at(i2) - start, 0), ours[j1:j2]))
    return out


def _retype(doc: SharedDoc, base: dict[str, str], ours: dict[str, str]) -> None:
    """Make on ``doc`` (the relay's draft) what changed from ``base`` to ``ours`` here."""
    for section in sorted(set(base) | set(ours)):
        was, mine = base.get(section, ""), ours.get(section, "")
        if was == mine:
            continue
        for start, length, text in reversed(_edits(was, mine, doc.text(section))):
            doc.replace(section, start, length, text)


def _rebuild(db: DB, doc_id: str, snapshot: bytes, peer: int, carry: bool) -> None:
    """The replica becomes the relay's draft under ``peer``; with ``carry``,
    what changed here since the base re-typed onto it."""
    replica, row = _replica(db, doc_id), _link(db, doc_id)
    doc = SharedDoc.load(snapshot)
    # The number's earlier changes are the relay's, all in the snapshot: only now may it write.
    doc.set_peer_id(peer)
    if carry and replica is not None:
        base = _base(replica, row)
        _retype(doc, doc.sections() if base is None else base, replica.sections())
    plan_drafts.adopt(db, doc_id, doc)
    _set_relay_version(db, doc_id, update_version(snapshot))


def _take_state(db: DB, doc_id: str, snapshot: bytes, issued: list[int], claimed: list[int],
                rebase: bool) -> bool:
    """A join's answer: record the numbers it issued, then bring the replica
    up to the relay's draft. True if the replica was replaced (app windows
    on it must load it again)."""
    fresh = [n for n in issued if n not in claimed]
    roles = _peers(db, doc_id)
    with db.tx() as c:
        for n in fresh:
            role = "window" if "replica" in roles.values() else "replica"
            roles[n] = role
            c.execute("INSERT OR IGNORE INTO live_doc_peers (doc_id, peer, role) VALUES (?, ?, ?)", (doc_id, n, role))
    mine = next((n for n, r in roles.items() if r == "replica"), None)
    replica, row = _replica(db, doc_id), _link(db, doc_id)
    if mine is None:        # a reader: no numbers, nothing of its own to send
        if replica is not None and row is not None and row["relay_version"] is not None and not rebase:
            try:
                replica.apply_remote(snapshot)
                _set_relay_version(db, doc_id, merge_versions(row["relay_version"], update_version(snapshot)))
                return False
            except CrdtError:
                pass
        plan_drafts.adopt(db, doc_id, SharedDoc.load(snapshot))
        _set_relay_version(db, doc_id, update_version(snapshot))
        return replica is not None
    if replica is None:
        _rebuild(db, doc_id, snapshot, mine, carry=False)
        return False
    if rebase or row is None or row["relay_version"] is None or replica.peer_id != mine:
        _rebuild(db, doc_id, snapshot, mine, carry=True)
        return True
    try:
        replica.apply_remote(snapshot)
    except CrdtError:       # the relay compacted past what this replica holds
        _rebuild(db, doc_id, snapshot, mine, carry=True)
        return True
    _set_relay_version(db, doc_id, merge_versions(row["relay_version"], update_version(snapshot)))
    return False


def _join_numbers(db: DB, doc_id: str, in_use: set[int], wanted_windows: int, reader: bool) -> tuple[list[int], int]:
    """The numbers to claim and how many new ones to register."""
    if reader:
        return [], 0
    roles = _peers(db, doc_id)
    replica = [n for n, r in roles.items() if r == "replica"]
    windows = sorted((n for n, r in roles.items() if r == "window"), key=lambda n: n not in in_use)
    claims = (replica + windows)[:MAX_CLAIMS]
    free = sum(1 for n in windows if n in claims and n not in in_use)
    need = (0 if replica else 1) + max(0, wanted_windows + SPARE_WINDOWS - free)
    return claims, min(need, MAX_REGISTER, MAX_CLAIMS - len(claims))


def _next_batch(db: DB, doc_id: str) -> tuple[bytes, bytes] | None:
    """The unsent changes, and the version the relay holds once it took them."""
    replica, row = _replica(db, doc_id), _link(db, doc_id)
    if replica is None or row is None or row["relay_version"] is None or replica.holds(row["relay_version"]):
        return None
    return replica.export_updates(row["relay_version"]), replica.version()


def _acked(db: DB, doc_id: str, version: bytes) -> None:
    row = _link(db, doc_id)
    _set_relay_version(db, doc_id, merge_versions(row["relay_version"] if row else None, version))


def _remote(db: DB, doc_id: str, batch: bytes) -> None:
    """Another writer's batch, which the relay holds: into the replica."""
    replica, row = _replica(db, doc_id), _link(db, doc_id)
    if replica is None or row is None:
        return
    replica.apply_remote(batch)
    _set_relay_version(db, doc_id, merge_versions(row["relay_version"], update_version(batch)))


def _suggest_offline(db: DB, doc_id: str) -> int:
    """Turn what changed here since the relay last held this replica into
    suggestions on the plan, then drop the replica (its numbers were retired)
    so the next join starts again from the relay's draft. Returns how many."""
    from brindle import plan_docs
    from brindle.service.errors import ServiceError

    replica, row = _replica(db, doc_id), _link(db, doc_id)
    made = 0
    share = db.conn.execute("SELECT plan_id FROM plan_shares WHERE doc_id=?", (doc_id,)).fetchone()
    base = _base(replica, row) if replica is not None else None
    rev = None
    if share is not None:
        try:
            rev = plan_docs._plan(db, share["plan_id"])["rev"]
        except ServiceError as e:
            log.warning("brindle: offline edits to %s have no plan to become suggestions on: %s", doc_id, e)
    if replica is not None and rev is not None and base is not None:
        ours = replica.sections()
        for section in sorted(set(base) | set(ours)):
            was, mine = base.get(section, ""), ours.get(section, "")
            ops = difflib.SequenceMatcher(None, was, mine, autojunk=False).get_opcodes()
            for tag, i1, i2, j1, j2 in ops:
                if tag == "equal":
                    continue
                try:
                    plan_docs.suggest(db, share["plan_id"], rev, section, i1, was[i1:i2], mine[j1:j2],
                                      "Written offline while the author's lock had lapsed")
                    made += 1
                except ServiceError as e:
                    log.warning("brindle: an offline edit to %s couldn't become a suggestion: %s", doc_id, e)
    plan_drafts.drop(db, doc_id)
    return made


# -- the relay connection ------------------------------------------------------------------------


class Connection(Protocol):
    """One relay socket: JSON text frames each way."""

    async def send(self, text: str) -> None: ...
    async def recv(self) -> str | None: ...     # None once it closed
    async def close(self, code: int) -> None: ...


class Transport(Protocol):
    async def connect(self, doc_id: str) -> Connection: ...


class Sink(Protocol):
    """An app window's local socket (routes_visuals.visual_live)."""

    async def send(self, frame: dict) -> None: ...
    async def close(self, code: int) -> None: ...


@dataclass(eq=False)
class Lease:
    """An app window on a doc and the number it writes under (None: read only
    until one is free; ``Manager.open_window`` returns None, not a Lease, when
    the relay isn't in use here at all)."""
    doc_id: str
    sink: Sink
    peer: int | None = None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("expected base64")
    return base64.b64decode(value, validate=True)


@dataclass(eq=False)
class DocLink:
    """One doc's relay connection, and the app windows open on it."""
    manager: Manager
    doc_id: str
    windows: list[Lease] = field(default_factory=list)
    conn: Connection | None = None
    author: str = ""
    frozen: Any = None
    author_offline: Any = None
    blocked: bool = False       # a lock or freeze holds the outbox back
    reader: bool = False        # this account can't write in the doc
    rebase: bool = False        # the next join rebuilds the replica from the relay's draft
    claimed: list[int] = field(default_factory=list)
    inflight: dict[int, bytes] = field(default_factory=dict)
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def wanted(self) -> bool:
        return bool(self.windows) or bool(self.inflight) or self._unsent

    _unsent: bool = False

    # -- the app's side

    def state_frame(self) -> dict:
        lock = "sign_off" if self.frozen else "author_offline" if self.author_offline else None
        return {"state": {"author": self.author, "lock": lock, "version": None}}

    async def _to_windows(self, frame: dict, skip: Lease | None = None) -> None:
        for w in tuple(self.windows):
            if w is skip:
                continue
            try:
                await w.sink.send(frame)
            except Exception:  # noqa: BLE001 - a window that went away is dropped when its socket ends
                log.debug("brindle: a live window didn't take a frame", exc_info=True)

    async def _reload_windows(self) -> None:
        for w in tuple(self.windows):
            try:
                await w.sink.close(RELOAD)
            except Exception:  # noqa: BLE001
                log.debug("brindle: a live window didn't close", exc_info=True)

    async def _lend_numbers(self, quiet: Lease | None = None) -> None:
        """Give windows still without a number a free one, and tell them
        (``quiet``: the window being opened, told by its socket itself)."""
        roles = await self.manager.work(_peers, self.doc_id)
        used = {w.peer for w in self.windows}
        free = [n for n, r in roles.items() if r == "window" and n not in used and n in self.claimed]
        for w in self.windows:
            if w.peer is None and free:
                w.peer = free.pop(0)
                if w is not quiet:
                    await w.sink.send({"peer": str(w.peer)})

    # -- the relay's side

    async def _send(self, frame: dict) -> None:
        if self.conn is None:
            raise RelayError("not connected")
        await self.conn.send(json.dumps(frame, separators=(",", ":")))

    async def connect(self) -> None:
        """Join the doc's room: claim this account's numbers, register what
        is missing, bring the replica up to the relay's draft, then send the outbox."""
        async with self.lock:
            await self._join()
        await self.flush()

    async def _join(self) -> None:
        in_use = {w.peer for w in self.windows if w.peer is not None}
        waiting = sum(1 for w in self.windows if w.peer is None)
        claims, register = await self.manager.work(_join_numbers, self.doc_id, in_use, waiting, self.reader)
        conn = await self.manager.transport.connect(self.doc_id)
        self.conn, self.inflight = conn, {}
        try:
            await self._send({"type": "join", "replicas": claims, "register": [MARK] * register,
                              "open": not self.reader})
            text = await conn.recv()
        except BaseException:
            self.conn = None
            raise
        said = _parse(text) if text is not None else {"type": "error", "error": "closed"}
        if said.get("type") != "state":
            self.conn = None
            error = said.get("error") if isinstance(said.get("error"), str) else "refused"
            if error == "writer":       # a number is no longer this account's: start again under new ones
                await self.manager.work(_forget_peers, self.doc_id)
                self.rebase = True
            elif error == "forbidden" and not self.reader:
                self.reader = True      # may read the draft, not write in it
            raise RelayError(f"the relay refused to join {self.doc_id}: {error}", error)
        self.claimed = [int(n) for n in said.get("replicas") or []]
        self.author, self.frozen = said.get("author") or "", said.get("frozen")
        self.author_offline = said.get("author_offline")
        self.blocked = bool(self.frozen)
        replaced = await self.manager.work(_take_state, self.doc_id, _unb64(said.get("snapshot")),
                                           self.claimed, claims, self.rebase)
        self.rebase = False
        if replaced:
            await self._reload_windows()
        else:
            await self._to_windows(self.state_frame())
            await self._lend_numbers()

    async def flush(self) -> None:
        """Send the next batch of the outbox, if nothing is in flight."""
        async with self.lock:
            if self.conn is None or self.inflight or self.blocked or self.reader:
                self._unsent = await self.manager.work(unsent, self.doc_id)
                return
            batch = await self.manager.work(_next_batch, self.doc_id)
            self._unsent = batch is not None
            if batch is None:
                return
            n = next(self._ids)
            self.inflight[n] = batch[1]
            await self._send({"type": "update", "id": n, "batch": _b64(batch[0])})

    async def handle(self, text: str) -> None:
        """One frame from the relay."""
        said = _parse(text)
        kind = said.get("type")
        if kind == "ack":
            async with self.lock:
                version = self.inflight.pop(said.get("id"), None)
                if version is not None:
                    await self.manager.work(_acked, self.doc_id, version)
            await self.flush()
        elif kind == "update":
            batch = _unb64(said.get("batch"))
            async with self.lock:
                await self.manager.work(_remote, self.doc_id, batch)
            await self._to_windows({"update": said["batch"]})
        elif kind == "refused":
            await self._refused(said)
        elif kind == "lock":
            self.author_offline = said.get("author_offline")
            if self.author_offline is None and not self.frozen:
                self.blocked = False
            await self._to_windows(self.state_frame())
            await self.flush()
        elif kind == "awareness":
            state = said.get("state")
            if isinstance(state, dict) and isinstance(state.get("section"), str) \
                    and isinstance(state.get("awareness"), str):
                await self._to_windows({"section": state["section"], "awareness": state["awareness"]})
        elif kind == "error":
            log.warning("brindle: the live relay said %s on %s", said.get("error"), self.doc_id)

    async def _refused(self, said: dict) -> None:
        error = said.get("error")
        async with self.lock:
            self.inflight.pop(said.get("id"), None)
        if error == "stale_offline":
            made = await self.manager.work(_suggest_offline, self.doc_id)
            log.info("brindle: %d edit(s) to %s typed offline became suggestions", made, self.doc_id)
            await self.manager.work(_forget_peers, self.doc_id)
            await self._reload_windows()
            await self.close()
        elif error == "author_offline":
            self.author_offline, self.blocked = {k: v for k, v in said.items() if k in ("by", "at", "until")}, True
            await self._to_windows(self.state_frame())
        elif error == "frozen":
            self.frozen, self.blocked = said.get("rev") or True, True
            await self._to_windows(self.state_frame())
        elif error == "batch_refused":
            # Changes the relay can't take as they are (not under this account's
            # numbers, or built on what it doesn't hold): re-type them on its draft.
            log.warning("brindle: the relay refused a batch of %s (%s): re-typing it", self.doc_id, said.get("reason"))
            self.rebase = True
            await self.close()
        # "conflict" and the rest: the batch goes again with the next flush.

    async def awareness(self, lease: Lease, section: str, data: str) -> None:
        """An app window's caret, passed to the room (never stored). The
        local socket passes it to this machine's other windows itself."""
        if self.conn is not None and len(data) + len(section) < MAX_AWARENESS:
            try:
                await self._send({"type": "awareness", "state": {"section": section, "awareness": data}})
            except (RelayError, OSError):
                log.debug("brindle: a caret didn't reach the relay", exc_info=True)

    async def close(self) -> None:
        """Leave the room cleanly: never read as the author dropping mid-edit."""
        conn, self.conn = self.conn, None
        self.inflight = {}
        if conn is not None:
            try:
                await conn.close(CLOSE_CLEAN)
            except (OSError, RelayError):
                log.debug("brindle: the relay connection didn't close cleanly", exc_info=True)

    async def serve(self) -> None:
        """Read the relay's frames until the connection ends; ping meanwhile."""
        while self.conn is not None:
            if not self.wanted():
                await self.close()
                return
            conn = self.conn
            try:
                text = await asyncio.wait_for(conn.recv(), PING_EVERY)
            except TimeoutError:
                await self._send({"type": "ping"})
                continue
            if text is None:
                if self.conn is conn:
                    self.conn, self.inflight = None, {}
                return
            await self.handle(text)

    async def drive(self) -> None:
        """Keep the link up while it is wanted (the manager's task for it)."""
        attempt = 0
        while self.wanted() and not self.manager.stopping:
            try:
                await self.connect()
                attempt = 0
                await self.serve()
            except (RelayError, OSError, ValueError) as e:
                log.info("brindle: the live relay link for %s broke: %s", self.doc_id, e)
            except Exception:  # noqa: BLE001 - a link must not take the daemon down
                log.warning("brindle: the live relay link for %s failed", self.doc_id, exc_info=True)
            self._unsent = await self.manager.work(unsent, self.doc_id)
            if self.wanted() and not self.manager.stopping and not self.rebase:
                await self.manager.wait(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                attempt += 1


def _parse(text: str) -> dict:
    said = json.loads(text)
    if not isinstance(said, dict):
        raise ValueError("a relay frame is an object")
    return said


async def _elapse(seconds: float) -> None:
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    handle = loop.call_later(seconds, done.set_result, None)
    try:
        await done
    finally:
        handle.cancel()


class Manager:
    """Every doc's relay link on this machine. Runs on the daemon's event loop;
    the database is used from one thread of its own."""

    def __init__(self, transport: Transport | None = None, *, db_factory: Callable[[], DB] = DB,
                 wait: Callable[[float], Awaitable[None]] = _elapse,
                 available: Callable[[], bool] | None = None, drive: bool = True) -> None:
        self.transport = transport or RelayTransport()
        self.wait = wait
        self.stopping = False
        self._db_factory = db_factory
        self._db: DB | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brindle-live-docs")
        self._available = available or _signed_in_to_a_team
        self._drive = drive
        self.links: dict[str, DocLink] = {}
        self._sweeper: asyncio.Task | None = None

    async def work(self, fn: Callable[..., Any], *args: Any) -> Any:
        """``fn(db, *args)`` on the manager's database thread."""
        def call() -> Any:
            if self._db is None:
                self._db = self._db_factory()
            return fn(self._db, *args)

        return await asyncio.get_running_loop().run_in_executor(self._pool, copy_context().run, call)

    def link(self, doc_id: str) -> DocLink:
        if doc_id not in self.links:
            self.links[doc_id] = DocLink(self, doc_id)
        return self.links[doc_id]

    def _ensure(self, link: DocLink) -> None:
        if self._drive and not self.stopping and (link.task is None or link.task.done()):
            link.task = asyncio.get_running_loop().create_task(link.drive())

    async def available(self) -> bool:
        return await self.work(lambda _db: self._available())

    async def open_window(self, doc_id: str, sink: Sink) -> Lease | None:
        """An app window opened ``doc_id``: its lease, with the number it
        writes under once one is free, or None when the relay isn't in use
        here (signed out, no Team plan, air-gapped): the window then writes
        under its own number, re-typed at the first join."""
        if self.stopping or not await self.available():
            return None
        link = self.link(doc_id)
        lease = Lease(doc_id, sink)
        link.windows.append(lease)
        if link.conn is not None:
            await link._lend_numbers(quiet=lease)
            if lease.peer is None and not link.reader:     # none free: join again to register more
                await link.close()
        self._ensure(link)
        return lease

    async def close_window(self, lease: Lease) -> None:
        link = self.links.get(lease.doc_id)
        if link is None or lease not in link.windows:
            return
        link.windows.remove(lease)
        await link.flush()
        if not link.wanted():
            await link.close()

    async def changed(self, doc_id: str) -> None:
        """The replica of ``doc_id`` changed here: send it on."""
        link = self.link(doc_id)
        link._unsent = True
        await link.flush()
        self._ensure(link)

    async def awareness(self, lease: Lease, section: str, data: str) -> None:
        link = self.links.get(lease.doc_id)
        if link is not None:
            await link.awareness(lease, section, data)

    async def sweep(self) -> None:
        """Start a link for every doc whose replica holds unsent edits."""
        if not await self.available():
            return
        for doc_id in await self.work(docs_with_unsent):
            link = self.link(doc_id)
            link._unsent = True
            self._ensure(link)

    async def _sweep_forever(self) -> None:
        while not self.stopping:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 - the sweep must never stop
                log.debug("brindle: couldn't look for unsent live edits", exc_info=True)
            await self.wait(SWEEP_EVERY)

    def start(self) -> None:
        self._sweeper = asyncio.get_running_loop().create_task(self._sweep_forever())

    async def stop(self) -> None:
        """Close every link cleanly; unsent edits stay in the outbox for the next daemon."""
        self.stopping = True
        if self._sweeper is not None:
            self._sweeper.cancel()
        for link in tuple(self.links.values()):
            await link.close()
            if link.task is not None:
                link.task.cancel()

        def close_db() -> None:
            if self._db is not None:
                self._db.conn.close()
                self._db = None

        await asyncio.get_running_loop().run_in_executor(self._pool, close_db)
        self._pool.shutdown(wait=False)


def _signed_in_to_a_team() -> bool:
    from brindle.pro import shared_docs

    try:
        shared_docs._require_team(None)
    except Exception:  # noqa: BLE001 - signed out, no Team plan, air-gapped: no relay
        return False
    return True


# -- the real transport: a WebSocket to the Library (wsproto over asyncio streams) ----------------


def _bearer() -> tuple[str, str]:
    """The Library's base URL and a current access token (refreshed if it is
    about to lapse). Raises ``RuntimeError`` when signed out or not entitled."""
    from brindle.pro import analysis_docs as ad
    from brindle.pro import auth

    client, store = ad._session()
    ad.require_entitlement(store)
    cur = store.load() or {}
    if not cur.get("refresh_token"):
        raise RelayError("not logged in to brindle Pro", "not_logged_in")
    if float(cur.get("access_expires_at") or 0) <= time.time() + auth.ACCESS_SLACK:
        auth.refresh(client, store, cur)
        cur = store.load() or {}
    return client.base, cur.get("access_token") or ""


class RelayTransport:
    """Connects to ``{base}/analysis-docs/{doc}/design/live`` as the signed-in account."""

    def __init__(self, bearer: Callable[[], tuple[str, str]] = _bearer) -> None:
        self._bearer = bearer

    async def connect(self, doc_id: str) -> Connection:
        from urllib.parse import urlsplit

        from brindle import airgap
        from brindle.pro import analysis_docs as ad
        from brindle.pro import auth

        base, token = await asyncio.to_thread(self._bearer)
        url = base + ad._doc_path(doc_id, "/design/live")
        auth.check_url(url)
        airgap.guard(url, "the live relay")
        parts = urlsplit(url)
        secure = parts.scheme == "https"
        context = None
        if secure:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        port = parts.port or (443 if secure else 80)
        reader, writer = await asyncio.open_connection(parts.hostname, port, ssl=context)
        return await WsConnection.open(reader, writer, parts.netloc, parts.path, token)


class WsConnection:
    """A client WebSocket on an asyncio stream: text frames only."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ws: Any) -> None:
        self._reader, self._writer, self._ws = reader, writer, ws
        self._parts: list[str] = []
        self.closed_code: int | None = None

    @classmethod
    async def open(cls, reader, writer, host: str, target: str, token: str) -> WsConnection:
        from wsproto import ConnectionType, WSConnection
        from wsproto.events import AcceptConnection, RejectConnection, RejectData, Request

        ws = WSConnection(ConnectionType.CLIENT)
        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        writer.write(ws.send(Request(host=host, target=target, extra_headers=headers)))
        await writer.drain()
        self = cls(reader, writer, ws)
        while True:
            for event in ws.events():
                if isinstance(event, AcceptConnection):
                    return self
                if isinstance(event, (RejectConnection, RejectData)):
                    writer.close()
                    raise RelayError(f"the relay refused the connection (HTTP {getattr(event, 'status_code', '?')})",
                                     "rejected")
            data = await reader.read(65536)
            if not data:
                writer.close()
                raise RelayError("the relay closed the connection during the handshake", "closed")
            ws.receive_data(data)

    async def send(self, text: str) -> None:
        from wsproto.events import TextMessage

        self._writer.write(self._ws.send(TextMessage(data=text)))
        await self._writer.drain()

    async def recv(self) -> str | None:
        from wsproto.events import CloseConnection, Ping, TextMessage

        while True:
            for event in self._ws.events():
                if isinstance(event, TextMessage):
                    self._parts.append(event.data)
                    if event.message_finished:
                        text, self._parts = "".join(self._parts), []
                        return text
                elif isinstance(event, Ping):
                    self._writer.write(self._ws.send(event.response()))
                elif isinstance(event, CloseConnection):
                    self.closed_code = event.code
                    try:
                        self._writer.write(self._ws.send(event.response()))
                        await self._writer.drain()
                    except Exception:  # noqa: BLE001 - it is going anyway
                        pass
                    self._writer.close()
                    return None
            if self.closed_code is not None:
                return None
            try:
                data = await self._reader.read(65536)
            except (OSError, ssl.SSLError):
                data = b""
            if not data:
                self.closed_code = 1006
                self._writer.close()
                return None
            self._ws.receive_data(data)

    async def close(self, code: int) -> None:
        from wsproto.connection import ConnectionState
        from wsproto.events import CloseConnection

        if self.closed_code is None and self._ws.state == ConnectionState.OPEN:
            self.closed_code = code
            self._writer.write(self._ws.send(CloseConnection(code=code)))
            await self._writer.drain()
        self._writer.close()

