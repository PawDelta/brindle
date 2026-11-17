"""The fast path (repo setting ``fast_path``): a small request goes straight
to one cheap worker with a finish line; no goal, milestones or completion
audit, one light review and the repo's checks, then the merge. Runs the real
assign, report, review-after-checks and finish-review code, with only the
agent CLIs and the machine-wide looks faked; the detached brindle helpers run
in this process through the real CLI."""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
from fast_repo import repo, repo_template  # noqa: F401
from brindle import agents, autopilot, cli, mcp_server, pipeline, workspaces
from test_merge_order import add, fake_spawn, quiet_removal

HELPERS = ("_review-after-checks", "_finish-review", "_drain-ready")


@pytest.fixture
def detached(monkeypatch):
    """Every brindle helper that would have started as a process of its own."""
    queue: list[list[str]] = []
    monkeypatch.setattr(agents, "_detach", lambda argv: queue.append(list(argv)))
    monkeypatch.setattr(agents, "_detach_orphan", lambda argv: queue.append(list(argv)))
    return queue


def run_helpers(queue: list[list[str]]) -> list[str]:
    """Run the queued review and merge helpers through the CLI, as their
    detached processes would; returns the ones run, in order."""
    ran = []
    while queue:
        argv = queue.pop(0)
        name = next((a for a in argv if a in HELPERS), None)
        if name is None:
            continue   # check warming: the review's own check run covers it
        result = CliRunner().invoke(cli.app, argv[argv.index(name):], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        ran.append(name)
    return ran


@pytest.fixture
def session(db, repo, tmp_path, monkeypatch, detached):
    """An autopilot supervisor with no goal on a repo with checks; ``setup``
    writes and commits the repo's settings."""
    log = tmp_path / "checks.log"   # outside the worktree, so the check doesn't dirty it
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    db.add_autopilot("boss")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    monkeypatch.setattr(pipeline.autopilot, "_unavailable", lambda *a, **k: None)
    quiet_removal(monkeypatch)

    def setup(**settings) -> Path:
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps(
            {"checks": [f"echo checked >> {log}"], "auto_merge_default_branch": True, **settings}))
        sh("git add .brindle/config.json && git commit -qm settings", repo)
        return log
    return setup


def assign(db, repo, **kw) -> tuple[str, str]:
    """``mcp_server.assign`` of a small request: (its reply, the worker's id)."""
    reply = asyncio.run(mcp_server.assign(task="Fix the typo in the greeting", branch="fix-typo", **kw))
    [t] = db.list_tasks(str(repo))
    return reply, t.agent_id


@pytest.mark.parametrize("on", [pytest.param(True, marks=pytest.mark.variant("the fast path skips the second review a repo asks for on light work")), False])
def test_small_request_merges_after_one_light_review_and_the_checks(db, repo, session, detached, on):
    # On: a second review the repo asks for on light work is skipped too (one light review).
    checks = session(fast_path=on, **({"second_review": {"light": "reviewer-codex"}} if on else {}))
    reply, worker_id = assign(db, repo, weight="light", done_when="app.py prints hello", fast_path=True)
    assert (autopilot.GOAL_REMINDER in reply) is not on   # off: the full path, which wants a goal
    ws = db.get_workspace(db.get_agent(worker_id).workspace_id)
    (Path(ws.path) / "app.py").write_text("print('hello')\n")
    sh("git commit -qam typo", Path(ws.path))

    agents.report_result(db, worker_id, "Fixed the typo.")
    assert run_helpers(detached) == ["_review-after-checks"]
    [reviewer] = [a for a in db.list_agents(ws.id) if a.mode == "review"]
    assert (pipeline.FAST_PATH_REVIEW in reviewer.task) is on
    assert "checked" in checks.read_text()   # the repo's checks ran before the review

    agents.submit_review(db, reviewer.id, True, "lgtm")   # its finish runs inline (conftest)
    run_helpers(detached)
    assert (repo / "app.py").read_text() == "print('hello')\n"   # merged into the supervisor's branch
    assert db.get_workspace(ws.id) is None
    ap = db.get_autopilot("boss")
    assert not ap.goal and db.milestones("boss") == [] and ap.auditor_id is None
    assert autopilot.on_stop(db, db.get_agent("boss"), {}) is None   # nothing left to nudge or audit


def test_fast_path_routing_never_falls_through_to_a_heavy_worker(db, repo, session, monkeypatch):
    """The medium tier's list ends in developer-heavy: with the cheaper
    candidates out, real routing must still not hand a fast-path task to it."""
    session(fast_path=True)
    monkeypatch.setattr(autopilot, "_unavailable",
                        lambda name, *a, **k: None if name == "developer-heavy" else "not installed")
    _reply, worker_id = assign(db, repo, weight="medium", done_when="app.py prints hello", fast_path=True)
    assert autopilot.profile_weight(db.get_agent(worker_id).profile, None, str(repo)) in ("light", "medium")


@pytest.mark.parametrize("kw", [
    {"weight": "heavy", "done_when": "app.py prints hello"},
    {"weight": "light"},
    {"agent_profile": "developer-heavy", "done_when": "app.py prints hello"},
])
def test_fast_path_refuses_big_or_open_ended_work(db, repo, session, kw):
    session(fast_path=True)
    reply = asyncio.run(mcp_server.assign(task="Fix the typo", fast_path=True, **kw))
    assert reply.startswith("Not started") and db.list_tasks(str(repo)) == []
