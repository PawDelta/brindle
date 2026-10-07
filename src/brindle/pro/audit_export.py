"""Audit export (brindle Enterprise, feature ``audit_export``): ship the audit
records to your own logging stack, and prune what is kept locally.

Two sources feed it, both only with a verified entitlement carrying
``audit_export`` (checked with :func:`brindle.pro.license.has` when sending,
offline when recording an event; no entitlement, or air-gap mode, sends and
spools nothing):

* the local audit chain (:mod:`brindle.pro.audit_chain`): every record of every
  log under ``$BRINDLE_HOME/audit``, as written -- ``seq``, ``ts``, the event,
  ``prev_hash``, ``hash`` and ``sig`` -- plus the log's name. The chain's own
  ``hash`` is the record's chain hash, so the receiving end can check the
  chain, and ``brindle audit pubkey`` gives the key for the signatures.
* the Team event feed (:mod:`brindle.pro.team_events`): the same org-keyed
  payload the Team backend gets (HMAC refs, never raw names), spooled by
  this plugin. Each carries ``hash`` (SHA-256 of its canonical payload) and
  ``chain_hash``, the hash of the local chain record of the same event when
  one is found.

Config, under ``audit_export`` in ``~/.brindle/config.json``::

    {"audit_export": {
        "retention_days": 90,
        "sinks": [
          {"type": "webhook", "url": "https://siem.example.com/brindle", "secret_env": "BRINDLE_WEBHOOK_SECRET"},
          {"type": "splunk",  "url": "https://splunk.example.com:8088", "token_env": "SPLUNK_HEC_TOKEN"},
          {"type": "datadog", "site": "datadoghq.com", "api_key_env": "DD_API_KEY"},
          {"type": "s3",      "bucket": "acme-audit", "prefix": "brindle/"}]}}

Secrets are never in the config: each ``*_env`` names an environment variable
that is read when sending. A sink that can't be set up (a missing variable, a
non-https URL, ``boto3`` not installed for ``s3``) raises :class:`NotConfigured`
for that sink only; the others keep shipping.

Delivery: each sink has its own cursor (per audit log, and for the Team spool)
in ``audit/export-state.json``, so records are sent in order, in batches, at
least once. A failed send leaves the cursor where it was and is retried with
exponential backoff, from the logs and spool on disk, so it survives offline
periods and restarts. A batch the receiver rejects as malformed (400, 413,
422) is dropped with a warning, like the Team feed does; anything else, a
401 or 5xx included, is retried indefinitely. Only one process at a time sends
(a non-blocking ``flock``).

Retention: ``retention_days`` prunes local audit records that old once every
sink has received them (records no sink has seen yet are kept), through
:func:`brindle.pro.audit_chain.prune`, which leaves a signed checkpoint so
``brindle audit verify`` still passes.
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from brindle.events import Event, EventsPlugin
from brindle.pro import audit_chain
from brindle.pro.audit_chain import canonical, sha256

log = logging.getLogger(__name__)

FEATURE = "audit_export"
SETTING = "audit_export"
BATCH = 100
BATCH_BYTES = 512 * 1024
MAX_SPOOL_ENTRIES = 5000
MAX_SPOOL_BYTES = 2 * 1024 * 1024
MAX_BACKOFF = 300.0
SEND_TIMEOUT = 10.0
FLUSH_AT_EXIT = 2.0
DROP_STATUSES = (400, 413, 422)
SINK_TYPES = ("webhook", "splunk", "datadog", "s3")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
SITE_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,62}[a-z0-9])?$")
STATE_FILE = "export-state.json"
SPOOL_FILE = "export.spool"
SENDER_LOCK = "export-sender.lock"


class NotConfigured(Exception):
    """A sink (or the export itself) is not set up: a missing setting, an
    unset secret variable, or an optional dependency that isn't installed."""


class SinkError(Exception):
    """A send failed. ``drop``: the receiver rejected the batch itself, so
    retrying can't help and the batch is skipped."""

    def __init__(self, message: str, *, drop: bool = False) -> None:
        super().__init__(message)
        self.drop = drop


# -- config ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportConfig:
    sinks: tuple[dict, ...] = ()
    retention_days: float | None = None


def parse_config(raw) -> ExportConfig:
    """The ``audit_export`` setting, validated (raises :class:`NotConfigured`)."""
    if raw in (None, {}):
        return ExportConfig()
    if not isinstance(raw, dict):
        raise NotConfigured(f'"{SETTING}" must be an object')
    sinks = raw.get("sinks", [])
    if not isinstance(sinks, list) or not all(isinstance(s, dict) for s in sinks):
        raise NotConfigured(f'"{SETTING}.sinks" must be a list of objects')
    days = raw.get("retention_days")
    if days is not None:
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
            raise NotConfigured(f'"{SETTING}.retention_days" must be a positive number of days')
    return ExportConfig(tuple(sinks), float(days) if days is not None else None)


def load_config(settings: dict | None = None) -> ExportConfig:
    if settings is None:
        from brindle.config import user_settings

        settings = user_settings()
    return parse_config(settings.get(SETTING))


# -- transport --------------------------------------------------------------------------------


class UrllibTransport:
    """``post(url, headers, body) -> HTTP status`` over urllib, never following
    a redirect (a redirect would move the audit data somewhere unconfigured)."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    def __init__(self, timeout: float = SEND_TIMEOUT) -> None:
        self.timeout = timeout
        self._opener = urllib.request.build_opener(self._NoRedirect)

    def post(self, url: str, headers: dict, body: bytes) -> int:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SinkError(f"could not reach {urllib.parse.urlsplit(url).hostname}: "
                            f"{type(e).__name__}") from e


# -- sinks ------------------------------------------------------------------------------------


def _env(spec: dict, key: str, *, required: bool = True) -> str | None:
    """The value of the environment variable named by ``spec[key]``."""
    name = spec.get(key)
    if name is None:
        if required:
            raise NotConfigured(f'{spec.get("type")} sink: "{key}" (an environment variable name) is required')
        return None
    if not isinstance(name, str) or not ENV_RE.match(name):
        raise NotConfigured(f'{spec.get("type")} sink: "{key}" must name an environment variable')
    value = os.environ.get(name)
    if not value:
        if required:
            raise NotConfigured(f"{spec.get('type')} sink: environment variable {name} is not set")
        return None
    return value


def _https_url(spec: dict, key: str = "url") -> urllib.parse.SplitResult:
    url = spec.get(key)
    if not isinstance(url, str):
        raise NotConfigured(f'{spec.get("type")} sink: "{key}" is required')
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise NotConfigured(f'{spec.get("type")} sink: "{key}" must be an https:// URL')
    return parts


def _post(transport, url: str, headers: dict, body: bytes, who: str) -> None:
    status = transport.post(url, headers, body)
    if 200 <= status < 300:
        return
    raise SinkError(f"{who} answered HTTP {status}", drop=status in DROP_STATUSES)


class Sink:
    """One destination. ``send`` delivers a batch of records (dicts) or raises
    :class:`SinkError`."""

    type = ""

    def __init__(self, name: str) -> None:
        self.name = name

    def send(self, records: list[dict]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class WebhookSink(Sink):
    """POST ``{"version", "source", "sent_at", "records"}`` as JSON, signed:
    ``X-Brindle-Signature: sha256=<HMAC-SHA256 of the raw body under the secret>``."""

    type = "webhook"

    def __init__(self, name: str, spec: dict, transport) -> None:
        super().__init__(name)
        self.url = _https_url(spec).geturl()
        self._secret = _env(spec, "secret_env").encode("utf-8")
        self.transport = transport

    def send(self, records: list[dict]) -> None:
        sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        body = json.dumps({"version": 1, "source": "brindle", "sent_at": sent_at, "records": records},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        mac = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        _post(self.transport, self.url,
              {"Content-Type": "application/json", "X-Brindle-Signature": f"sha256={mac}"},
              body, "the webhook")


class SplunkSink(Sink):
    """Splunk HTTP Event Collector: one ``{"time", "host", "source",
    "sourcetype", "event"}`` object per record, concatenated."""

    type = "splunk"

    def __init__(self, name: str, spec: dict, transport) -> None:
        super().__init__(name)
        parts = _https_url(spec)
        if parts.path in ("", "/"):
            parts = parts._replace(path="/services/collector/event")
        self.url = parts.geturl()
        self._token = _env(spec, "token_env")
        self.fields = {"source": str(spec.get("source", "brindle")),
                       "sourcetype": str(spec.get("sourcetype", "brindle:audit")),
                       "host": str(spec.get("host") or socket.gethostname())}
        if spec.get("index"):
            self.fields["index"] = str(spec["index"])
        self.transport = transport

    def send(self, records: list[dict]) -> None:
        lines = []
        for r in records:
            try:
                t = datetime.fromisoformat(str(r.get("ts")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                t = time.time()
            lines.append(json.dumps({"time": t, **self.fields, "event": r}, sort_keys=True,
                                    separators=(",", ":")))
        _post(self.transport, self.url,
              {"Content-Type": "application/json", "Authorization": f"Splunk {self._token}"},
              "\n".join(lines).encode("utf-8"), "Splunk HEC")


class DatadogSink(Sink):
    """Datadog Logs intake (v2): one log per record, the record's canonical
    JSON as the message."""

    type = "datadog"

    def __init__(self, name: str, spec: dict, transport) -> None:
        super().__init__(name)
        site = spec.get("site", "datadoghq.com")
        if not isinstance(site, str) or not SITE_RE.match(site):
            raise NotConfigured('datadog sink: "site" must be a Datadog site such as datadoghq.com')
        self.url = f"https://http-intake.logs.{site}/api/v2/logs"
        self._key = _env(spec, "api_key_env")
        tags = spec.get("tags", [])
        self.tags = ",".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
        self.service = str(spec.get("service", "brindle"))
        self.host = str(spec.get("host") or socket.gethostname())
        self.transport = transport

    def send(self, records: list[dict]) -> None:
        body = json.dumps([{"ddsource": "brindle", "service": self.service, "ddtags": self.tags,
                            "hostname": self.host, "hash": r.get("hash"), "message": canonical(r)}
                           for r in records], separators=(",", ":")).encode("utf-8")
        _post(self.transport, self.url,
              {"Content-Type": "application/json", "DD-API-KEY": self._key}, body, "Datadog")


def import_boto3():
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as e:
        raise NotConfigured("s3 sink: boto3 is not installed (pip install boto3)") from e
    return boto3


class S3Sink(Sink):
    """One JSONL object per batch under ``<prefix>YYYY/MM/DD/``, named by the
    batch's content so a retried batch overwrites itself. Credentials are
    boto3's usual chain, or the variables named by ``access_key_env`` /
    ``secret_key_env`` (/ ``session_token_env``)."""

    type = "s3"

    def __init__(self, name: str, spec: dict, boto3_module=None, client=None) -> None:
        super().__init__(name)
        bucket = spec.get("bucket")
        if not isinstance(bucket, str) or not re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", bucket):
            raise NotConfigured('s3 sink: "bucket" must be a bucket name')
        self.bucket = bucket
        prefix = str(spec.get("prefix", "brindle/"))
        self.prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        self.sse = spec.get("sse")
        if client is None:
            boto3 = boto3_module or import_boto3()
            kwargs = {}
            if spec.get("region"):
                kwargs["region_name"] = str(spec["region"])
            if spec.get("endpoint_url"):
                kwargs["endpoint_url"] = _https_url(spec, "endpoint_url").geturl()
            if spec.get("access_key_env"):
                kwargs["aws_access_key_id"] = _env(spec, "access_key_env")
                kwargs["aws_secret_access_key"] = _env(spec, "secret_key_env")
                token = _env(spec, "session_token_env", required=False)
                if token:
                    kwargs["aws_session_token"] = token
            client = boto3.client("s3", **kwargs)
        self.client = client

    def send(self, records: list[dict]) -> None:
        body = "".join(canonical(r) + "\n" for r in records).encode("utf-8")
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        key = f"{self.prefix}{day}/brindle-audit-{hashlib.sha256(body).hexdigest()[:24]}.jsonl"
        extra = {"ServerSideEncryption": self.sse} if self.sse else {}
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                   ContentType="application/x-ndjson", **extra)
        except Exception as e:  # noqa: BLE001 - botocore's errors are many; all are retried
            raise SinkError(f"S3 put failed: {type(e).__name__}") from e


def build_sink(spec: dict, name: str, *, transport=None, boto3_module=None, s3_client=None) -> Sink:
    kind = spec.get("type")
    if kind not in SINK_TYPES:
        raise NotConfigured(f"unknown sink type {kind!r} (one of {', '.join(SINK_TYPES)})")
    transport = transport or UrllibTransport()
    if kind == "webhook":
        return WebhookSink(name, spec, transport)
    if kind == "splunk":
        return SplunkSink(name, spec, transport)
    if kind == "datadog":
        return DatadogSink(name, spec, transport)
    return S3Sink(name, spec, boto3_module, s3_client)


def sink_names(specs) -> list[str]:
    """A stable name per sink spec: its ``name``, else its type, numbered when repeated."""
    seen: dict[str, int] = {}
    out = []
    for s in specs:
        base = str(s.get("name") or s.get("type") or "sink")
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return out


# -- state and spool --------------------------------------------------------------------------


@dataclass
class SinkResult:
    sent: int = 0
    dropped: int = 0
    error: str | None = None


@dataclass
class Item:
    key: str            # "log:<file name>" or "spool"
    seq: int
    record: dict


def _read_json(path: Path) -> dict:
    try:
        fd = audit_chain._open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as fh:
        try:
            data = json.loads(fh.read(1 << 20))
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


class Store:
    """The export state (cursors) and the Team-event spool, in the audit
    directory, under one lock."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home
        self.dropped = 0

    @property
    def dir(self) -> Path:
        return audit_chain.audit_dir(self.home)

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILE

    @property
    def spool_path(self) -> Path:
        return self.dir / SPOOL_FILE

    def locked(self):
        return audit_chain._locked(self.state_path)

    def state(self) -> dict:
        st = _read_json(self.state_path)
        st.setdefault("spool_next", 1)
        st.setdefault("sinks", {})
        return st

    def _write_state(self, st: dict) -> None:
        audit_chain._write_atomic(self.state_path, (json.dumps(st, sort_keys=True) + "\n").encode())

    def cursors(self, sink: str) -> dict:
        return dict((self.state()["sinks"].get(sink) or {}).get("cursors") or {})

    def advance(self, sink: str, positions: dict[str, int], *, sent: int = 0, dropped: int = 0) -> None:
        with self.locked():
            st = self.state()
            entry = st["sinks"].setdefault(sink, {})
            cur = entry.setdefault("cursors", {})
            for k, v in positions.items():
                cur[k] = max(v, cur.get(k, 0))
            entry["sent"] = entry.get("sent", 0) + sent
            entry["dropped"] = entry.get("dropped", 0) + dropped
            self._write_state(st)

    # the spool: lines of {"n": int, "record": {...}}

    def _spool_entries(self) -> list[dict]:
        try:
            fd = audit_chain._open_private(self.spool_path, os.O_RDONLY)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_SPOOL_BYTES * 2)
        out = []
        for ln in raw.splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("n"), int) and isinstance(e.get("record"), dict):
                out.append(e)
        return out

    def _write_spool(self, entries: list[dict]) -> None:
        lines = [json.dumps(e, sort_keys=True, separators=(",", ":")) for e in entries]
        while lines and (len(lines) > MAX_SPOOL_ENTRIES or sum(len(x) + 1 for x in lines) > MAX_SPOOL_BYTES):
            lines.pop(0)
            self.dropped += 1
        audit_chain._write_atomic(self.spool_path, ("\n".join(lines) + "\n" if lines else "").encode())
        if self.dropped:
            log.warning("brindle audit export spool full; %d oldest record(s) dropped", self.dropped)

    def spool_append(self, record: dict) -> None:
        with self.locked():
            st = self.state()
            n = st["spool_next"]
            st["spool_next"] = n + 1
            self._write_spool(self._spool_entries() + [{"n": n, "record": record}])
            self._write_state(st)

    def spool_after(self, cursor: int) -> list[dict]:
        with self.locked():
            return [e for e in self._spool_entries() if e["n"] > cursor]

    def spool_trim(self, sink_names: list[str]) -> None:
        """Drop the entries every sink has received."""
        with self.locked():
            st = self.state()
            floor = min((((st["sinks"].get(n) or {}).get("cursors") or {}).get("spool", 0)
                         for n in sink_names), default=0)
            entries = self._spool_entries()
            kept = [e for e in entries if e["n"] > floor]
            if len(kept) != len(entries):
                self._write_spool(kept)


def log_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.jsonl") if p.is_file())


# -- the exporter -----------------------------------------------------------------------------


class AuditExporter(EventsPlugin):
    def __init__(self, repo_root: str = "", *, settings: dict | None = None, home: Path | None = None,
                 sinks: list[Sink] | None = None, transport=None, boto3_module=None, s3_client=None,
                 entitled=None, team_payload=None, start_thread: bool = True, clock=time.time) -> None:
        self.repo_root = repo_root
        self._settings = settings
        self.home = home
        self._fixed_sinks = sinks
        self._transport, self._boto3, self._s3_client = transport, boto3_module, s3_client
        self._entitled = entitled
        self._team_payload = team_payload
        self._start_thread = start_thread
        self._clock = clock
        self.store = Store(home)
        self._built: dict[str, Sink] = {}
        self._identities: dict[str, str | None] = {}
        self._keys = None
        self._wake = threading.Event()
        self._thread = None
        self._delay: dict[str, float] = {}
        self._next_try: dict[str, float] = {}
        self.errors: dict[str, str] = {}

    # -- gates and config ----------------------------------------------------------------

    def entitled(self, *, offline: bool = False) -> bool:
        """Whether the plan includes ``audit_export``. Fails closed. ``offline``
        reads the stored entitlement without refreshing it, so recording an
        event never waits on the network."""
        if self._entitled is not None:
            return bool(self._entitled())
        from brindle.pro import license

        try:
            if offline:
                return FEATURE in license.current(refresh=False).features
            return license.has(FEATURE)
        except Exception:  # noqa: BLE001
            return False

    def config(self) -> ExportConfig:
        if self._fixed_sinks is not None and self._settings is None:
            return ExportConfig()
        return load_config(self._settings)

    def sinks(self, cfg: ExportConfig) -> tuple[list[Sink], dict[str, str]]:
        """The sinks that can be built now, and the problem of each that can't."""
        if self._fixed_sinks is not None:
            return list(self._fixed_sinks), {}
        built, problems = [], {}
        for spec, name in zip(cfg.sinks, sink_names(cfg.sinks)):
            try:
                built.append(build_sink(spec, name, transport=self._transport,
                                        boto3_module=self._boto3, s3_client=self._s3_client))
            except NotConfigured as e:
                problems[name] = str(e)
        return built, problems

    def _all_sink_names(self, cfg: ExportConfig) -> list[str]:
        if self._fixed_sinks is not None:
            return [s.name for s in self._fixed_sinks]
        return sink_names(cfg.sinks)

    # -- recording -------------------------------------------------------------------------

    def _identity(self, repo_root: str) -> str | None:
        if self._identities.get(repo_root) is None:
            from brindle.pro.orgkey import repo_identity

            self._identities[repo_root] = repo_identity(repo_root)
        return self._identities[repo_root]

    def team_payload(self, event: Event) -> dict | None:
        """The Team feed's payload for ``event``, or None without a team org
        and a cached org key (nothing is ever keyed any other way)."""
        if self._team_payload is not None:
            return self._team_payload(event)
        from brindle.pro import license, team_events
        from brindle.pro.orgkey import OrgKeys

        ent = license.current(refresh=False)
        if team_events.FEATURE not in ent.features or not team_events.ORG_RE.match(ent.org_id):
            return None
        if self._keys is None:
            self._keys = OrgKeys()
        key = self._keys.cached(ent.org_id)
        if key is None:
            return None
        return team_events.event_payload(key, self._identity(event.repo_root), event)

    def emit(self, event: Event) -> None:
        from brindle import airgap

        if airgap.enabled():
            return
        try:
            cfg = self.config()
            if not cfg.sinks and self._fixed_sinks is None:
                return
            if not self.entitled(offline=True):
                return
            payload = None
            try:
                payload = self.team_payload(event)
            except Exception:  # noqa: BLE001 - the chain records still ship
                log.info("brindle audit export: no Team payload for this event", exc_info=True)
            if payload is not None:
                self.store.spool_append({"source": "team_events", "ts": _iso(payload.get("at")),
                                         "event": payload, "hash": sha256(canonical(payload))})
            self._ensure_sender()
            self._wake.set()
        except Exception:  # noqa: BLE001 - never fail the operation being reported
            log.warning("brindle: couldn't record an audit export event", exc_info=True)

    # -- sending ---------------------------------------------------------------------------

    def _ensure_sender(self) -> None:
        if self._thread is not None or not self._start_thread:
            return
        self._thread = threading.Thread(target=self._run, name="brindle-audit-export", daemon=True)
        self._thread.start()
        self._wake.set()                       # ship whatever an earlier run left behind
        atexit.register(self._flush_quietly)

    def _flush_quietly(self) -> None:
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            log.warning("brindle audit export: final flush failed", exc_info=True)

    def _run(self) -> None:
        while True:
            now = self._clock()
            waits = [t - now for t in self._next_try.values() if t > now]
            self._wake.wait(min(waits) if waits else None)
            self._wake.clear()
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.warning("brindle audit export error", exc_info=True)

    @contextlib.contextmanager
    def _sender(self):
        """Yields True for the one process allowed to send right now."""
        fd = audit_chain._open_private(self.store.dir / SENDER_LOCK, os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _pending(self, sink: str, limit: int | None = None) -> list[Item]:
        """The next records ``sink`` hasn't received, oldest log record first,
        then the Team spool, within ``limit`` (default ``BATCH``) records and
        ``BATCH_BYTES``."""
        limit = limit or BATCH
        cursors = self.store.cursors(sink)
        items: list[Item] = []
        chain_hashes: dict[tuple, str] = {}
        logs: list[tuple[str, list[dict]]] = []
        for path in log_files(self.store.dir):
            try:
                recs = [r for r in audit_chain.read_records(path)
                        if r.get("kind") != audit_chain.CHECKPOINT_KIND and isinstance(r.get("seq"), int)]
            except Exception:  # noqa: BLE001
                log.warning("brindle audit export: couldn't read %s", path.name, exc_info=True)
                continue
            logs.append((path.name, recs))
            for r in recs:
                ev = r.get("event") or {}
                chain_hashes.setdefault((ev.get("kind"), ev.get("at")), r.get("hash"))
        for name, recs in logs:
            after = cursors.get(f"log:{name}", 0)
            for r in recs:
                if r["seq"] > after:
                    items.append(Item(f"log:{name}", r["seq"], {"source": "audit_chain", "log": name, **r}))
        for e in self.store.spool_after(cursors.get("spool", 0)):
            rec = dict(e["record"])
            ev = rec.get("event") or {}
            rec.setdefault("chain_hash", chain_hashes.get((ev.get("kind"), ev.get("at"))))
            items.append(Item("spool", e["n"], rec))
        out, size = [], 0
        for it in items:
            n = len(canonical(it.record))
            if len(out) >= limit or (out and size + n > BATCH_BYTES):
                break
            out.append(it)
            size += n
        return out

    def _backoff(self, sink: str) -> None:
        d = min(MAX_BACKOFF, max(1.0, self._delay.get(sink, 0.0) * 2))
        self._delay[sink] = d
        self._next_try[sink] = self._clock() + d * random.uniform(0.8, 1.2)

    def ship(self, sink: Sink, *, force: bool = False) -> SinkResult:
        """Send ``sink`` everything it hasn't received, batch by batch, until
        done or a send fails (then it backs off)."""
        res = SinkResult()
        if not force and self._clock() < self._next_try.get(sink.name, 0.0):
            res.error = self.errors.get(sink.name, "backing off")
            return res
        while True:
            batch = self._pending(sink.name)
            if not batch:
                self._delay[sink.name] = 0.0
                self._next_try.pop(sink.name, None)
                self.errors.pop(sink.name, None)
                return res
            positions: dict[str, int] = {}
            for it in batch:
                positions[it.key] = max(it.seq, positions.get(it.key, 0))
            try:
                sink.send([it.record for it in batch])
            except SinkError as e:
                if e.drop:
                    log.warning("brindle audit export: %s rejected %d record(s) (%s); dropped",
                                sink.name, len(batch), e)
                    self.store.advance(sink.name, positions, dropped=len(batch))
                    res.dropped += len(batch)
                    continue
                res.error = str(e)
            except Exception as e:  # noqa: BLE001 - a sink bug must not kill the others
                res.error = f"{type(e).__name__}: {e}"
            if res.error:
                self.errors[sink.name] = res.error
                self._backoff(sink.name)
                return res
            self.store.advance(sink.name, positions, sent=len(batch))
            res.sent += len(batch)

    def flush(self, *, force: bool = False) -> dict[str, SinkResult]:
        """Ship to every sink, then trim the spool and prune old local records.
        Nothing without the entitlement or in air-gap mode."""
        from brindle import airgap

        if airgap.enabled() or not self.entitled():
            return {}
        cfg = self.config()
        sinks, problems = self.sinks(cfg)
        results = {name: SinkResult(error=msg) for name, msg in problems.items()}
        with self._sender() as mine:
            if not mine:
                return results
            for sink in sinks:
                results[sink.name] = self.ship(sink, force=force)
            names = self._all_sink_names(cfg)
            try:
                self.store.spool_trim(names)
                if cfg.retention_days:
                    self.prune(cfg.retention_days, names)
            except Exception:  # noqa: BLE001
                log.warning("brindle audit export: housekeeping failed", exc_info=True)
        return results

    # -- retention -------------------------------------------------------------------------

    def prune(self, retention_days: float, sink_names: list[str] | None = None, *,
              repo_root: str | None = None, now: float | None = None) -> dict[str, int]:
        """Prune audit records older than ``retention_days``, but never one a
        configured sink hasn't received. All logs, or only ``repo_root``'s.
        Returns the number dropped per log file."""
        if sink_names is None:
            sink_names = self._all_sink_names(self.config())
        paths = ([audit_chain.log_path(repo_root, self.home)] if repo_root
                 else log_files(self.store.dir))
        state = self.store.state()
        out = {}
        for path in paths:
            if not path.exists():
                continue
            cap = None
            if sink_names:
                cap = min((((state["sinks"].get(n) or {}).get("cursors") or {}).get(f"log:{path.name}", 0)
                           for n in sink_names), default=0)
            try:
                k = audit_chain.prune(path=path, home=self.home, retention_days=retention_days,
                                      max_seq=cap, now=now)
            except audit_chain.AuditError as e:
                log.warning("brindle audit export: %s", e)
                continue
            if k:
                out[path.name] = k
        return out


def _iso(at) -> str:
    try:
        t = float(at)
    except (TypeError, ValueError):
        t = time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def make(repo_root: str) -> AuditExporter:
    return AuditExporter(repo_root)


__all__ = ["FEATURE", "AuditExporter", "ExportConfig", "NotConfigured", "Sink", "SinkError", "SinkResult",
           "build_sink", "load_config", "make", "parse_config"]
