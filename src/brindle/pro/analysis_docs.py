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
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from brindle.pro import analysis_policy
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


def resolve_switch(name: str, user: dict | None = None, *, store=None) -> bool:
    """Resolve the switch using the cached org policy and the user's choice."""
    return analysis_policy.resolve_switch(name, user, store=store)


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
            return resolve_switch("store", fetch_switches(client, store), store=store)
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


def api(method: str, path: str, body: dict | None = None, *, client=None, store=None) -> dict:
    """Call a Bearer endpoint; raises ``RuntimeError`` with a readable reason
    on a non-200 answer, ``auth.AuthError`` when not logged in or offline."""
    from brindle.pro import auth

    client, store = _session(client, store)
    require_entitlement(store)
    status, resp = auth.authed(client, store, method, path, auth.JSONBody(body) if body is not None else None)
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
