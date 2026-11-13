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
    row = c.execute("SELECT peer_id, state, seq FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone()
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

    def __init__(self, db: DB, doc_id: str, peer_id: int, doc: SharedDoc, seq: int) -> None:
        self.db = db
        self.doc_id = doc_id
        self.peer_id = peer_id
        self._doc = doc
        self._seq = seq

    @classmethod
    def open(cls, db: DB, doc_id: str, initial: bytes | None = None) -> DraftReplica:
        """The stored replica of ``doc_id``, or a new one starting from
        ``initial`` (a snapshot from the Library) under a fresh peer id."""
        while True:
            with db.snapshot() as c:
                got = _read(c, doc_id)
            if got is not None:
                row, doc, _ = got
                return cls(db, doc_id, row["peer_id"], doc, row["seq"])
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
                return cls(db, doc_id, peer, doc, 0)
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

    def lacks(self, since: bytes) -> bool:
        """Whether a replica at version ``since`` lacks a change this one holds."""
        return self._doc.lacks(since)

    def cursor(self, section: str, offset: int) -> bytes:
        return self._doc.cursor(section, offset)

    def resolve(self, cursor: bytes) -> int:
        return self._doc.resolve(cursor)

    # -- writing --------------------------------------------------------

    def insert(self, section: str, offset: int, text: str) -> bytes:
        return self._write(lambda d: d.insert(section, offset, text))[0]

    def delete(self, section: str, offset: int, length: int) -> bytes:
        return self._write(lambda d: d.delete(section, offset, length))[0]

    def replace(self, section: str, offset: int, length: int, text: str,
                also: Callable[[sqlite3.Connection], None] | None = None) -> bytes:
        """``also`` runs in the same transaction (marking the suggestion this
        accepts, say): if it raises, neither it nor the edit is stored."""
        return self._write(lambda d: d.replace(section, offset, length, text), also=also)[0]

    def apply_remote(self, data: bytes) -> bool:
        """Merge another replica's updates; False while they wait on changes
        not yet received (they are kept, and merged once those arrive). Not
        positional, so another holder's writes since do not refuse it."""
        return data not in self._write(lambda d: d.import_updates(data), incoming=data)[1]

    def _write(self, change: Callable[[SharedDoc], object], incoming: bytes | None = None,
               also: Callable[[sqlite3.Connection], None] | None = None) -> tuple[bytes, list[bytes]]:
        """Apply ``change`` to the stored draft; the update it made, and the
        updates still waiting on changes not yet received."""
        with self.db.tx() as c:
            got = _read(c, self.doc_id)
            if got is None or got[0]["peer_id"] != self.peer_id:
                raise DraftGone(f"the draft replica of {self.doc_id} is no longer this one")
            row, doc, pending = got
            if incoming is None and row["seq"] != self._seq:
                self._doc, self._seq = doc, row["seq"]
                raise StaleDraft(f"the draft of {self.doc_id} changed since this holder read it")
            before = doc.version()
            change(doc)
            if also is not None:
                also(c)
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


def drop(db: DB, doc_id: str) -> bool:
    """Delete this machine's replica of ``doc_id`` (access was cut, or the
    doc stopped being shared), with any updates it held back. A replica made
    later writes under a new id."""
    with db.tx() as c:
        return c.execute("DELETE FROM plan_drafts WHERE doc_id=?", (doc_id,)).rowcount > 0
