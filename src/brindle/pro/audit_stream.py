"""Audit streaming (brindle Enterprise): a sink on the ``audit`` events plugin
(``audit_chain.AuditChain.emit``, registered through ``brindle.events``) that
sends each audit record, as it is written, to a company SIEM.

Targets are plain values handed to :func:`configure` (a list, or a callable
returning one); this module reads no policy::

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
a target that is down loses nothing: the queue is drained, in order, on the next
write or on :func:`flush`. A failed send stops the drain and leaves the rest
queued. After a failure the target is left alone for ``COOLDOWN_SECONDS``
(``flush`` ignores that), and every send is bounded by ``SEND_SECONDS``, so
the agent never waits on a dead SIEM. Delivery is at least once. Nothing here
raises into brindle.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

from brindle.pro.audit_chain import _locked, _open_private, _write_atomic, audit_dir, canonical, sha256

log = logging.getLogger(__name__)

SEND_SECONDS = 3
COOLDOWN_SECONDS = 30
SYSLOG_PRI = 13 * 8 + 6          # facility "log audit", severity informational
KINDS = ("syslog", "https")

_targets = []                    # a list of target dicts, or a callable returning one
_home: Path | None = None
_transport = None                # (target, record) -> None, raising when it can't deliver
_clock = time.monotonic
_down_until: dict[str, float] = {}


def configure(targets=None, *, home: Path | None = None, transport=None, clock=None) -> None:
    """Set the targets (a list or a ``targets_provider`` callable); None or an
    empty list turns audit streaming off. ``transport`` replaces the real
    network sender (tests)."""
    global _targets, _home, _transport, _clock
    _targets = targets if targets is not None else []
    _home = home
    _transport = transport
    _clock = clock or time.monotonic
    _down_until.clear()


def _current() -> list[dict]:
    raw = _targets() if callable(_targets) else _targets
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


def _enqueue(path: Path, record: dict | None) -> list[bytes]:
    """Add ``record`` (if any) at the end of the queue; returns the queue."""
    with _locked(path):
        if record is not None:
            fd = _open_private(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                os.write(fd, (canonical(record) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        return _read_queue(path)


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
    with urllib.request.urlopen(req, timeout=SEND_SECONDS) as resp:
        if resp.status >= 300:
            raise OSError(f"the SIEM answered {resp.status}")


# -- sending --------------------------------------------------------------------------------------


def _drain(target: dict, record: dict | None, home: Path | None, force: bool) -> None:
    path = queue_path(target, home)
    queued = _enqueue(path, record)
    tid = _target_id(target)
    if not queued or (not force and _clock() < _down_until.get(tid, 0.0)):
        return
    send = _transport or deliver
    sent = 0
    try:
        for line in queued:
            send(target, json.loads(line))
            sent += 1
        _down_until.pop(tid, None)
    except Exception:  # noqa: BLE001 - an unreachable target leaves the rest queued
        _down_until[tid] = _clock() + COOLDOWN_SECONDS
        log.warning("brindle: audit streaming target %s is unreachable; %d record(s) queued",
                    target["url"], len(queued) - sent)
    finally:
        if sent:
            _drop_sent(path, sent)


def send(record: dict, home: Path | None = None) -> None:
    """Queue ``record`` for every target and deliver what the targets accept."""
    _run(record, home, force=False)


def flush() -> None:
    """Try to deliver everything queued now, ignoring the cooldown."""
    _run(None, None, force=True)


def _run(record: dict | None, home: Path | None, *, force: bool) -> None:
    try:
        targets = _current()
    except Exception:  # noqa: BLE001 - a broken provider must not touch the agent
        log.warning("brindle: couldn't read the audit streaming targets", exc_info=True)
        return
    for target in targets:
        try:
            _drain(target, record, home, force)
        except Exception:  # noqa: BLE001 - never fail the operation being reported
            log.warning("brindle: audit streaming failed", exc_info=True)


__all__ = ["configure", "deliver", "flush", "queue_path", "send", "syslog_message"]
