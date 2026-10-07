"""Audit export (brindle Enterprise, ``audit_export``): the audit chain and the
Team feed shipped to a webhook, Splunk HEC, Datadog or S3 from a durable
cursor/spool, and local retention that keeps the chain verifiable. Sinks are
fakes; nothing here touches the network."""

import hashlib
import hmac
import json
import sys
import time
import types

import pytest
from typer.testing import CliRunner

from brindle import airgap
from brindle.cli import app
from brindle.config import user_config_path
from brindle.events import Event
from brindle.pro import audit_chain, audit_export, credentials
from brindle.pro.audit_chain import canonical, verify
from brindle.pro.audit_export import AuditExporter, NotConfigured, Sink, SinkError
from pro_fixtures import BASE, claims, pro_env, sign, signing_key  # noqa: F401 - fixtures

REPO = "/work/acme-app"
DAY = 86400.0


class FakeSink(Sink):
    def __init__(self, name="fake", fail=None):
        super().__init__(name)
        self.batches: list[list[dict]] = []
        self.fail = fail              # None, "retry", "drop" or an Exception

    def send(self, records):
        if self.fail == "retry":
            raise SinkError("receiver is down")
        if self.fail == "drop":
            raise SinkError("receiver said 400", drop=True)
        if isinstance(self.fail, Exception):
            raise self.fail
        self.batches.append(list(records))

    @property
    def records(self):
        return [r for b in self.batches for r in b]


class FakeTransport:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def post(self, url, headers, body):
        self.calls.append((url, headers, body))
        return self.status


def ev(kind="merge", **over):
    base = dict(kind=kind, repo_root=REPO, agent_id="worker-7", branch="feat/zebra", profile="developer",
                provider="claude", model="claude-sonnet-4", actor="supervisor-1",
                at=1_800_000_000.5, workspace_id="ws-1")
    base.update(over)
    return Event(**base)


def write_chain(home, n=3, *, age_days=0.0, start=0):
    """``n`` chain records, each ``age_days`` old."""
    for i in range(n):
        audit_chain.append(REPO, audit_chain.event_record(ev(at=1_800_000_000 + start + i)), home=home,
                           ts=audit_chain._iso(time.time() - age_days * DAY))


def exporter(home, sinks, **kw):
    kw.setdefault("entitled", lambda: True)
    return AuditExporter(REPO, home=home, sinks=sinks, start_thread=False, **kw)


def log_name(home):
    return audit_chain.log_path(REPO, home).name


# -- shipping the chain -----------------------------------------------------------------------


def test_chain_records_ship_in_order_with_their_chain_hash(brindle_home):
    write_chain(brindle_home, 3)
    sink = FakeSink()
    res = exporter(brindle_home, [sink]).flush()
    assert res["fake"].sent == 3 and res["fake"].error is None
    recs = sink.records
    assert [r["seq"] for r in recs] == [1, 2, 3]
    on_disk = audit_chain.read_records(audit_chain.log_path(REPO, brindle_home))
    for sent, disk in zip(recs, on_disk):
        assert sent["source"] == "audit_chain" and sent["log"] == log_name(brindle_home)
        assert {k: sent[k] for k in audit_chain.RECORD_KEYS} == disk       # as written, signature and all
    assert recs[1]["prev_hash"] == audit_chain.record_hash(on_disk[0])    # the receiver can re-check the chain
    assert recs[0]["event"]["kind"] == "merge" and recs[0]["event"]["agent"] == "worker-7"


def test_a_second_flush_sends_only_what_is_new(brindle_home):
    write_chain(brindle_home, 2)
    sink, ex = FakeSink(), None
    ex = exporter(brindle_home, [sink])
    ex.flush()
    assert ex.flush()["fake"].sent == 0
    write_chain(brindle_home, 2, start=10)
    assert ex.flush()["fake"].sent == 2
    assert [r["seq"] for r in sink.records] == [1, 2, 3, 4]
    # A new exporter (another process, a restart) resumes from the saved cursor.
    again = FakeSink()
    exporter(brindle_home, [again]).flush()
    assert again.records == []


def test_batches_are_capped(brindle_home, monkeypatch):
    monkeypatch.setattr(audit_export, "BATCH", 2)
    write_chain(brindle_home, 5)
    sink = FakeSink()
    exporter(brindle_home, [sink]).flush()
    assert [len(b) for b in sink.batches] == [2, 2, 1]


def test_each_sink_has_its_own_cursor(brindle_home):
    write_chain(brindle_home, 2)
    good, bad = FakeSink("good"), FakeSink("bad", fail="retry")
    ex = exporter(brindle_home, [bad, good])
    res = ex.flush()
    assert res["good"].sent == 2 and res["bad"].sent == 0 and "down" in res["bad"].error
    bad.fail = None
    res = ex.flush(force=True)                    # force: don't wait out the backoff
    assert res["bad"].sent == 2 and res["good"].sent == 0
    assert [r["seq"] for r in bad.records] == [1, 2] and len(good.records) == 2


def test_a_failed_send_is_retried_with_backoff_from_disk(brindle_home):
    write_chain(brindle_home, 2)
    now = [1000.0]
    sink = FakeSink(fail="retry")
    ex = exporter(brindle_home, [sink], clock=lambda: now[0])
    assert ex.flush()["fake"].error
    sink.fail = None
    assert "down" in ex.flush()["fake"].error and sink.records == []     # still backing off
    now[0] += audit_export.MAX_BACKOFF + 1
    assert ex.flush()["fake"].sent == 2
    # And across a restart: a new exporter with no memory of the failure.
    sink2 = FakeSink(fail="retry")
    write_chain(brindle_home, 1, start=50)
    exporter(brindle_home, [sink2]).flush()
    sink2.fail = None
    assert exporter(brindle_home, [sink2]).flush(force=True)["fake"].sent == 1


def test_a_batch_the_receiver_rejects_is_dropped_not_retried_forever(brindle_home):
    write_chain(brindle_home, 2)
    sink = FakeSink(fail="drop")
    res = exporter(brindle_home, [sink]).flush()
    assert res["fake"].dropped == 2 and res["fake"].error is None
    sink.fail = None
    write_chain(brindle_home, 1, start=9)
    assert exporter(brindle_home, [sink]).flush()["fake"].sent == 1


def test_an_unexpected_sink_exception_is_contained(brindle_home):
    write_chain(brindle_home, 1)
    good = FakeSink("good")
    res = exporter(brindle_home, [FakeSink("boom", fail=RuntimeError("bug")), good]).flush()
    assert "RuntimeError" in res["boom"].error and res["good"].sent == 1


def test_every_audit_log_is_shipped(brindle_home):
    audit_chain.append("/work/a", audit_chain.event_record(ev(repo_root="/work/a")), home=brindle_home)
    audit_chain.append("/work/b", audit_chain.event_record(ev(repo_root="/work/b")), home=brindle_home)
    sink = FakeSink()
    exporter(brindle_home, [sink]).flush()
    assert {r["log"] for r in sink.records} == {audit_chain.log_path("/work/a", brindle_home).name,
                                                audit_chain.log_path("/work/b", brindle_home).name}


# -- the gates --------------------------------------------------------------------------------


def test_not_entitled_sends_and_spools_nothing(brindle_home):
    write_chain(brindle_home, 2)
    sink = FakeSink()
    ex = AuditExporter(REPO, home=brindle_home, sinks=[sink], entitled=lambda: False,
                       start_thread=False, team_payload=lambda e: {"kind": "merge", "at": 1.0})
    assert ex.flush() == {}
    ex.emit(ev())
    assert sink.records == [] and not ex.store.spool_path.exists()


def test_the_real_gate_fails_closed_and_needs_audit_export(brindle_home, signing_key):
    from brindle.pro import license

    sink = FakeSink()
    ex = AuditExporter(REPO, home=brindle_home, sinks=[sink], start_thread=False)   # the real gate
    write_chain(brindle_home, 1)
    assert ex.flush() == {}                                    # no login at all
    credentials.default_store().save({"base_url": BASE, "entitlement": sign(
        signing_key, claims(features=["audit", "team"]))})
    license.clear_cache()
    assert ex.flush() == {} and not ex.entitled(offline=True)
    credentials.default_store().save({"base_url": BASE, "entitlement": sign(
        signing_key, claims(features=["audit_export"]))})
    license.clear_cache()
    assert ex.entitled() and ex.entitled(offline=True)
    assert ex.flush()["fake"].sent == 1


def test_air_gap_mode_sends_nothing(brindle_home, monkeypatch):
    write_chain(brindle_home, 1)
    sink = FakeSink()
    monkeypatch.setenv(airgap.ENV, "1")
    ex = exporter(brindle_home, [sink], team_payload=lambda e: {"kind": "merge", "at": 1.0})
    assert ex.flush() == {}
    ex.emit(ev())
    assert sink.records == [] and not ex.store.spool_path.exists()


def test_emit_never_raises(brindle_home):
    def boom(event):
        raise RuntimeError("no key")

    exporter(brindle_home, [FakeSink()], team_payload=boom).emit(ev())
    AuditExporter(REPO, settings={"audit_export": "nonsense"}, entitled=lambda: True).emit(ev())


# -- the Team feed ----------------------------------------------------------------------------


def test_team_events_are_spooled_and_carry_a_content_hash_and_the_chain_hash(brindle_home):
    payload = {"kind": "merge", "agent_ref": "a" * 64, "branch_ref": None, "profile": "developer",
               "provider": "claude", "model": None, "actor_ref": "user", "at": 1_800_000_000.0,
               "approved": None, "merged": True}
    write_chain(brindle_home, 1)                                   # the same event, as the chain saw it
    sink = FakeSink()
    ex = exporter(brindle_home, [sink], team_payload=lambda e: payload)
    ex.emit(ev())
    res = ex.flush()
    assert res["fake"].sent == 2
    chain_rec, team = sink.records
    assert chain_rec["source"] == "audit_chain" and team["source"] == "team_events"
    assert team["event"] == payload and "worker-7" not in json.dumps(team)      # refs, never raw names
    assert team["hash"] == hashlib.sha256(canonical(payload).encode()).hexdigest()
    assert team["chain_hash"] == chain_rec["hash"]
    assert team["ts"].endswith("Z")
    assert exporter(brindle_home, [sink]).flush()["fake"].sent == 0           # the cursor moved past it


def test_without_a_team_payload_only_the_chain_ships(brindle_home):
    write_chain(brindle_home, 1)
    sink = FakeSink()
    ex = exporter(brindle_home, [sink], team_payload=lambda e: None)
    ex.emit(ev())
    ex.flush()
    assert [r["source"] for r in sink.records] == ["audit_chain"]


def test_the_spool_is_trimmed_once_every_sink_has_it(brindle_home):
    a, b = FakeSink("a"), FakeSink("b", fail="retry")
    ex = exporter(brindle_home, [a, b], team_payload=lambda e: {"kind": "merge", "at": 1.0})
    ex.emit(ev())
    ex.flush()
    assert len(ex.store._spool_entries()) == 1              # b hasn't got it
    b.fail = None
    ex.flush(force=True)
    assert ex.store._spool_entries() == []


def test_files_are_private(brindle_home):
    import os
    import stat

    write_chain(brindle_home, 1)
    ex = exporter(brindle_home, [FakeSink()], team_payload=lambda e: {"kind": "merge", "at": 1.0})
    ex.emit(ev())
    ex.flush()
    for p in (ex.store.state_path, ex.store.spool_path):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, p.name


# -- the real sinks ---------------------------------------------------------------------------


RECORDS = [{"source": "audit_chain", "log": "x.jsonl", "seq": 1, "ts": "2026-10-07T10:00:00.000000Z",
            "event": {"kind": "merge"}, "prev_hash": "0" * 64, "hash": "h" * 64, "sig": "s"}]


def test_webhook_signs_the_body_with_hmac(monkeypatch):
    monkeypatch.setenv("WH_SECRET", "s3cret")
    t = FakeTransport()
    sink = audit_export.build_sink({"type": "webhook", "url": "https://siem.example.com/in",
                                    "secret_env": "WH_SECRET"}, "webhook", transport=t)
    sink.send(RECORDS)
    url, headers, body = t.calls[0]
    assert url == "https://siem.example.com/in"
    assert headers["X-Brindle-Signature"] == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    doc = json.loads(body)
    assert doc["records"] == RECORDS and doc["source"] == "brindle" and doc["version"] == 1


def test_secrets_come_from_the_environment_never_the_config(monkeypatch):
    spec = {"type": "webhook", "url": "https://siem.example.com/in", "secret_env": "WH_SECRET"}
    monkeypatch.delenv("WH_SECRET", raising=False)
    with pytest.raises(NotConfigured, match="WH_SECRET is not set"):
        audit_export.build_sink(spec, "webhook", transport=FakeTransport())
    with pytest.raises(NotConfigured, match="must name an environment variable"):
        audit_export.build_sink({**spec, "secret_env": "not a name"}, "webhook")
    with pytest.raises(NotConfigured, match="secret_env"):
        audit_export.build_sink({k: v for k, v in spec.items() if k != "secret_env"}, "webhook")


@pytest.mark.parametrize("url", ["http://siem.example.com/in", "ftp://x/y", "siem.example.com", "https:///x"])
def test_urls_must_be_https(url, monkeypatch):
    monkeypatch.setenv("WH_SECRET", "x")
    with pytest.raises(NotConfigured, match="https"):
        audit_export.build_sink({"type": "webhook", "url": url, "secret_env": "WH_SECRET"}, "w")


def test_http_statuses_map_to_retry_or_drop(monkeypatch):
    monkeypatch.setenv("WH_SECRET", "x")
    t = FakeTransport(500)
    sink = audit_export.build_sink({"type": "webhook", "url": "https://h.example.com/", "secret_env": "WH_SECRET"},
                                   "w", transport=t)
    with pytest.raises(SinkError) as e:
        sink.send(RECORDS)
    assert not e.value.drop
    for status, drop in ((401, False), (403, False), (429, False), (400, True), (413, True), (422, True)):
        t.status = status
        with pytest.raises(SinkError) as e:
            sink.send(RECORDS)
        assert e.value.drop is drop, status
    t.status = 204
    sink.send(RECORDS)


def test_splunk_hec_format(monkeypatch):
    monkeypatch.setenv("HEC", "tok-1")
    t = FakeTransport()
    sink = audit_export.build_sink({"type": "splunk", "url": "https://splunk.example.com:8088",
                                    "token_env": "HEC", "index": "audit", "host": "h1"}, "splunk", transport=t)
    sink.send(RECORDS * 2)
    url, headers, body = t.calls[0]
    assert url == "https://splunk.example.com:8088/services/collector/event"
    assert headers["Authorization"] == "Splunk tok-1"
    events = [json.loads(ln) for ln in body.decode().split("\n")]
    assert len(events) == 2 and events[0]["event"] == RECORDS[0] and events[0]["index"] == "audit"
    assert events[0]["sourcetype"] == "brindle:audit" and events[0]["host"] == "h1"
    assert abs(events[0]["time"] - 1_791_367_200) < 5 * DAY       # parsed from the record's ts


def test_datadog_logs_format(monkeypatch):
    monkeypatch.setenv("DDKEY", "dd-1")
    t = FakeTransport(202)
    sink = audit_export.build_sink({"type": "datadog", "site": "datadoghq.eu", "api_key_env": "DDKEY",
                                    "tags": ["env:prod", "team:sec"], "host": "h1"}, "datadog", transport=t)
    sink.send(RECORDS)
    url, headers, body = t.calls[0]
    assert url == "https://http-intake.logs.datadoghq.eu/api/v2/logs" and headers["DD-API-KEY"] == "dd-1"
    (log,) = json.loads(body)
    assert log["ddsource"] == "brindle" and log["ddtags"] == "env:prod,team:sec" and log["hash"] == "h" * 64
    assert json.loads(log["message"]) == RECORDS[0]
    with pytest.raises(NotConfigured, match="site"):
        audit_export.build_sink({"type": "datadog", "site": "evil.com/x?", "api_key_env": "DDKEY"}, "d")


class FakeS3:
    def __init__(self, fail=False):
        self.puts, self.fail = [], fail

    def put_object(self, **kw):
        if self.fail:
            raise OSError("throttled")
        self.puts.append(kw)


def test_s3_writes_one_jsonl_object_per_batch():
    s3 = FakeS3()
    sink = audit_export.build_sink({"type": "s3", "bucket": "acme-audit", "prefix": "brindle"}, "s3",
                                   s3_client=s3)
    sink.send(RECORDS * 2)
    sink.send(RECORDS * 2)
    (a, b) = s3.puts
    assert a["Bucket"] == "acme-audit" and a["Key"].startswith("brindle/") and a["Key"].endswith(".jsonl")
    assert a["Key"] == b["Key"]                                      # a retried batch overwrites itself
    assert [json.loads(ln) for ln in a["Body"].decode().splitlines()] == RECORDS * 2
    s3.fail = True
    with pytest.raises(SinkError, match="OSError"):
        sink.send(RECORDS)


def test_s3_needs_boto3(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)                  # as if it isn't installed
    with pytest.raises(NotConfigured, match="boto3"):
        audit_export.build_sink({"type": "s3", "bucket": "acme-audit"}, "s3")


def test_s3_builds_its_client_from_boto3(monkeypatch):
    seen = {}
    fake = types.SimpleNamespace(client=lambda svc, **kw: seen.update(svc=svc, **kw) or FakeS3())
    monkeypatch.setenv("AK", "ak")
    monkeypatch.setenv("SK", "sk")
    audit_export.build_sink({"type": "s3", "bucket": "acme-audit", "region": "eu-west-1",
                             "access_key_env": "AK", "secret_key_env": "SK"}, "s3", boto3_module=fake)
    assert seen == {"svc": "s3", "region_name": "eu-west-1", "aws_access_key_id": "ak",
                    "aws_secret_access_key": "sk"}
    with pytest.raises(NotConfigured, match="bucket"):
        audit_export.build_sink({"type": "s3", "bucket": "X"}, "s3", boto3_module=fake)


def test_a_sink_that_cant_be_built_is_reported_and_the_others_still_ship(brindle_home, monkeypatch):
    monkeypatch.delenv("WH_SECRET", raising=False)
    write_chain(brindle_home, 1)
    t = FakeTransport()
    monkeypatch.setenv("DDKEY", "k")
    ex = AuditExporter(REPO, home=brindle_home, entitled=lambda: True, start_thread=False, transport=t,
                       settings={"audit_export": {"sinks": [
                           {"type": "webhook", "url": "https://h.example.com/", "secret_env": "WH_SECRET"},
                           {"type": "datadog", "api_key_env": "DDKEY"},
                           {"type": "carrier-pigeon"}]}})
    res = ex.flush()
    assert "WH_SECRET" in res["webhook"].error and "unknown sink type" in res["carrier-pigeon"].error
    assert res["datadog"].sent == 1 and len(t.calls) == 1


def test_sink_names_are_stable_and_unique():
    assert audit_export.sink_names([{"type": "s3"}, {"type": "s3"}, {"type": "s3", "name": "cold"}]) \
        == ["s3", "s3-2", "cold"]


def test_config_validation():
    assert audit_export.parse_config(None) == audit_export.ExportConfig()
    cfg = audit_export.parse_config({"retention_days": 30, "sinks": [{"type": "s3"}]})
    assert cfg.retention_days == 30.0 and cfg.sinks == ({"type": "s3"},)
    for bad in ("x", {"sinks": "x"}, {"sinks": ["x"]}, {"retention_days": 0}, {"retention_days": "9"},
                {"retention_days": True}):
        with pytest.raises(NotConfigured):
            audit_export.parse_config(bad)


# -- retention --------------------------------------------------------------------------------


def test_pruning_keeps_the_chain_verifiable_via_a_signed_checkpoint(brindle_home):
    write_chain(brindle_home, 4, age_days=100)
    write_chain(brindle_home, 2, age_days=1, start=10)
    path = audit_chain.log_path(REPO, brindle_home)
    before = audit_chain.read_records(path)
    assert audit_chain.prune(REPO, retention_days=30, home=brindle_home) == 4
    report = verify(REPO, home=brindle_home)
    assert report.ok and report.records == 2 and "pruned" in report.describe(), report.describe()
    lines = path.read_bytes().split(b"\n")[:-1]
    cp = json.loads(lines[0])
    assert cp["kind"] == "checkpoint" and cp["seq"] == 4 and cp["pruned"] == 4
    assert cp["through_hash"] == audit_chain.record_hash(before[3])
    assert audit_chain.signature_ok(audit_chain.public_key(brindle_home), cp["hash"], cp["sig"])
    assert [json.loads(ln)["seq"] for ln in lines[1:]] == [5, 6]
    # Appending carries on from the last record, and the chain still verifies.
    write_chain(brindle_home, 1, start=20)
    assert [r["seq"] for r in audit_chain.read_records(path)][-1] == 7
    assert verify(REPO, home=brindle_home).ok
    # A second prune folds into one checkpoint.
    assert audit_chain.prune(REPO, retention_days=30, home=brindle_home, now=time.time() + 60 * DAY) == 2
    report = verify(REPO, home=brindle_home)
    assert report.ok and report.records == 1
    cp = json.loads(path.read_bytes().split(b"\n")[0])
    assert cp["seq"] == 6 and cp["pruned"] == 6


def test_the_newest_record_is_never_pruned(brindle_home):
    write_chain(brindle_home, 3, age_days=500)
    assert audit_chain.prune(REPO, retention_days=1, home=brindle_home) == 2
    assert verify(REPO, home=brindle_home).records == 1
    assert audit_chain.prune(REPO, retention_days=1, home=brindle_home) == 0
    write_chain(brindle_home, 1)
    assert verify(REPO, home=brindle_home).ok


def test_nothing_to_prune_changes_nothing(brindle_home):
    write_chain(brindle_home, 3)
    path = audit_chain.log_path(REPO, brindle_home)
    raw = path.read_bytes()
    assert audit_chain.prune(REPO, retention_days=30, home=brindle_home) == 0
    assert path.read_bytes() == raw
    with pytest.raises(audit_chain.AuditError):
        audit_chain.prune(REPO, retention_days=0, home=brindle_home)


def test_tampering_with_a_pruned_log_is_still_caught(brindle_home):
    write_chain(brindle_home, 5, age_days=100)
    audit_chain.prune(REPO, retention_days=30, home=brindle_home)
    path = audit_chain.log_path(REPO, brindle_home)
    good = path.read_bytes().split(b"\n")[:-1]

    def put(lines):
        path.write_bytes(b"\n".join(lines) + b"\n")

    cp = json.loads(good[0])
    put([canonical({**cp, "through_hash": "f" * 64}).encode(), *good[1:]])
    report = verify(REPO, home=brindle_home)
    assert report.broken_seq == 1 and "checkpoint" in report.reason and "altered" in report.reason
    forged = {**cp, "seq": 2}
    forged["hash"] = audit_chain.checkpoint_hash(forged)           # rehashed without the key
    put([canonical(forged).encode(), *good[1:]])
    assert "signature" in verify(REPO, home=brindle_home).reason
    put(good[1:])                                                  # checkpoint removed
    assert verify(REPO, home=brindle_home).broken_seq == 1
    put([good[0]])                                                 # records removed after it
    assert not verify(REPO, home=brindle_home).ok
    rec = json.loads(good[1])
    rec["event"]["merged"] = True
    put([good[0], canonical(rec).encode()])
    assert verify(REPO, home=brindle_home).broken_seq == 5
    put(good)
    assert verify(REPO, home=brindle_home).ok


def test_a_log_that_doesnt_verify_is_not_pruned(brindle_home):
    write_chain(brindle_home, 3, age_days=100)
    path = audit_chain.log_path(REPO, brindle_home)
    ls = path.read_bytes().split(b"\n")[:-1]
    rec = json.loads(ls[1])
    rec["event"]["actor"] = "x"
    ls[1] = canonical(rec).encode()
    path.write_bytes(b"\n".join(ls) + b"\n")
    with pytest.raises(audit_chain.AuditError, match="doesn't verify"):
        audit_chain.prune(REPO, retention_days=30, home=brindle_home)
    assert path.read_bytes() == b"\n".join(ls) + b"\n"


def test_export_still_reads_a_pruned_log(brindle_home):
    write_chain(brindle_home, 3, age_days=100)
    audit_chain.prune(REPO, retention_days=30, home=brindle_home)
    out = audit_chain.export(REPO, home=brindle_home)
    assert [json.loads(ln).get("kind", "record") for ln in out.splitlines()] == ["checkpoint", "record"]
    assert audit_chain.export(REPO, home=brindle_home, fmt="csv").count("\n") == 3


def test_retention_never_prunes_what_a_sink_has_not_received(brindle_home):
    write_chain(brindle_home, 4, age_days=100)
    settings = {"audit_export": {"retention_days": 30}}
    sink = FakeSink(fail="retry")
    ex = exporter(brindle_home, [sink], settings=settings)
    ex.flush()
    assert verify(REPO, home=brindle_home).records == 4              # unshipped: kept
    sink.fail = None
    ex.flush(force=True)                                              # shipped, then pruned
    report = verify(REPO, home=brindle_home)
    assert report.ok and report.records == 1 and len(sink.records) == 4
    write_chain(brindle_home, 2, age_days=100, start=30)              # old, but not shipped yet
    sink2 = FakeSink()
    ex2 = exporter(brindle_home, [sink2], settings=settings)
    ex2.flush()
    assert [r["seq"] for r in sink2.records] == [5, 6] and verify(REPO, home=brindle_home).ok


def test_retention_applies_with_no_sinks_configured_when_entitled(brindle_home):
    write_chain(brindle_home, 3, age_days=100)
    ex = exporter(brindle_home, [], settings={"audit_export": {"retention_days": 30}})
    ex.flush()
    assert verify(REPO, home=brindle_home).records == 1


def test_a_sink_that_never_got_a_log_blocks_its_pruning(brindle_home):
    write_chain(brindle_home, 3, age_days=100)
    ex = exporter(brindle_home, [FakeSink("never")], settings={"audit_export": {"retention_days": 30}})
    assert ex.prune(30, ["never"]) == {}
    assert verify(REPO, home=brindle_home).records == 3


# -- the plugin and the CLI -------------------------------------------------------------------


def test_the_entry_point_is_registered():
    from importlib.metadata import entry_points

    assert [e.value for e in entry_points(group="brindle.events") if e.name == "audit_export"] \
        == ["brindle.pro.audit_export:make"]
    assert isinstance(audit_export.make(REPO), AuditExporter)


def test_the_sender_thread_ships_after_an_event(brindle_home):
    write_chain(brindle_home, 1)
    sink = FakeSink()
    ex = AuditExporter(REPO, home=brindle_home, sinks=[sink], entitled=lambda: True, start_thread=True)
    ex.emit(ev())
    deadline = time.time() + 5
    while not sink.records and time.time() < deadline:
        time.sleep(0.02)
    assert [r["seq"] for r in sink.records] == [1]


def test_without_sinks_an_event_costs_nothing(brindle_home):
    ex = AuditExporter(REPO, home=brindle_home, entitled=lambda: True, start_thread=False,
                       settings={}, team_payload=lambda e: {"kind": "merge", "at": 1.0})
    ex.emit(ev())
    assert ex.store.state_path.exists() is False and ex._thread is None


def login(signing_key, features):
    from brindle.pro import license

    credentials.default_store().save({"base_url": BASE, "entitlement": sign(signing_key, claims(features=features))})
    license.clear_cache()


def test_cli_ship_and_prune(brindle_home, repo, monkeypatch, signing_key):
    runner = CliRunner()
    monkeypatch.chdir(repo)
    root = str(repo)
    audit_chain.append(root, audit_chain.event_record(ev(repo_root=root)), home=brindle_home,
                       ts=audit_chain._iso(time.time() - 100 * DAY))
    audit_chain.append(root, audit_chain.event_record(ev(repo_root=root)), home=brindle_home)
    res = runner.invoke(app, ["audit", "ship"])
    assert res.exit_code == 2 and "plan doesn't include" in res.output or "no sinks" in res.output
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"audit_export": {"sinks": [
        {"type": "webhook", "url": "https://siem.example.com/in", "secret_env": "WH_SECRET"}]}}))
    login(signing_key, ["audit_export"])
    monkeypatch.delenv("WH_SECRET", raising=False)
    res = runner.invoke(app, ["audit", "ship"])
    assert res.exit_code == 1 and "WH_SECRET is not set" in res.output, res.output
    monkeypatch.setenv("WH_SECRET", "x")
    t = FakeTransport()
    monkeypatch.setattr(audit_export, "UrllibTransport", lambda *a, **kw: t)
    res = runner.invoke(app, ["audit", "ship"])
    assert res.exit_code == 0 and "webhook: sent 2" in res.output, res.output
    assert len(json.loads(t.calls[0][2])["records"]) == 2
    res = runner.invoke(app, ["audit", "prune"])
    assert res.exit_code == 2 and "retention_days" in res.output          # no retention configured
    res = runner.invoke(app, ["audit", "prune", "--days", "30"])
    assert res.exit_code == 0 and "pruned 1 record(s)" in res.output, res.output
    res = runner.invoke(app, ["audit", "verify"])
    assert res.exit_code == 0 and "chain intact" in res.output and "pruned" in res.output, res.output
    login(signing_key, ["audit"])
    res = runner.invoke(app, ["audit", "ship"])
    assert res.exit_code == 2 and "plan doesn't include" in res.output
