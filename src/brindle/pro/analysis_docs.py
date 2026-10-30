"""The personal analysis-doc library client (brindle Pro; contract in
``docs/analysis-docs-plan.md``).

After a branch merges, each added or changed ``.brindle/deliverables/<slug>/``
(``main.md`` plus its ``appendices/*.md``) is uploaded to ``POST
/analysis-docs`` with scope ``user``, from a small bounded queue drained by one
background thread, so brindle never waits on the network and nothing raises
into it. When the queue is full, uploads are dropped.

Nothing is sent in air-gap mode, for a deliverable with a file under a
protected path (the repo's or the org's ``protected_paths``), without the
``analysis_docs`` entitlement, or when the ``store`` switch resolves to off
(:func:`resolve_switch`). The first upload prints a one-time notice naming
``brindle docs off``. ``repo_key`` is the org-key HMAC of the repo's identity
(:mod:`brindle.pro.orgkey`); without an org key or an identity nothing is sent.

The ``brindle docs`` commands (:func:`api`, :func:`set_switch`) talk to the
same endpoints for listing, showing, deleting, sharing and exporting.
"""

from __future__ import annotations

import atexit
import logging
import queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from brindle.pro.orgkey import OrgKeys, repo_identity

log = logging.getLogger(__name__)

FEATURE = "analysis_docs"
DELIVERABLES = ".brindle/deliverables"
QUEUE_SIZE = 64
FLUSH_AT_EXIT = 2.0
SEND_WAIT = 8.0
MAX_FILE = 512 * 1024
MAX_APPENDICES = 20
SWITCHES = ("standard", "store", "share", "assist", "prompt_analysis")
PLAN_DEFAULT_ON = frozenset({"store", "standard"})      # on for Pro, off otherwise
LABELS = ("verified", "inference", "not checked")
NOTICE_MARK = "analysis-docs-notice"
NOTICE = ("brindle stores your analysis deliverables (.brindle/deliverables) encrypted in your "
          "personal library. Turn this off any time with `brindle docs off`.")


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


# -- reading deliverables ------------------------------------------------------------------------


@dataclass
class Doc:
    slug: str
    title: str
    body_md: str
    appendices: list[dict]
    meta: dict
    files: list[str] = field(default_factory=list)      # repo-relative paths, for protected_paths


def _frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}, text
    out = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_]+", k.strip()):
            out[k.strip()] = v.strip().strip("\"'")
    return out, text[m.end():]


def _section(body: str, name: str) -> str:
    m = re.search(rf"^##\s+{name}\s*$(.*?)(?=^##\s|\Z)", body, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def _bullets(section: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^\s*[-*]\s+(.*)$", section, re.M)]


def _meta(front: dict, body: str) -> dict:
    results, conclusion = _section(body, "Results"), _section(body, "Conclusion")
    labels = {"verified": 0, "inference": 0, "not_checked": 0}
    for b in _bullets(results) + _bullets(conclusion):
        for lab in LABELS:
            if f"[{lab}]" in b.lower():
                labels[lab.replace(" ", "_")] += 1
    return {"question": front.get("question", ""), "conclusion": conclusion[:4000],
            "metrics": [b[:300] for b in _bullets(results) if re.search(r"\d", b)][:50],
            "labels": labels}


def _read(path: Path) -> str:
    with open(path, "rb") as fh:
        return fh.read(MAX_FILE).decode("utf-8", "replace")


def collect(root: str, changed: list[str]) -> list[Doc]:
    """The deliverables under ``root`` that ``changed`` (repo-relative paths)
    touches, parsed. A slug without a readable ``main.md`` is skipped."""
    slugs = []
    for p in changed:
        m = re.match(rf"^{re.escape(DELIVERABLES)}/([^/]+)/", p.replace("\\", "/"))
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))
    docs = []
    for slug in slugs:
        d = Path(root) / DELIVERABLES / slug
        try:
            main = _read(d / "main.md")
            front, body = _frontmatter(main)
            apps = sorted((d / "appendices").glob("*.md"))[:MAX_APPENDICES]
            docs.append(Doc(
                slug=slug, title=front.get("title") or slug, body_md=main,
                appendices=[{"name": a.name, "md": _read(a)} for a in apps],
                meta=_meta(front, body),
                files=[f"{DELIVERABLES}/{slug}/main.md",
                       *(f"{DELIVERABLES}/{slug}/appendices/{a.name}" for a in apps)]))
        except OSError:
            log.info("analysis doc %s unreadable; skipped", slug)
    return docs


def changed_files(path: str, base_ref: str) -> list[str]:
    """Deliverable files the branch at ``path`` adds or changes against ``base_ref``."""
    from brindle import git

    return git.paths(["diff", "--name-only", "--diff-filter=AMR", f"{base_ref}...HEAD", "--", DELIVERABLES], path)


# -- the uploader ----------------------------------------------------------------------------------


def _say(text: str) -> None:
    import sys

    print(f"brindle: {text}", file=sys.stderr)


class AnalysisDocs:
    def __init__(self, repo_root: str, *, client=None, store=None, key_store=None,
                 identity: Callable[[], str | None] | None = None,
                 protected: Callable[[list[str]], list[str]] | None = None,
                 notice: Callable[[str], None] | None = None,
                 start_thread: bool = True) -> None:
        self.repo_root = repo_root
        self._client, self._store = client, store
        self.keys = OrgKeys(store=store, client=client, key_store=key_store)
        self._identity = identity or (lambda: repo_identity(repo_root))
        self._protected = protected or (lambda files: protected_files(repo_root, files))
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
            if ent is None or identity is None or self._protected(doc.files) or not self._store_on():
                return
            key = self.keys.get(ent.org_id)
            self._first_notice()
            client, store = self._deps()
            body = {"scope": {"kind": "user", "id": ent.sub}, "title": doc.title, "body_md": doc.body_md,
                    "appendices": doc.appendices, "meta": doc.meta, "repo_key": key.repo_key(identity)}
            status, _ = auth.authed(client, store, "POST", "/analysis-docs", auth.JSONBody(body))
            if status != 200:
                log.info("analysis doc not stored (HTTP %s)", status)
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


def protected_files(repo_root: str, files: list[str]) -> list[str]:
    """``files`` that the repo's or the org's ``protected_paths`` cover. An
    org policy that can't be read protects everything."""
    from brindle import rule_checks
    from brindle.config import load_repo_config

    globs, why = rule_checks.org_protected_paths(repo_root)
    if why:
        return list(files)
    return rule_checks.protected_files(files, [*load_repo_config(repo_root).protected_paths, *globs])


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


def api(method: str, path: str, body: dict | None = None, *, client=None, store=None) -> dict:
    """Call a Bearer endpoint; raises ``RuntimeError`` with a readable reason
    on a non-200 answer, ``auth.AuthError`` when not logged in or offline."""
    from brindle.pro import auth

    client, store = _session(client, store)
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
