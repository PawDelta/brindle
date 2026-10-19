"""Knowledge sources and their indexed documents: add, sync and remove.

This module alone writes ``kb_sources``, ``kb_docs``, ``kb_chunks``, ``kb_fts``,
``kb_links`` and ``kb_embeddings``. An indexed source's documents are split by
heading into chunks, scanned (secrets redacted, narrow injection signatures
quarantined) and put in the ``kb_fts`` full-text index; a quarantined chunk is
kept for review but never indexed. A federated source stores nothing. Adding
and removing a source is recorded in ``kb_events`` (:func:`knowledge.append`).

:func:`sync` commits per batch of documents, so a large source never holds the
database lock for long: run it in its own process, never inside an MCP tool call.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from brindle import knowledge, secrets
from brindle.db import DB

SOURCE_ADD = "source_add"
SOURCE_REMOVE = "source_remove"
FOLDER = "folder"
DOC_SUFFIXES = (".md", ".markdown", ".txt")
MAX_DOC_BYTES = 5_000_000
# Documents per transaction.
BATCH = 50
# Chunk sizes in tokens, estimated at four characters a token: a heading's
# section, its subsections included, is one chunk up to MAX_TOKENS; a longer
# one gives its own text and each subsection a chunk of their own, and text
# still longer than that is cut at paragraphs into pieces of about TARGET_TOKENS.
TARGET_TOKENS = 500
MAX_TOKENS = 1000
REDACTED = "[redacted]"

# Secret-shaped values as documents hold them: provider key and token formats,
# private key blocks, a secret-named setting given a value with a digit in it,
# and a value given to a variable brindle keeps from every agent (secrets.py).
_SECRET_VALUE = re.compile(
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)"
    r"|(?<![\w-])(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})", re.DOTALL)
_VALUE = r"[^\s'\"`<>]{8,}"
_SECRET_SETTING = re.compile(
    r"(?i)(\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key)[\w.-]*\s*[:=]\s*[\"']?)"
    rf"((?=[^\s'\"`<>]*\d){_VALUE})")
_SECRET_VARIABLE = re.compile(
    r"(\b(?:" + "|".join(sorted(map(re.escape, {*secrets.SECRET_ENV, *secrets.PERSONAL_KEYS})))
    + rf")\s*[:=]\s*[\"']?)({_VALUE})")

# Narrow injection signatures, never ordinary imperatives (a runbook says "run
# this"). Hidden text: zero-width, bidirectional-override and Unicode tag
# characters, and HTML styled or marked not to render.
_HIDDEN = re.compile(
    "[​-‍⁠-⁤‪-‮⁦-⁩﻿\U000e0000-\U000e007f]"
    r"|<[a-z][^>]*(?:\shidden[\s>=/]|style\s*=\s*[\"'][^\"']*(?:display\s*:\s*none"
    r"|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])))", re.IGNORECASE)
_AI = (r"(?:AI|LLMs?|(?:AI\s+|large\s+)?language\s+models?|AI\s+(?:assistants?|agents?)|chatbots?"
       r"|Claude|ChatGPT|GPT(?:-\d+)?|Copilot|Gemini)")
# Text addressed to an AI, and chat-template control tokens.
_ADDRESSED = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:(?:all|any|the|your)\s+)*(?:previous|prior|above"
    r"|earlier|preceding|original|system)\s+(?:instructions?|directions?|prompts?|rules|guidelines)"
    rf"|\bif\s+you\s+are\s+an?\s+{_AI}\b|\b(?:dear|attention|hey|hello|note\s+to(?:\s+the)?)\s+{_AI}\s*[,:!]"
    rf"|\b{_AI}\s+(?:reading|processing|summari[sz]ing|parsing)\s+this\b"
    r"|<\|im_start\|>|<\|(?:system|user|assistant)\|>|\[/?INST\]|<<SYS>>", re.IGNORECASE)
# An HTML comment (which a rendered page hides; one never closed runs to the
# end) that gives an instruction: a line of it opens with a verb an agent could
# act on, or it tells "you" what to do.
_COMMENT = re.compile(r"<!--(.*?)(?:-->|\Z)", re.DOTALL)
_INSTRUCTION = re.compile(
    r"(?m)^\W*(?:ignore|disregard|forget|run|execute|send|email|post|upload|delete|remove|reveal"
    r"|print|output|include|insert|tell|say|respond|reply|answer|write|call|fetch|open|visit"
    r"|approve|merge|push|commit|install|add|use|always|never|do\s+not|don't)\b"
    r"|\byou\s+(?:must|should|need\s+to|have\s+to|are\s+to|will|shall)\b", re.IGNORECASE)

_HEADING = re.compile(r"(#{1,6})\s+(.*\S)\s*$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


@dataclass
class Chunk:
    heading_path: str
    text: str
    lines: range   # the document lines the chunk was cut from
    heading_lines: tuple[int, ...] = ()   # the document lines of its heading path's headings


@dataclass
class _Section:
    level: int
    title: str
    start: int   # the document line of its first line
    lines: list[str]
    children: list[_Section] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join([*self.lines, *(c.text() for c in self.children)])

    def size(self) -> int:
        return len(self.lines) + sum(c.size() for c in self.children)


def _tokens(text: str) -> int:
    return len(text) // 4


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fence(line: str, fenced: bool) -> bool:
    """Whether a code fence is open after ``line``."""
    return not fenced if line.lstrip().startswith(("```", "~~~")) else fenced


def chunk_markdown(text: str) -> list[Chunk]:
    """Split a Markdown document by heading (``#`` lines in code fences are not
    headings). Each chunk carries its heading path, ``"Title > Section"``; text
    before the first heading has the empty path."""
    root = _Section(0, "", 0, [])
    stack = [root]
    fenced = False
    for n, line in enumerate(_normalized(text).split("\n")):
        heading = None if fenced else _HEADING.match(line)
        fenced = _fence(line, fenced)
        if heading:
            level = len(heading.group(1))
            while stack[-1].level >= level:
                stack.pop()
            section = _Section(level, heading.group(2), n, [line])
            stack[-1].children.append(section)
            stack.append(section)
        else:
            stack[-1].lines.append(line)
    chunks: list[Chunk] = []
    _emit(root, [], chunks)
    return chunks


def _emit(section: _Section, path: list[tuple[str, int]], out: list[Chunk]) -> None:
    """Chunk ``section``; ``path`` is its ancestors' (heading, document line)."""
    path = [*path, (section.title, section.start)] if section.title else path
    own = "".join(section.lines[1 if section.title else 0:]).strip()
    if not own and len(section.children) == 1:   # a title over one section: name the chunk by both
        _emit(section.children[0], path, out)
        return
    heading_path = " > ".join(title for title, _ in path)
    heading_lines = tuple(n for _, n in path)
    whole = section.text().strip("\n")
    if _tokens(whole) <= MAX_TOKENS:
        if whole.strip():
            out.append(Chunk(heading_path, whole, range(section.start, section.start + section.size()),
                             heading_lines))
        return
    if own:
        out += [Chunk(heading_path, piece, lines, heading_lines)
                for piece, lines in _pieces(section.lines, section.start)]
    for child in section.children:
        _emit(child, path, out)


def _pieces(lines: list[str], start: int) -> list[tuple[str, range]]:
    """``lines`` (from document line ``start``) cut at blank lines outside code
    fences into pieces of about TARGET_TOKENS, each with its document lines."""
    pieces: list[tuple[str, range]] = []
    first = 0
    fenced = False
    for i, line in enumerate(lines):
        fenced = _fence(line, fenced)
        if not fenced and not line.strip() and _tokens("\n".join(lines[first:i + 1])) >= TARGET_TOKENS:
            pieces.append(("\n".join(lines[first:i + 1]).strip("\n"), range(start + first, start + i + 1)))
            first = i + 1
    if "".join(lines[first:]).strip():
        pieces.append(("\n".join(lines[first:]).strip("\n"), range(start + first, start + len(lines))))
    return pieces


def redact(text: str) -> str:
    """``text`` with secret-shaped values replaced by REDACTED."""
    text = _SECRET_VALUE.sub(REDACTED, text)
    for setting in (_SECRET_SETTING, _SECRET_VARIABLE):
        text = setting.sub(lambda m: m.group(1) + REDACTED, text)
    return text


def _signatures(text: str):
    """Each injection signature in ``text``: (reason, start, end) offsets."""
    for m in _HIDDEN.finditer(text):
        yield "hidden text", m.start(), m.end()
    for m in _ADDRESSED.finditer(text):
        yield "addressed to an AI", m.start(), m.end()
    for m in _COMMENT.finditer(text):
        if _INSTRUCTION.search(m.group(1)):
            yield "instruction in an HTML comment", m.start(), m.end()


def quarantine_reason(text: str) -> str | None:
    """Why ``text`` must not reach an agent, or None: hidden text, text
    addressed to an AI, or an instruction in an HTML comment."""
    return next((reason for reason, _, _ in _signatures(text)), None)


def scan_document(text: str) -> list[tuple[Chunk, str | None]]:
    """``text``'s chunks, each with why it is quarantined or None. The
    signatures are found in the whole document, not chunk by chunk, so a chunk
    boundary cutting through one (an HTML comment spanning headings, say) never
    hides it: every chunk cut from a signature's lines, or whose heading path
    takes a heading from them (a ``#`` line inside a hidden comment), is
    quarantined."""
    text = _normalized(text)
    starts = [0, *(m.end() for m in re.finditer("\n", text))]
    flagged: dict[int, str] = {}
    for reason, start, end in _signatures(text):
        for n in range(bisect.bisect_right(starts, start) - 1, bisect.bisect_right(starts, max(start, end - 1))):
            flagged.setdefault(n, reason)
    # The joined heading path is read on its own too, so this whole-document
    # pass only ever adds quarantines.
    return [(chunk, next((flagged[n] for n in (*chunk.heading_lines, *chunk.lines) if n in flagged), None)
             or quarantine_reason(chunk.heading_path)) for chunk in chunk_markdown(text)]


def chunk_id(source_id: str, external_id: str, heading_path: str, repeat: int = 0) -> str:
    """A chunk's stable id: a hash of its source, document and heading path
    (and, for the ``repeat``-th later chunk with that path, the count)."""
    key = f"{source_id}\n{external_id}\n{heading_path}" + (f"\n{repeat}" if repeat else "")
    return hashlib.sha256(key.encode()).hexdigest()


def add_source(db: DB, repo_key: str, kind: str, scope: str, *, mode: str = "indexed",
               visibility: str = "repo", credential_name: str | None = None,
               retention_days: int | None = None, actor: str | None = None,
               session: str | None = None) -> str:
    """Add a source (a folder's scope is its absolute path) and log it; returns
    its id, the existing one when it was already added."""
    if kind == FOLDER:
        scope = str(Path(scope).expanduser().resolve())
    row = db.conn.execute("SELECT id FROM kb_sources WHERE repo_key = ? AND kind = ? AND scope = ?",
                          (repo_key, kind, scope)).fetchone()
    if row:
        return row["id"]
    source_id = uuid.uuid4().hex
    with db.tx() as c:
        c.execute("INSERT INTO kb_sources (id, repo_key, kind, scope, mode, visibility, credential_name,"
                  " retention_days, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (source_id, repo_key, kind, scope, mode, visibility, credential_name,
                   retention_days, time.time()))
    knowledge.append(db, repo_key, SOURCE_ADD,
                     {"source": source_id, "kind": kind, "scope": scope, "mode": mode,
                      "visibility": visibility}, actor=actor, session=session)
    return source_id


def remove_source(db: DB, source_id: str, *, actor: str | None = None,
                  session: str | None = None) -> None:
    """Delete a source with its documents, chunks, index rows, links and
    embeddings in one transaction, and log the removal."""
    with db.tx() as c:
        src = c.execute("SELECT * FROM kb_sources WHERE id = ?", (source_id,)).fetchone()
        if src is None:
            raise KeyError(source_id)
        c.execute("DELETE FROM kb_fts WHERE rowid IN (SELECT kb_chunks.rowid FROM kb_chunks"
                  " JOIN kb_docs ON kb_docs.id = kb_chunks.doc_id WHERE kb_docs.source_id = ?)",
                  (source_id,))
        c.execute("DELETE FROM kb_sources WHERE id = ?", (source_id,))   # cascades to the rest
    knowledge.append(db, src["repo_key"], SOURCE_REMOVE,
                     {"source": source_id, "kind": src["kind"], "scope": src["scope"]},
                     actor=actor, session=session)


@dataclass
class _Doc:
    external_id: str
    url: str
    title: str
    updated_at: float
    content_hash: str
    text: str


def _folder_paths(root: Path) -> list[tuple[str, Path]]:
    """The documents under ``root`` as (external id, path), leaving out
    anything in a dot-directory or dot-file, symlinks, anything that resolves
    outside ``root`` and files over MAX_DOC_BYTES."""
    root = root.resolve()
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if (path.suffix.lower() in DOC_SUFFIXES and not any(p.startswith(".") for p in rel.parts)
                and not path.is_symlink() and path.resolve().is_relative_to(root) and path.is_file()
                and path.stat().st_size <= MAX_DOC_BYTES):
            out.append((rel.as_posix(), path))
    return out


def _read(external_id: str, path: Path) -> _Doc:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    title = next((m.group(2) for m in map(_HEADING.match, text.splitlines()) if m and len(m.group(1)) == 1),
                 path.stem)
    title = redact(title)
    return _Doc(external_id, path.resolve().as_uri(), title, path.stat().st_mtime,
                hashlib.sha256(raw).hexdigest(), text)


def _scanned(source_id: str, doc: _Doc) -> list[tuple[str, str, str, str | None]]:
    """``doc``'s chunks as (id, redacted heading path, redacted text, quarantine
    reason)."""
    seen: dict[str, int] = {}
    rows = []
    for chunk, reason in scan_document(doc.text):
        repeat = seen.get(chunk.heading_path, 0)
        seen[chunk.heading_path] = repeat + 1
        rows.append((chunk_id(source_id, doc.external_id, chunk.heading_path, repeat),
                     redact(chunk.heading_path), redact(chunk.text), reason))
    return rows


def _drop_chunks(c, doc_id: int) -> None:
    c.execute("DELETE FROM kb_fts WHERE rowid IN (SELECT rowid FROM kb_chunks WHERE doc_id = ?)",
              (doc_id,))
    c.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))


def graph_names(graph: Path | None) -> dict[str, list[str]]:
    """The graphify graph's node ids by name (its label without call
    parentheses or a leading dot); empty without a readable graph."""
    if graph is None:
        return {}
    try:
        nodes = json.loads(Path(graph).read_text()).get("nodes", [])
    except (OSError, ValueError, AttributeError):
        return {}
    names: dict[str, list[str]] = {}
    for node in nodes:
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        name = str(node.get("label") or node["id"]).removesuffix("()").lstrip(".")
        names.setdefault(name, []).append(str(node["id"]))
    return names


def code_links(text: str, names: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(symbol, node id) for each identifier in ``text``'s code spans that names
    a graph node."""
    links: dict[tuple[str, str], None] = {}
    for span in _CODE_SPAN.findall(text):
        for symbol in _IDENTIFIER.findall(span):
            for node_id in names.get(symbol, ()):
                links[symbol, node_id] = None
    return list(links)


def _index(c, source_id: str, doc: _Doc, chunks, names: dict[str, list[str]]) -> None:
    doc_id = c.execute(
        "INSERT INTO kb_docs (source_id, external_id, url, title, updated_at, content_hash, tombstoned)"
        " VALUES (?, ?, ?, ?, ?, ?, 0) ON CONFLICT (source_id, external_id) DO UPDATE SET"
        " url = excluded.url, title = excluded.title, updated_at = excluded.updated_at,"
        " content_hash = excluded.content_hash, tombstoned = 0 RETURNING id",
        (source_id, doc.external_id, doc.url, doc.title, doc.updated_at, doc.content_hash)).fetchone()[0]
    _drop_chunks(c, doc_id)
    for cid, heading_path, text, quarantined in chunks:
        rowid = c.execute("INSERT INTO kb_chunks (id, doc_id, heading_path, text, quarantined)"
                          " VALUES (?, ?, ?, ?, ?)", (cid, doc_id, heading_path, text, quarantined)).lastrowid
        if quarantined is None:
            c.execute("INSERT INTO kb_fts (rowid, text, heading_path, chunk_id) VALUES (?, ?, ?, ?)",
                      (rowid, text, heading_path, cid))
            for symbol, node_id in code_links(text, names):
                c.execute("INSERT OR IGNORE INTO kb_links (chunk_id, symbol, node_id) VALUES (?, ?, ?)",
                          (cid, symbol, node_id))


def sources(db: DB, repo_key: str) -> list[dict]:
    """``repo_key``'s sources, oldest first, each with its live document count."""
    return [dict(r) for r in db.conn.execute(
        "SELECT s.*, (SELECT count(*) FROM kb_docs d WHERE d.source_id = s.id AND NOT d.tombstoned)"
        " AS docs FROM kb_sources s WHERE s.repo_key = ? ORDER BY s.added_at", (repo_key,))]


def find_source(db: DB, repo_key: str, ref: str) -> str:
    """The id of ``repo_key``'s source named by an id (or a unique prefix of
    one) or a folder path; KeyError when none or several match."""
    scope = str(Path(ref).expanduser().resolve())
    found = [s["id"] for s in sources(db, repo_key) if s["id"].startswith(ref) or s["scope"] == scope]
    if len(found) != 1:
        raise KeyError(ref)
    return found[0]


def search(db: DB, repo_key: str, query: str, sources: list[str] | None = None,
           k: int = 8) -> list[dict]:
    """The best ``k`` chunks for ``query`` by full-text rank, among the
    non-quarantined chunks of ``repo_key``'s sources (only ``sources``, source
    ids, when given). Each hit has its chunk id, heading path, text, source link
    and updated date."""
    terms = re.findall(r"\w+", query)
    if not terms or k < 1 or sources == []:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    sql = ("SELECT c.id AS chunk_id, c.heading_path, c.text, d.url, d.title, d.updated_at"
           " FROM kb_fts JOIN kb_chunks c ON c.rowid = kb_fts.rowid JOIN kb_docs d ON d.id = c.doc_id"
           " JOIN kb_sources s ON s.id = d.source_id"
           " WHERE kb_fts MATCH ? AND s.repo_key = ? AND c.quarantined IS NULL AND NOT d.tombstoned")
    args: list = [match, repo_key]
    if sources is not None:
        sql += f" AND s.id IN ({','.join('?' * len(sources))})"
        args += sources
    rows = db.conn.execute(sql + " ORDER BY kb_fts.rank LIMIT ?", (*args, k)).fetchall()
    return [{"chunk_id": r["chunk_id"], "heading_path": r["heading_path"], "text": r["text"],
             "url": r["url"], "title": r["title"],
             "updated": time.strftime("%Y-%m-%d", time.localtime(r["updated_at"])) if r["updated_at"] else None}
            for r in rows]


def sync(db: DB, source_id: str, graph: Path | None = None) -> dict[str, int]:
    """Bring an indexed source's documents up to date: (re)index the new and
    changed ones and tombstone the ones it no longer has, committing per BATCH
    documents. Code-formatted identifiers that name a node of the repo's
    ``graph`` (codemap.graph_path) become ``kb_links``; without one, none do.
    Returns how many were indexed, unchanged and tombstoned."""
    src = db.conn.execute("SELECT * FROM kb_sources WHERE id = ?", (source_id,)).fetchone()
    if src is None:
        raise KeyError(source_id)
    if src["mode"] != "indexed":
        raise ValueError(f"source {source_id} is federated: nothing is stored to sync")
    if src["kind"] != FOLDER:
        raise ValueError(f"no connector for {src['kind']} sources yet")
    known = {r["external_id"]: r for r in db.conn.execute(
        "SELECT id, external_id, content_hash, tombstoned FROM kb_docs WHERE source_id = ?", (source_id,))}
    paths = _folder_paths(Path(src["scope"]))
    counts = {"indexed": 0, "unchanged": 0, "tombstoned": 0}
    batch: list[tuple[_Doc, list]] = []
    names = graph_names(graph)

    def flush() -> None:
        with db.tx() as c:
            for doc, chunks in batch:
                _index(c, source_id, doc, chunks, names)
        counts["indexed"] += len(batch)
        batch.clear()

    for external_id, path in paths:
        doc = _read(external_id, path)
        old = known.get(external_id)
        if old is not None and not old["tombstoned"] and old["content_hash"] == doc.content_hash:
            counts["unchanged"] += 1
            continue
        batch.append((doc, _scanned(source_id, doc)))
        if len(batch) >= BATCH:
            flush()
    if batch:
        flush()

    present = {external_id for external_id, _ in paths}
    gone = [r["id"] for ext, r in known.items() if ext not in present and not r["tombstoned"]]
    for i in range(0, len(gone), BATCH):
        with db.tx() as c:
            for doc_id in gone[i:i + BATCH]:
                _drop_chunks(c, doc_id)
                c.execute("UPDATE kb_docs SET tombstoned = 1 WHERE id = ?", (doc_id,))
    counts["tombstoned"] = len(gone)
    with db.tx() as c:
        c.execute("UPDATE kb_sources SET sync_cursor = ? WHERE id = ?", (str(time.time()), source_id))
    return counts
