"""Adding an imported Knowledge folder to the personal analysis-doc library
(brindle Pro). Imported folders stay local unless the person adds one.

:func:`dry_run` lists exactly which files would be uploaded and which are
skipped, and why: a file that looks like a secret (a secret-named file, or text
:func:`brindle.kb.redact` would change), text held back as an injection, a file
over the size limit or not UTF-8. :func:`add_to_library` uploads the rest with
source kind ``import``, one library doc per file; :func:`sync_upload` (called by
:func:`brindle.kb.sync`) re-uploads a changed file, replacing its old doc, and
deletes the docs of files gone from the folder; :func:`remove_from_library`
deletes them all server-side.

Nothing is sent in air-gap mode, without the ``analysis_docs`` entitlement, or
when the ``import_upload`` switch resolves to off (the user's choice, the
team's and the enterprise's, :func:`analysis_policy.resolve_switch`).
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from brindle import kb
from brindle.db import DB
from brindle.pro import analysis_docs, analysis_policy
from brindle.pro.orgkey import OrgKeys, repo_identity

log = logging.getLogger(__name__)

SWITCH = "import_upload"
SOURCE_KIND = "import"
_SECRET_NAMES = ("*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credential*",
                 "*secret*", "*password*", "*passwd*", "*token*", "*.env", "env.*", "*apikey*",
                 "*api-key*", "*api_key*", "*private*key*")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")


class ImportUploadOff(RuntimeError):
    """Nothing may be sent: air-gap, no entitlement, or the switch is off."""


# -- what would go -----------------------------------------------------------------------------


def _skip_reason(name: str, data: bytes | None) -> str | None:
    base = Path(name).name.lower()
    if any(fnmatch.fnmatch(base, pattern) for pattern in _SECRET_NAMES):
        return "its name looks like a secret"
    if data is None:
        return "too large or not UTF-8 text"
    text = data.decode("utf-8")
    if _PRIVATE_KEY.search(text) or kb.redact(text) != text:
        return "it looks like it contains a secret"
    if kb.quarantine_reason(text):
        return "it looks like instructions to an agent"
    return None


def _candidates(scope: str) -> list[tuple[str, bytes | None, str | None]]:
    """(name, bytes, why it is skipped or None) for each document of the folder."""
    out = []
    for name, path in kb._folder_paths(Path(scope)):
        try:
            data = analysis_docs._read(path)
        except OSError:
            data = None
        out.append((name, data, _skip_reason(name, data)))
    return out


def _source(db: DB, source_id: str):
    src = db.conn.execute("SELECT * FROM kb_sources WHERE id = ?", (source_id,)).fetchone()
    if src is None:
        raise KeyError(source_id)
    if src["kind"] != kb.FOLDER:
        raise ValueError("only imported folders can be added to the library")
    return src


def dry_run(db: DB, source_id: str) -> dict:
    """Which files would be uploaded and which skipped (with why). Reads the
    folder only; nothing is sent. ``allowed`` says whether an upload could happen now."""
    src = _source(db, source_id)
    files, skipped = [], []
    for name, data, why in _candidates(src["scope"]):
        if why:
            skipped.append({"name": name, "reason": why})
        else:
            files.append({"name": name, "bytes": len(data)})
    return {"source": source_id, "ref": _ref(src), "files": files, "skipped": skipped,
            "allowed": _off_reason() is None, "in_library": in_library(db, source_id)}


def in_library(db: DB, source_id: str) -> bool:
    return db.conn.execute("SELECT 1 FROM kb_import_library WHERE source_id = ?",
                           (source_id,)).fetchone() is not None


def _ref(src) -> str:
    return Path(src["scope"]).name or src["scope"]


# -- sending -----------------------------------------------------------------------------------


def _off_reason(client=None, store=None) -> str | None:
    """Why nothing may be sent, or None. Failing to learn the switch counts as off."""
    from brindle import airgap

    if airgap.enabled():
        return "air-gap mode is on: nothing is sent"
    try:
        analysis_docs.require_entitlement(store)
        switches = analysis_docs.fetch_switches(client, store)
        if not analysis_policy.resolve_switch(SWITCH, switches, store=store):
            return "uploading imported folders is turned off"
    except Exception as e:  # noqa: BLE001 - fail closed
        return str(e) or "the library is unavailable"
    return None


def _post(client, store, key, ent, identity, ref, name, data):
    from brindle.pro import auth

    body = {"scope": {"kind": "user", "id": ent.sub}, "files": [{"name": name, "md": data.decode("utf-8")}],
            "source": {"kind": SOURCE_KIND, "ref": ref}, "repo_key": key.repo_key(identity)}
    status, got = auth.authed(client, store, "POST", "/analysis-docs", auth.JSONBody(body))
    doc_id = got.get("id") if isinstance(got, dict) else None
    if status != 200 or not isinstance(doc_id, str):
        raise RuntimeError(f"{name} was not stored (HTTP {status})")
    return doc_id


def _delete(client, store, doc_id: str) -> bool:
    from brindle.pro import auth

    status, _ = auth.authed(client, store, "DELETE", f"/analysis-docs/{quote(doc_id, safe='')}", None)
    return status in (200, 404)


def sync_upload(db: DB, source_id: str, repo_root: str, *, client=None, store=None, key_store=None,
                identity=None) -> dict:
    """Bring the library up to date with an added folder: upload new and
    changed files, delete the docs of changed-away and removed ones. Nothing
    for a folder not added, or when sending is off; never raises."""
    result = {"uploaded": 0, "deleted": 0, "skipped": 0, "sent": False}
    try:
        if not in_library(db, source_id) or _off_reason(client, store) is not None:
            return result
        src = _source(db, source_id)
        client, store = analysis_docs._session(client, store)
        ent =analysis_docs.require_entitlement(store)
        ident = (identity or (lambda: repo_identity(repo_root)))()
        if ident is None:
            return result
        key = OrgKeys(store=store, client=client, key_store=key_store).get(ent.org_id)
        known = {r["external_id"]: r for r in db.conn.execute(
            "SELECT * FROM kb_import_uploads WHERE source_id = ?", (source_id,))}
        sendable = {}
        for name, data, why in _candidates(src["scope"]):
            if why:
                result["skipped"] += 1
            else:
                sendable[name] = data
        for name, data in sendable.items():
            old = known.get(name)
            digest = kb.content_hash(data.decode("utf-8"))
            if old is not None and old["content_hash"] == digest:
                continue
            doc_id = _post(client, store, key, ent, ident, _ref(src), name, data)
            with db.tx() as c:
                c.execute("INSERT OR REPLACE INTO kb_import_uploads (source_id, external_id, content_hash,"
                          " doc_id) VALUES (?, ?, ?, ?)", (source_id, name, digest, doc_id))
            result["uploaded"] += 1
            result["sent"] = True
            if old is not None and _delete(client, store, old["doc_id"]):
                result["deleted"] += 1
        for name, old in known.items():
            if name not in sendable and _delete(client, store, old["doc_id"]):
                with db.tx() as c:
                    c.execute("DELETE FROM kb_import_uploads WHERE source_id = ? AND external_id = ?",
                              (source_id, name))
                result["deleted"] += 1
    except Exception as e:  # noqa: BLE001
        log.info("imported folder not fully uploaded (%s)", type(e).__name__)
    return result


def add_to_library(db: DB, source_id: str, repo_root: str, **deps) -> dict:
    """Mark the folder as added and upload it. ``ImportUploadOff`` (nothing
    recorded) when sending is off."""
    _source(db, source_id)
    reason = _off_reason(deps.get("client"), deps.get("store"))
    if reason is not None:
        raise ImportUploadOff(reason)
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO kb_import_library (source_id, added_at) VALUES (?, ?)",
                  (source_id, time.time()))
    return sync_upload(db, source_id, repo_root, **deps)


def remove_from_library(db: DB, source_id: str, *, client=None, store=None) -> dict:
    """Delete the folder's docs server-side and forget it was added. A doc the
    server would not delete stays recorded, and the folder stays added."""
    _source(db, source_id)
    client, store = analysis_docs._session(client, store)
    rows = db.conn.execute("SELECT external_id, doc_id FROM kb_import_uploads WHERE source_id = ?",
                           (source_id,)).fetchall()
    deleted = 0
    for r in rows:
        if _delete(client, store, r["doc_id"]):
            with db.tx() as c:
                c.execute("DELETE FROM kb_import_uploads WHERE source_id = ? AND external_id = ?",
                          (source_id, r["external_id"]))
            deleted += 1
    if deleted == len(rows):
        with db.tx() as c:
            c.execute("DELETE FROM kb_import_library WHERE source_id = ?", (source_id,))
    return {"deleted": deleted, "left": len(rows) - deleted}
