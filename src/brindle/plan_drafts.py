"""This machine's replica of a shared design doc's live draft.

Each shared doc has at most one replica here, a row in ``plan_drafts``: the
draft's Loro state (see :mod:`brindle.crdt`) and the peer id this machine
writes it under. The peer id is chosen once, when the replica is made, and
kept with the state for as long as the replica exists, so the draft's text
can always say which replica wrote each letter.

A Loro peer numbers its own changes from where its earlier ones stop. A peer
id used without the changes already made under it would number new text the
same as old, and replicas would disagree about both. So:

- the peer id is set only after the stored state is loaded into the document;
- the id and the state are read and written together, in one row;
- every write happens inside a transaction against the latest stored state,
  never over it; an edit by a holder that missed another's write (two
  processes, one replica) is refused with :class:`StaleDraft`, since its
  offsets were counted in text that has since changed;
- :func:`drop` deletes the row, and a replica made later gets a new id.

An update that arrives before the changes it builds on is left out of Loro's
snapshot, so it is kept in ``plan_draft_pending`` and imported again on every
load until what it waits on arrives.
"""

from __future__ import annotations

import difflib
import itertools
import re
import secrets
import sqlite3
import time
from typing import Callable

from brindle.crdt import SharedDoc
from brindle.db import DB

# Peer ids stay within a JS number's exact range, for the app's loro-crdt.
_PEER_ID_SPACE = 2**53 - 1


class DraftGone(LookupError):
    """The replica was dropped (or replaced) while this holder had it open."""


class StaleDraft(RuntimeError):
    """Another holder changed the draft since this one last read it, so the
    edit's offsets no longer name the text they were counted in. Nothing was
    written; the holder now has the latest text: read it and edit again."""


def _fresh_peer(doc: SharedDoc) -> int:
    while True:
        peer = secrets.randbelow(_PEER_ID_SPACE) + 1
        if doc.written_by(peer) == 0:
            return peer


def _read(c: sqlite3.Connection, doc_id: str) -> tuple[sqlite3.Row, SharedDoc, list[bytes]] | None:
    """The stored row, its document with the waiting updates imported, and those updates."""
    row = c.execute("SELECT peer_id, state, seq, created_at FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone()
    if row is None:
        return None
    doc = SharedDoc.load(row["state"])
    pending = [r["data"] for r in c.execute(
        "SELECT data FROM plan_draft_pending WHERE doc_id=? ORDER BY n", (doc_id,))]
    for data in pending:
        doc.import_updates(data)
    doc.set_peer_id(row["peer_id"])     # only now that its own earlier changes are in
    return row, doc, pending


class DraftReplica:
    """A shared doc's draft as this machine holds it. Edits take code-point
    offsets into the text as this holder last read it, and are stored before
    they return; each returns the Loro update it made, for the other replicas."""

    def __init__(self, db: DB, doc_id: str, peer_id: int, doc: SharedDoc, seq: int, made: float) -> None:
        self.db = db
        self.doc_id = doc_id
        self.peer_id = peer_id
        self._doc = doc
        self._seq = seq
        self._made = made       # which replica of the doc this is: :func:`rebuild` makes a new one

    def _same(self, row: sqlite3.Row) -> bool:
        return row["peer_id"] == self.peer_id and row["created_at"] == self._made

    @classmethod
    def open(cls, db: DB, doc_id: str, initial: bytes | None = None) -> DraftReplica:
        """The stored replica of ``doc_id``, or a new one starting from
        ``initial`` (a snapshot from the Library) under a fresh peer id."""
        while True:
            with db.snapshot() as c:
                got = _read(c, doc_id)
            if got is not None:
                row, doc, _ = got
                return cls(db, doc_id, row["peer_id"], doc, row["seq"], row["created_at"])
            doc = SharedDoc.load(initial) if initial else SharedDoc.new()
            peer = _fresh_peer(doc)
            doc.set_peer_id(peer)
            now = time.time()
            try:
                with db.tx() as c:
                    added = c.execute(
                        "INSERT OR IGNORE INTO plan_drafts (doc_id, peer_id, state, seq, created_at, updated_at)"
                        " VALUES (?, ?, ?, 0, ?, ?)", (doc_id, peer, doc.export_snapshot(), now, now)).rowcount
            except sqlite3.IntegrityError:
                continue    # another replica already has that peer id: pick again
            if added:
                return cls(db, doc_id, peer, doc, 0, now)
            # Another process made it first: use theirs.

    # -- reading --------------------------------------------------------

    def sections(self) -> dict[str, str]:
        return self._doc.sections()

    def text(self, section: str) -> str:
        return self._doc.text(section)

    def version(self) -> bytes:
        return self._doc.version()

    def export_updates(self, since: bytes | None = None) -> bytes:
        return self._doc.export_updates(since)

    def holds(self, version: bytes) -> bool:
        """Whether a replica at ``version`` already has every change here."""
        return self._doc.holds(version)

    def sections_at(self, version: bytes) -> dict[str, str]:
        return self._doc.sections_at(version)

    def export_bootstrap(self) -> list[bytes]:
        """The latest snapshot and every update still waiting on dependencies.

        A snapshot alone omits pending operations. Read both in one database
        snapshot so a joining client can finish them when dependencies arrive.
        """
        with self.db.snapshot() as c:
            got = _read(c, self.doc_id)
        if got is None or not self._same(got[0]):
            raise DraftGone(f"the draft replica of {self.doc_id} is no longer this one")
        row, doc, pending = got
        self._doc, self._seq = doc, row["seq"]
        return [doc.export_snapshot(), *pending]

    # -- writing --------------------------------------------------------

    def insert(self, section: str, offset: int, text: str) -> bytes:
        return self._write(lambda d: d.insert(section, offset, text))[0]

    def delete(self, section: str, offset: int, length: int) -> bytes:
        return self._write(lambda d: d.delete(section, offset, length))[0]

    def replace(self, section: str, offset: int, length: int, text: str) -> bytes:
        return self._write(lambda d: d.replace(section, offset, length, text))[0]

    def apply_remote(self, data: bytes) -> bool:
        """Merge another replica's updates; False while they wait on changes
        not yet received (they are kept, and merged once those arrive). Not
        positional, so another holder's writes since do not refuse it.

        If the replica was rebuilt since this holder read it (:func:`rebuild`),
        ``data`` was made against the old one and cannot merge into the new:
        what it changed in the text this holder read is re-typed onto the new
        replica instead (:func:`retype`), and this holder goes on with it."""
        return data not in self._write(lambda d: d.import_updates(data), incoming=data)[1]

    def _carry(self, data: bytes) -> Callable[[SharedDoc], None]:
        """``data``'s change to the text this holder last read, as an edit for another replica."""
        base = self._doc.sections()
        after = SharedDoc.load(self._doc.export_snapshot())
        after.import_updates(data)
        ours = after.sections()
        return lambda doc: retype(doc, base, ours)

    def _write(self, change: Callable[[SharedDoc], object],
               incoming: bytes | None = None) -> tuple[bytes, list[bytes]]:
        """Apply ``change`` to the stored draft; the update it made, and the
        updates still waiting on changes not yet received."""
        with self.db.tx() as c:
            got = _read(c, self.doc_id)
            if got is None or (not self._same(got[0]) and incoming is None):
                raise DraftGone(f"the draft replica of {self.doc_id} is no longer this one")
            if not self._same(got[0]):
                row, doc, pending = got
                before = doc.version()
                self._carry(incoming)(doc)
                seq = row["seq"] + 1
                c.execute("UPDATE plan_drafts SET state=?, seq=?, updated_at=? WHERE doc_id=?",
                          (doc.export_snapshot(), seq, time.time(), self.doc_id))
                self.peer_id, self._made, self._doc, self._seq = row["peer_id"], row["created_at"], doc, seq
                return doc.export_updates(before), pending
            row, doc, pending = got
            if incoming is None and row["seq"] != self._seq:
                self._doc, self._seq = doc, row["seq"]
                raise StaleDraft(f"the draft of {self.doc_id} changed since this holder read it")
            before = doc.version()
            change(doc)
            waiting = pending
            if incoming is not None:
                # Importing again is a no-op once merged, so what still says
                # "pending" is exactly what still waits.
                waiting = [d for d in [*pending, incoming] if not doc.import_updates(d)]
                c.execute("DELETE FROM plan_draft_pending WHERE doc_id=?", (self.doc_id,))
                c.executemany("INSERT INTO plan_draft_pending (doc_id, data) VALUES (?, ?)",
                              [(self.doc_id, d) for d in waiting])
            seq = row["seq"] + 1
            c.execute("UPDATE plan_drafts SET state=?, seq=?, updated_at=? WHERE doc_id=?",
                      (doc.export_snapshot(), seq, time.time(), self.doc_id))
        # Only once it is stored does this holder see it: a refused edit changes nothing.
        self._doc, self._seq = doc, seq
        return doc.export_updates(before), waiting


def rebuild(db: DB, doc_id: str,
            make: Callable[[sqlite3.Connection, SharedDoc | None], SharedDoc]) -> DraftReplica:
    """Replace this machine's replica of ``doc_id`` with ``make(c, current)``
    (current: the stored replica, its waiting updates in, or None), writing
    under the new document's peer id from now on: the relay's draft with the
    local text re-typed onto it (:mod:`brindle.pro.live_docs`). The new
    document's peer id must be one whose earlier changes it already holds.

    The read, ``make`` and the write are one write transaction, so nothing
    another holder commits can fall between what ``make`` read and what
    replaces it; ``make`` writes what goes with it through ``c``. The result
    is a new replica (a new ``created_at``): an old holder's positional edit
    gets :class:`DraftGone`, and its ``apply_remote`` is re-typed onto it."""
    with db.tx() as c:
        got = _read(c, doc_id)
        doc = make(c, None if got is None else got[1])
        now = time.time()
        if got is not None:
            # Strictly newer than the replica it replaces, however coarse the clock.
            now = max(now, got[0]["created_at"] + 1e-6)
        seq = 0 if got is None else got[0]["seq"] + 1
        c.execute("DELETE FROM plan_drafts WHERE doc_id=?", (doc_id,))      # its held-back updates go with it
        c.execute("INSERT INTO plan_drafts (doc_id, peer_id, state, seq, created_at, updated_at)"
                  " VALUES (?, ?, ?, ?, ?, ?)", (doc_id, doc.peer_id, doc.export_snapshot(), seq, now, now))
    return DraftReplica(db, doc_id, doc.peer_id, doc, seq, now)


def adopt(db: DB, doc_id: str, doc: SharedDoc) -> DraftReplica:
    """:func:`rebuild` with ``doc`` as it is."""
    return rebuild(db, doc_id, lambda c, current: doc)


_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


def hunks(base: str, ours: str) -> list[tuple[int, int, int, int]]:
    """What changed from ``base`` to ``ours``, word by word: (i1, i2, j1, j2)
    for each ``base[i1:i2]`` that became ``ours[j1:j2]``."""
    a, b = _TOKEN.findall(base), _TOKEN.findall(ours)
    at_a = [0, *itertools.accumulate(len(t) for t in a)]
    at_b = [0, *itertools.accumulate(len(t) for t in b)]
    return [(at_a[i1], at_a[i2], at_b[j1], at_b[j2])
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if tag != "equal"]


def mapping(base: str, other: str) -> tuple[Callable[[int], int], list[tuple[int, int]]]:
    """Where an offset into ``base`` is in ``other``, another descendant of it
    (an offset inside a stretch ``other`` changed lands just after that
    change), and the stretches of ``base`` that ``other`` changed."""
    ops = difflib.SequenceMatcher(None, base, other, autojunk=False).get_opcodes()

    def at(pos: int) -> int:
        for tag, i1, i2, j1, _ in ops:
            if tag == "equal" and i1 <= pos <= i2:
                return j1 + pos - i1
        return next((j2 for _, i1, i2, _, j2 in ops if i1 <= pos <= i2), len(other))

    return at, [(i1, i2) for tag, i1, i2, _, _ in ops if tag != "equal"]


def retype(doc: SharedDoc, base: dict[str, str], ours: dict[str, str]) -> None:
    """Make on ``doc`` what changed from ``base`` to ``ours`` (both texts by
    section) somewhere else. Where ``doc``'s text also changed a stretch that
    ours changed, ours goes in beside it and deletes nothing of it."""
    for section in sorted(set(base) | set(ours)):
        was, mine = base.get(section, ""), ours.get(section, "")
        if was == mine:
            continue
        at, changed = mapping(was, doc.text(section))
        edits = []
        for i1, i2, j1, j2 in hunks(was, mine):
            start = at(i1)
            touched = any(b1 < i2 and i1 < b2 for b1, b2 in changed)
            edits.append((start, 0 if touched else max(at(i2) - start, 0), mine[j1:j2]))
        for start, length, text in reversed(edits):
            doc.replace(section, start, length, text)


def drop(db: DB, doc_id: str) -> bool:
    """Delete this machine's replica of ``doc_id`` (access was cut, or the
    doc stopped being shared), with any updates it held back. A replica made
    later writes under a new id."""
    with db.tx() as c:
        # Its link to the relay goes with it: its unsent edits (the outbox) and the numbers it held.
        c.execute("DELETE FROM live_docs WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM live_doc_peers WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM live_doc_offline WHERE doc_id=?", (doc_id,))
        return c.execute("DELETE FROM plan_drafts WHERE doc_id=?", (doc_id,)).rowcount > 0
