"""The plan library kept on this Mac (Settings › Library › Library location).

The person's ``store`` switch picks where their approved plans live: on, on
brindle's servers (:mod:`brindle.pro.analysis_docs` uploads them, as before);
off, here, one private JSON file per plan under ``$BRINDLE_HOME/pro/library``,
and nothing is uploaded. An org or team that enforces the switch
(:func:`analysis_policy.resolve_switch`) beats the person's choice.

Their choice is kept here too (``library-location.json``), so an approval or
a Library read knows where to go without asking the server; it is learned
from the server's switch the first time it is needed.

:func:`set_location` moves the plans: to the servers, each local plan is
uploaded and its local copy removed after a 200; to this Mac, each of the
person's own server docs is downloaded and the server copy deleted only once
the local file is written. A doc shared with a team or org keeps its server
copy (``server_id``) for as long as it is shared.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from brindle.pro import analysis_policy
from brindle.pro._files import private_dir

log = logging.getLogger(__name__)

LOCAL, SERVER = "local", "server"
LOCATIONS = (LOCAL, SERVER)
SWITCH = "store"                    # on: SERVER, off: LOCAL
CHOICE_FILE = "library-location.json"
DIR = "library"
MAX_RECORD = 4 * 1024 * 1024
ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
# What a listing carries of a doc (the server's _listed shape); files and bookkeeping stay out.
LISTED = ("title", "summary", "kind", "findings", "open_questions", "topics", "category", "created_at",
          "updated_at", "version", "source", "related", "supersedes", "superseded_by", "stale")


# -- files -----------------------------------------------------------------------------------------


def _dir(create: bool = True) -> Path | None:
    base = private_dir(create)
    if base is None:
        return None
    d = base / DIR
    if create:
        d.mkdir(mode=0o700, exist_ok=True)
    elif not d.is_dir():
        return None
    return d


def _write(path: Path, data: dict) -> None:
    """Atomically replace ``path``: a temp file of its own in the same folder, 0600."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        raw = fh.read(MAX_RECORD + 1)
    try:
        got = json.loads(raw) if len(raw) <= MAX_RECORD else None
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _path(doc_id: str, create: bool = False) -> Path | None:
    if not isinstance(doc_id, str) or not ID_RE.fullmatch(doc_id):
        return None
    d = _dir(create)
    return d / f"{doc_id}.json" if d is not None else None


# -- the person's choice -----------------------------------------------------------------------------


def choice() -> str | None:
    """The location the person chose on this Mac, or None while unknown."""
    d = _dir(create=False)
    got = _read(d / CHOICE_FILE) if d is not None else None
    value = (got or {}).get("location")
    return value if value in LOCATIONS else None


def remember(location: str) -> None:
    if location not in LOCATIONS:
        raise ValueError(f"location is one of {', '.join(LOCATIONS)}")
    d = _dir()
    _write(d / CHOICE_FILE, {"location": location, "at": time.time()})


def _switch(location: str | None) -> dict | None:
    return {SWITCH: "on" if location == SERVER else "off"} if location in LOCATIONS else None


def person_choice(fetch: Callable[[], dict] | None = None) -> str | None:
    """:func:`choice`, or, while unknown, the server's ``store`` switch as
    ``fetch`` reads it (remembered when it says on or off). Failing to read
    it leaves the choice unknown."""
    got = choice()
    if got is not None or fetch is None:
        return got
    try:
        server = (fetch() or {}).get(SWITCH)
    except Exception:  # noqa: BLE001 - unknown: the defaults decide
        log.info("library location not read from the server", exc_info=True)
        return None
    if server not in ("on", "off"):
        return None
    got = SERVER if server == "on" else LOCAL
    try:
        remember(got)
    except OSError:
        log.info("library location not remembered", exc_info=True)
    return got


def location(store=None, *, fetch: Callable[[], dict] | None = None) -> str:
    """Where plans live now: an enforced org or team value, else the person's
    choice, else the defaults (this Mac). Uses only cached policy; ``fetch``
    is asked only while the person's choice is unknown here."""
    on = analysis_policy.resolve_switch(SWITCH, _switch(person_choice(fetch)), store=store)
    return SERVER if on else LOCAL


# -- the plans --------------------------------------------------------------------------------------


def _title(files: list[dict]) -> str | None:
    for f in files:
        for line in str(f.get("md") or "").splitlines():
            if line.startswith("# "):
                return line[2:].strip() or None
    return files[0].get("name") if files else None


def add(files: list[dict], source: dict | None, *, repo_root: str | None = None, doc_id: str | None = None,
        meta: dict | None = None) -> dict:
    """Keep a doc here: ``files`` ({name, md}), its ``source``, and what a
    listing shows of it (``meta``, as the server listed it). A new local id
    unless ``doc_id`` (a downloaded doc keeps the server's). Raises on a
    failed write."""
    doc_id = doc_id or f"local-{secrets.token_hex(8)}"
    path = _path(doc_id, create=True)
    if path is None:
        raise ValueError(f"not a doc id: {doc_id!r}")
    record = {k: v for k, v in (meta or {}).items() if k in LISTED}
    record.update(doc_id=doc_id, files=list(files), source=dict(source or {}), repo_root=repo_root,
                  shared=[], server_id=None)
    record.setdefault("title", _title(files))
    record.setdefault("created_at", time.time())
    _write(path, record)
    return record


def get(doc_id: str) -> dict | None:
    path = _path(doc_id)
    return _read(path) if path is not None else None


def update(doc_id: str, **fields) -> dict:
    record = get(doc_id)
    if record is None:
        raise KeyError(doc_id)
    record.update(fields)
    _write(_path(doc_id), record)
    return record


def remove(doc_id: str) -> bool:
    path = _path(doc_id)
    try:
        if path is not None:
            path.unlink()
            return True
    except FileNotFoundError:
        pass
    return False


def records() -> list[dict]:
    """Every doc kept here, newest first."""
    d = _dir(create=False)
    if d is None:
        return []
    got = [r for p in d.glob("*.json") if p.name != CHOICE_FILE and not p.name.startswith(".")
           for r in [_read(p)] if r and isinstance(r.get("doc_id"), str)]
    return sorted(got, key=lambda r: -float(r.get("created_at") or 0))


def _matches(record: dict, query: dict) -> bool:
    return ((not query.get("topic") or query["topic"] in (record.get("topics") or []))
            and (not query.get("kind") or record.get("kind") == query["kind"])
            and (not query.get("session") or (record.get("source") or {}).get("session") == query["session"]))


def listed(query: dict | None = None, scope: dict | None = None) -> list[dict]:
    """The docs kept here as the server lists docs (``topic``, ``kind`` and
    ``session`` filter them), each in the person's own ``scope``."""
    return [{**{k: r[k] for k in LISTED if k in r}, "doc_id": r["doc_id"], "local": True,
             **({"scope": scope} if scope else {})}
            for r in records() if _matches(r, query or {})]


def view(record: dict, scope: dict | None = None) -> dict:
    """One doc as the server shows it (``GET /analysis-docs/{id}``)."""
    shared = [s for s in record.get("shared") or [] if isinstance(s, dict)]
    return {**{k: record[k] for k in LISTED if k in record}, "id": record["doc_id"], "local": True,
            "files": record.get("files") or [], "scopes": [*([scope] if scope else []), *shared],
            **({"scope": scope} if scope else {})}


# -- moving between here and the servers ---------------------------------------------------------------


def _upload(record: dict, client=None, store=None) -> str | None:
    """Put a kept doc on the server; its server id, or None when it wasn't stored."""
    from brindle.pro import analysis_docs

    up = analysis_docs.AnalysisDocs(record.get("repo_root") or "", client=client, store=store,
                                    start_thread=False)
    return up.upload(analysis_docs.Doc(files=record.get("files") or [], source=record.get("source") or {}))


def server_copy(doc_id: str, client=None, store=None) -> str:
    """The server id of kept doc ``doc_id``, uploading it first if it has none
    (sharing a doc puts that one doc on the server). Raises RuntimeError when
    it can't be stored."""
    record = get(doc_id)
    if record is None:
        raise KeyError(doc_id)
    if record.get("server_id"):
        return record["server_id"]
    server_id = _upload(record, client, store)
    if not server_id:
        raise RuntimeError("the plan couldn't be put on brindle's servers to share it")
    update(doc_id, server_id=server_id)
    return server_id


def _to_server(client, store) -> tuple[int, int]:
    moved = left = 0
    for record in records():
        try:
            if not record.get("server_id"):
                server_id = _upload(record, client, store)
                if not server_id:
                    left += 1
                    continue
            remove(record["doc_id"])            # the server copy is the doc now
            moved += 1
        except Exception:  # noqa: BLE001 - it stays here, listed, for the next try
            log.info("plan %s not moved to the servers", record.get("doc_id"), exc_info=True)
            left += 1
    return moved, left


def _to_local(sub: str, client, store) -> tuple[int, int]:
    from brindle.pro import analysis_docs

    def api(method: str, path: str) -> dict:
        return analysis_docs.api(method, path, client=client, store=store)

    moved = left = 0
    query = urlencode({"scope_kind": "user", "scope_id": sub})
    for listed_doc in api("GET", f"/analysis-docs?{query}").get("docs") or []:
        doc_id = listed_doc.get("doc_id") if isinstance(listed_doc, dict) else None
        if _path(doc_id) is None:
            continue
        path = f"/analysis-docs/{quote(doc_id, safe='')}"
        try:
            full = api("GET", path)
            add(full.get("files") or [], full.get("source"), doc_id=doc_id, meta={**listed_doc, **full})
        except Exception:  # noqa: BLE001 - not written here: the server copy stays
            log.info("plan %s not downloaded", doc_id, exc_info=True)
            left += 1
            continue
        shared = [s for s in full.get("scopes") or [] if isinstance(s, dict) and s.get("kind") in ("group", "org")]
        if shared:                              # shared: its server copy stays while it is
            update(doc_id, server_id=doc_id, shared=shared)
            moved += 1
            continue
        try:
            api("DELETE", path)
            moved += 1
        except Exception:  # noqa: BLE001 - kept in both places; the next move deletes it
            log.info("plan %s kept on the server too", doc_id, exc_info=True)
            left += 1
    return moved, left


def set_location(target: str, *, client=None, store=None) -> dict:
    """Move the library to ``target`` (local or server) and remember it:
    ``{location, moved, left}``, ``left`` counting plans not moved yet (each
    still where it was, and moved by the next call). Raises
    :class:`analysis_docs.Locked` when the person's org enforces the other."""
    from brindle.pro import analysis_docs

    if target not in LOCATIONS:
        raise ValueError(f"location is one of {', '.join(LOCATIONS)}")
    client, store = analysis_docs._session(client, store)
    on = analysis_policy.resolve_switch(SWITCH, _switch(target), store=store)
    if on != (target == SERVER):
        raise analysis_docs.Locked(f"your organisation keeps plans {'on brindle servers' if on else 'on this Mac'} "
                                   "for everyone")
    ent = analysis_docs.require_entitlement(store)
    try:                                        # the server's switch says the same, for its own checks
        switches = {**analysis_docs.fetch_switches(client, store), SWITCH: "on" if target == SERVER else "off"}
        analysis_docs.api("PUT", "/me/analysis-docs/settings", {"switches": switches}, client=client, store=store)
    except Exception:  # noqa: BLE001
        if target == SERVER:
            raise
        log.info("server store switch not updated", exc_info=True)
    remember(target)
    moved, left = _to_server(client, store) if target == SERVER else _to_local(ent.sub, client, store)
    return {"location": target, "moved": moved, "left": left}
