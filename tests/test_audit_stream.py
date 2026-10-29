"""Audit streaming: records written while the SIEM is unreachable are queued
durably and delivered in order once it is back. A fake transport stands in
for the network."""

import pytest

from brindle.pro import audit_stream
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
    audit_stream.configure([TARGET], home=brindle_home, transport=siem, clock=lambda: now[0])
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
    audit_stream.configure([TARGET], home=brindle_home, transport=siem, clock=lambda: 0.0)
    chain = AuditChain(REPO, home=brindle_home, entitled=lambda: True)
    chain.emit(ev())
    chain.emit(ev())
    siem.up = True
    chain.emit(ev())                                 # still cooling down: nothing sent
    assert siem.got == []
    audit_stream.flush()
    assert siem.got == [1, 2, 3]

    path = audit_stream.queue_path(TARGET, brindle_home)
    with audit_stream._try_drain_lock(path) as mine:  # another process is draining
        assert mine
        chain.emit(ev())
    assert siem.got == [1, 2, 3]                     # queued, not sent, and not waited for
    assert len(audit_stream._read_queue(path)) == 1
