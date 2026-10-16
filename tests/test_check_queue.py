"""Check runs share the machine (a queue with a few slots), a run far past
its last duration is reported, and stale or redundant runs give up."""
import json
import threading
import time
from pathlib import Path

import pytest

from conftest import full_text, sh
from brindle import agents, autopilot, gates, workspaces
from brindle.config import load_repo_config
from brindle.db import DB, Agent


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", result=None, **kw):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", result,
                       time.time(), **kw))


def branch(db, repo, name):
    ws = workspaces.create(db, str(repo), name).workspace
    (Path(ws.path) / f"{name}.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


def inbox(db, agent_id):
    return [full_text(db, agent_id, r["body"]) for r in db.conn.execute(
        "SELECT body FROM inbox WHERE agent_id=? AND COALESCE(held,0)=0 ORDER BY id", (agent_id,))]


def configure(repo, **values):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(values))


@pytest.fixture
def session(db, repo, monkeypatch):
    """A busy supervisor in the checkout and a worker's branch with a commit."""
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = branch(db, repo, "feat")
    add(db, ws, "w1", "assign", parent="boss", status="done", result="did it")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    return root, ws


class Meter:
    """Counts how many bodies run at once."""

    def __init__(self, hold=0.15):
        self.hold, self.now, self.peak, self.lock = hold, 0, 0, threading.Lock()

    def __call__(self, *a, **kw):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(self.hold)
        with self.lock:
            self.now -= 1
        return True, "ok"


def in_threads(n, target):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)


# -- the queue ---------------------------------------------------------------------


@pytest.mark.parametrize("limit", [2])
def test_at_most_limit_runs_hold_a_slot_at_once(brindle_home, monkeypatch, limit):
    monkeypatch.setattr(gates, "SLOT_POLL", 0.01)
    meter = Meter(hold=0.1)

    def run(_i):
        with gates._check_slot(limit):
            meter()

    in_threads(4, run)
    assert meter.peak == limit


def test_an_unset_check_concurrency_follows_the_host_size(repo, brindle_home, monkeypatch):
    # #169: with no check_concurrency in any config, a small host gets fewer slots than a big one
    from brindle import config

    def host(cores, gb):
        monkeypatch.setattr(config.os, "cpu_count", lambda: cores)
        pages = int(gb * 2**30 // 4096)
        monkeypatch.setattr(config.os, "sysconf", lambda name: 4096 if name == "SC_PAGE_SIZE" else pages)
        return load_repo_config(str(repo)).check_concurrency

    assert host(2, 4) == 2
    assert host(16, 64) > host(2, 4)


def test_check_summary_and_the_merge_gate_share_the_queue(db, repo, monkeypatch):
    configure(repo, check_concurrency=1, pre_commit=False)
    monkeypatch.setattr(gates, "SLOT_POLL", 0.01)
    spaces = [branch(db, repo, "one"), branch(db, repo, "two")]
    meter = Meter()
    monkeypatch.setattr(autopilot, "run_check", meter)
    cfg = load_repo_config(str(repo))
    cfg.checks = ["suite"]

    def run(i):
        if i == 0:
            gates.check_summary(DB(), spaces[0], cfg)                      # _review-after/_warm-checks
        else:
            assert gates.run(DB(), spaces[1], cfg, review_required=False).ok   # the merge gate

    in_threads(2, run)
    assert meter.peak == 1


# -- a run far past its last duration ------------------------------------------------


def test_a_passing_run_records_its_duration_and_a_failing_one_does_not(db, repo, monkeypatch):
    ws = branch(db, repo, "one")
    assert db.check_duration(ws.repo_root, "true") is None
    gates.run_checked(db, ws, "false", {}, 30)
    assert db.check_duration(ws.repo_root, "false") is None
    gates.run_checked(db, ws, "true", {}, 30)
    assert db.check_duration(ws.repo_root, "true") is not None


def test_a_run_far_past_its_last_duration_is_reported_to_the_supervisor(db, session, monkeypatch):
    root, ws = session
    monkeypatch.setattr(gates, "SLOW_MIN_EXTRA", 0.0)
    db.set_check_duration(ws.repo_root, "suite", 0.05)
    monkeypatch.setattr(autopilot, "run_check", Meter(hold=0.6))
    gates.run_checked(db, ws, "suite", {}, 30)
    [msg] = inbox(db, "boss")
    assert "`suite` on `feat` has been running" in msg and "check_concurrency" in msg


def test_a_slow_milestone_check_in_the_supervisors_checkout_reaches_it(db, session):
    root, ws = session
    assert gates.supervisor_id(db, root) == "boss"
    assert gates.supervisor_id(db, ws) == "boss"


# -- stale and redundant runs ----------------------------------------------------------


def test_a_run_on_a_commit_the_branch_left_gives_up_and_frees_its_slot(db, repo, monkeypatch):
    configure(repo, check_concurrency=1)
    monkeypatch.setattr(autopilot, "CANCEL_POLL", 0.05)
    ws = branch(db, repo, "one")
    sha = gates.head(ws)
    (Path(ws.path) / "more.py").write_text("y = 1\n")
    sh("git add -A && git commit -qm more", Path(ws.path))   # the head moves on
    t0 = time.monotonic()
    with pytest.raises(gates.Abandoned):
        gates.run_checked(db, ws, "sleep 30", {}, 60, cancel=lambda: gates.head(ws) != sha)
    assert time.monotonic() - t0 < 10
    with gates._check_slot(1):   # would hang if the stale run still held the slot
        pass


def test_warm_checks_gives_up_when_the_head_moves(db, repo, monkeypatch):
    from brindle import cli

    ws = branch(db, repo, "one")
    seen = {}

    def fake_summary(db_, ws_, cfg, cancel=None):
        seen["before"] = cancel()
        (Path(ws.path) / "more.py").write_text("y = 1\n")
        sh("git add -A && git commit -qm more", Path(ws.path))
        seen["after"] = cancel()
        raise gates.Abandoned("x")

    monkeypatch.setattr(gates, "check_summary", fake_summary)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    cli.warm_checks_cmd(ws.id)    # swallows Abandoned
    assert seen == {"before": False, "after": True}


def test_a_waiter_reuses_the_holders_failure_instead_of_rerunning(db, repo, monkeypatch):
    configure(repo, check_concurrency=0)
    ws = branch(db, repo, "one")
    calls = []

    def failing(cmd, cwd, env, timeout, **kw):
        calls.append(cmd)
        time.sleep(0.3)
        return False, "boom (timed out)"

    monkeypatch.setattr(autopilot, "run_check", failing)
    results = []

    def run(i):
        time.sleep(0.05 * i)
        results.append(gates.run_checked(DB(), ws, "suite", {}, 30))

    in_threads(3, run)
    assert calls == ["suite"]
    assert results == [(False, "boom (timed out)")] * 3
