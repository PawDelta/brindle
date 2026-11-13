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
- every write happens inside a transaction against the latest stored state:
  a holder that missed another's write (two processes, one replica) reloads
  first instead of writing over it;
- :func:`drop` deletes the row, and a replica made later gets a new id.
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


def _fresh_peer(doc: SharedDoc) -> int:
    while True:
        peer = secrets.randbelow(_PEER_ID_SPACE) + 1
        if doc.written_by(peer) == 0:
            return peer


class DraftReplica:
    """A shared doc's draft as this machine holds it. Edits take code-point
    offsets and are stored before they return; each returns the Loro update
    it made, for sending to the other replicas."""

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
            row = db.conn.execute("SELECT peer_id, state, seq FROM plan_drafts WHERE doc_id=?",
                                  (doc_id,)).fetchone()
            if row is not None:
                return cls(db, doc_id, row["peer_id"], cls._load(row["state"], row["peer_id"]), row["seq"])
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

    @staticmethod
    def _load(state: bytes, peer: int) -> SharedDoc:
        doc = SharedDoc.load(state)
        doc.set_peer_id(peer)     # only now that its own earlier changes are in
        return doc

    # -- reading --------------------------------------------------------

    def sections(self) -> dict[str, str]:
        return self._doc.sections()

    def text(self, section: str) -> str:
        return self._doc.text(section)

    def version(self) -> bytes:
        return self._doc.version()

    def export_updates(self, since: bytes | None = None) -> bytes:
        return self._doc.export_updates(since)

    # -- writing --------------------------------------------------------

    def insert(self, section: str, offset: int, text: str) -> bytes:
        return self._write(lambda d: d.insert(section, offset, text))

    def delete(self, section: str, offset: int, length: int) -> bytes:
        return self._write(lambda d: d.delete(section, offset, length))

    def replace(self, section: str, offset: int, length: int, text: str) -> bytes:
        return self._write(lambda d: d.replace(section, offset, length, text))

    def apply_remote(self, data: bytes) -> bool:
        """Merge another replica's updates; False if some wait on others."""
        done: list[bool] = []
        self._write(lambda d: done.append(d.import_updates(data)))
        return done[0]

    def _write(self, change: Callable[[SharedDoc], object]) -> bytes:
        with self.db.tx() as c:
            row = c.execute("SELECT peer_id, state, seq FROM plan_drafts WHERE doc_id=?",
                            (self.doc_id,)).fetchone()
            if row is None or row["peer_id"] != self.peer_id:
                raise DraftGone(f"the draft replica of {self.doc_id} is no longer this one")
            # Always from the stored state: another holder may have written
            # under this peer id since this one last read (seq moved on).
            doc = self._load(row["state"], self.peer_id)
            before = doc.version()
            change(doc)
            seq = row["seq"] + 1
            c.execute("UPDATE plan_drafts SET state=?, seq=?, updated_at=? WHERE doc_id=?",
                      (doc.export_snapshot(), seq, time.time(), self.doc_id))
        # Only once it is stored does this holder see it: a refused edit changes nothing.
        self._doc, self._seq = doc, seq
        return doc.export_updates(before)


def drop(db: DB, doc_id: str) -> bool:
    """Delete this machine's replica of ``doc_id`` (access was cut, or the
    doc stopped being shared). A replica made later writes under a new id."""
    with db.tx() as c:
        return c.execute("DELETE FROM plan_drafts WHERE doc_id=?", (doc_id,)).rowcount > 0
