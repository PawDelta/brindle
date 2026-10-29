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
        self.got.append(record["seq"])


@pytest.fixture(autouse=True)
def reset_stream():
    yield
    audit_stream.configure(None)


def test_records_written_while_the_target_is_down_arrive_in_order_once_it_is_up(brindle_home):
    siem, now = Siem(), [0.0]
    audit_stream.configure([TARGET], home=brindle_home, transport=siem, clock=lambda: now[0])
    chain = AuditChain(REPO, home=brindle_home, entitled=lambda: True)
    for i in range(3):
        chain.emit(ev(at=1_800_000_000 + i))
    assert siem.got == []
    assert len(audit_stream._read_queue(audit_stream.queue_path(TARGET, brindle_home))) == 3

    siem.up, now[0] = True, audit_stream.COOLDOWN_SECONDS + 1
    chain.emit(ev(at=1_800_000_010))                 # the next write drains the backlog first

    assert siem.got == [1, 2, 3, 4]
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
