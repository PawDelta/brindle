"""Adding an imported Knowledge folder to the personal analysis-doc library
(brindle Pro). Imported folders stay local unless the person adds one.

:func:`dry_run` lists exactly which files would be uploaded and which are
skipped, and why: a file that looks like a secret (a secret-named file, or text
:func:`brindle.kb.redact` would change), text held back as an injection, a file
over the size limit or not UTF-8. :func:`add_to_library` uploads the rest with
through ``POST /analysis-docs/imports``, one library doc per file, in batches of
at most 20: hashes first, then the text of only the files the server answers
``needs_content`` for. The server is the source of truth (it keys docs by
folder and path, the folder id being the source id): :func:`sync_upload`
(called by :func:`brindle.kb.sync`) re-sends a changed file (the server updates
its doc in place) and then has the server prune the docs of paths the folder no
longer has (``.../imports/{folder_id}/prune``); :func:`remove_from_library`
deletes them all with ``DELETE /analysis-docs/imports/{folder_id}``. The local
upload table only shows state (path to hash); every sync sends every file's hash.

Nothing is sent in air-gap mode, without the ``analysis_docs`` entitlement, or
when the ``import_upload`` switch resolves to off (the user's choice, the
team's and the enterprise's, :func:`analysis_policy.resolve_switch`).

A folder's library state lives in the local database, so the CLI and the
daemon agree on it: no row is *local*; otherwise ``adding``, ``in_library`` or
``removing``. Every row (membership, each cached upload, each queued removal)
names the identity that authorized it: the backend, user, org and scope of the
verified sign-in (:func:`current_identity`). An operation acts only on its own
identity's rows; after an account switch the folder is local again for the new
identity, which needs its own explicit Add, and the other identity's rows and
queued removals stay untouched until it is signed in.

Every operation on a folder (add, background sync, remove, finish removing)
first claims it in one transaction (:func:`_claim`: an ``op`` with a token and
a lease); a loser does nothing (a sync) or is ``Busy`` (add, remove). The holder
does its network work and writes the cache only while its token still matches;
a run that loses the claim simply stops, and the next sync converges. Removal
claims and flips the state to ``removing``; the folder is dropped only once the
server confirmed the folder DELETE, which every later sync retries (a removal
for a source deleted meanwhile is kept in ``kb_import_removals``).
:func:`sync_upload` looks at the switch before anything else, unfinished
removals included; removal is not switch-gated.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from brindle import kb
from brindle.db import DB
from brindle.pro import analysis_docs, analysis_policy

log = logging.getLogger(__name__)

SWITCH = "import_upload"
BATCH = 20         # files per POST /analysis-docs/imports (the backend's limit)
MAX_FILES = 10000  # paths one prune can name (the backend's limit)
ADDING, IN_LIBRARY, REMOVING = "adding", "in_library", "removing"
LEASE = 300.0      # seconds a claim lasts without renewal (a crashed holder's claim then lapses)
_SECRET_NAMES = ("*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credential*",
                 "*secret*", "*password*", "*passwd*", "*token*", "*.env", "env.*", "*apikey*",
                 "*api-key*", "*api_key*", "*private*key*")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")


class ImportUploadOff(RuntimeError):
    """Nothing may be sent: air-gap, no entitlement, or the switch is off."""


class TooManyFiles(ValueError):
    """The folder has more files than one library folder can hold."""

    def __init__(self, count: int) -> None:
        super().__init__(f"too many files to add ({count}, at most {MAX_FILES}); pick a smaller folder")


class Busy(RuntimeError):
    """The folder's library state is held by another add or removal."""


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


def dry_run(db: DB, source_id: str, *, client=None, store=None) -> dict:
    """Which files would be uploaded and which skipped (with why). Reads the
    folder only; nothing is sent. ``allowed`` says whether an upload could happen
    now; ``token`` is what :func:`add_to_library` takes to upload exactly these."""
    src = _source(db, source_id)
    sendable, skipped = _plan(src)
    return {"source": source_id, "ref": _ref(src), "skipped": skipped, "token": _token(sendable),
            "files": [{"name": n, "bytes": len(d)} for n, d in sendable.items()],
            "allowed": _off_reason(client, store) is None,
            "in_library": state(db, source_id, current_identity(client, store)) == IN_LIBRARY}


# -- identity and state ---------------------------------------------------------------------------


def _identity(base: str, sub: str, org_id: str) -> str:
    return "\n".join((base, sub, org_id, "user"))


def _pin_identity(rec: dict) -> str | None:
    """The identity (backend, user, org, scope) one credential record names, from
    the record's own ``base_url`` and its entitlement's signed claims (expiry is
    not looked at: it does not change who the record is). None when it names none."""
    from brindle.pro import auth, license

    try:
        base = auth.base_url(rec.get("base_url"))
        claims = license.verify_signed(rec.get("entitlement") or "", typ=license.TYP, token_use=license.TOKEN_USE)
        if claims.get("iss") != base:
            return None
        return _identity(base, claims["sub"], claims["org_id"])
    except Exception:  # noqa: BLE001
        return None


def _who(client=None, store=None):
    """(client, store, entitlement, identity) of the verified sign-in, all from
    one read of the credential store and its own ``base_url``; raises
    (``RuntimeError``) in air-gap mode, signed out, or without the library."""
    from brindle import airgap
    from brindle.pro import auth, credentials, license

    if airgap.enabled():
        raise RuntimeError("air-gap mode is on: nothing is sent")
    store = store or credentials.default_store()
    rec = store.load() or {}
    base = auth.base_url(rec.get("base_url"))
    try:
        ent = license.verify(rec.get("entitlement") or "", issuer=base)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("not logged in to brindle Pro (`brindle account login`)") from e
    if analysis_docs.FEATURE not in ent.features or ent.in_grace:
        raise RuntimeError("your plan has no analysis doc library (the `analysis_docs` entitlement)")
    client = client or auth.Client(base, auth.UrllibTransport(timeout=analysis_docs.SEND_WAIT))
    return client, store, ent, _identity(base, ent.sub, ent.org_id)


def current_identity(client=None, store=None) -> str | None:
    """The backend, user, org and scope the library rows are filed under, from
    the verified sign-in (offline); None when there is none."""
    try:
        return _who(client, store)[3]
    except Exception:  # noqa: BLE001
        return None


class AccountChanged(RuntimeError):
    """The sign-in is no longer the one a run took its snapshot of, or its access
    token needs refreshing: the run stops and every remaining row stays queued."""


@dataclass
class Snapshot:
    """The verified identity and access token taken once when a run starts. Every
    request of the run is sent with it, never with a reloaded sign-in."""
    client: object
    store: object
    ent: object
    who: str
    token: str
    expires: float
    stopped: bool = False    # the account changed during a request that was already sent


def _snapshot(client=None, store=None) -> Snapshot:
    """Snapshot the sign-in: refresh an expiring access token first (before
    anything is sent), then read the credential store once and take the token,
    the verified entitlement, the identity and the backend from that one record.
    Raises (``RuntimeError``) like :func:`_who`."""
    from brindle import airgap
    from brindle.pro import auth, credentials, license

    if airgap.enabled():
        raise RuntimeError("air-gap mode is on: nothing is sent")
    store = store or credentials.default_store()
    rec = store.load() or {}
    base = auth.base_url(rec.get("base_url"))
    client = client or auth.Client(base, auth.UrllibTransport(timeout=analysis_docs.SEND_WAIT))
    pinned = _pin_identity(rec)
    if pinned is None or client.base != base:
        raise AccountChanged("the stored sign-in does not match the backend in use")
    if float(rec.get("access_expires_at") or 0) <= time.time() + auth.ACCESS_SLACK and rec.get("refresh_token"):
        # Under the refresh lock: nothing is sent unless the store still holds this record, whose own
        # refresh token goes to its own backend; the rotated pair is saved before the entitlement is fetched.
        try:
            rec = auth.refresh_captured(store, rec, client.transport)
        except auth.AuthError as e:
            if e.code == "record_changed":
                raise AccountChanged("the sign-in changed while it was refreshed") from e
            raise
    ent = license.verify(rec.get("entitlement") or "", issuer=base)
    if analysis_docs.FEATURE not in ent.features or ent.in_grace:
        raise RuntimeError("your plan has no analysis doc library (the `analysis_docs` entitlement)")
    if not rec.get("access_token"):
        raise AccountChanged("not signed in")
    return Snapshot(client, store, ent, _identity(base, ent.sub, ent.org_id), rec["access_token"],
                    float(rec.get("access_expires_at") or 0))


def _call(snap: Snapshot, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """One request with the snapshot's token. Before sending, the store must
    still hold the snapshot's identity and the token must not be near its end:
    otherwise ``AccountChanged`` and nothing is sent. A 401 (refresh needed) also
    raises. If the account changed while the request was out, its answer is still
    returned (it was made with the snapshot's token, for the snapshot's account),
    but ``snap.stopped`` is set so the run ends and a 404 is not believed."""
    from brindle.pro import auth

    # The identity is read from the store's current record, backend included, never from snap.client.
    if snap.stopped or _pin_identity(snap.store.load() or {}) != snap.who \
            or snap.expires <= time.time() + auth.ACCESS_SLACK:
        raise AccountChanged("the signed-in account changed")
    status, got = snap.client.call(method, path, body, token=snap.token)
    if status == 401:
        raise AccountChanged("the sign-in needs refreshing")
    if _pin_identity(snap.store.load() or {}) != snap.who:
        snap.stopped = True
    return status, got


def state(db: DB, source_id: str, who: str | None) -> str | None:
    """The folder's library state for identity ``who``, None when it is local to
    them (including another identity's folder, or no identity)."""
    row = db.conn.execute("SELECT state FROM kb_import_library WHERE source_id = ? AND identity = ?",
                          (source_id, who)).fetchone()
    return row["state"] if row else None


def in_library(db: DB, source_id: str) -> bool:
    """Whether any identity has library state for the folder."""
    return db.conn.execute("SELECT 1 FROM kb_import_library WHERE source_id = ?", (source_id,)).fetchone() is not None


def _claim(db: DB, source_id: str, op: str, allowed: tuple[str | None, ...], who: str, *,
           new_state: str | None = None, local=None) -> str | None:
    """Claim the folder for identity ``who``'s operation (``add``, ``sync`` or
    ``remove``) in one transaction: its state for them must be one of ``allowed``
    (None: no row yet) and no other operation may hold it (a claim past its
    ``op_until`` is void). Moves it to ``new_state`` and runs ``local(cursor)`` in
    the same transaction. Returns the claim's token, or None when the folder is
    held or in another state."""
    now = time.time()
    token = uuid.uuid4().hex
    with db.tx() as c:
        row = c.execute("SELECT state, op, op_until FROM kb_import_library WHERE source_id = ? AND identity = ?",
                        (source_id, who)).fetchone()
        current = row["state"] if row else None
        if current not in allowed or (row and row["op"] and (row["op_until"] or 0) >= now):
            return None
        if row is None:
            c.execute("INSERT INTO kb_import_library (source_id, identity, added_at, state, op, op_token, op_until)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (source_id, who, now, new_state or IN_LIBRARY, op, token, now + LEASE))
        else:
            c.execute("UPDATE kb_import_library SET state = ?, op = ?, op_token = ?, op_until = ?"
                      " WHERE source_id = ? AND identity = ?", (new_state or current, op, token, now + LEASE,
                                                                source_id, who))
        if local:
            local(c)
    return token


def _holds(c, source_id: str, token: str) -> bool:
    return c.execute("SELECT 1 FROM kb_import_library WHERE source_id = ? AND op_token = ?",
                     (source_id, token)).fetchone() is not None


def _renew(db: DB, source_id: str, token: str) -> bool:
    """Extend the claim; False when it was lost (it ran out and the folder was
    claimed again, or the folder is gone)."""
    with db.tx() as c:
        return c.execute("UPDATE kb_import_library SET op_until = ? WHERE source_id = ? AND op_token = ?",
                         (time.time() + LEASE, source_id, token)).rowcount == 1


def _release(db: DB, source_id: str, token: str, *, state: str | None = None, drop: bool = False) -> None:
    """End the claim (only if still held): optionally move to ``state`` or drop
    the folder's row."""
    with db.tx() as c:
        if drop:
            c.execute("DELETE FROM kb_import_library WHERE source_id = ? AND op_token = ?", (source_id, token))
        else:
            c.execute("UPDATE kb_import_library SET op = NULL, op_token = NULL, op_until = NULL,"
                      " state = COALESCE(?, state) WHERE source_id = ? AND op_token = ?", (state, source_id, token))


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


class Rejected(RuntimeError):
    """The backend refused a batch as invalid (422: a bad path or a hash that does
    not match its text), so sending the same batch again is pointless."""


def _post_batch(snap: Snapshot, folder_id: str, files: list[dict]) -> dict[str, dict]:
    """One ``POST /analysis-docs/imports``: ``{path: the file's result}``."""
    from brindle.pro import auth

    body = {"scope": {"kind": "user", "id": snap.ent.sub}, "folder_id": folder_id, "files": files}
    status, got = _call(snap, "POST", "/analysis-docs/imports", auth.JSONBody(body))
    results = got.get("files") if isinstance(got, dict) else None
    if status == 422:
        raise Rejected(got.get("error") if isinstance(got, dict) and got.get("error") else "rejected")
    if status != 200 or not isinstance(results, list):
        raise RuntimeError(f"not stored (HTTP {status})")
    return {r["path"]: r for r in results if isinstance(r, dict) and isinstance(r.get("path"), str)}


def _exchange(snap: Snapshot, folder_id: str, items: list[tuple[str, str, bytes]]) -> dict[str, tuple[str, str]]:
    """Send a batch of (path, hash, content): hashes first, then the content of
    only the files the server asks for. ``{path: (doc id, status)}`` of those it
    stored or already had; a file missing from it was not stored."""
    got = _post_batch(snap, folder_id, [{"path": n, "hash": h} for n, h, _ in items])
    need = [i for i in items if (got.get(i[0]) or {}).get("status") == "needs_content"]
    if need:
        got.update(_post_batch(snap, folder_id, [{"path": n, "hash": h, "md": d.decode("utf-8")}
                                                 for n, h, d in need]))
    return {n: (r["doc_id"], r["status"]) for n, _, _ in items
            if (r := got.get(n)) and r.get("status") in ("created", "updated", "unchanged")
            and isinstance(r.get("doc_id"), str)}


def _send(snap: Snapshot, folder_id: str, items: list[tuple[str, str, bytes]]) -> dict[str, tuple[str, str]]:
    """:func:`_exchange`, but a rejected batch is retried one file at a time so
    one bad file does not hold back the rest."""
    try:
        return _exchange(snap, folder_id, items)
    except Rejected:
        if len(items) == 1:
            return {}
        stored: dict[str, tuple[str, str]] = {}
        for item in items:
            stored.update(_send(snap, folder_id, [item]))
        return stored


def _delete_folder(snap: Snapshot, source_id: str) -> bool:
    """``DELETE /analysis-docs/imports/{folder_id}``: the identity's docs of the
    folder, all at once. True once the server confirms (a 404 counts, unless the
    account changed while it was out); False when it did not, or the account changed."""
    try:
        status, _ = _call(snap, "DELETE", f"/analysis-docs/imports/{quote(source_id, safe='')}"
                          f"?scope_kind=user&scope_id={quote(snap.ent.sub, safe='')}")
        return status == 200 or (status == 404 and not snap.stopped)
    except AccountChanged:
        log.info("library folder not deleted: the sign-in changed")
    except Exception:  # noqa: BLE001 - tried again on the next sync or removal
        log.info("library folder not deleted yet")
    return False


def _prune(snap: Snapshot, source_id: str, paths: list[str]) -> int:
    """``POST /analysis-docs/imports/{folder_id}/prune``: the server deletes the
    identity's docs of the folder for every path not in ``paths`` (idempotent, not
    switch-gated). Returns how many it deleted."""
    from brindle.pro import auth

    body = {"scope": {"kind": "user", "id": snap.ent.sub}, "keep_paths": paths}
    status, got = _call(snap, "POST", f"/analysis-docs/imports/{quote(source_id, safe='')}/prune",
                        auth.JSONBody(body))
    if status != 200:
        raise RuntimeError(f"not pruned (HTTP {status})")
    deleted = got.get("deleted") if isinstance(got, dict) else 0
    return deleted if isinstance(deleted, int) else 0


def drain_pending(db: DB, *, client=None, store=None) -> None:
    """Finish the signed-in identity's unfinished removals: every ``removing``
    folder of theirs (under its own claim; a folder another operation holds is left
    alone), and every removal queued for a source that has since been deleted. A
    folder is dropped, or its queue entry cleared, only once the server confirmed
    the DELETE. Never raises."""
    try:
        snap = _snapshot(client, store)
        who = snap.who
        for r in db.conn.execute("SELECT source_id FROM kb_import_library WHERE identity = ? AND state = ?",
                                 (who, REMOVING)).fetchall():
            token = _claim(db, r["source_id"], "remove", (REMOVING,), who)
            if token:
                done = False
                try:
                    done = _delete_folder(snap, r["source_id"])
                    if done:
                        with db.tx() as c:
                            c.execute("DELETE FROM kb_import_uploads WHERE source_id = ? AND identity = ?",
                                      (r["source_id"], who))
                finally:
                    _release(db, r["source_id"], token, drop=done)
        for r in db.conn.execute(
                "SELECT source_id FROM kb_import_removals x WHERE identity = ? AND NOT EXISTS"
                " (SELECT 1 FROM kb_import_library l WHERE l.source_id = x.source_id AND l.identity = x.identity)",
                (who,)).fetchall():
            if _delete_folder(snap, r["source_id"]):
                with db.tx() as c:
                    c.execute("DELETE FROM kb_import_removals WHERE source_id = ? AND identity = ?",
                              (r["source_id"], who))
    except Exception:  # noqa: BLE001
        log.info("library removals not finished")


def _upload(db: DB, source_id: str, repo_root: str, claim: str, who: str, only: dict[str, str] | None = None, *,
            client=None, store=None, key_store=None, identity=None) -> dict:
    """Upload new and changed files (only those in ``only``, name to content
    digest, when given) in batches, then have the server prune the docs of files
    the folder no longer has. The server keys docs by (folder, path), so it is the
    source of truth: every sync sends every file's hash, and the local table (path
    to hash) only shows state, never decides what to skip. ``claim`` is the caller's operation token: it is renewed
    before each batch and before the prune and checked when the cache is written;
    a run that loses it simply stops (the next sync converges). ``key_store`` and
    ``identity`` are accepted for callers and unused: the import endpoint files
    docs by folder, not by repo. ``who`` is the identity the claim was made for;
    the sign-in must still be theirs. A file that fails stays out of the cache so
    the next call tries it again. Raises when nothing can be attempted."""
    result = {"uploaded": 0, "deleted": 0, "skipped": 0, "sent": False, "failed": []}
    reason = _off_reason(client, store)
    if reason is not None:
        raise ImportUploadOff(reason)
    src = _source(db, source_id)
    snap = _snapshot(client, store)
    if snap.who != who:
        raise RuntimeError("the signed-in account changed")
    sendable, skipped = _plan(src)
    if len(sendable) > MAX_FILES:       # never prune a folder the server cannot take whole
        raise TooManyFiles(len(sendable))
    result["skipped"] = len(skipped)
    # Every file's hash goes every time (the server answers unchanged or needs_content): the local
    # cache is never what decides to skip a file, so a late stale write corrects itself next sync.
    todo = [(name, _digest(data), data) for name, data in sendable.items()
            if only is None or only.get(name) == _digest(data)]
    lost = False
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        if not _renew(db, source_id, claim):
            lost = True
            break
        try:
            stored = _send(snap, source_id, batch)
        except AccountChanged:      # stop: what is left is neither sent nor marked failed
            lost = True
            break
        except Exception as e:  # noqa: BLE001
            log.info("imported files not uploaded (%s: %s)", type(e).__name__, e)
            result["failed"] += [n for n, _, _ in batch]
            continue
        with db.tx() as c:      # the local cache is written only while this run still holds the folder
            if not _holds(c, source_id, claim):
                lost = True
            else:
                for name, digest, _ in batch:
                    if name not in stored:
                        result["failed"].append(name)
                        continue
                    c.execute("INSERT OR REPLACE INTO kb_import_uploads (source_id, identity, external_id,"
                              " content_hash, doc_id) VALUES (?, ?, ?, ?, ?)",
                              (source_id, who, name, digest, stored[name][0]))
                    if stored[name][1] != "unchanged":
                        result["uploaded"] += 1
                        result["sent"] = True
        if snap.stopped:        # its docs are the snapshot's identity's; send no more
            lost = True
        if lost:
            break
    if not lost:
        try:
            if not _renew(db, source_id, claim):
                lost = True
            else:       # the server is the source of truth: it drops what the folder no longer has
                result["deleted"] = _prune(snap, source_id, sorted(sendable))
                with db.tx() as c:
                    if _holds(c, source_id, claim):
                        c.execute("DELETE FROM kb_import_uploads WHERE source_id = ? AND identity = ?"
                                  f" AND external_id NOT IN ({','.join('?' * len(sendable))})",
                                  (source_id, who, *sendable))
        except AccountChanged:
            lost = True
        except Exception as e:  # noqa: BLE001 - the next sync prunes again
            log.info("imported folder not pruned (%s)", type(e).__name__)
    with db.tx() as c:
        c.execute("UPDATE kb_import_library SET pending = ? WHERE source_id = ? AND op_token = ?",
                  (len(result["failed"]), source_id, claim))
    return result


def sync_upload(db: DB, source_id: str, repo_root: str, **deps) -> dict:
    """Bring the library up to date with an added folder (what :func:`brindle.kb.sync`
    calls). The switch is read first: while it is off, or in air-gap mode, nothing
    is requested, unfinished removals included, and they stay as they are. Only
    the signed-in identity's rows are touched: a folder another identity added is
    local to this one and gets nothing. It then finishes the identity's removals,
    claims the folder, and does nothing when another operation holds it or it is
    not ``in_library``. Never raises."""
    nothing = {"uploaded": 0, "deleted": 0, "skipped": 0, "sent": False, "failed": []}
    claim = None
    try:
        if _off_reason(deps.get("client"), deps.get("store")) is not None:
            return nothing
        who = _who(deps.get("client"), deps.get("store"))[3]
        drain_pending(db, client=deps.get("client"), store=deps.get("store"))
        claim = _claim(db, source_id, "sync", (IN_LIBRARY,), who)
        return _upload(db, source_id, repo_root, claim, who, **deps) if claim else nothing
    except Exception as e:  # noqa: BLE001
        log.info("imported folder not uploaded (%s)", type(e).__name__)
        return nothing
    finally:
        if claim:
            _release(db, source_id, claim)


class ManifestChanged(RuntimeError):
    """The folder changed since the preview; ``plan`` is a fresh one to confirm."""

    def __init__(self, plan: dict) -> None:
        super().__init__("the folder changed since it was listed")
        self.plan = plan


class UploadFailed(RuntimeError):
    """Some files could not be uploaded; the ones that were stay uploaded."""


def add_to_library(db: DB, source_id: str, repo_root: str, token: str | None = None, **deps) -> dict:
    """Upload exactly the files a :func:`dry_run` listed (its ``token``) and mark
    the folder added for the signed-in identity. ``ImportUploadOff`` when sending
    is off, ``ManifestChanged`` when the folder differs from what was listed,
    ``Busy`` when another add or a removal holds the folder, ``UploadFailed`` when
    a file was rejected (the folder is added only if something went up)."""
    src = _source(db, source_id)
    reason = _off_reason(deps.get("client"), deps.get("store"))
    if reason is not None:
        raise ImportUploadOff(reason)
    who = _who(deps.get("client"), deps.get("store"))[3]
    sendable, _ = _plan(src)
    if len(sendable) > MAX_FILES:
        raise TooManyFiles(len(sendable))
    if token != _token(sendable):
        raise ManifestChanged(dry_run(db, source_id, client=deps.get("client"), store=deps.get("store")))
    # ADDING is listed for the add that crashed: its lease must have lapsed, as for any claim.
    claim = _claim(db, source_id, "add", (None, IN_LIBRARY, ADDING), who, new_state=ADDING)
    if claim is None:
        raise Busy(source_id)
    failed: list[str] | str = []
    result: dict = {}
    try:
        result = _upload(db, source_id, repo_root, claim, who, {n: _digest(d) for n, d in sendable.items()}, **deps)
        failed = result["failed"]
    except Exception as e:  # noqa: BLE001
        failed = str(e) or type(e).__name__
    tracked = db.conn.execute("SELECT 1 FROM kb_import_uploads WHERE source_id = ? AND identity = ?",
                              (source_id, who)).fetchone()
    _release(db, source_id, claim, state=IN_LIBRARY, drop=not tracked)
    if failed:
        raise UploadFailed(failed if isinstance(failed, str) else f"not uploaded: {', '.join(failed)}")
    return result


def remove_from_library(db: DB, source_id: str, *, client=None, store=None) -> dict:
    """Delete the signed-in identity's docs of the folder server-side with the
    folder DELETE. The folder is claimed and moved to ``removing``; it stays
    ``removing`` (so the app can finish it, and every later index retries the
    DELETE) until the server confirms, then it is dropped. ``left`` counts the
    docs still in the library (0 once deleted). ``Busy`` while another operation
    holds the folder. Removal is not switch-gated: deleting reduces what is held,
    so it works with import_upload off; in air-gap mode, or signed out, it raises
    and the folder stays as it was."""
    _source(db, source_id)
    snap = _snapshot(client, store)
    who = snap.who
    if state(db, source_id, who) is None:
        return {"left": 0}
    claim = _claim(db, source_id, "remove", (IN_LIBRARY, REMOVING, ADDING), who, new_state=REMOVING)
    if claim is None:
        raise Busy(source_id)
    done = False
    try:
        done = _delete_folder(snap, source_id)
        if done:
            with db.tx() as c:
                c.execute("DELETE FROM kb_import_uploads WHERE source_id = ? AND identity = ?", (source_id, who))
    finally:
        _release(db, source_id, claim, drop=done)
    return {"left": 0 if done else max(1, db.conn.execute(
        "SELECT count(*) FROM kb_import_uploads WHERE source_id = ? AND identity = ?", (source_id, who)).fetchone()[0])}
