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


def _plan(src) -> tuple[dict[str, bytes], list[dict]]:
    sendable, skipped = {}, []
    for name, data, why in _candidates(src["scope"]):
        if why:
            skipped.append({"name": name, "reason": why})
        else:
            sendable[name] = data
    return sendable, skipped


def _digest(data: bytes) -> str:
    return kb.content_hash(data.decode("utf-8"))


def _token(sendable: dict[str, bytes]) -> str:
    """Names the exact files and contents a preview listed."""
    return kb.content_hash("\n".join(f"{n}\0{_digest(d)}" for n, d in sorted(sendable.items())))


def dry_run(db: DB, source_id: str) -> dict:
    """Which files would be uploaded and which skipped (with why). Reads the
    folder only; nothing is sent. ``allowed`` says whether an upload could happen
    now; ``token`` is what :func:`add_to_library` takes to upload exactly these."""
    src = _source(db, source_id)
    sendable, skipped = _plan(src)
    return {"source": source_id, "ref": _ref(src), "skipped": skipped, "token": _token(sendable),
            "files": [{"name": n, "bytes": len(d)} for n, d in sendable.items()],
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


def _drain(db: DB, client, store) -> None:
    """Delete the docs queued for deletion; each stays queued until the server confirms."""
    for r in db.conn.execute("SELECT doc_id FROM kb_import_deletions").fetchall():
        try:
            if _delete(client, store, r["doc_id"]):
                with db.tx() as c:
                    c.execute("DELETE FROM kb_import_deletions WHERE doc_id = ?", (r["doc_id"],))
        except Exception:  # noqa: BLE001 - retried on the next upload or removal
            log.info("library doc not deleted yet")


def _queue_deletion(c, source_id: str, external_id: str) -> None:
    c.execute("INSERT OR IGNORE INTO kb_import_deletions SELECT doc_id FROM kb_import_uploads"
              " WHERE source_id = ? AND external_id = ?", (source_id, external_id))
    c.execute("DELETE FROM kb_import_uploads WHERE source_id = ? AND external_id = ?",
              (source_id, external_id))


def _upload(db: DB, source_id: str, repo_root: str, only: dict[str, str] | None = None, *, client=None,
            store=None, key_store=None, identity=None) -> dict:
    """Upload new and changed files (only those in ``only``, name to content
    digest, when given), queue the docs of changed-away and removed ones for
    deletion, and drain the queue. A file that fails stays out of the
    tracking so the next call retries it; the rest keep their progress.
    Raises when nothing can be attempted."""
    result = {"uploaded": 0, "deleted": 0, "skipped": 0, "sent": False, "failed": []}
    reason = _off_reason(client, store)
    if reason is not None:
        raise ImportUploadOff(reason)
    src = _source(db, source_id)
    client, store = analysis_docs._session(client, store)
    ent = analysis_docs.require_entitlement(store)
    ident = (identity or (lambda: repo_identity(repo_root)))()
    if ident is None:
        raise RuntimeError("this folder's repo has no identity to file the docs under")
    key = OrgKeys(store=store, client=client, key_store=key_store).get(ent.org_id)
    _drain(db, client, store)
    known = {r["external_id"]: r for r in db.conn.execute(
        "SELECT * FROM kb_import_uploads WHERE source_id = ?", (source_id,))}
    sendable, skipped = _plan(src)
    result["skipped"] = len(skipped)
    for name, data in sendable.items():
        digest = _digest(data)
        old = known.get(name)
        if (only is not None and only.get(name) != digest) or (old is not None and old["content_hash"] == digest):
            continue
        try:
            doc_id = _post(client, store, key, ent, ident, _ref(src), name, data)
        except Exception as e:  # noqa: BLE001
            log.info("imported file not uploaded (%s)", type(e).__name__)
            result["failed"].append(name)
            continue
        with db.tx() as c:
            if old is not None:
                _queue_deletion(c, source_id, name)
            c.execute("INSERT INTO kb_import_uploads (source_id, external_id, content_hash, doc_id)"
                      " VALUES (?, ?, ?, ?)", (source_id, name, digest, doc_id))
        result["uploaded"] += 1
        result["sent"] = True
    if only is None:
        with db.tx() as c:
            for name in known.keys() - sendable.keys():
                _queue_deletion(c, source_id, name)
    before = db.conn.execute("SELECT count(*) FROM kb_import_deletions").fetchone()[0]
    _drain(db, client, store)
    result["deleted"] = before - db.conn.execute("SELECT count(*) FROM kb_import_deletions").fetchone()[0]
    with db.tx() as c:
        c.execute("UPDATE kb_import_library SET pending = ? WHERE source_id = ?",
                  (len(result["failed"]), source_id))
    return result


def sync_upload(db: DB, source_id: str, repo_root: str, **deps) -> dict:
    """Bring the library up to date with an added folder (what :func:`brindle.kb.sync`
    calls). Nothing for a folder not added or when sending is off; never raises."""
    nothing = {"uploaded": 0, "deleted": 0, "skipped": 0, "sent": False, "failed": []}
    try:
        return _upload(db, source_id, repo_root, **deps) if in_library(db, source_id) else nothing
    except Exception as e:  # noqa: BLE001
        log.info("imported folder not uploaded (%s)", type(e).__name__)
        return nothing


class ManifestChanged(RuntimeError):
    """The folder changed since the preview; ``plan`` is a fresh one to confirm."""

    def __init__(self, plan: dict) -> None:
        super().__init__("the folder changed since it was listed")
        self.plan = plan


class UploadFailed(RuntimeError):
    """Some files could not be uploaded; the ones that were stay uploaded."""


def add_to_library(db: DB, source_id: str, repo_root: str, token: str | None = None, **deps) -> dict:
    """Upload exactly the files a :func:`dry_run` listed (its ``token``) and mark
    the folder added. ``ImportUploadOff`` when sending is off, ``ManifestChanged``
    when the folder differs from what was listed, ``UploadFailed`` when a file
    was rejected (the folder is marked added only if something went up)."""
    src = _source(db, source_id)
    reason = _off_reason(deps.get("client"), deps.get("store"))
    if reason is not None:
        raise ImportUploadOff(reason)
    sendable, _ = _plan(src)
    if token != _token(sendable):
        raise ManifestChanged(dry_run(db, source_id))
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO kb_import_library (source_id, added_at) VALUES (?, ?)",
                  (source_id, time.time()))
    failed: list[str] | str = []
    try:
        result = _upload(db, source_id, repo_root, {n: _digest(d) for n, d in sendable.items()}, **deps)
        failed = result["failed"]
    except Exception as e:  # noqa: BLE001
        failed = str(e) or type(e).__name__
    if failed:
        if not db.conn.execute("SELECT 1 FROM kb_import_uploads WHERE source_id = ?", (source_id,)).fetchone():
            with db.tx() as c:
                c.execute("DELETE FROM kb_import_library WHERE source_id = ?", (source_id,))
        raise UploadFailed(failed if isinstance(failed, str) else f"not uploaded: {', '.join(failed)}")
    return result


def remove_from_library(db: DB, source_id: str, *, client=None, store=None) -> dict:
    """Forget the folder's docs and delete them server-side. They are queued
    first, so a delete the server does not confirm is retried by the next
    upload or removal; ``left`` counts those."""
    _source(db, source_id)
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO kb_import_deletions SELECT doc_id FROM kb_import_uploads"
                  " WHERE source_id = ?", (source_id,))
        c.execute("DELETE FROM kb_import_uploads WHERE source_id = ?", (source_id,))
        c.execute("DELETE FROM kb_import_library WHERE source_id = ?", (source_id,))
    client, store = analysis_docs._session(client, store)
    _drain(db, client, store)
    return {"left": db.conn.execute("SELECT count(*) FROM kb_import_deletions").fetchone()[0]}
