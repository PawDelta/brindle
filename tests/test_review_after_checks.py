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


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", **kw):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", None,
                       time.time(), **kw))


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


def test_check_results_appear_in_the_reviewer_prompt(db, ws, boss, spawned):
    agents.review_after_checks(db, "boss", ws, "reviewer", None, RepoConfig(checks=["true", "false"]))
    [prompt] = spawned
    sha = gates.head(ws)
    assert f"Checks for {ws.branch} at {sha[:8]}" in prompt
    assert "PASS `true`" in prompt and "FAIL `false`" in prompt
    assert "still running" not in prompt and "arrive as a message" not in prompt


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


def test_no_reviewer_exists_while_the_checks_run(db, ws, boss, spawned, monkeypatch):
    seen = {}

    def slow_summary(db_, ws_, cfg, cancel=None):
        seen["reviewers"] = [a for a in db.list_agents(ws.id) if a.mode == "review"]
        seen["spawned"] = len(spawned)
        return "PASS `suite`"

    monkeypatch.setattr(gates, "check_summary", slow_summary)
    agents.review_after_checks(db, "boss", ws, "reviewer", None, RepoConfig(checks=["suite"]))
    assert seen == {"reviewers": [], "spawned": 0}
    assert len(spawned) == 1 and "PASS `suite`" in spawned[0]


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


def test_a_reviewer_that_cannot_start_is_refused_before_any_check_runs(db, ws, boss, detached):
    with pytest.raises(agents.AgentError):
        agents.start_review(db, boss, ws, "no-such-profile", None, RepoConfig(checks=["true"]))
    assert detached == []


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


def test_the_run_stops_when_the_workspace_is_gone(db, ws, boss, spawned, monkeypatch):
    def removed(db_, ws_, cfg, cancel=None):
        db.delete_workspace(ws.id)
        return "PASS `suite`"

    monkeypatch.setattr(gates, "check_summary", removed)
    assert agents.review_after_checks(db, "boss", ws, "reviewer", None,
                                      RepoConfig(checks=["suite"])) is None
    assert spawned == []


# -- no checks configured: reviewed at once --------------------------------------------


def test_without_checks_the_reviewer_starts_immediately(db, ws, boss, spawned, detached):
    reviewer = agents.start_review(db, boss, ws, "reviewer", None, RepoConfig(checks=[]))
    assert reviewer is not None and reviewer.id == "rev1"
    assert len(spawned) == 1 and detached == []
    assert "checks" not in spawned[0].lower()


# -- the callers -----------------------------------------------------------------------


def test_the_mcp_tool_returns_at_once_and_detaches_the_review(db, repo, ws, boss, spawned, detached):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": ["true"]}))
    db.set_check_duration(ws.repo_root, "true", 120.0)
    out = asyncio.run(mcp_server.request_review(ws.id, profile="reviewer"))
    assert "checks are running" in out and "about 2 min" in out
    assert spawned == [] and len(detached) == 1 and "_review-after-checks" in detached[0]


def test_the_mcp_tool_starts_the_review_at_once_without_checks(db, ws, boss, spawned, detached):
    out = asyncio.run(mcp_server.request_review(ws.id, profile="reviewer"))
    assert "Reviewer rev1" in out and len(spawned) == 1 and detached == []


@pytest.fixture
def piped(db, repo, ws, boss, monkeypatch, spawned):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"review": True, "checks": ["true"], "review_profile": "reviewer"}))
    add(db, ws, "w1", "assign", parent="boss", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    return ws


def test_a_report_with_checks_defers_the_review_and_returns_at_once(db, piped, detached, spawned):
    out = agents.report_result(db, "w1", "added new.py")
    assert "brindle is having your branch reviewed" in out
    assert spawned == [] and len(detached) == 1 and "_review-after-checks" in detached[0]
    assert db.get_agent("w1").pipeline == "reviewing"
    assert inbox(db, "boss") == []


def test_the_cli_command_runs_the_checks_then_starts_the_review(db, ws, boss, spawned, monkeypatch):
    (Path(ws.repo_root) / ".brindle").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".brindle" / "config.json").write_text(json.dumps({"checks": ["false"]}))
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    cli.review_after_checks_cmd(ws.id, "boss", "reviewer", None)
    [prompt] = spawned
    assert "FAIL `false`" in prompt


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
