"""With checks configured the reviewer starts only after they finish, with
their results in its prompt; a repo without checks is reviewed at once."""
import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, gates, mcp_server, pipeline, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent
from test_merge_order import add


def inbox(db, agent_id):
    return [r["body"] for r in db.conn.execute(
        "SELECT body FROM inbox WHERE agent_id=? ORDER BY id", (agent_id,))]


def commit(ws, name):
    (Path(ws.path) / name).write_text("y = 2\n")
    sh("git add -A && git commit -qm more", Path(ws.path))


@pytest.fixture
def ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


@pytest.fixture
def boss(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return db.get_agent("boss")


@pytest.fixture
def spawned(monkeypatch):
    """Every reviewer spawn, recorded instead of started."""
    calls = []

    def fake_spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review", **kw):
        calls.append(prompt)
        return Agent("rev1", ws_.id, profile, "claude", parent_id, mode, "starting", "@0",
                     None, time.time())

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    return calls


@pytest.fixture
def detached(monkeypatch):
    """Every helper process that would have been started detached."""
    calls = []
    monkeypatch.setattr(agents, "_detach", lambda argv: calls.append(argv))
    return calls


# -- the results are in the reviewer's prompt ------------------------------------------


@pytest.mark.parametrize("caller", ["boss"])
def test_a_crashing_check_run_starts_no_review_and_tells_the_caller(db, ws, boss, spawned, monkeypatch,
                                                                    caller):
    """Explicit request (no pipeline report): the caller is told, or for a UI
    request with no caller the worker's supervisor; no reviewer starts."""
    add(db, ws, "w1", "assign", parent="boss", result="done")
    monkeypatch.setattr(gates, "check_summary",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert agents.review_after_checks(db, caller, ws, "reviewer", None,
                                      RepoConfig(checks=["true"])) is None
    assert spawned == []
    told = db.pop_pending("boss")
    assert told is not None and "crashed" in told.body and "boom" in told.body


def test_timed_out_checks_start_no_review_and_are_not_sent_as_a_failure(db, ws, boss, spawned, monkeypatch):
    add(db, ws, "w1", "assign", parent="boss", result="done")
    failed = []
    monkeypatch.setattr(pipeline, "on_checks_failed", lambda *a: failed.append(a))
    monkeypatch.setattr(gates, "check_summary", lambda *a, **kw: "TIMEOUT `suite`\ntimed out, not a failure")
    assert agents.review_after_checks(db, "boss", ws, "reviewer", None, RepoConfig(checks=["suite"])) is None
    assert spawned == [] and failed == []
    assert db.pop_pending("boss") is not None   # the supervisor was told


def test_a_branch_that_moves_before_dispatch_is_checked_again_not_reviewed_unchecked(
        db, ws, boss, spawned, monkeypatch):
    """The head moves after the checks end, while the run waits for the
    dispatch lock: the old commit's results never reach a reviewer of the new one."""
    ran = []

    def checks(db_, ws_, cfg, cancel=None):
        ran.append(gates.head(ws))
        return "PASS `suite`"

    real_lock = agents.review_dispatch_lock
    moved = []

    def lock(ws_id, **kw):
        if ran and not moved:
            moved.append(1)
            commit(ws, "more.py")   # moved on lock acquisition
        return real_lock(ws_id, **kw)

    monkeypatch.setattr(gates, "check_summary", checks)
    monkeypatch.setattr(agents, "review_dispatch_lock", lock)
    agents.review_after_checks(db, "boss", ws, "reviewer", None, RepoConfig(checks=["suite"]))
    assert len(ran) == 2 and ran[0] != ran[1] and len(spawned) == 1 and ran[1][:8] in spawned[0]


# -- no reviewer until the checks finish -----------------------------------------------


# -- the head moving cancels -----------------------------------------------------------


def test_the_run_gives_up_when_the_head_moves_during_the_checks(db, ws, boss, spawned, monkeypatch):
    seen = {}

    def fake_summary(db_, ws_, cfg, cancel=None):
        seen["before"] = cancel()
        commit(ws, "more.py")
        seen["after"] = cancel()
        raise gates.Abandoned("moved")

    monkeypatch.setattr(gates, "check_summary", fake_summary)
    assert agents.review_after_checks(db, "boss", ws, "reviewer", None,
                                      RepoConfig(checks=["suite"])) is None
    assert seen == {"before": False, "after": True}
    assert spawned == []


# -- the callers -----------------------------------------------------------------------


def test_the_mcp_tool_returns_at_once_and_detaches_the_review(db, repo, ws, boss, spawned, detached):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["true"]}))
    db.set_check_duration(ws.repo_root, "true", 120.0)
    out = asyncio.run(mcp_server.request_review(ws.id, profile="reviewer"))
    assert "checks are running" in out and "about 2 min" in out
    assert spawned == [] and len(detached) == 1 and "_review-after-checks" in detached[0]


def test_the_mcp_tool_says_so_when_the_worktree_is_gone(db, repo, ws, boss, spawned, detached):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["true"]}))
    shutil.rmtree(ws.path)   # removed outside brindle: the row outlives the directory
    out = asyncio.run(mcp_server.request_review(ws.id, profile="reviewer"))
    assert "No reviewer started" in out and ws.path in out
    assert spawned == [] and detached == []
