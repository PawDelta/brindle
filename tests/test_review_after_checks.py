"""With checks configured the reviewer starts only after they finish, with
their results in its prompt; a repo without checks is reviewed at once."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, cli, gates, mcp_server, workspaces
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


def test_a_crashing_check_run_still_starts_the_review_and_says_so(db, ws, boss, spawned, monkeypatch):
    monkeypatch.setattr(gates, "check_summary",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    agents.review_after_checks(db, "boss", ws, "reviewer", None, RepoConfig(checks=["true"]))
    assert "crashed" in spawned[0] and "boom" in spawned[0]


# -- no reviewer until the checks finish -----------------------------------------------


def test_with_checks_nothing_starts_until_a_detached_process_has_run_them(db, ws, boss, spawned, detached):
    cfg = RepoConfig(checks=["true"])
    assert agents.start_review(db, boss, ws, "reviewer", "the parser", cfg) is None
    assert spawned == []
    [argv] = detached
    assert argv[argv.index("_review-after-checks") + 1] == ws.id
    assert argv[argv.index("--caller") + 1] == "boss"
    assert argv[argv.index("--focus") + 1] == "the parser"


def test_a_reviewer_already_at_work_on_this_commit_means_no_second_run(db, ws, boss, spawned,
                                                                     detached, monkeypatch):
    add(db, ws, "rev0", "review", "reviewer", parent="boss", status="processing",
        review_sha=gates.head(ws))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(gates, "check_summary", lambda *a, **kw: pytest.fail("must not run"))
    cfg = RepoConfig(checks=["true"])
    assert agents.start_review(db, boss, ws, "reviewer", None, cfg).id == "rev0"
    assert agents.review_after_checks(db, "boss", ws, "reviewer", None, cfg) is None
    assert detached == [] and spawned == []


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


def test_results_for_a_commit_the_branch_has_left_start_no_reviewer(db, ws, boss, spawned, monkeypatch):
    def finishes_late(db_, ws_, cfg, cancel=None):
        commit(ws, "more.py")   # moved after the last check ended
        return "PASS `suite`"

    monkeypatch.setattr(gates, "check_summary", finishes_late)
    assert agents.review_after_checks(db, "boss", ws, "reviewer", None,
                                      RepoConfig(checks=["suite"])) is None
    assert spawned == []


# -- the callers -----------------------------------------------------------------------


def test_the_mcp_tool_returns_at_once_and_detaches_the_review(db, repo, ws, boss, spawned, detached):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["true"]}))
    db.set_check_duration(ws.repo_root, "true", 120.0)
    out = asyncio.run(mcp_server.request_review(ws.id, profile="reviewer"))
    assert "checks are running" in out and "about 2 min" in out
    assert spawned == [] and len(detached) == 1 and "_review-after-checks" in detached[0]


def test_a_review_that_cannot_start_after_the_checks_reaches_the_supervisor(db, ws, boss, monkeypatch):
    add(db, ws, "w1", "assign", parent="boss", status="processing", pipeline="reviewing")
    db.update_agent("w1", pipeline="reviewing")

    def refuse(*a, **kw):
        raise agents.AgentError("codex is not installed")

    monkeypatch.setattr(agents, "review_after_checks", refuse)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    cli.review_after_checks_cmd(ws.id, "boss", None, None)
    [msg] = inbox(db, "boss")
    assert "No reviewer could start" in msg and "codex is not installed" in msg
    assert db.get_agent("w1").pipeline is None
