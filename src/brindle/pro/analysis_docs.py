"""The personal analysis-doc library client (brindle Pro; contract in
``docs/analysis-docs-plan.md``).

Docs are free-form files written for large work in the branch's docs folder
outside the repo (:func:`brindle.pro.analysis_brief.docs_dir`). When the
branch merges they are uploaded together, with their source, to ``POST
/analysis-docs`` with scope ``user``, from a small bounded queue drained by
one background thread, so brindle never waits on the network and nothing
raises into it. When the queue is full, uploads are dropped. After a 200 the
local copies are deleted; on any failure they stay.

Nothing is sent in air-gap mode, without the ``analysis_docs`` entitlement
(the docs then stay in the folder, a Knowledge source), or when the ``store``
switch resolves to off (:func:`resolve_switch`). The first upload prints a
one-time notice naming ``brindle docs off``. ``repo_key`` is the org-key HMAC
of the repo's identity (:mod:`brindle.pro.orgkey`); without an org key or an
identity nothing is sent.

The ``brindle docs`` commands (:func:`api`, :func:`set_switch`) talk to the
same endpoints for listing, showing, deleting, sharing and exporting.

Sessions also read and edit docs already in the library (see "shared
editing" below): reads are pinned to a version, edits of a living doc go to a
server-side draft published on merge, and a session is told when a doc it
read has a newer version.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from brindle.pro.orgkey import OrgKeys, repo_identity

log = logging.getLogger(__name__)

FEATURE = "analysis_docs"
QUEUE_SIZE = 64
FLUSH_AT_EXIT = 2.0
SEND_WAIT = 8.0
MAX_FILE = 512 * 1024
MAX_FILES = 20
SWITCHES = ("standard", "store", "share", "assist", "prompt_analysis")
PLAN_DEFAULT_ON = frozenset({"store", "standard"})      # on for Pro, off otherwise
NOTICE_MARK = "analysis-docs-notice"
NOTICE = ("brindle stores your analysis docs encrypted in your personal library once their "
          "branch merges. Turn this off any time with `brindle docs off`.")


# -- switches -----------------------------------------------------------------------------------


def resolve_switch(name: str, user: dict | None = None) -> bool:
    """The effective value of switch ``name``. Today: the user's own choice
    (``"on"``/``"off"``), else the plan default (``store`` and ``standard`` on
    for Pro, the rest off). Team and enterprise defaults and enforcement slot
    in here, ahead of the user's choice."""
    choice = (user or {}).get(name)
    if choice in ("on", "off"):
        return choice == "on"
    return name in PLAN_DEFAULT_ON


# -- reading docs --------------------------------------------------------------------------------


@dataclass
class Doc:
    files: list[dict]                                    # {name, md}, named below the docs folder
    source: dict                                         # {kind, ref}
    paths: list[tuple[Path, bytes]] = field(default_factory=list)   # each local copy and what was sent


def _read(path: Path) -> bytes | None:
    """The whole file, or None when it is over MAX_FILE or isn't UTF-8 text."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_FILE + 1)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return data if len(data) <= MAX_FILE else None


def collect(repo_root: str, branch: str, source: dict) -> list[Doc]:
    """The doc written for ``branch``: every document in its docs folder (as
    a Knowledge folder source would read them) small enough to upload whole.
    A larger file is left out, and so never deleted."""
    from brindle import kb
    from brindle.pro.analysis_brief import docs_dir

    folder = docs_dir(repo_root, branch)
    files, paths = [], []
    try:
        if folder.is_symlink() or not folder.is_dir():
            return []
        for name, p in kb._folder_paths(folder):
            data = _read(p)
            if data is not None and len(files) < MAX_FILES:
                files.append({"name": name, "md": data.decode("utf-8")})
                paths.append((p, data))
    except OSError:
        log.info("analysis doc for %s unreadable; skipped", branch)
        return []
    return [Doc(files=files, source=source, paths=paths)] if files else []


def _delete_local(doc: Doc) -> None:
    """Remove the uploaded local copies, leaving any file changed since it was read."""
    for p, sent in doc.paths:
        try:
            if _read(p) == sent:
                p.unlink()
        except OSError:
            log.info("analysis doc copy %s not removed", p.name)


# -- the uploader ----------------------------------------------------------------------------------


def _say(text: str) -> None:
    import sys

    print(f"brindle: {text}", file=sys.stderr)


class AnalysisDocs:
    def __init__(self, repo_root: str, *, client=None, store=None, key_store=None,
                 identity: Callable[[], str | None] | None = None,
                 notice: Callable[[str], None] | None = None,
                 start_thread: bool = True) -> None:
        self.repo_root = repo_root
        self._client, self._store = client, store
        self.keys = OrgKeys(store=store, client=client, key_store=key_store)
        self._identity = identity or (lambda: repo_identity(repo_root))
        self._notice = notice or _say
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.dropped = 0
        self._thread = None
        if start_thread:
            self._thread = threading.Thread(target=self._sender, name="brindle-pro-analysis-docs",
                                            daemon=True)
            self._thread.start()
            atexit.register(self.flush, FLUSH_AT_EXIT)

    def _deps(self):
        from brindle.pro import auth, credentials

        if self._store is None:
            self._store = credentials.default_store()
        if self._client is None:
            creds = self._store.load() or {}
            self._client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=SEND_WAIT))
        return self._client, self._store

    def entitlement(self):
        """The verified entitlement when it includes the library, else None
        (always None in air-gap mode). Offline verification only."""
        from brindle import airgap

        if airgap.enabled():
            return None
        try:
            from brindle.pro import license

            ent = license.current(refresh=False, store=self._store)
            return ent if FEATURE in ent.features and not ent.in_grace else None
        except Exception:  # noqa: BLE001 - fail closed: send nothing
            return None

    def submit(self, docs: list[Doc]) -> None:
        """Queue ``docs`` for upload. Never blocks, never raises."""
        try:
            if not docs or self.entitlement() is None:
                return
            for d in docs:
                try:
                    self.queue.put_nowait(d)
                except queue.Full:
                    self.dropped += 1
        except Exception:  # noqa: BLE001
            log.info("analysis doc submit skipped", exc_info=True)

    def _store_on(self) -> bool:
        """Whether the user's ``store`` switch is on. Failing to read it counts as off."""
        client, store = self._deps()
        try:
            return resolve_switch("store", fetch_switches(client, store))
        except Exception:  # noqa: BLE001
            return False

    def _send_one(self, doc: Doc) -> None:
        try:
            from brindle.pro import auth

            ent = self.entitlement()
            identity = self._identity() if ent else None
            if ent is None or identity is None or not self._store_on():
                return
            key = self.keys.get(ent.org_id)
            self._first_notice()
            client, store = self._deps()
            body = {"scope": {"kind": "user", "id": ent.sub}, "files": doc.files, "source": doc.source,
                    "repo_key": key.repo_key(identity)}
            status, _ = auth.authed(client, store, "POST", "/analysis-docs", auth.JSONBody(body))
            if status != 200:
                log.info("analysis doc not stored (HTTP %s)", status)
                return
            _delete_local(doc)
        except Exception as e:  # noqa: BLE001
            log.info("analysis doc not sent (%s)", type(e).__name__)

    def _first_notice(self) -> None:
        from brindle.pro._files import private_dir, write_private

        try:
            mark = private_dir() / NOTICE_MARK
            if mark.exists():
                return
            write_private(mark, b"1")
        except Exception:  # noqa: BLE001 - at worst the notice shows again
            log.info("analysis doc notice marker unavailable", exc_info=True)
        self._notice(NOTICE)

    def _sender(self) -> None:
        while True:
            doc = self.queue.get()
            try:
                self._send_one(doc)
            finally:
                self.queue.task_done()

    def flush(self, wait: float = FLUSH_AT_EXIT) -> bool:
        """Wait up to ``wait`` seconds for queued uploads (or, with no sender
        thread, send them inline). True when the queue drained."""
        if self._thread is None:
            while True:
                try:
                    doc = self.queue.get_nowait()
                except queue.Empty:
                    return True
                self._send_one(doc)
                self.queue.task_done()
        waiter = threading.Thread(target=self.queue.join, daemon=True)
        waiter.start()
        waiter.join(wait)
        return not self.queue.unfinished_tasks


_instances: dict[str, AnalysisDocs] = {}
_instances_lock = threading.Lock()


def on_merged(repo_root: str, docs: list[Doc]) -> None:
    """Called once a branch has merged. Never blocks and never raises."""
    try:
        with _instances_lock:
            up = _instances.get(repo_root)
            if up is None:
                up = _instances[repo_root] = AnalysisDocs(repo_root)
        up.submit(docs)
    except Exception:  # noqa: BLE001
        log.info("analysis docs not queued", exc_info=True)


# -- the library API (brindle docs) ------------------------------------------------------------------


def _session(client=None, store=None):
    from brindle.pro import auth, credentials

    store = store or credentials.default_store()
    if client is None:
        creds = store.load() or {}
        client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=SEND_WAIT))
    return client, store


def require_entitlement(store=None):
    """The verified entitlement, or ``RuntimeError`` (before anything is sent)
    in air-gap mode or without the ``analysis_docs`` feature."""
    from brindle import airgap
    from brindle.pro import license

    if airgap.enabled():
        raise RuntimeError("air-gap mode is on: nothing is sent")
    try:
        ent = license.current(refresh=False, store=store)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("not logged in to brindle Pro (`brindle account login`)") from e
    if FEATURE not in ent.features or ent.in_grace:
        raise RuntimeError("your plan has no analysis doc library (the `analysis_docs` entitlement)")
    return ent


def _call(method: str, path: str, body: dict | None = None, *, client=None, store=None) -> tuple[int, dict]:
    """Call a Bearer endpoint once entitled: ``(status, response)``."""
    from brindle.pro import auth

    client, store = _session(client, store)
    require_entitlement(store)
    status, resp = auth.authed(client, store, method, path, auth.JSONBody(body) if body is not None else None)
    return status, resp if isinstance(resp, dict) else {}


def api(method: str, path: str, body: dict | None = None, *, client=None, store=None) -> dict:
    """Call a Bearer endpoint; raises ``RuntimeError`` with a readable reason
    on a non-200 answer, ``auth.AuthError`` when not logged in or offline."""
    status, resp = _call(method, path, body, client=client, store=store)
    if status != 200:
        raise RuntimeError(resp.get("error") if isinstance(resp, dict) and resp.get("error") else f"HTTP {status}")
    return resp if isinstance(resp, dict) else {}


def fetch_switches(client=None, store=None) -> dict:
    sw = api("GET", "/me/analysis-docs/settings", client=client, store=store).get("switches")
    return sw if isinstance(sw, dict) else {}


def set_switch(on: bool, *, client=None, store=None) -> dict:
    """Set the user's ``store`` switch, keeping the others."""
    switches = {**fetch_switches(client, store), "store": "on" if on else "off"}
    return api("PUT", "/me/analysis-docs/settings", {"switches": switches}, client=client, store=store)


# -- shared editing ----------------------------------------------------------------------------------
#
# The server holds every doc as a chain of versions. A session reads a doc at
# a version (pinned in its receipt) and edits a living doc in a draft on the
# server tied to its session and branch, on top of that version. The draft
# publishes when the branch merges and is dropped when the branch is removed
# unmerged. The server combines edits to different sections; a clash on the
# same section is kept as a draft and sent to the session's supervisor to
# reconcile. Snapshot docs are never edited: new work supersedes them.
#
# The session root's receipt keeps each draft, keyed by branch and doc, with
# its state: "open" while the branch is unmerged and "clash" once sent to the
# supervisor. A publish or drop the server hasn't confirmed yet is owed: it
# waits in one queue of its own (OWED, apart from any session, so it outlives
# the session's agents) and is sent again at the end of any agent's turn
# (:func:`on_turn`) until the server answers.

DRAFT_ITEM = "analysis_draft"          # on a session: [branch, doc id] -> {"state", "by": the drafter}
OWED = "analysis-docs:owed"            # the queue's receipt: [session, branch, doc id] -> {"op", "by"}
OWED_ITEM = "analysis_draft_owed"
TOLD_ITEM = "analysis_doc_told"        # the newest version of a doc the agent was told about
CHECKED_ITEM = "analysis_docs_checked"  # when the agent's reads were last checked
CHECK_EVERY = 300.0                    # seconds between an agent's checks of its reads
SNAPSHOT = ("{doc} is a snapshot doc: it is never edited. Write a new doc for this work instead; "
            "it supersedes the old one.")


def _doc_path(doc_id: str, tail: str = "") -> str:
    from urllib.parse import quote

    return f"/analysis-docs/{quote(doc_id, safe='')}{tail}"


def _pins(db, agent_id: str, kind: str) -> dict[str, str]:
    from brindle import knowledge

    return {item: value for k, item, value in knowledge.receipt(db, agent_id) if k == kind}


def _unpin(db, agent_id: str, kind: str, doc_id: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM context_receipts WHERE task_id = ? AND item_kind = ? AND item_id = ?",
                  (agent_id, kind, doc_id))


def _version(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_doc(db, agent_id: str, doc_id: str, *, client=None, store=None) -> dict:
    """``doc_id`` at its newest version, pinned: the version is recorded as
    what ``agent_id`` read (as a brief's citations are)."""
    from brindle import knowledge
    from brindle.pro.analysis_brief import DOC_ITEM

    doc = api("GET", _doc_path(doc_id), client=client, store=store)
    knowledge.record_receipt(db, agent_id, [(DOC_ITEM, doc_id, str(doc.get("version", "")))])
    return doc


def drafts(db, session: str) -> dict[tuple[str, str], dict]:
    """The session's drafts: ``(branch, doc id)`` -> ``{"state", "by"}``."""
    out = {}
    for key, value in _pins(db, session, DRAFT_ITEM).items():
        try:
            branch, doc_id = json.loads(key)
            out[(branch, doc_id)] = json.loads(value)
        except (TypeError, ValueError):
            continue
    return out


def _set_draft(db, session: str, branch: str, doc_id: str, state: str, by: str) -> None:
    from brindle import knowledge

    knowledge.record_receipt(db, session, [(DRAFT_ITEM, json.dumps([branch, doc_id]),
                                            json.dumps({"state": state, "by": by}))])


def save_draft(db, agent, branch: str, doc_id: str, files: list[dict], *, clashed_branch: str | None = None,
               client=None, store=None) -> str:
    """Put ``agent``'s edit of ``doc_id`` in its session's draft for
    ``branch``, on top of the version it read. When the session's supervisor
    reconciles a clash (on ``clashed_branch``, which may be left out when
    only one draft of the doc clashes), that draft is rebased instead and
    published at once (its branch has merged). Raises ``RuntimeError`` with
    the reason."""
    from brindle import autopilot
    from brindle.pro.analysis_brief import DOC_ITEM

    base = _version(_pins(db, agent.id, DOC_ITEM).get(doc_id))
    if base is None:
        raise RuntimeError(f"read {doc_id} first: an edit goes on top of the version you read")
    session = autopilot.root_of(db, agent.id)
    clashed = sorted(b for (b, d), s in drafts(db, session).items()
                     if d == doc_id and s.get("state") == "clash") if session == agent.id else []
    if clashed_branch is not None and clashed_branch not in clashed:
        raise RuntimeError(f"no clashed draft of {doc_id} on {clashed_branch}")
    if clashed_branch is None and len(clashed) > 1:
        raise RuntimeError(f"drafts of {doc_id} clash on branches {', '.join(clashed)}: name the one "
                           "to reconcile (clashed_branch)")
    target = clashed_branch or (clashed[0] if clashed else None)
    body = {"session": session, "branch": target or branch, "base_version": base, "files": files}
    status, resp = _call("PUT", _doc_path(doc_id, "/drafts"), body, client=client, store=store)
    if status == 409 and resp.get("error") == "snapshot":
        raise RuntimeError(SNAPSHOT.format(doc=doc_id))
    if status != 200:
        raise RuntimeError(resp.get("error") or f"HTTP {status}")
    if target:
        state = _settle(db, session, target, doc_id, "publish", agent.id, client, store)
        return {"done": f"Reconciled {doc_id} on {target}: published.",
                "clash": f"{doc_id} on {target} still clashes: see the message sent to you."
                }.get(state, f"Reconciled {doc_id} on {target}; brindle sends it again until the server takes it.")
    _set_draft(db, session, branch, doc_id, "open", agent.id)
    return f"Draft of {doc_id} saved on version {base}; it publishes when {branch} merges."


def owed(db) -> dict[tuple[str, str, str], dict]:
    """The queue of unconfirmed publishes and drops: ``(session, branch, doc
    id)`` -> ``{"op", "by"}``."""
    out = {}
    for key, value in _pins(db, OWED, OWED_ITEM).items():
        try:
            session, branch, doc_id = json.loads(key)
            out[(session, branch, doc_id)] = json.loads(value)
        except (TypeError, ValueError):
            continue
    return out


def _settle(db, session: str, branch: str, doc_id: str, op: str, by: str, client=None, store=None) -> str:
    """``op`` ("publish" or "drop") the session's draft of ``doc_id`` on
    ``branch``: owed (queued in OWED) until the server confirms. Returns
    "done", "clash" (the session's supervisor is told to reconcile it, never
    the person) or ``op`` (still owed). Never raises."""
    from brindle import gates, knowledge

    key = json.dumps([session, branch, doc_id])
    _unpin(db, session, DRAFT_ITEM, json.dumps([branch, doc_id]))
    knowledge.record_receipt(db, OWED, [(OWED_ITEM, key, json.dumps({"op": op, "by": by}))])
    try:
        status, resp = _call("POST", _doc_path(doc_id, f"/drafts/{op}"), {"session": session, "branch": branch},
                             client=client, store=store)
    except Exception as e:  # noqa: BLE001 - owed: settled at a later turn
        log.info("analysis doc draft not settled (%s)", type(e).__name__)
        return op
    if status == 200 or (op == "publish" and status == 409 and resp.get("error") == "clash"):
        _unpin(db, OWED, OWED_ITEM, key)
    if status == 200:
        version = _version(resp.get("version"))
        if op == "publish" and version is not None:   # the drafter made this version: nothing to tell it
            knowledge.record_receipt(db, by, [(TOLD_ITEM, doc_id, str(version))])
            tell_readers(db, doc_id, version)
        return "done"
    if op == "publish" and status == 409 and resp.get("error") == "clash":
        clashes = [c for c in resp.get("clashes") or [] if isinstance(c, dict)]
        where = ", ".join(json.dumps(str(c.get("section", ""))) for c in clashes) or "a section"
        newest = max((_version(c.get("version")) or 0 for c in clashes), default=0)
        _set_draft(db, session, branch, doc_id, "clash", by)
        gates.tell(db, session,
                   f"[brindle] Your session's edit of analysis doc {doc_id} on branch {branch} clashes "
                   f"with version {newest or 'newer'} on {where}. Reconcile it: read the doc again "
                   f"(read_analysis_doc) and save the combined text with edit_analysis_doc "
                   f"(clashed_branch {json.dumps(branch)}); it publishes then.")
        return "clash"
    log.info("analysis doc draft not settled (HTTP %s)", status)
    return op


def _branch_drafts(db, ws, state: str) -> list[tuple[str, str, str]]:
    """``(session, doc id, drafter)`` of each draft on ``ws``'s branch in
    ``state``, across the sessions of ``ws``'s agents."""
    from brindle import autopilot

    sessions = dict.fromkeys(autopilot.root_of(db, a.id) for a in db.list_agents(ws.id))
    return [(s, doc_id, d.get("by", s)) for s in sessions for (branch, doc_id), d in drafts(db, s).items()
            if branch == ws.branch and d.get("state") == state]


def publish_drafts(db, ws, *, client=None, store=None) -> None:
    """``ws``'s branch merged: publish its drafts. Never raises."""
    for session, doc_id, by in _branch_drafts(db, ws, "open"):
        _settle(db, session, ws.branch, doc_id, "publish", by, client, store)


def drop_drafts(db, ws, *, client=None, store=None) -> None:
    """``ws`` is being removed unmerged: drop its drafts. Never raises."""
    for session, doc_id, by in _branch_drafts(db, ws, "open"):
        _settle(db, session, ws.branch, doc_id, "drop", by, client, store)


def settle_owed(db, *, client=None, store=None) -> None:
    """Send again every publish and drop the server hasn't confirmed yet,
    whatever became of the session that owes it."""
    for (session, branch, doc_id), d in owed(db).items():
        if d.get("op") in ("publish", "drop"):
            _settle(db, session, branch, doc_id, d["op"], d.get("by", session), client, store)


def _tell_stale(db, agent_id: str, doc_id: str, version: int) -> bool:
    """Tell ``agent_id`` once that ``doc_id``, which it read at an older
    version, now has ``version``. Only the session is told, never the person."""
    from brindle import gates, knowledge
    from brindle.pro.analysis_brief import DOC_ITEM

    read = _version(_pins(db, agent_id, DOC_ITEM).get(doc_id))
    told = _version(_pins(db, agent_id, TOLD_ITEM).get(doc_id))
    if read is None or version <= max(read, told or 0):
        return False
    knowledge.record_receipt(db, agent_id, [(TOLD_ITEM, doc_id, str(version))])
    gates.tell(db, agent_id, f"[brindle] Analysis doc {doc_id}, which you read at version {read}, now has "
                             f"version {version}. Read it again (read_analysis_doc) before you rely on it.")
    return True


def tell_readers(db, doc_id: str, version: int) -> None:
    """Tell each running agent that read ``doc_id`` before ``version``."""
    from brindle.pro.analysis_brief import DOC_ITEM

    rows = db.conn.execute("SELECT task_id FROM context_receipts WHERE item_kind = ? AND item_id = ?",
                           (DOC_ITEM, doc_id)).fetchall()
    for (agent_id,) in rows:
        agent = db.get_agent(agent_id)
        if agent is not None and agent.result is None:
            _tell_stale(db, agent_id, doc_id, version)


def _newer(db, agent_id: str, client=None, store=None) -> list[tuple[str, int, int]]:
    """``(doc id, version read, newest version)`` of each doc ``agent_id``
    read that has a newer version (``POST /analysis-docs/versions``)."""
    from brindle.pro.analysis_brief import DOC_ITEM

    read = {d: v for d, v in ((d, _version(v)) for d, v in _pins(db, agent_id, DOC_ITEM).items()) if v is not None}
    if not read:
        return []
    versions = api("POST", "/analysis-docs/versions", {"ids": sorted(read)}, client=client, store=store)
    return [(d, read[d], _version(v)) for d, v in sorted((versions.get("versions") or {}).items())
            if d in read and _version(v) is not None and _version(v) > read[d]]


def check_reads(db, agent_id: str, *, client=None, store=None) -> None:
    """Tell ``agent_id`` about the docs it read that changed since."""
    for doc_id, _, version in _newer(db, agent_id, client, store):
        _tell_stale(db, agent_id, doc_id, version)


def stale_before_result(db, agent_id: str, *, client=None, store=None) -> str | None:
    """Before ``agent_id``'s result (a report or a review verdict) is
    accepted: why it can't be yet, when a doc it read has a newer version
    it hasn't read. None to go ahead, also when the server can't be asked
    (the docs are context, never a reason to hold work up)."""
    from brindle import knowledge

    try:
        newer = _newer(db, agent_id, client, store)
    except Exception as e:  # noqa: BLE001
        log.info("analysis doc reads not checked (%s)", type(e).__name__)
        return None
    if not newer:
        return None
    knowledge.record_receipt(db, agent_id, [(TOLD_ITEM, d, str(v)) for d, _, v in newer])
    docs = ", ".join(f"{d} (you read version {r}, it is now at {v})" for d, r, v in newer)
    return (f"Not recorded yet: analysis docs you read have changed: {docs}. Read them again "
            "(read_analysis_doc), check your result still holds, then submit it again.")


def on_turn(db, agent, now: float | None = None) -> None:
    """At the end of ``agent``'s turn, at most every CHECK_EVERY seconds:
    tell it about docs it read that changed since (here or elsewhere), and
    send what any session still owes the server. Nothing is sent without
    reads to check or anything owed. Never raises."""
    import time

    from brindle import knowledge
    from brindle.pro.analysis_brief import DOC_ITEM

    any_owed = bool(owed(db))
    if not any_owed and not _pins(db, agent.id, DOC_ITEM):
        return
    now = time.time() if now is None else now
    try:
        last = float(_pins(db, agent.id, CHECKED_ITEM).get("reads", "0"))
    except ValueError:
        last = 0.0
    if now - last < CHECK_EVERY:
        return
    knowledge.record_receipt(db, agent.id, [(CHECKED_ITEM, "reads", str(now))])
    try:
        check_reads(db, agent.id)
    except Exception as e:  # noqa: BLE001
        log.info("analysis doc reads not checked (%s)", type(e).__name__)
    if any_owed:
        settle_owed(db)
