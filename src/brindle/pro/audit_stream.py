"""Audit streaming (brindle Enterprise): a sink on the ``audit`` events plugin
(``audit_chain.AuditChain.emit``, registered through ``brindle.events``) that
sends each audit record, as it is written, to a company SIEM.

Targets are plain values owned by each AuditChain (a list, or a callable
returning one); this module reads no policy. Standalone callers can set
defaults with :func:`configure`::

    {"kind": "syslog", "url": "udp://siem.example.com:514"}     # or tcp://
    {"kind": "https", "url": "https://siem.example.com/ingest",
     "format": "json" | "otlp", "headers": {...}}               # format defaults to json

With no target configured nothing happens and nothing is written.

What is sent is the audit record itself, exactly as the chain wrote it (its
canonical JSON): as the RFC 5424 message of a syslog line (TCP uses RFC 6587
octet counting), as the JSON body, or as the body of one OTLP log record. The
syslog header carries no host name.

Records are first put in a durable per-target queue
(``$BRINDLE_HOME/audit/stream-<id>.queue``, 0600) and then sent oldest first, so
a target that is down loses nothing. The record is queued while the chain still
holds its append lock (so queue order is chain order), and sent from a
background thread: the agent only ever does local file work. The queue is
drained, in order, on the next write or on :func:`flush` (which sends in the
caller's thread). A failed send stops the drain and leaves the rest queued. After a failure the target is left alone for ``COOLDOWN_SECONDS``
(``flush`` ignores that), and every send is bounded by ``SEND_SECONDS``, so
the agent never waits on a dead SIEM. Only one process drains a target's queue
at a time (a non-blocking lock); a process that finds it taken just queues its
record and returns. The queue holds ``MAX_QUEUE`` records: a full queue keeps the
oldest and counts what it turned away, and the next complete drain sends one
``audit_stream.dropped`` record with that count, so the SIEM sees the gap.
Nothing here raises into brindle.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from brindle.pro.audit_chain import _iso, _locked, _open_private, _write_atomic, audit_dir, canonical, sha256

log = logging.getLogger(__name__)

SEND_SECONDS = 3
COOLDOWN_SECONDS = 30
MAX_QUEUE = 50_000               # queued records per target; a full queue turns new ones away
DROPPED_KIND = "audit_stream.dropped"
SYSLOG_PRI = 13 * 8 + 6          # facility "log audit", severity informational
KINDS = ("syslog", "https")

_targets = []                    # a list of target dicts, or a callable returning one
_home: Path | None = None
_transport = None                # (target, record) -> None, raising when it can't deliver
_clock = time.monotonic
_down_until: dict[str, float] = {}
_after_empty_snapshot = lambda: None   # noqa: E731 - test seam: a drainer has just read an empty queue
_spawn = None                   # optional background drain starter (default: a daemon thread)


def configure(targets=None, *, home: Path | None = None, transport=None, clock=None,
              spawn=None) -> None:
    """Set the targets (a list or a ``targets_provider`` callable); None or an
    empty list turns audit streaming off. ``transport`` replaces the real
    network sender and ``spawn`` the background thread start (tests)."""
    global _targets, _home, _transport, _clock, _spawn
    _targets = targets if targets is not None else []
    _home = home
    _transport = transport
    _spawn = spawn or _start
    _clock = clock or time.monotonic
    _down_until.clear()


def _current(targets=None) -> list[dict]:
    targets = _targets if targets is None else targets
    raw = targets() if callable(targets) else targets
    return [t for t in (raw or []) if isinstance(t, dict) and t.get("kind") in KINDS and t.get("url")]


def _target_id(target: dict) -> str:
    return sha256(canonical(target))[:12]


def queue_path(target: dict, home: Path | None = None) -> Path:
    return audit_dir(home or _home) / f"stream-{_target_id(target)}.queue"


# -- the queue --------------------------------------------------------------------------------------


def _read_queue(path: Path) -> list[bytes]:
    try:
        fd = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as fh:
        return [ln for ln in fh.read().split(b"\n") if ln.strip()]


def _read_dropped(path: Path) -> int:
    try:
        fd = _open_private(path.with_suffix(".dropped"), os.O_RDONLY)
    except FileNotFoundError:
        return 0
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(32).strip()
    return int(raw) if raw.isdigit() else 0


def _add_dropped(path: Path, delta: int) -> None:
    """Change the count of records the full queue turned away (under the queue lock)."""
    total = max(0, _read_dropped(path) + delta)
    dropped = path.with_suffix(".dropped")
    if total:
        _write_atomic(dropped, str(total).encode("ascii"))
    else:
        try:
            os.unlink(dropped)
        except FileNotFoundError:
            pass


def _enqueue(path: Path, record: dict) -> None:
    """Add ``record`` at the end of the queue. A full queue keeps what it has
    and only counts the record as dropped."""
    with _locked(path):
        if len(_read_queue(path)) >= MAX_QUEUE:
            _add_dropped(path, 1)
            return
        fd = _open_private(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, canonical(record).encode("utf-8") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)


def _take_drain(path: Path) -> int | None:
    """The drain lock's descriptor when this process may drain the queue (only
    one at a time); None at once, without waiting, when another is draining."""
    fd = _open_private(path.with_suffix(".drain"), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_drain(fd: int) -> None:
    os.close(fd)                       # closing the descriptor drops the flock


def _drop_sent(path: Path, count: int) -> None:
    """Remove the first ``count`` queued records (the ones just delivered)."""
    with _locked(path):
        rest = _read_queue(path)[count:]
        if rest:
            _write_atomic(path, b"".join(ln + b"\n" for ln in rest))
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


# -- transports -----------------------------------------------------------------------------------


def syslog_message(record: dict) -> bytes:
    """RFC 5424: ``<PRI>1 TIMESTAMP HOST APP PROCID MSGID SD MSG`` with the record as MSG."""
    ts = record.get("ts") if isinstance(record.get("ts"), str) else "-"
    return f"<{SYSLOG_PRI}>1 {ts} - brindle-audit - audit - {canonical(record)}".encode("utf-8")


def otlp_body(record: dict) -> dict:
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [
        {"body": {"stringValue": canonical(record)}}]}]}]}


def deliver(target: dict, record: dict) -> None:
    """Send one record over the real network; raises when it can't."""
    from brindle import airgap

    airgap.guard(target.get("url"), "audit stream")
    if target["kind"] == "syslog":
        url = urllib.parse.urlsplit(target["url"])
        if url.scheme not in ("udp", "tcp") or not url.hostname:
            raise ValueError("a syslog url is udp://host:port or tcp://host:port")
        msg = syslog_message(record)
        port = url.port or 514
        if url.scheme == "udp":
            with socket.socket(socket.AF_INET6 if ":" in url.hostname else socket.AF_INET,
                               socket.SOCK_DGRAM) as s:
                s.settimeout(SEND_SECONDS)
                s.sendto(msg, (url.hostname, port))
        else:
            with socket.create_connection((url.hostname, port), SEND_SECONDS) as s:
                s.sendall(str(len(msg)).encode("ascii") + b" " + msg)
        return
    body = otlp_body(record) if target.get("format") == "otlp" else record
    req = urllib.request.Request(
        target["url"], data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **(target.get("headers") or {})})
    if not req.full_url.lower().startswith("https://"):
        raise ValueError("an https target needs an https:// url")
    with _opener.open(req, timeout=SEND_SECONDS) as resp:
        if resp.status >= 300:
            raise OSError(f"the SIEM answered {resp.status}")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would turn the POST into a bodiless GET and could carry the
    configured headers to another host, so it counts as a failed send."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener(*handlers):
    return urllib.request.build_opener(_NoRedirects, *handlers)


_opener = _build_opener()


# -- sending --------------------------------------------------------------------------------------


def _snapshot(path: Path) -> list[bytes]:
    """A drainer's read of the queue; the caller holds the queue lock."""
    queued = _read_queue(path)
    if not queued:
        _after_empty_snapshot()
    return queued


def _drain(target: dict, home: Path | None, force: bool) -> None:
    """Deliver the target's queue, oldest first, until it is empty or a send
    fails. Does nothing while another drainer holds the lock (it picks up
    whatever is queued meanwhile, because it re-reads the queue)."""
    path = queue_path(target, home)
    tid = _target_id(target)
    if not force and _clock() < _down_until.get(tid, 0.0):
        return
    send = _transport or deliver
    fd = _take_drain(path)
    if fd is None:
        return
    try:
        while True:
            with _locked(path):
                queued = _snapshot(path)
                gap = _read_dropped(path)
                if not queued and not gap:
                    # Giving up the drain lock under the queue lock: a writer's record
                    # is either seen here, or queued after this and drained by its own kick.
                    _release_drain(fd)
                    fd = None
                    return
            sent = 0
            try:
                for line in queued:
                    send(target, json.loads(line))
                    sent += 1
                if gap:
                    send(target, {"ts": _iso(), "event": {"kind": DROPPED_KIND, "dropped": gap}})
                    with _locked(path):
                        _add_dropped(path, -gap)
                _down_until.pop(tid, None)
            except Exception:  # noqa: BLE001 - an unreachable target leaves the rest queued
                _down_until[tid] = _clock() + COOLDOWN_SECONDS
                log.warning("brindle: audit streaming target %s is unreachable; %d record(s) queued",
                            target["url"], len(queued) - sent)
                return
            finally:
                if sent:
                    _drop_sent(path, sent)
    finally:
        if fd is not None:
            _release_drain(fd)


def _start(fn) -> None:
    threading.Thread(target=fn, daemon=True, name="audit-streaming").start()


def _each_target(action, targets=None) -> None:
    """Run ``action(target)`` for every target; a failure never leaves this module."""
    try:
        targets = _current(targets)
    except Exception:  # noqa: BLE001 - a broken provider must not touch the agent
        log.warning("brindle: couldn't read the audit streaming targets", exc_info=True)
        return
    for target in targets:
        try:
            action(target)
        except Exception:  # noqa: BLE001 - never fail the operation being reported
            log.warning("brindle: audit streaming failed", exc_info=True)


def enqueue(record: dict, home: Path | None = None, *, targets=None) -> None:
    """Put ``record`` in every target's durable queue. Local file work only (no
    network), so the audit chain calls it while still holding its append lock,
    which keeps queue order the same as chain order."""
    _each_target(lambda target: _enqueue(queue_path(target, home), record), targets)


def kick(home: Path | None = None, *, targets=None) -> None:
    """Start delivering the queues in the background; returns at once."""
    _each_target(lambda target: (_spawn or _start)(lambda: _drain_quietly(target, home, False)), targets)


def flush() -> None:
    """Deliver everything queued now, here, ignoring the cooldown."""
    _each_target(lambda target: _drain(target, None, True))


def _drain_quietly(target: dict, home: Path | None, force: bool) -> None:
    try:
        _drain(target, home, force)
    except Exception:  # noqa: BLE001 - a background drain has nobody to raise to
        log.warning("brindle: audit streaming failed", exc_info=True)


__all__ = ["configure", "deliver", "enqueue", "flush", "kick", "queue_path", "syslog_message"]
