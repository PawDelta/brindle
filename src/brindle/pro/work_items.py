"""Work items (Jira, Azure DevOps) linked to a Library design.

The server (pawdelta-web ``tracker_routes``) holds the org's tracker connection
and does every tracker call; this module is the client side of its routes:

    GET    /orgs/{org}/analysis-docs/{doc}/work-items            linked items and states
    POST   /orgs/{org}/analysis-docs/{doc}/work-items            create (design, task, passage)
    POST   /orgs/{org}/analysis-docs/{doc}/work-items/links      link an existing item
    DELETE /orgs/{org}/analysis-docs/{doc}/work-items/links/{id} unlink
    GET    /orgs/{org}/integrations                              admin: connections and settings
    PUT    /orgs/{org}/integrations/settings                     admin: mapping and switches
    POST   /orgs/{org}/integrations/{tracker}/test                admin
    DELETE /orgs/{org}/integrations/{tracker}                     admin

Every function takes ``api(method, path, body=None)``, the daemon's ``docs_api``
bound to a repo, so nothing here reaches the network on its own.

Reads are cached and fail soft: when the server or the tracker can't be
reached, ``items`` answers the last read marked stale instead of an error, so
a design always shows its epic. Writes are never cached or retried; a failed
link or create raises, because the person must know it didn't happen.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable
from urllib.parse import quote, urljoin

from brindle.pro import _files

Api = Callable[..., dict]

TRACKERS = ("jira", "azure_devops")
KINDS = ("design", "task", "passage")
CACHE_FILE = "work-items.json"
MAX_CACHE = 600
MAX_BYTES = 4 * 1024 * 1024
_lock = threading.RLock()          # around every read-modify-write of the cache file


def _base(org_id: str, doc_id: str, tail: str = "") -> str:
    return f"/orgs/{quote(org_id, safe='')}/analysis-docs/{quote(doc_id, safe='')}/work-items{tail}"


def unreachable(e: BaseException) -> bool:
    """The server or tracker couldn't answer (offline, 5xx, 502 from a tracker): a read may fall back to
    its cache. A refusal the person must see (402, 403, 404, 409, 422) never does."""
    from brindle.pro import auth

    if isinstance(e, (auth.TransportError, OSError, TimeoutError)):
        return True
    status = getattr(e, "http_status", None) or getattr(e, "status", None)
    return isinstance(status, int) and status >= 500


# -- the cache ----------------------------------------------------------------------------------


def _path():
    d = _files.private_dir()
    return d / CACHE_FILE


def _load() -> dict:
    try:
        raw = _files.read_private(_path(), MAX_BYTES)
        data = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 - a damaged cache is an empty one, never an error
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("docs"), dict) else {}


def _save(data: dict) -> None:
    docs = data["docs"]
    for old in sorted(docs, key=lambda k: docs[k].get("read_at") or 0)[:max(0, len(docs) - MAX_CACHE)]:
        del docs[old]
    try:
        _files.write_private(_path(), json.dumps(data, separators=(",", ":")).encode())
    except Exception:  # noqa: BLE001 - not caching must not fail a read that succeeded
        pass


def _key(who: str, org_id: str, doc_id: str) -> str:
    # The signed-in account (server and subject) is part of the key: what was read for one member is never
    # shown to another who signs in on this machine.
    return f"{who}|{org_id}/{doc_id}"


def cached(org_id: str, doc_id: str, *, who: str = "") -> dict | None:
    with _lock:
        return _load().get("docs", {}).get(_key(who, org_id, doc_id))


def _store(who: str, org_id: str, doc_id: str, change: Callable[[dict], dict]) -> dict:
    """Apply ``change`` to the doc's cached record and keep it."""
    with _lock:
        data = _load() or {"docs": {}}
        k = _key(who, org_id, doc_id)
        rec = change(dict(data["docs"].get(k) or {"items": [], "connected": {}}))
        data["docs"][k] = rec
        _save(data)
        return rec


def _forget(who: str, org_id: str, doc_id: str) -> None:
    with _lock:
        data = _load()
        if data.get("docs", {}).pop(_key(who, org_id, doc_id), None) is not None:
            _save(data)


# -- one design ---------------------------------------------------------------------------------


def summary(items: list[dict]) -> dict:
    """The design's own item (its epic) and how many of the items under it are done."""
    epic = next((i for i in items if (i.get("target") or {}).get("kind") == "design"), None)
    kids = [i for i in items if i is not epic]
    return {"epic": epic, "done": sum(1 for i in kids if i.get("done")), "total": len(kids)}


def _view(rec: dict, **extra) -> dict:
    items = [i for i in rec.get("items") or [] if isinstance(i, dict)]
    return {"items": items, "connected": rec.get("connected") or {}, "read_at": rec.get("read_at"),
            **summary(items), **extra}


DENIED = (401, 403, 404)


def items(org_id: str, doc_id: str, *, api: Api, who: str = "", now: Callable[[], float] = time.time) -> dict:
    """The design's linked items with their states. Unreachable: the last read, every item marked stale
    (``unreachable`` true; an empty list when nothing was ever read). A refusal saying this account may not
    see the design drops what was kept for it."""
    try:
        got = api("GET", _base(org_id, doc_id))
    except Exception as e:  # noqa: BLE001
        if not unreachable(e):
            if (getattr(e, "http_status", None) or getattr(e, "status", None)) in DENIED:
                _forget(who, org_id, doc_id)
            raise
        rec = cached(org_id, doc_id, who=who) or {}
        stale = dict(rec, items=[dict(i, stale=True) for i in rec.get("items") or [] if isinstance(i, dict)])
        return _view(stale, unreachable=True)
    listed = [i for i in got.get("items") or [] if isinstance(i, dict)]
    connected = got.get("connected") if isinstance(got.get("connected"), dict) else {}
    rec = _store(who, org_id, doc_id, lambda r: dict(r, items=listed, connected=connected, read_at=now()))
    return _view(rec, unreachable=False)


def link(org_id: str, doc_id: str, item: str, *, api: Api, who: str = "", tracker: str | None = None,
         target: dict | None = None) -> dict:
    """Link an existing item (a key like BRN-88 or AB#4810, or a pasted link) to the design, a section or a task."""
    item = (item or "").strip()
    if not item:
        raise ValueError("name the item: its key or its link")
    if tracker is not None and tracker not in TRACKERS:
        raise ValueError("tracker is jira or azure_devops")
    body: dict = {"item": item, "target": target or {"kind": "design"}}
    if tracker:
        body["tracker"] = tracker
    made = api("POST", _base(org_id, doc_id, "/links"), body)
    _store(who, org_id, doc_id, lambda r: dict(r, items=[*(r.get("items") or []), made]))
    return made


def create(org_id: str, doc_id: str, kind: str, *, api: Api, who: str = "", task: str | None = None,
           section: str | None = None, quote_text: str | None = None, title: str | None = None) -> dict:
    """Make items from the design (its epic), one task of its split (every task without an item when ``task`` is
    left out) or a passage (a quote from ``section``). The server keeps what it made if a later one fails."""
    if kind not in KINDS:
        raise ValueError("kind is design, task or passage")
    body: dict = {"kind": kind}
    for k, v in (("task", task), ("section", section), ("quote", quote_text), ("title", title)):
        if v:
            body[k] = v
    got = api("POST", _base(org_id, doc_id), body)
    made = [i for i in got.get("created") or [] if isinstance(i, dict)]
    if made:
        _store(who, org_id, doc_id, lambda r: dict(r, items=[*(r.get("items") or []), *made]))
    return got


def unlink(org_id: str, doc_id: str, link_id: str, *, api: Api, who: str = "") -> dict:
    got = api("DELETE", _base(org_id, doc_id, f"/links/{quote(link_id, safe='')}"))
    _store(who, org_id, doc_id, lambda r: dict(r, items=[i for i in r.get("items") or []
                                                         if not (isinstance(i, dict) and i.get("link_id") == link_id)]))
    return got


FRESH_FOR = 300.0      # seconds a design's summary is trusted before the next list reads it again


def summaries(org_id: str, doc_ids: list[str], *, api: Api | None = None, who: str = "", fetch: int = 0,
              now: Callable[[], float] = time.time) -> dict:
    """``{"items": {doc_id: {epic, done, total, read_at}}, "remaining": [doc_id]}`` for the Library list and
    board. A design never read, or last read over ``FRESH_FOR`` seconds ago, is read now, up to ``fetch`` of them
    (never read first); the rest are named in ``remaining`` for the caller to ask again. One that can't be read
    keeps its last value marked ``stale``; one never read, or refused, stays out."""
    data = _load().get("docs", {})
    out: dict[str, dict] = {}
    due: list[tuple[float, str]] = []
    t = now()
    for d in doc_ids:
        rec = data.get(_key(who, org_id, d))
        if rec is not None:
            out[d] = _brief(_view(rec), rec)
        if rec is None or t - (rec.get("read_at") or 0) > FRESH_FOR:
            due.append((-1.0 if rec is None else rec.get("read_at") or 0.0, d))
    due.sort()
    attempt = [d for _, d in due][:fetch] if api else []
    for d in attempt:
        try:
            got = items(org_id, d, api=api, who=who, now=now)
        except Exception:  # noqa: BLE001 - one design's refusal leaves the others alone
            out.pop(d, None)
            continue
        if got["unreachable"]:
            if d in out:
                out[d] = dict(out[d], stale=True)
        else:
            out[d] = _brief(got, got)
    return {"items": out, "remaining": [d for _, d in due if api and d not in attempt]}


def _brief(view: dict, rec: dict) -> dict:
    return {"epic": view["epic"], "done": view["done"], "total": view["total"], "read_at": rec.get("read_at")}


# -- Settings › Integrations --------------------------------------------------------------------

SETTING_KEYS = ("repos", "types", "done_states", "allow", "who_can_create")


def integrations(org_id: str, *, api: Api) -> dict:
    """Connections (never a token) and settings; admin only, which the server checks."""
    return api("GET", f"/orgs/{quote(org_id, safe='')}/integrations")


def save_settings(org_id: str, settings: dict, *, api: Api) -> dict:
    """Replace the org's mapping and switches. Only the settings' own keys are sent; the server validates them."""
    unknown = sorted(set(settings) - set(SETTING_KEYS))
    if unknown:
        raise ValueError(f"not a setting: {', '.join(unknown)}")
    return api("PUT", f"/orgs/{quote(org_id, safe='')}/integrations/settings", dict(settings))


def check_connection(org_id: str, tracker: str, *, api: Api) -> dict:
    if tracker not in TRACKERS:
        raise ValueError("tracker is jira or azure_devops")
    return api("POST", f"/orgs/{quote(org_id, safe='')}/integrations/{tracker}/test", {})


def disconnect(org_id: str, tracker: str, *, api: Api) -> dict:
    if tracker not in TRACKERS:
        raise ValueError("tracker is jira or azure_devops")
    return api("DELETE", f"/orgs/{quote(org_id, safe='')}/integrations/{tracker}")


_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,139}$")


def local_repos(roots: list[str], org_id: str, *, key_of: Callable[[str], str | None] | None = None) -> list[dict]:
    """``[{repo, repo_key, repo_root}]`` for the repos on this machine, the way the server tells a design's repo:
    the org-key HMAC of the repo's identity (never the path). One that has no identity or key, or a name the
    server wouldn't take, is left out."""
    if key_of is None:
        from brindle.pro.orgkey import OrgKeys, repo_identity

        keys = OrgKeys()

        def key_of(root: str) -> str | None:
            identity = repo_identity(root)
            return keys.get(org_id).repo_key(identity) if identity else None
    out = []
    for root in roots:
        name = root.rstrip("/").rsplit("/", 1)[-1]
        try:
            key = key_of(root)
        except Exception:  # noqa: BLE001 - no org key right now: the repo just can't be mapped yet
            continue
        if key and _REPO_NAME.match(name):
            out.append({"repo": name, "repo_key": key, "repo_root": root})
    return out


def connect_url(base: str, org_id: str) -> str:
    """The account page where an admin signs in to the tracker. Connecting is a browser OAuth flow on
    pawdelta.com (the provider redirects back to the browser that started it), never the app's."""
    return urljoin(base, f"/brindle/account/integrations?org_id={quote(org_id, safe='')}")
