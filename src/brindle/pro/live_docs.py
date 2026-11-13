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
  snapshot), so two writers never share a number. The session's supervisor
  writes under one registered for it with the ``supervisor`` mark
  (``plan_drafts.supervisor_peer``), so the draft credits its letters to it. Every number is claimed
  again on every join; new ones are registered at a join (two at most).
- **Outbox.** Everything the replica holds past ``live_docs.relay_version``
  (what the relay is known to hold) is unsent: it goes up as one batch at a
  time, and the version moves on with each ack. Since the replica is in
  SQLite, unsent edits outlive a dropped connection and a restart.
- **Remote changes** are merged into the replica and passed to the app's
  windows, and so, at a join, is whatever others wrote while this machine was
  away (the relay replays nothing). Lock and freeze states go to them as the
  app contract's ``{"state": {...}}`` (``app/src/api/client.ts``). A lock or
  freeze holds the outbox back until it ends; the relay says nothing when an
  author-offline lock lapses or a version is reopened over REST, so a held
  link checks (:meth:`DocLink.reconcile`): the lock's ``until``, or a join.
- **Re-typing.** A replica the relay cannot take as it is (seeded here before
  its first join, which would put the revision's text in twice, or under a
  number that is no longer this account's) is rebuilt: the relay's draft,
  with what changed here since the base re-typed onto it under the
  replica's number (:func:`plan_drafts.retype`), in one write transaction
  with reading the old one (:func:`plan_drafts.rebuild`). An app window's
  edit that arrives on the old replica after that is re-typed onto the new
  one, and windows are told to open the channel again (``RELOAD``).
- **Stale offline edits.** After an author-offline lock lapsed (24 hours),
  the relay refuses the author's old replicas (``stale_offline``). In the
  write that drops the replica, each of their edits is kept in
  ``live_doc_offline`` with the live text it was made against; each is
  forgotten only once it stands on the plan's revision as a suggestion (or,
  for words only removed, a comment), filed now or at a later sweep. The
  replica starts again from the relay's draft under new numbers.
- **Closing.** Only an unclean drop mid-edit sets the author-offline lock,
  so once the last window closes and nothing is unsent the connection is
  closed cleanly (1000), and on the daemon's way out too.

The daemon starts the :class:`Manager` and stops it (``daemon/lifecycle.py``).
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import secrets
import sqlite3
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
from brindle.plan_drafts import matched, retype

log = logging.getLogger(__name__)

PING_EVERY = 30.0           # the relay's protocol: a daemon pings every 30 s
SWEEP_EVERY = 15.0          # how often docs with unsent edits are looked for
BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)   # between connection attempts, in turn
MAX_CLAIMS = 8              # analysis_live.MAX_REPLICAS: numbers one connection may claim
MAX_REGISTER = 2            # analysis_live: "register" lists at most two marks
SPARE_WINDOWS = 1           # numbers kept free, so a window opened offline can still type
MAX_AWARENESS = 4000        # under the relay's 4096-character awareness state
DRAIN_FOR = 10.0            # seconds a Freeze waits for the edits typed before it to reach the relay
DRAIN_STEP = 0.1            # between looks
MARK = "person"             # whom a number writes for: the person (replica and windows) ...
SUPERVISOR = "supervisor"   # ... or the session's supervisor (plan_drafts.supervisor_peer)

CLOSE_CLEAN = 1000
RELOAD = 4205               # to the app: the live text here was replaced; open the channel again


class RelayError(RuntimeError):
    """The relay refused a join, or the connection broke."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# -- what this machine keeps (sync; one DB connection, the manager's thread) ------------------------


def note_seed(db: DB, doc_id: str, sections: dict[str, str], version: bytes | None = None) -> None:
    """A replica was seeded here from the doc's revision before any join:
    keep that text (and the replica's ``version`` right after seeding) as
    the base its local edits are re-typed from."""
    with db.tx() as c:
        c.execute("INSERT INTO live_docs (doc_id, relay_version, seed, seed_version, updated_at)"
                  " VALUES (?, NULL, ?, ?, ?) ON CONFLICT(doc_id) DO NOTHING",
                  (doc_id, json.dumps(sections), version, time.time()))


def _link(db: DB | sqlite3.Connection, doc_id: str):
    c = db.conn if isinstance(db, DB) else db
    return c.execute("SELECT relay_version, seed, seed_version FROM live_docs WHERE doc_id=?", (doc_id,)).fetchone()


def _put_relay_version(c: sqlite3.Connection, doc_id: str, version: bytes) -> None:
    c.execute("INSERT INTO live_docs (doc_id, relay_version, seed, updated_at) VALUES (?, ?, NULL, ?)"
              " ON CONFLICT(doc_id) DO UPDATE SET relay_version=excluded.relay_version, seed=NULL,"
              " updated_at=excluded.updated_at", (doc_id, version, time.time()))


def _set_relay_version(db: DB, doc_id: str, version: bytes) -> None:
    with db.tx() as c:
        _put_relay_version(c, doc_id, version)


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


def _base(replica: plan_drafts.DraftReplica | SharedDoc, row) -> dict[str, str] | None:
    """The text the relay last held of this replica, or None if unknown."""
    if row is not None and row["relay_version"] is not None:
        return replica.sections_at(row["relay_version"])
    if row is not None and row["seed"] is not None:
        return json.loads(row["seed"])
    return None


def _base_version(row) -> bytes | None:
    """The version :func:`_base` is the text of, when it is known."""
    if row is None:
        return None
    return row["relay_version"] if row["relay_version"] is not None else row["seed_version"]


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


def _rebuild(db: DB, doc_id: str, snapshot: bytes, peer: int | None, carry: bool,
             supervisor: int | None = None) -> None:
    """The replica becomes the relay's draft, written under ``peer`` (None:
    a reader's, which writes nothing) and the supervisor's under
    ``supervisor``; with ``carry``, what changed here since the base re-typed
    onto it. One write transaction from reading the old replica to storing
    the new one and the relay's version with it, so an edit another holder
    commits meanwhile lands before (and is carried) or after (and is
    re-typed onto the new replica: DraftReplica.apply_remote)."""
    def make(c: sqlite3.Connection, current: SharedDoc | None) -> SharedDoc:
        doc = SharedDoc.load(snapshot)
        if peer is not None:
            # The number's earlier changes are the relay's, all in the snapshot: only now may it write.
            doc.set_peer_id(peer)
        if carry and current is not None:
            link = _link(c, doc_id)
            base, since = _base(current, link), _base_version(link)
            ours = current.sections()
            spans = None
            if since is not None:
                # The changes since, letter by letter (which letters were typed and which removed),
                # never a guess from how the texts look: an untouched letter is never re-typed.
                base = current.sections_at(since)
                then, now = current.letters(since), current.letters()
                spans = {s: matched(then.get(s, []), now.get(s, [])) for s in set(base) | set(ours)}
            # What the supervisor typed or deleted (under its relay number, or one it chose before
            # it had one) is re-typed under its relay number; the rest under the person's.
            row = c.execute("SELECT supervisor_peer FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone()
            theirs = {n for n in (row["supervisor_peer"] if row else None, supervisor) if n is not None}
            writers = deleters = None
            if supervisor is not None and theirs:
                writers = {s: [supervisor if e in theirs else doc.peer_id for e in current.editors(s)]
                           for s in ours}
                if since is not None:
                    deleters = {s: [supervisor if e in theirs else doc.peer_id for e in who]
                                for s, who in current.deleters_since(since).items()}
            retype(doc, doc.sections() if base is None else base, ours, writers, deleters, spans)
        _put_relay_version(c, doc_id, update_version(snapshot))
        return doc

    plan_drafts.rebuild(db, doc_id, make, supervisor=supervisor)


def _record_numbers(db: DB, doc_id: str, issued: list[int], claimed: list[int], marks: list[str]) -> None:
    """The numbers a join issued, by role (``marks``: what it asked for, all
    for one mark, so each new number's role is known)."""
    fresh = [n for n in issued if n not in claimed]
    roles = _peers(db, doc_id)
    with db.tx() as c:
        for n in fresh:
            role = ("supervisor" if marks and marks[0] == SUPERVISOR
                    else "window" if "replica" in roles.values() else "replica")
            roles[n] = role
            c.execute("INSERT OR IGNORE INTO live_doc_peers (doc_id, peer, role) VALUES (?, ?, ?)", (doc_id, n, role))


def _take_state(db: DB, doc_id: str, snapshot: bytes, issued: list[int], claimed: list[int],
                rebase: bool, marks: list[str]) -> tuple[bool, bytes | None]:
    """A join's answer: record the numbers it issued (:func:`_record_numbers`),
    then bring the replica up to the relay's draft, with the supervisor
    writing under its own relay number. Returns whether the replica was
    replaced (app windows on it must load it again) and, if not, what the
    join brought in that the windows lack (the relay sends nobody what it
    accepted while this machine was away)."""
    _record_numbers(db, doc_id, issued, claimed, marks)
    roles = _peers(db, doc_id)
    mine = next((n for n, r in roles.items() if r == "replica"), None)   # None: a reader, nothing to send
    theirs = next((n for n, r in roles.items() if r == "supervisor"), None)
    replica, row = _replica(db, doc_id), _link(db, doc_id)
    if replica is None:
        # The first text here (a teammate's window waits on it, holding nothing): windows load it.
        _rebuild(db, doc_id, snapshot, mine, carry=False, supervisor=theirs)
        return True, None
    stored = db.conn.execute("SELECT supervisor_peer FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone()
    # A supervisor that wrote under an id of its own: its letters are re-typed under its relay number.
    other_supervisor = theirs is not None and stored["supervisor_peer"] not in (None, theirs)
    if rebase or row is None or row["relay_version"] is None or (mine is not None and replica.peer_id != mine) \
            or other_supervisor:
        _rebuild(db, doc_id, snapshot, mine, carry=mine is not None, supervisor=theirs)
        return True, None
    before = replica.version()
    try:
        replica.apply_remote(snapshot)
    except (CrdtError, plan_drafts.DraftGone):     # compacted past what this replica holds, or replaced
        _rebuild(db, doc_id, snapshot, mine, carry=mine is not None, supervisor=theirs)
        return True, None
    with db.tx() as c:
        _put_relay_version(c, doc_id, merge_versions(row["relay_version"], update_version(snapshot)))
        if theirs is not None:
            # From now on the supervisor writes under the number the relay issued it. Whatever it
            # wrote before under one of its own goes as unsent edits, refused and re-typed once.
            c.execute("UPDATE plan_drafts SET supervisor_peer=? WHERE doc_id=?", (theirs, doc_id))
    return False, (None if replica.holds(before) else replica.export_updates(before))


def _join_numbers(db: DB, doc_id: str, in_use: set[int], wanted_windows: int,
                  reader: bool) -> tuple[list[int], list[str]]:
    """The numbers to claim and the marks to register new ones for: the
    replica's first, then the supervisor's, then windows', never two marks in
    one join (its answer lists numbers without saying which mark each is)."""
    if reader:
        return [], []
    roles = _peers(db, doc_id)
    replica = [n for n, r in roles.items() if r == "replica"]
    supervisor = [n for n, r in roles.items() if r == "supervisor"]
    windows = sorted((n for n, r in roles.items() if r == "window"), key=lambda n: n not in in_use)
    claims = (replica + supervisor + windows)[:MAX_CLAIMS]
    room = MAX_CLAIMS - len(claims)
    free = sum(1 for n in windows if n in claims and n not in in_use)
    windows_short = max(0, wanted_windows + SPARE_WINDOWS - free)
    if not replica:
        return claims, [MARK] * min(1 + windows_short, MAX_REGISTER, room)
    if not supervisor:
        return claims, [SUPERVISOR] * min(1, room)
    return claims, [MARK] * min(windows_short, MAX_REGISTER, room)


def _missing_supervisor(db: DB, doc_id: str) -> bool:
    return "supervisor" not in _peers(db, doc_id).values()


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


def _hold_offline(db: DB, doc_id: str) -> int:
    """The relay refused this replica's unsent edits as stale (typed offline
    under numbers retired when the author-offline lock lapsed). In one write:
    keep each edit, with the live text it was made against, in
    ``live_doc_offline``, and drop the replica and its numbers, so the next
    join starts again from the relay's draft. Returns how many were kept."""
    kept = 0
    with db.tx() as c:
        got = plan_drafts._read(c, doc_id)
        base = _base(got[1], _link(c, doc_id)) if got is not None else None
        if got is not None and base is not None:
            ours = got[1].sections()
            for section in sorted(set(base) | set(ours)):
                was, mine = base.get(section, ""), ours.get(section, "")
                for i1, i2, j1, j2 in (plan_drafts.hunks(was, mine) if was != mine else []):
                    c.execute("INSERT INTO live_doc_offline (doc_id, section, base, start, before, after, key)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?)", (doc_id, section, was, i1, was[i1:i2], mine[j1:j2],
                                                                f"live-offline:{secrets.token_hex(12)}"))
                    kept += 1
        for table in ("live_docs", "live_doc_peers", "plan_drafts"):
            c.execute(f"DELETE FROM {table} WHERE doc_id=?", (doc_id,))
    return kept


OFFLINE = "Typed offline while the author's lock had lapsed."
PART = 1900     # of an offline edit's text, per comment or reply (under visuals.MAX_NOTE)


def _origin(db: DB, origin: str) -> int | None:
    row = db.conn.execute("SELECT id FROM plan_threads WHERE origin=?", (origin,)).fetchone()
    return None if row is None else row["id"]


def _recovery_text(edit) -> str:
    """The whole of an offline edit as a note says it."""
    text = f"{OFFLINE} It changed {edit['section']} in the live draft, and has no place in this revision as it is."
    if edit["before"]:
        text += f"\n\nIt replaced:\n\n{edit['before']}"
    return text + (f"\n\nIt wrote:\n\n{edit['after']}" if edit["after"] else "\n\n(It wrote nothing in its place.)")


def _parts(edit) -> list[str]:
    """:func:`_recovery_text` in parts a comment or reply each holds; «» keep
    the edges of each part's slice from being trimmed. Each part alone is
    scanned for secrets as it is written, so the whole is scanned first
    (:func:`_file_one`): a secret split across two parts passes neither."""
    text = _recovery_text(edit)
    chunks = [text[i:i + PART] for i in range(0, len(text), PART)]
    return [f"({i}/{len(chunks)}) «{chunk}»" for i, chunk in enumerate(chunks, 1)]


def _file_one(db: DB, plan_id: str, edit) -> None:
    """One offline edit onto the plan's revision as it is now, as the thread
    whose ``origin`` is the edit's ``key`` (written in that thread's own
    insert). If that thread exists, a crash came after it was made: it is the
    edit, whatever the revision has become since, and nothing is made again.

    - where the words it replaced (or removed) still stand there, a suggestion
      replacing them with its words (none, for a deletion);
    - else, if it wrote words, a suggestion inserting them where they would go,
      its note saying what they replaced in the live draft;
    - else, or when that would not fit a suggestion (it only removed words the
      revision doesn't have, its section is gone, a quote or note past its
      limit), its whole text as a comment and replies to it, on its section if
      the revision still has it, else on the overview or the first part it has.

    Raises ServiceError when the plan won't take it now."""
    from brindle import plan_docs, plans, visuals
    from brindle.service.errors import ServiceError

    key, section, before, after = edit["key"], edit["section"], edit["before"], edit["after"]
    head = _origin(db, key)
    p = plan_docs._plan(db, plan_id)
    if head is None:
        # What suggest checks against: the live draft's text if this machine holds one, else the revision.
        body = plan_docs._body(plan_docs.current(db, p), section)
        if body is not None:
            at, _ = plan_drafts.mapping(edit["base"], body)
            start, end = at(edit["start"]), at(edit["start"] + len(before))
            if before and body[start:end] == before and len(before) <= plan_docs.MAX_QUOTE:
                plan_docs.suggest(db, plan_id, p["rev"], section, start, before, after, OFFLINE, origin=key)
                return
            note = f"{OFFLINE} In the live draft it replaced “{before}”." if before else OFFLINE
            if after and len(note) <= PART:
                plan_docs.suggest(db, plan_id, p["rev"], section, start, "", after, note, origin=key)
                return
    elif db.conn.execute("SELECT kind FROM plan_threads WHERE id=?", (head,)).fetchone()["kind"] == "suggestion":
        return
    # The whole text, before any part of it is written (a resumed filing too): a
    # secret across a part boundary would pass each part's own scan.
    visuals._refuse_secrets([("an edit typed offline", _recovery_text(edit))])
    parts = _parts(edit)
    if head is None:
        places = plans.anchors(p)
        on = next((s for s in (section, plans.LEDE, *places) if s in places), None)
        if on is None:
            raise ServiceError("invalid", "this revision has nothing to note an offline edit on", 409)
        head = plan_docs.comment(db, plan_id, p["rev"], {"kind": "section", "section": on}, parts[0], origin=key)
    for i, part in enumerate(parts[1:], 2):
        if _origin(db, f"{key}#{i}") is None:
            plan_docs.comment(db, plan_id, parent=head, text=part, origin=f"{key}#{i}")


def _forget_offline(db: DB, n: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM live_doc_offline WHERE n=?", (n,))


def _file_offline(db: DB, doc_id: str) -> int:
    """File the edits ``live_doc_offline`` keeps for ``doc_id`` onto its plan;
    each is forgotten only once all of it stands there. One that can't be
    filed now (the plan is approved, a secret in it, ...) stays for the next
    pass. Returns how many are left."""
    from brindle.service.errors import ServiceError

    share = db.conn.execute("SELECT plan_id FROM plan_shares WHERE doc_id=?", (doc_id,)).fetchone()
    edits = db.conn.execute("SELECT * FROM live_doc_offline WHERE doc_id=? ORDER BY n", (doc_id,)).fetchall()
    if share is None:
        return len(edits)
    left = 0
    for edit in edits:
        try:
            _file_one(db, share["plan_id"], edit)
        except ServiceError as e:
            left += 1
            log.warning("brindle: an edit to %s typed offline isn't on the plan yet: %s", doc_id, e)
            continue
        _forget_offline(db, edit["n"])
    return left


def docs_with_offline(db: DB) -> list[str]:
    return [r["doc_id"] for r in db.conn.execute("SELECT DISTINCT doc_id FROM live_doc_offline ORDER BY doc_id")]


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
    freezing: bool = False      # a Freeze or Reopen is being asked of the Library: hold the outbox (only it clears this)
    freeze_lock: asyncio.Lock = field(default_factory=asyncio.Lock)    # one Freeze or Reopen at a time
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
    _reconciled: float = 0.0     # when a blocked outbox was last checked (manager.clock)

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
            # A join registers one mark: the supervisor's number takes a second, straight after.
            # Only then is the draft taken, so anything re-typed already has both numbers to go under.
            later = not self.reader and await self.manager.work(_missing_supervisor, self.doc_id)
            await self._join(take=not later)
            if later and self.conn is not None:
                await self.close()
                await self._join()
        await self.flush()

    async def _join(self, take: bool = True) -> None:
        in_use = {w.peer for w in self.windows if w.peer is not None}
        waiting = sum(1 for w in self.windows if w.peer is None)
        claims, marks = await self.manager.work(_join_numbers, self.doc_id, in_use, waiting, self.reader)
        conn = await self.manager.transport.connect(self.doc_id)
        self.conn, self.inflight = conn, {}
        try:
            await self._send({"type": "join", "replicas": claims, "register": marks, "open": not self.reader})
            text = await conn.recv()
        except BaseException:
            self.conn = None
            await conn.close(CLOSE_CLEAN)
            raise
        said = _parse(text) if text is not None else {"type": "error", "error": "closed"}
        if said.get("type") != "state":
            self.conn = None
            await conn.close(CLOSE_CLEAN)
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
        if not take:
            await self.manager.work(_record_numbers, self.doc_id, self.claimed, claims, marks)
            return
        replaced, missed = await self.manager.work(_take_state, self.doc_id, _unb64(said.get("snapshot")),
                                                   self.claimed, claims, self.rebase, marks)
        self.rebase = False
        if replaced:
            await self._reload_windows()
        else:
            await self._to_windows(self.state_frame())
            if missed is not None:      # what others wrote while this machine was away
                await self._to_windows({"update": _b64(missed)})
            await self._lend_numbers()

    async def reconcile(self) -> None:
        """While a lock or a freeze holds the outbox back, find out whether it
        still does: the relay tells nobody when an author-offline lock lapses
        or a frozen version is reopened (a REST call). A lapsed lock is read
        from its ``until``; a freeze, from a join's state (the connection is
        closed cleanly and joined again)."""
        if not self.blocked:
            return
        if self.frozen:
            if self.conn is not None:
                await self.close()
                await self.connect()
            return
        until = (self.author_offline or {}).get("until")
        if not isinstance(until, (int, float)) or self.manager.clock() >= until:
            self.author_offline, self.blocked = None, False
            await self._to_windows(self.state_frame())
            await self.flush()

    async def flush(self) -> None:
        """Send the next batch of the outbox, if nothing is in flight."""
        async with self.lock:
            if self.conn is None or self.inflight or self.blocked or self.freezing or self.reader:
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
            kept = await self.manager.work(_hold_offline, self.doc_id)
            left = await self.manager.work(_file_offline, self.doc_id)
            log.info("brindle: %d edit(s) to %s typed offline go to the plan (%d not yet)", kept, self.doc_id, left)
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
                text = ""
            if text is None:
                if self.conn is conn:
                    self.conn, self.inflight = None, {}
                return
            if text:
                await self.handle(text)
            if self.blocked and self.manager.clock() - self._reconciled >= PING_EVERY:
                self._reconciled = self.manager.clock()
                await self.reconcile()

    async def drive(self) -> None:
        """Keep the link up while it is wanted (the manager's task for it)."""
        if not await self.manager.prepare(self.doc_id):
            return
        attempt = 0
        while self.wanted() and not self.manager.stopping:
            try:
                if self.conn is None:
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
                 available: Callable[[], bool] | None = None, drive: bool = True,
                 clock: Callable[[], float] = time.time) -> None:
        self.transport = transport or RelayTransport()
        self.wait = wait
        self.clock = clock          # the relay's lock times are its wall clock's
        self.stopping = False
        self._db_factory = db_factory
        self._db: DB | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brindle-live-docs")
        self._available = available or _signed_in_to_a_team
        self._drive = drive
        self.links: dict[str, DocLink] = {}
        self._probes: dict[str, asyncio.Task[bool]] = {}
        self._unsupported: set[str] = set()
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
        if (self._drive and not self.stopping and link.doc_id not in self._unsupported
                and (link.task is None or link.task.done())):
            link.task = asyncio.get_running_loop().create_task(link.drive())

    async def available(self) -> bool:
        return await self.work(lambda _db: self._available())

    async def prepare(self, doc_id: str) -> bool:
        """Confirm the relay before a window advertises live text or state.

        A missing or refusing relay leaves the reader on its HTTP controls.
        Share one initial probe across windows and remember its failure until
        the daemon restarts, so opening readers and sweeping cannot hammer an
        undeployed relay. Once confirmed, ordinary drops keep the live outbox.
        """
        if self.stopping or not await self.available():
            return False

        async def probe() -> bool:
            link = self.link(doc_id)
            try:
                if link.conn is None:
                    await asyncio.wait_for(link.connect(), DRAIN_FOR)
                return True
            except Exception:  # noqa: BLE001 - live capability is optional; HTTP remains available
                self._unsupported.add(doc_id)
                await link.close()
                log.debug("brindle: live unavailable for %s; using HTTP", doc_id, exc_info=True)
                return False

        if doc_id not in self._probes:
            self._probes[doc_id] = asyncio.create_task(probe())
        return await asyncio.shield(self._probes[doc_id])

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

    async def freeze(self, doc_id: str, reopen: bool = False) -> str | None:
        """An app window's Freeze (or Reopen): the Library makes the draft's
        text a version out for sign-off (or withdraws it), for the author only.
        The relay tells nobody of either, so this machine's windows are told
        here. Returns why it was refused (for the window that asked), else
        None; a refusal leaves the state, and every unsent edit, as they were.

        A freeze is of what was typed here before it: those edits reach the
        relay (acknowledged) first, else the Library is never asked. While it
        is asked, edits are held (``freezing``: nothing but this clears it, so
        a reconnect or a lock check can't let one race the freeze), and they go
        once it is refused or the version is reopened. One Freeze or Reopen
        runs at a time per doc."""
        from brindle.redact import mask

        link = self.link(doc_id)
        async with link.freeze_lock:
            refused = None
            try:
                if not reopen and not link.frozen:
                    await self._drain(link)
                link.freezing = True
                link.frozen = await asyncio.to_thread(_library_freeze, doc_id, reopen)
            except Exception as e:  # noqa: BLE001 - refused or unreachable: the windows keep the state they had
                refused = mask(str(e)) or "the Library couldn't be reached"
                log.warning("brindle: couldn't %s %s: %s", "reopen" if reopen else "freeze", doc_id, refused)
            finally:
                link.freezing = False
            link.blocked = bool(link.frozen or link.author_offline)
            await link._to_windows(link.state_frame())
            await link.flush()
            return refused

    async def _drain(self, link: DocLink) -> None:
        """Until every edit this machine holds of the doc is acknowledged by
        the relay; raises RelayError if that can't happen now (no connection,
        a lock holding the outbox back, or not within DRAIN_FOR)."""
        waited = 0.0
        while True:
            await link.flush()
            if not link.inflight and not link._unsent:
                return
            if link.conn is None or link.blocked or waited >= DRAIN_FOR:
                raise RelayError("edits made here haven't reached the relay yet: nothing was frozen")
            await self.wait(DRAIN_STEP)
            waited += DRAIN_STEP

    async def sweep(self) -> None:
        """File edits typed offline that aren't on their plan yet, and start a
        link for every doc whose replica holds unsent edits."""
        for doc_id in await self.work(docs_with_offline):
            await self.work(_file_offline, doc_id)
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
        for probe in self._probes.values():
            if not probe.done():
                probe.cancel()
        await asyncio.gather(*self._probes.values(), return_exceptions=True)

        def close_db() -> None:
            if self._db is not None:
                self._db.conn.close()
                self._db = None

        await asyncio.get_running_loop().run_in_executor(self._pool, close_db)
        self._pool.shutdown(wait=False)


def _library_freeze(doc_id: str, reopen: bool) -> Any:
    """Freeze the draft as it stands at the Library (its ``rev`` on screen
    being the draft's), or reopen the version out for sign-off. Returns the
    frozen revision, or None once it is open. Raises with the Library's refusal."""
    from brindle.pro import analysis_docs as ad

    def ask(method: str, tail: str, body: dict | None = None) -> dict:
        status, got = ad._call(method, ad._doc_path(doc_id, "/design" + tail), body)
        if status != 200:
            raise RelayError(f"the Library refused it: {got.get('error') or f'HTTP {status}'}", got.get("error"))
        return got

    draft = ask("GET", "/draft")
    if reopen and draft.get("frozen"):
        ask("POST", "/reopen", {"rev": draft["frozen"]})
    elif not reopen and not draft.get("frozen"):
        ask("POST", "/freeze", {"rev": draft["rev"]})
    return ask("GET", "/draft").get("frozen")


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

