"""A worker's report always gets its branch checked and reviewed (#190, #186):
the check run outlives the worker, and a branch nothing is reviewing is
started again rather than left to the supervisor."""
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, cull, pipeline, procs, providers, tmux, workspaces
from test_merge_order import add


@pytest.fixture
def reported(db, repo):
    """A repo with checks, and a worker that reported on its branch."""
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["true"]}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    add(db, ws, "w1", "assign", parent="boss", status="idle", result="did it")
    return db.get_agent("w1"), ws


def test_the_check_run_a_report_starts_is_not_stopped_with_the_worker(db, reported, monkeypatch):
    """#190: closing a reported worker stops every process under its MCP
    server; the check run its report started, still waiting for a check slot,
    went with it, and the branch was never reviewed."""
    worker, ws = reported
    mark = f"brindle-review-helper-{uuid.uuid4().hex}"
    monkeypatch.setattr(providers, "brindle_invocation",
                        lambda: [sys.executable, "-c", "import time; time.sleep(60)", mark])
    assert pipeline.on_report(db, worker, ws, "did it")
    table = procs.table()
    helper = {p.pid for p in table.values() if mark in p.text and "_review-after-checks" in p.text}
    try:
        assert helper and not helper & procs.descendants([os.getpid()], table)
    finally:
        for pid in helper:
            os.kill(pid, signal.SIGKILL)


def test_a_branch_nothing_is_reviewing_gets_its_review_started_again(db, reported, monkeypatch):
    """#190, #186: a reported branch with no check run, reviewer or verdict
    for half an hour is started again by brindle, not left to the supervisor."""
    worker, ws = reported
    db.update_agent(worker.id, pipeline="reviewing", reviewing_since=time.time() - cull.UNPICKED_AFTER - 60)
    detached, sent = [], []
    monkeypatch.setattr(agents, "_detach", detached.append)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append((to, body)))
    panes = tmux.PaneSnapshot(tmux.current_server())
    cull.note_reported_stuck(db, time.time(), panes)
    cull.note_reported_stuck(db, time.time(), panes)
    assert [argv[argv.index("_review-after-checks") + 1] for argv in detached] == [ws.id]
    assert [to for to, _ in sent] == ["boss"]
