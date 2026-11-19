"""The plan library kept on this Mac (Settings › Library › Library location).

The person's ``store`` switch picks where their approved plans live: on, on
brindle's servers (:mod:`brindle.pro.analysis_docs` uploads them, as before);
off, here, one private JSON file per plan, and nothing is uploaded. An
enforced org or team value (:func:`analysis_policy.resolve_switch`, with the
person's team) beats the person's choice: :func:`effective` is the one place
storage, Settings and moves read it from.

Everything is kept per account: ``$BRINDLE_HOME/pro/library/<account>``, the
account a hash of the verified entitlement's org and subject. Another
account signed in on this Mac never lists, opens, shares or uploads them.
Plans approved while signed out go to ``signed-out``, which no account
reads; they stay there, never uploaded.

Each account's ``account.json`` keeps its location choice (learned from the
server's switch the first time it is needed) and its team in the org (from
the scopes it reads), so an approval or a Library read knows where plans go
without asking the server.

:func:`set_location` moves the plans: to the servers, each local plan is
uploaded and its local copy removed after a 200; to this Mac, each of the
person's own server docs is downloaded and the server copy deleted only once
the local file is written. A doc shared with a team or org keeps its server
copy (``server_id``) for as long as it is shared. Each plan keeps where it
came from (``identity``: its repo's identity, or ``repo_key``: the server's
key for it), so it goes back to the servers under the same repo, whatever
the working directory. Moves are idempotent: one local record per server doc.
"""

from __future__ import annotations

import hashlib
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
ACCOUNT_FILE = "account.json"
DIR = "library"
SIGNED_OUT = "signed-out"
MAX_RECORD = 4 * 1024 * 1024
ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
# What a listing carries of a doc (the server's _listed shape); files and bookkeeping stay out.
LISTED = ("title", "summary", "kind", "findings", "open_questions", "topics", "category", "created_at",
          "updated_at", "version", "source", "related", "supersedes", "superseded_by", "stale")


# -- whose library -------------------------------------------------------------------------------


def entitlement(store=None):
    """The verified entitlement of the account signed in here (offline), or None."""
    from brindle.pro import license

    try:
        return license.current(refresh=False, store=store)
    except Exception:  # noqa: BLE001 - signed out
        return None


def _account_dir_name(ent) -> str:
    if ent is None:
        return SIGNED_OUT
    return hashlib.sha256(f"{ent.org_id}\0{ent.sub}".encode()).hexdigest()[:32]


def _dir(store=None, create: bool = True, ent=...) -> Path | None:
    """This account's folder (``ent``: whose, the signed-in one by default)."""
    ent = entitlement(store) if ent is ... else ent
    base = private_dir(create)
    if base is None:
        return None
    d = base / DIR / _account_dir_name(ent)
    if create:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _path(doc_id: str, store=None, create: bool = False) -> Path | None:
    if not isinstance(doc_id, str) or not ID_RE.fullmatch(doc_id) or entitlement(store) is None:
        return None             # signed out: nothing kept here is read
    d = _dir(store, create)
    return d / f"{doc_id}.json" if d is not None else None


# -- the person's choice and team ----------------------------------------------------------------


def _account(store=None) -> dict:
    d = _dir(store, create=False)
    return (_read(d / ACCOUNT_FILE) if d is not None else None) or {}


def _save_account(store=None, **fields) -> None:
    if entitlement(store) is None:
        return
    d = _dir(store)
    _write(d / ACCOUNT_FILE, {**(_read(d / ACCOUNT_FILE) or {}), **fields, "at": time.time()})


def choice(store=None) -> str | None:
    """The location this account chose on this Mac, or None while unknown."""
    value = _account(store).get("location")
    return value if value in LOCATIONS else None


def remember(location: str, store=None) -> None:
    if location not in LOCATIONS:
        raise ValueError(f"location is one of {', '.join(LOCATIONS)}")
    _save_account(store, location=location)


def remember_group(group: str | None, store=None) -> None:
    """This account's team in its org (None: none, or not exactly one)."""
    _save_account(store, group=group)


def learn_group(client=None, store=None) -> str | None:
    """Read this account's team from the server (with its org's policy,
    cached for offline reads) and remember it."""
    from brindle.pro import analysis_docs

    client, store = analysis_docs._session(client, store)
    ent = entitlement(store)
    if ent is None or "analysis_docs_team" not in ent.features:
        remember_group(None, store)
        return None
    policy = analysis_policy.fetch(ent.org_id, client, store)
    group = analysis_docs._own_group(policy, analysis_docs._group_scopes(client, store))
    remember_group(group, store)
    return group


def _switch(location: str | None) -> dict | None:
    return {SWITCH: "on" if location == SERVER else "off"} if location in LOCATIONS else None


def person_choice(fetch: Callable[[], dict] | None = None, store=None) -> str | None:
    """:func:`choice`, or, while unknown, the server's ``store`` switch as
    ``fetch`` reads it (remembered when it says on or off). Failing to read
    it leaves the choice unknown."""
    got = choice(store)
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
        remember(got, store)
    except OSError:
        log.info("library location not remembered", exc_info=True)
    return got


def effective(user: dict | None, store=None, *, group=...) -> bool:
    """Whether ``store`` resolves on for this account with ``user``'s choice:
    the cached org policy and its team's (``group``: the remembered one,
    which Settings records, by default). Storage and moves both read it."""
    ent = entitlement(store)
    if ent is None:
        return analysis_policy.resolve_switch(SWITCH, user, policy={"switches": {}, "groups": {}},
                                              org="", plan="free")
    group = _account(store).get("group") if group is ... else group
    # Every cached enforcement counts (fail closed), the team's included.
    return analysis_policy.resolve_switch(SWITCH, user, group=group, policy=analysis_policy.load_cached(ent.org_id),
                                          org=ent.org_id, plan=ent.plan, store=store)


def location(store=None, *, fetch: Callable[[], dict] | None = None,
             learn: Callable[[], object] | None = None) -> str:
    """Where this account's plans live now: an enforced org or team value,
    else the person's choice, else the defaults (this Mac). Uses cached
    policy; ``fetch`` (the server's switches) and ``learn`` (the person's
    team, :func:`learn_group`) are asked only while unknown here."""
    if learn is not None and "group" not in _account(store) and entitlement(store) is not None:
        try:
            learn()
        except Exception:  # noqa: BLE001 - no team known: the org's values still apply
            log.info("library team not learned", exc_info=True)
    on = effective(_switch(person_choice(fetch, store)), store)
    return SERVER if on else LOCAL


# -- the plans --------------------------------------------------------------------------------------


def _title(files: list[dict]) -> str | None:
    for f in files:
        for line in str(f.get("md") or "").splitlines():
            if line.startswith("# "):
                return line[2:].strip() or None
    return files[0].get("name") if files else None


def add(files: list[dict], source: dict | None, *, repo_root: str | None = None, identity: str | None = None,
        repo_key: str | None = None, doc_id: str | None = None, meta: dict | None = None, store=None) -> dict:
    """Keep a doc for the signed-in account (signed out: in ``signed-out``,
    read by no account): ``files`` ({name, md}), its ``source``, where it
    came from (``repo_root`` and its ``identity``, or the server's
    ``repo_key``), and what a listing shows of it (``meta``). A new local id
    unless ``doc_id``. Raises on a failed write."""
    doc_id = doc_id or f"local-{secrets.token_hex(8)}"
    if not ID_RE.fullmatch(doc_id):
        raise ValueError(f"not a doc id: {doc_id!r}")
    path = _dir(store) / f"{doc_id}.json"
    record = {k: v for k, v in (meta or {}).items() if k in LISTED}
    record.update(doc_id=doc_id, files=list(files), source=dict(source or {}), repo_root=repo_root,
                  identity=identity, repo_key=repo_key, shared=[], server_id=None)
    record.setdefault("title", _title(files))
    record.setdefault("created_at", time.time())
    _write(path, record)
    return record


def get(doc_id: str, store=None) -> dict | None:
    path = _path(doc_id, store)
    return _read(path) if path is not None else None


def update(doc_id: str, store=None, **fields) -> dict:
    record = get(doc_id, store)
    if record is None:
        raise KeyError(doc_id)
    record.update(fields)
    _write(_path(doc_id, store), record)
    return record


def remove(doc_id: str, store=None) -> bool:
    path = _path(doc_id, store)
    try:
        if path is not None:
            path.unlink()
            return True
    except FileNotFoundError:
        pass
    return False


def records(store=None) -> list[dict]:
    """Every doc kept for the signed-in account, newest first (signed out: none)."""
    d = _dir(store, create=False) if entitlement(store) is not None else None
    if d is None:
        return []
    got = [r for p in d.glob("*.json") if p.name != ACCOUNT_FILE and not p.name.startswith(".")
           for r in [_read(p)] if r and isinstance(r.get("doc_id"), str)]
    return sorted(got, key=lambda r: -float(r.get("created_at") or 0))


def _holding(server_id: str, store=None) -> dict | None:
    """The one record of server doc ``server_id``: the one holding its server copy, or named by it."""
    return next((r for r in records(store) if server_id in (r.get("server_id"), r["doc_id"])), None)


def _matches(record: dict, query: dict) -> bool:
    return ((not query.get("topic") or query["topic"] in (record.get("topics") or []))
            and (not query.get("kind") or record.get("kind") == query["kind"])
            and (not query.get("session") or (record.get("source") or {}).get("session") == query["session"]))


def listed(query: dict | None = None, scope: dict | None = None, store=None) -> list[dict]:
    """The docs kept here as the server lists docs (``topic``, ``kind`` and
    ``session`` filter them), each in the person's own ``scope``."""
    return [{**{k: r[k] for k in LISTED if k in r}, "doc_id": r["doc_id"], "local": True,
             **({"scope": scope} if scope else {})}
            for r in records(store) if _matches(r, query or {})]


def view(record: dict, scope: dict | None = None) -> dict:
    """One doc as the server shows it (``GET /analysis-docs/{id}``)."""
    shared = [s for s in record.get("shared") or [] if isinstance(s, dict)]
    return {**{k: record[k] for k in LISTED if k in record}, "id": record["doc_id"], "local": True,
            "files": record.get("files") or [], "scopes": [*([scope] if scope else []), *shared],
            **({"scope": scope} if scope else {})}


# -- moving between here and the servers ---------------------------------------------------------------


def _upload(record: dict, client=None, store=None) -> str | None:
    """Put a kept doc on the server under the repo it came from; its server
    id, or None when it wasn't stored (or nothing says which repo)."""
    from brindle.pro import analysis_docs

    if not record.get("repo_key") and not record.get("identity"):
        return None
    up = analysis_docs.AnalysisDocs(record.get("repo_root") or "", client=client, store=store,
                                    identity=lambda: record.get("identity"), start_thread=False)
    return up.upload(analysis_docs.Doc(files=record.get("files") or [], source=record.get("source") or {}),
                     repo_key=record.get("repo_key"))


def server_copy(doc_id: str, client=None, store=None) -> str:
    """The server id of kept doc ``doc_id``, uploading it first if it has none
    (sharing a doc puts that one doc on the server). Raises RuntimeError when
    it can't be stored."""
    record = get(doc_id, store)
    if record is None:
        raise KeyError(doc_id)
    if record.get("server_id"):
        return record["server_id"]
    server_id = _upload(record, client, store)
    if not server_id:
        raise RuntimeError("the plan couldn't be put on brindle's servers to share it")
    update(doc_id, store, server_id=server_id)
    return server_id


def _on_server(server_id: str, client, store) -> bool:
    """Whether the server still has ``server_id``: True only on a 200, False
    on a 404. Any other answer raises, so the local copy is kept."""
    from brindle.pro import analysis_docs

    try:
        analysis_docs.api("GET", f"/analysis-docs/{quote(server_id, safe='')}", client=client, store=store)
    except analysis_docs.Refused as e:
        if e.status == 404:
            return False
        raise
    return True


def _to_server(client, store) -> tuple[int, int]:
    moved = left = 0
    for record in records(store):
        try:
            if not record.get("server_id") or not _on_server(record["server_id"], client, store):
                if not _upload(record, client, store):
                    left += 1
                    continue
            remove(record["doc_id"], store)     # the server copy is the doc now
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
        server_id = listed_doc.get("doc_id") if isinstance(listed_doc, dict) else None
        if not isinstance(server_id, str) or not ID_RE.fullmatch(server_id):
            continue
        path = f"/analysis-docs/{quote(server_id, safe='')}"
        shared = []
        try:
            full = api("GET", path)
            shared = [s for s in full.get("scopes") or [] if isinstance(s, dict) and s.get("kind") in ("group", "org")]
            held = _holding(server_id, store)
            meta = {**listed_doc, **full}
            if held is None:
                held = add(full.get("files") or [], full.get("source"), doc_id=server_id, meta=meta,
                           repo_key=full.get("repo_key"), store=store)
            else:                               # one record per server doc: refresh it
                held = update(held["doc_id"], store, files=full.get("files") or held.get("files") or [],
                              repo_key=full.get("repo_key") or held.get("repo_key"),
                              **{k: meta[k] for k in LISTED if k in meta})
            # the server copy stands until deleted below; while shared it stays
            update(held["doc_id"], store, server_id=server_id, shared=shared)
        except Exception:  # noqa: BLE001 - not written here: the server copy stays
            log.info("plan %s not downloaded", server_id, exc_info=True)
            left += 1
            continue
        if shared:
            moved += 1
            continue
        try:
            api("DELETE", path)
        except Exception:  # noqa: BLE001 - kept in both places; the next move deletes it
            log.info("plan %s kept on the server too", server_id, exc_info=True)
            left += 1
            continue
        update(held["doc_id"], store, server_id=None)
        moved += 1
    return moved, left


def set_location(target: str, *, client=None, store=None) -> dict:
    """Move this account's library to ``target`` (local or server) and
    remember it: ``{location, moved, left}``, ``left`` counting plans not
    moved yet (each still where it was, and moved by the next call, which
    may name the same location). Raises :class:`analysis_docs.Locked` when
    the person's org or team enforces the other."""
    from brindle.pro import analysis_docs

    if target not in LOCATIONS:
        raise ValueError(f"location is one of {', '.join(LOCATIONS)}")
    client, store = analysis_docs._session(client, store)
    ent = analysis_docs.require_entitlement(store)
    try:
        learn_group(client, store)
    except Exception:  # noqa: BLE001 - the remembered team stands
        log.info("library team not refreshed", exc_info=True)
    on = effective(_switch(target), store)
    if on != (target == SERVER):
        raise analysis_docs.Locked(f"your organisation keeps plans {'on brindle servers' if on else 'on this Mac'} "
                                   "for everyone")
    try:                                        # the server's switch says the same, for its own checks
        switches = {**analysis_docs.fetch_switches(client, store), SWITCH: "on" if target == SERVER else "off"}
        analysis_docs.api("PUT", "/me/analysis-docs/settings", {"switches": switches}, client=client, store=store)
    except Exception:  # noqa: BLE001
        if target == SERVER:
            raise
        log.info("server store switch not updated", exc_info=True)
    remember(target, store)
    moved, left = _to_server(client, store) if target == SERVER else _to_local(ent.sub, client, store)
    return {"location": target, "moved": moved, "left": left}
