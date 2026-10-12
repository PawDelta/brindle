"""The ``audit`` events plugin (brindle Enterprise): a hash-chained, Ed25519
signed, append-only log of what brindle did, local to the install. Tamper
evidence is the point: every edit, removal or reordering must be caught at
the right seq. Without an ``audit`` entitlement it must write nothing."""

import csv
import io
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brindle import events, plugins, policy
from brindle.cli import app
from brindle.config import RepoConfig
from brindle.events import Event
from brindle.pro import audit_chain
from brindle.pro.audit_chain import (
    AuditChain, audit_dir, canonical, log_path, verify,
)
from pro_fixtures import pro_env  # noqa: F401 - fixtures
from test_plugins import Gate, Recorder, install

REPO = "/work/acme-app"
ENTITLED = lambda: True  # noqa: E731


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()


def ev(kind="merge", **over):
    base = dict(kind=kind, repo_root=REPO, agent_id="worker-7", branch="feat/zebra",
                profile="developer", provider="claude", model="claude-sonnet-4",
                actor="supervisor-1", at=1_800_000_000.5, workspace_id="ws-1")
    base.update(over)
    return Event(**base)


def chain(home, n=3, **over):
    p = AuditChain(REPO, home=home, entitled=ENTITLED)
    for i in range(n):
        p.emit(ev(kind=("assign", "review", "merge", "remove")[i % 4], at=1_800_000_000 + i, **over))
    return p


def workspace(root, branch="feat/x"):
    from brindle.db import Workspace

    return Workspace("ws-9", str(root), "x", "worktree", branch, "main", str(root / "wt"), None,
                     "brindle-x", time.time())


def lines(home):
    return log_path(REPO, home).read_bytes().split(b"\n")[:-1]


def rewrite(home, new_lines):
    log_path(REPO, home).write_bytes(b"\n".join(new_lines) + b"\n")


# -- the chain ------------------------------------------------------------------------------------


def test_every_signature_verifies_with_the_install_key(brindle_home):
    chain(brindle_home, 4)
    pub = audit_chain.public_key(brindle_home)
    for ln in lines(brindle_home):
        rec = json.loads(ln)
        assert audit_chain.signature_ok(pub, rec["hash"], rec["sig"])
        assert not audit_chain.signature_ok(pub, rec["hash"][::-1], rec["sig"])
    report = verify(REPO, home=brindle_home)
    assert report.ok and report.records == 4 and "chain intact" in report.describe()
    other = Path(str(brindle_home) + "-other")             # another install's key: not ours
    report = verify(REPO, home=brindle_home, pub=audit_chain.public_key(other))
    assert report.broken_seq == 1 and "signature" in report.reason


# -- tamper evidence ------------------------------------------------------------------------------


def test_editing_a_record_is_caught_at_its_seq(brindle_home):
    chain(brindle_home, 4)
    ls = lines(brindle_home)
    rec = json.loads(ls[2])
    rec["event"]["actor"] = "someone-else"
    ls[2] = canonical(rec).encode()
    rewrite(brindle_home, ls)
    report = verify(REPO, home=brindle_home)
    assert not report.ok and report.broken_seq == 3 and "altered" in report.reason
    assert report.records == 2


def test_rehashing_an_edited_record_without_the_key_is_caught_by_the_signature(brindle_home):
    chain(brindle_home, 3)
    ls = lines(brindle_home)
    rec = json.loads(ls[1])
    rec["event"]["approved"] = True
    rec["hash"] = audit_chain.body_hash(rec["seq"], rec["ts"], rec["event"], rec["prev_hash"])
    ls[1] = canonical(rec).encode()
    rewrite(brindle_home, ls)
    report = verify(REPO, home=brindle_home)
    assert report.broken_seq == 2 and "signature" in report.reason


def test_deleting_the_last_record_is_caught_by_the_head(brindle_home):
    chain(brindle_home, 5)
    ls = lines(brindle_home)
    rewrite(brindle_home, ls[:-1])
    report = verify(REPO, home=brindle_home)
    assert report.broken_seq == 5 and "truncated" in report.reason and report.records == 4
    rewrite(brindle_home, ls[:-2])
    assert verify(REPO, home=brindle_home).broken_seq == 4


def test_inserting_a_forged_record_breaks_the_chain(brindle_home):
    chain(brindle_home, 3)
    ls = lines(brindle_home)
    forged = json.loads(ls[1])
    forged["seq"] = 3
    ls.insert(2, canonical(forged).encode())
    rewrite(brindle_home, ls)
    report = verify(REPO, home=brindle_home)
    assert report.broken_seq == 3 and "prev_hash" in report.reason


def test_garbage_and_an_empty_log(brindle_home):
    assert verify(REPO, home=brindle_home).ok and not verify(REPO, home=brindle_home).exists
    chain(brindle_home, 2)
    rewrite(brindle_home, lines(brindle_home) + [b"not json"])
    report = verify(REPO, home=brindle_home)
    assert report.broken_seq == 3 and "unreadable" in report.reason


# -- files and permissions ------------------------------------------------------------------------


def test_files_are_private(brindle_home):
    chain(brindle_home, 1)
    d = audit_dir(brindle_home)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    for name in (log_path(REPO, brindle_home).name, audit_chain.KEY_FILE):
        assert stat.S_IMODE(os.stat(d / name).st_mode) == 0o600, name
    # Loosened by hand: the next append tightens them again.
    os.chmod(log_path(REPO, brindle_home), 0o644)
    os.chmod(d, 0o755)
    chain(brindle_home, 1)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(log_path(REPO, brindle_home)).st_mode) == 0o600


# -- the entitlement gate -------------------------------------------------------------------------


def test_emit_never_raises(brindle_home, monkeypatch):
    blocked = brindle_home / "audit"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("in the way")                 # the audit dir can't be made
    p = AuditChain(REPO, home=brindle_home, entitled=ENTITLED)
    p.emit(ev())
    assert p.dropped == 1

    def boom():
        raise RuntimeError("entitlement check exploded")

    AuditChain(REPO, home=brindle_home, entitled=boom).emit(ev())
    monkeypatch.setattr(audit_chain, "append", lambda *a, **kw: 1 / 0)
    AuditChain(REPO, home=brindle_home, entitled=ENTITLED).emit(ev())


# -- through brindle: fan-out, policy denials, merges and removals ------------------------------------


def test_every_events_plugin_hears_every_event(brindle_home, tmp_path, monkeypatch):
    rec = Recorder()
    audit = AuditChain(str(tmp_path), home=brindle_home, entitled=ENTITLED)
    install(monkeypatch, {plugins.EVENTS: [("audit", lambda r: audit), ("pro", lambda r: rec)]})
    from brindle.db import Agent

    ws = workspace(tmp_path)
    worker = Agent("w-1", "ws-9", "developer", "claude", "boss", "assign", "processing", "@0",
                   None, time.time())
    cfg = RepoConfig()
    assert {type(p) for p in events.plugins_for(cfg, str(tmp_path))} == {Recorder, AuditChain}
    events.emit(cfg, "merge", ws, worker, actor="boss")
    events.emit(cfg, "remove", ws, worker, actor="boss", merged=True)
    assert rec.kinds() == ["merge", "remove"]
    recs = audit_chain.read_records(log_path(str(tmp_path), brindle_home))
    assert [r["event"]["kind"] for r in recs] == ["merge", "remove"]
    assert recs[0]["event"]["workspace"] == "ws-9" and recs[1]["event"]["merged"] is True
    assert recs[0]["event"]["agent"] == "w-1" and recs[0]["event"]["actor"] == "boss"
    # The config can still name the plugins to use: one, several, or none.
    plugins.reset()
    assert [type(p) for p in events.plugins_for(RepoConfig(plugins={"events": "audit"}), "r")] \
        == [AuditChain]
    assert len(events.plugins_for(RepoConfig(plugins={"events": "pro, audit"}), "r")) == 2
    assert events.plugins_for(RepoConfig(plugins={"events": "off"}), "r") == []


def test_a_failing_plugin_does_not_silence_the_others(brindle_home, tmp_path, monkeypatch):
    audit = AuditChain(str(tmp_path), home=brindle_home, entitled=ENTITLED)
    install(monkeypatch, {plugins.EVENTS: [("bad", lambda r: Recorder(fail=True)),
                                           ("audit", lambda r: audit)]})
    events.emit(RepoConfig(), "merge", workspace(tmp_path), None)
    assert verify(str(tmp_path), home=brindle_home).records == 1


def test_policy_denials_are_recorded_with_the_reason(brindle_home, tmp_path, monkeypatch):
    audit = AuditChain(str(tmp_path), home=brindle_home, entitled=ENTITLED)
    install(monkeypatch, {plugins.EVENTS: [("audit", lambda r: audit)],
                          plugins.POLICY: [("gate", lambda r: Gate(assign="after hours",
                                                                   merge="needs two approvals"))]})
    from brindle.db import Agent

    cfg = RepoConfig()
    d = policy.check_assign(cfg, str(tmp_path), "developer", "the secret task", "handoff",
                            branch="feat/x", actor=Agent("boss", "ws", "supervisor", "claude", None,
                                                         "interactive", "idle", "@0", None, 0.0))
    assert not d.allowed
    assert not policy.check_merge(cfg, workspace(tmp_path, "feat/y"), None).allowed
    recs = audit_chain.read_records(log_path(str(tmp_path), brindle_home))
    assert [r["event"]["kind"] for r in recs] == ["deny_assign", "deny_merge"]
    a, m = (r["event"] for r in recs)
    assert a["reason"] == "after hours" and a["branch"] == "feat/x" and a["actor"] == "boss"
    assert a["profile"] == "developer" and "secret" not in json.dumps(recs)
    assert m["reason"] == "needs two approvals" and m["workspace"] == "ws-9" and m["branch"] == "feat/y"
    assert verify(str(tmp_path), home=brindle_home).ok


# -- export -----------------------------------------------------------------------------------------


def test_export_csv_has_one_row_per_record(brindle_home):
    chain(brindle_home, 2)
    out = audit_chain.export(REPO, home=brindle_home, fmt="csv")
    rows = list(csv.DictReader(io.StringIO(out)))
    assert list(rows[0]) == list(audit_chain.CSV_COLUMNS)
    assert [r["seq"] for r in rows] == ["1", "2"]
    assert rows[0]["kind"] == "assign" and rows[1]["kind"] == "review"
    assert rows[0]["agent"] == "worker-7" and rows[0]["approved"] == ""
    assert len(rows[0]["hash"]) == 64 and len(rows[0]["sig"]) == 128
    with pytest.raises(audit_chain.AuditError):
        audit_chain.export(REPO, home=brindle_home, fmt="xml")


def test_export_since_filters_by_record_time(brindle_home):
    p = AuditChain(REPO, home=brindle_home, entitled=ENTITLED)
    p.emit(ev())
    cut = datetime.now(timezone.utc)
    time.sleep(0.01)
    p.emit(ev(kind="remove"))
    out = audit_chain.export(REPO, home=brindle_home, since=cut)
    assert [json.loads(ln)["seq"] for ln in out.splitlines()] == [2]
    assert audit_chain.export(REPO, home=brindle_home, since=datetime(2000, 1, 1)).count("\n") == 2
    assert audit_chain.export(REPO, home=brindle_home, since=datetime(2999, 1, 1)) == ""
    assert audit_chain.parse_time("2026-09-30T10:00:00Z").tzinfo is not None
    assert audit_chain.parse_time("2026-09-30").tzinfo is not None


# -- the CLI --------------------------------------------------------------------------------------


def test_cli_verify_export_and_pubkey(brindle_home, repo, monkeypatch):
    runner = CliRunner()
    root = str(repo)
    AuditChain(root, entitled=ENTITLED).emit(ev(repo_root=root))
    AuditChain(root, entitled=ENTITLED).emit(ev(repo_root=root, kind="merge"))
    monkeypatch.chdir(repo)
    res = runner.invoke(app, ["audit", "verify"])
    assert res.exit_code == 0 and "2 record(s), chain intact" in res.output, res.output
    res = runner.invoke(app, ["audit", "verify", "--repo", str(repo / "sub")])
    assert res.exit_code == 0, res.output                  # a path inside the repo is the repo
    res = runner.invoke(app, ["audit", "export", "--format", "csv"])
    assert res.exit_code == 0 and res.output.startswith("seq,ts,kind,") and res.output.count("\n") == 3
    res = runner.invoke(app, ["audit", "export", "--since", "2999-01-01"])
    assert res.exit_code == 0 and res.output == ""
    res = runner.invoke(app, ["audit", "export", "--since", "yesterday"])
    assert res.exit_code == 2
    res = runner.invoke(app, ["audit", "pubkey"])
    assert res.exit_code == 0 and res.output.strip() == audit_chain.public_key_hex()
    # Tampered: the first broken seq, and exit 1.
    ls = log_path(root).read_bytes().split(b"\n")[:-1]
    rec = json.loads(ls[1])
    rec["event"]["merged"] = True
    ls[1] = canonical(rec).encode()
    log_path(root).write_bytes(b"\n".join(ls) + b"\n")
    res = runner.invoke(app, ["audit", "verify", "--repo", root])
    assert res.exit_code == 1 and "BROKEN at seq 2" in res.output, res.output
    monkeypatch.chdir(brindle_home.parent)
    res = runner.invoke(app, ["audit", "verify"])
    assert res.exit_code == 0 and "no audit log" in res.output
