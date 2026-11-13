"""A narrow adapter over Loro for shared design docs being co-edited live.

A shared design is one Loro document holding one root ``LoroText`` per
section, named by the section's id. Everything brindle needs from Loro goes
through here, so the rest of the engine never touches ``loro`` itself:

- offsets are Unicode code points, as Python's ``str`` counts them; the app's
  editor counts UTF-16 units, and :func:`utf16_offset` and
  :func:`code_point_offset` convert between the two;
- a cursor (:meth:`SharedDoc.cursor`) is a position that travels with the
  text around it, encoded as bytes any Loro peer (Rust, JS, Python) reads;
- state moves between replicas as Loro's own binary snapshots and updates,
  which the JS ``loro-crdt`` package imports unchanged.

Loro's Python binding raises a bare ``BaseException`` for every refusal
(bad offsets, undecodable bytes); this module checks what it can first and
turns the rest into :class:`CrdtError`.
"""

from __future__ import annotations

from collections.abc import Mapping

import loro

# Loro reserves the largest u64 as a peer id of its own.
PEER_ID_LIMIT = 2**64 - 1


class CrdtError(ValueError):
    """An edit, cursor or update Loro refused."""


class Presence:
    """A section's ephemeral caret/name state. Never part of a draft snapshot."""

    def __init__(self) -> None:
        self._store = loro.EphemeralStore(30_000)

    def apply(self, data: bytes) -> None:
        try:
            self._store.apply(data)
        except BaseException as e:
            if _refused(e):
                raise CrdtError("not a Loro awareness update") from None
            raise

    def encode(self) -> bytes:
        self._store.remove_outdated()
        return self._store.encode_all()


def _vv(version: bytes | None) -> loro.VersionVector:
    try:
        return loro.VersionVector() if not version else loro.VersionVector.decode(version)
    except BaseException as e:
        if _refused(e):
            raise CrdtError(f"not a version: {e}") from None
        raise


def merge_versions(a: bytes | None, b: bytes | None) -> bytes:
    """A version holding every change either one holds."""
    vv = _vv(a)
    vv.merge(_vv(b))
    return vv.encode()


def update_version(data: bytes) -> bytes:
    """The version a replica reaches once it holds everything in ``data``
    (updates or a snapshot), read from the bytes' header alone."""
    try:
        return loro.LoroDoc.decode_import_blob_meta(data, True).partial_end_vv.encode()
    except BaseException as e:
        if _refused(e):
            raise CrdtError(f"not a Loro update: {e}") from None
        raise


def _refused(e: BaseException) -> bool:
    # The binding's errors are exactly BaseException: never swallow
    # KeyboardInterrupt, SystemExit or anything more specific.
    return type(e) is BaseException


def utf16_offset(text: str, offset: int) -> int:
    """The UTF-16 offset of code-point ``offset`` in ``text``."""
    if not 0 <= offset <= len(text):
        raise CrdtError(f"offset {offset} is outside a text of {len(text)} characters")
    return offset + sum(1 for ch in text[:offset] if ord(ch) > 0xFFFF)


def code_point_offset(text: str, utf16: int) -> int:
    """The code-point offset of UTF-16 offset ``utf16`` in ``text``. An offset
    between the two halves of a surrogate pair names no position and is refused."""
    units = 0
    for i, ch in enumerate(text):
        if units == utf16:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > utf16:
            raise CrdtError(f"UTF-16 offset {utf16} splits a surrogate pair")
    if units == utf16:
        return len(text)
    raise CrdtError(f"UTF-16 offset {utf16} is outside a text of {units} units")


def _version(since: bytes | None) -> loro.VersionVector:
    try:
        return loro.VersionVector() if since is None else loro.VersionVector.decode(since)
    except BaseException as e:
        if _refused(e):
            raise CrdtError(f"not a version: {e}") from None
        raise


def _section_name(section: str) -> str:
    if not isinstance(section, str) or not section or "/" in section or "\0" in section:
        raise CrdtError(f"not a section id: {section!r}")
    return section


class SharedDoc:
    """One shared design: a Loro document with a text per section."""

    def __init__(self, doc: loro.LoroDoc) -> None:
        self._doc = doc

    @classmethod
    def new(cls, sections: Mapping[str, str] | None = None) -> SharedDoc:
        """A new document holding ``sections`` (section id -> text)."""
        shared = cls(loro.LoroDoc())
        for section, text in (sections or {}).items():
            shared.insert(section, 0, text)
        return shared

    @classmethod
    def load(cls, state: bytes) -> SharedDoc:
        """A document from a snapshot (or updates) another replica exported.
        It writes under a fresh random peer id until :meth:`set_peer_id`."""
        shared = cls(loro.LoroDoc())
        shared.import_updates(state)
        return shared

    # -- identity -------------------------------------------------------

    @property
    def peer_id(self) -> int:
        return self._doc.peer_id

    def set_peer_id(self, peer: int) -> None:
        """Write from now on as ``peer``. Set it only after the state written
        under it is loaded: a peer id writing without its own earlier changes
        would reuse their numbers for different text."""
        if not isinstance(peer, int) or isinstance(peer, bool) or not 0 <= peer < PEER_ID_LIMIT:
            raise CrdtError(f"not a peer id: {peer!r}")
        self._doc.commit()
        self._doc.peer_id = peer

    def written_by(self, peer: int) -> int:
        """How many changes (Loro ops) by ``peer`` this replica holds."""
        last = self._doc.oplog_vv.get_last(peer)
        return 0 if last is None else last + 1

    # -- reading --------------------------------------------------------

    def sections(self) -> dict[str, str]:
        """Every section's text, by section id."""
        return {k: v for k, v in self._doc.get_deep_value().items() if isinstance(v, str)}

    def text(self, section: str) -> str:
        return self._doc.get_text(_section_name(section)).to_string()

    def editors(self, section: str) -> list[int | None]:
        """For each character of ``section`` (code points), the peer id that
        typed it (None where Loro can't say)."""
        t = self._doc.get_text(_section_name(section))
        return [t.get_editor_at_unicode_pos(i) for i in range(t.len_unicode)]

    # -- local edits (code-point offsets) -------------------------------

    def insert(self, section: str, offset: int, text: str) -> None:
        t = self._doc.get_text(_section_name(section))
        self._check(t, offset, 0)
        if text:
            t.insert(offset, text)
            self._doc.commit()

    def delete(self, section: str, offset: int, length: int) -> None:
        t = self._doc.get_text(_section_name(section))
        self._check(t, offset, length)
        if length:
            t.delete(offset, length)
            self._doc.commit()

    def replace(self, section: str, offset: int, length: int, text: str) -> None:
        t = self._doc.get_text(_section_name(section))
        self._check(t, offset, length)
        if length or text:
            t.splice(offset, length, text)
            self._doc.commit()

    @staticmethod
    def _check(t: loro.LoroText, offset: int, length: int) -> None:
        size = t.len_unicode
        if not (isinstance(offset, int) and isinstance(length, int)
                and offset >= 0 and length >= 0 and offset + length <= size):
            raise CrdtError(f"range {offset}+{length} is outside a text of {size} characters")

    # -- cursors --------------------------------------------------------

    def cursor(self, section: str, offset: int) -> bytes:
        """A position that travels with the text: it stays before the
        character now at ``offset`` (or at the end, given the text's length)."""
        t = self._doc.get_text(_section_name(section))
        self._check(t, offset, 0)
        c = t.get_cursor(offset, loro.Side.Middle)
        if c is None:
            raise CrdtError(f"no cursor at {offset} in {section!r}")
        return c.encode()

    def resolve(self, cursor: bytes) -> int:
        """Where ``cursor`` stands now, as a code-point offset."""
        try:
            return self._doc.get_cursor_pos(loro.Cursor.decode(cursor)).current.pos
        except BaseException as e:
            if _refused(e):
                raise CrdtError(f"cursor not resolvable here: {e}") from None
            raise

    # -- replication ----------------------------------------------------

    def version(self) -> bytes:
        """Everything this replica has seen, for :meth:`export_updates` elsewhere."""
        return self._doc.oplog_vv.encode()

    def export_snapshot(self) -> bytes:
        return self._doc.export(loro.ExportMode.Snapshot())

    def export_updates(self, since: bytes | None = None) -> bytes:
        """The changes a replica at version ``since`` lacks (all of them if None)."""
        return self._doc.export(loro.ExportMode.Updates(_version(since)))

    def lacks(self, since: bytes) -> bool:
        """Whether a replica at version ``since`` lacks any change this one holds."""
        return not _version(since).includes_vv(self._doc.oplog_vv)

    def holds(self, version: bytes) -> bool:
        """Whether every change in ``version`` is already here: nothing past it
        would be exported to a replica at ``version``."""
        return _vv(version).includes_vv(self._doc.oplog_vv)

    def sections_at(self, version: bytes) -> dict[str, str]:
        """Every section's text as it stood once the changes in ``version``
        (those of them this replica holds) were made."""
        vv = _vv(version).intersection(self._doc.oplog_vv)
        then = self._doc.fork_at(self._doc.vv_to_frontiers(vv))
        return {k: v for k, v in then.get_deep_value().items() if isinstance(v, str)}

    def import_updates(self, data: bytes) -> bool:
        """Merge a snapshot or updates from another replica. False when some
        changes wait on others not yet imported (Loro keeps them pending)."""
        try:
            status = self._doc.import_(data)
        except BaseException as e:
            if _refused(e):
                raise CrdtError(f"not a Loro update: {e}") from None
            raise
        return status.pending is None
