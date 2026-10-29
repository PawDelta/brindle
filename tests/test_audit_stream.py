"""Audit streaming: records written while the SIEM is unreachable are queued
durably and delivered in order once it is back. A fake transport stands in
for the network."""

import email.message
import fcntl
import io
import os
import threading
import urllib.request
import urllib.response

import pytest

from brindle.pro import audit_chain, audit_stream
from brindle.pro.audit_chain import AuditChain
from test_audit_chain import ev

REPO = "/work/acme-app"
TARGET = {"kind": "syslog", "url": "udp://siem.example.com:514"}


class Siem:
    def __init__(self):
        self.up = False
        self.got = []

    def __call__(self, target, record):
        if not self.up:
            raise OSError("unreachable")
        event = record["event"]
        self.got.append((event["kind"], event["dropped"]) if "dropped" in event else record["seq"])


@pytest.fixture(autouse=True)
def reset_stream():
    yield
    audit_stream.configure(None)


@pytest.mark.parametrize("cap, queued, delivered", [
    (50_000, 3, [1, 2, 3, 4]),
    (2, 2, [1, 2, ("audit_stream.dropped", 2)]),     # a full queue keeps the oldest and reports the gap
])
def test_records_written_while_the_target_is_down_arrive_in_order_once_it_is_up(
        brindle_home, monkeypatch, cap, queued, delivered):
    monkeypatch.setattr(audit_stream, "MAX_QUEUE", cap)
    siem, now = Siem(), [0.0]
    audit_stream.configure([TARGET], home=brindle_home, transport=siem, clock=lambda: now[0], spawn=lambda fn: fn())
    chain = AuditChain(REPO, home=brindle_home, entitled=lambda: True)
    for i in range(3):
        chain.emit(ev(at=1_800_000_000 + i))
    assert siem.got == []
    assert len(audit_stream._read_queue(audit_stream.queue_path(TARGET, brindle_home))) == queued

    siem.up, now[0] = True, audit_stream.COOLDOWN_SECONDS + 1
    chain.emit(ev(at=1_800_000_010))                 # the next write drains the backlog first

    assert siem.got == delivered
    assert not audit_stream.queue_path(TARGET, brindle_home).exists()


def test_flush_delivers_during_the_cooldown_after_a_failed_send(brindle_home):
    siem = Siem()
    audit_stream.configure([TARGET], home=brindle_home, transport=siem, clock=lambda: 0.0, spawn=lambda fn: fn())
    chain = AuditChain(REPO, home=brindle_home, entitled=lambda: True)
    chain.emit(ev())
    chain.emit(ev())
    siem.up = True
    chain.emit(ev())                                 # still cooling down: nothing sent
    assert siem.got == []
    audit_stream.flush()
    assert siem.got == [1, 2, 3]

    path = audit_stream.queue_path(TARGET, brindle_home)
    other = audit_stream._take_drain(path)           # another process is draining
    assert other is not None
    chain.emit(ev())
    audit_stream._release_drain(other)
    assert siem.got == [1, 2, 3]                     # queued, not sent, and not waited for
    assert len(audit_stream._read_queue(path)) == 1


def lock_is_held(lock_file):
    """Whether someone holds the flock on ``lock_file`` (probed without waiting)."""
    fd = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def test_emit_returns_before_delivery_and_one_drainer_sends_everything(brindle_home, monkeypatch):
    pending, siem = [], Siem()
    siem.up = True
    chain = AuditChain(REPO, home=brindle_home, entitled=lambda: True)
    inner, release_under_queue_lock = [], []
    release = audit_stream._release_drain

    def probe_release(fd):                           # a writer arriving after the empty check must
        release_under_queue_lock.append(              # find the drain lock free, so this happens
            lock_is_held(audit_chain._lock_path(audit_stream.queue_path(TARGET, brindle_home))))
        release(fd)                                  # while the queue lock is still held

    monkeypatch.setattr(audit_stream, "_release_drain", probe_release)

    def gated(target, record):                       # the drain is mid-send when more writes arrive
        if not inner:
            inner.append(True)
            chain.emit(ev())
            for fn in list(pending[1:]):
                fn()                                 # a second drainer finds the lock taken
        siem(target, record)

    audit_stream.configure([TARGET], home=brindle_home, transport=gated, spawn=pending.append)
    chain.emit(ev())
    assert siem.got == [] and len(pending) == 1      # queued; delivery has not started
    pending[0]()
    assert siem.got == [1, 2]                        # the running drain also took the later record
    assert release_under_queue_lock == [True]

    # A write that lands after the drainer has read the (by then empty) queue but before it
    # gives up the drain lock: the writer's own drain finds that lock taken and leaves, so
    # the write must wait for the queue lock until the drainer has let go of both.
    siem, pending = Siem(), []
    siem.up = True
    audit_stream.configure([TARGET], home=brindle_home, transport=siem, spawn=pending.append)
    chain.emit(ev())
    monkeypatch.setattr(audit_stream, "_spawn", lambda fn: fn())
    looking, go, held = threading.Event(), threading.Event(), []

    def after_empty_snapshot():
        if not held:                                 # the pass after record 3 went out
            held.append(True)
            looking.set()
            go.wait()

    monkeypatch.setattr(audit_stream, "_after_empty_snapshot", after_empty_snapshot)
    drainer = threading.Thread(target=pending[0])
    drainer.start()
    looking.wait()
    writers = []
    if lock_is_held(audit_chain._lock_path(audit_stream.queue_path(TARGET, brindle_home))):
        writers.append(threading.Thread(target=chain.emit, args=(ev(),)))   # blocks on the queue lock
        writers[0].start()
    else:
        chain.emit(ev())                             # queued at once; its drain finds the lock taken
    go.set()
    drainer.join()
    for writer in writers:
        writer.join()
    assert siem.got == [3, 4]
    assert not audit_stream.queue_path(TARGET, brindle_home).exists()


def test_a_record_is_queued_under_the_chain_lock(brindle_home, monkeypatch):
    held = []

    def probe(rec, home=None):
        held.append(lock_is_held(audit_chain._lock_path(audit_chain.log_path(REPO, brindle_home))))

    monkeypatch.setattr(audit_stream, "enqueue", probe)
    audit_stream.configure([TARGET], home=brindle_home, spawn=lambda fn: None)
    AuditChain(REPO, home=brindle_home, entitled=lambda: True).emit(ev())
    assert held == [True]


class FakeWeb(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Answers every request with a redirect and remembers what was asked."""

    def __init__(self):
        urllib.request.HTTPHandler.__init__(self)
        urllib.request.HTTPSHandler.__init__(self)
        self.asked = []

    def https_open(self, req):
        self.asked.append((req.get_method(), req.full_url))
        headers = email.message.Message()
        headers["Location"] = "http://elsewhere.example/collect"
        resp = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        resp.msg = "Found"
        return resp

    http_open = https_open


def test_a_redirecting_siem_keeps_the_record_queued_and_gets_no_second_request(brindle_home, monkeypatch):
    web = FakeWeb()
    monkeypatch.setattr(audit_stream, "_opener", audit_stream._build_opener(web))
    target = {"kind": "https", "url": "https://siem.example.com/ingest", "headers": {"Authorization": "x"}}
    audit_stream.configure([target], home=brindle_home, spawn=lambda fn: fn())
    AuditChain(REPO, home=brindle_home, entitled=lambda: True).emit(ev())
    assert web.asked == [("POST", "https://siem.example.com/ingest")]
    assert len(audit_stream._read_queue(audit_stream.queue_path(target, brindle_home))) == 1
