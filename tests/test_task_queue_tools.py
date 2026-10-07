"""list_tasks(full=true), requeue, and assign/handoff's dry_run."""

import asyncio
import re
import time

import pytest

from brindle import agents, mcp_server, tasks, workspaces
from brindle.db import Agent


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"overlap": "warn", "pipeline": false}')
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


def queued_task(db, repo, worker_id, branch="feat-b", **kw):
    out = asyncio.run(mcp_server.assign(
        "developer", "the full brief for B", branch=branch, depends_on=[worker_id],
        done_when="pytest passes", files=["src/b.py"], **kw))
    assert "Queued task" in out, out
    return db.list_tasks(str(repo), state="pending")[-1]


def started_a(db):
    out = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    return re.search(r"Started worker (\S+)", out).group(1)


# -- list_tasks full ------------------------------------------------------------


def test_list_tasks_default_omits_the_brief_and_full_shows_it(db, repo, boss):
    b = queued_task(db, repo, started_a(db))
    assert "the full brief" not in mcp_server.list_tasks()
    full = mcp_server.list_tasks(full=True)
    assert b.id in full
    assert "task_text: the full brief for B" in full
    assert "done_when: pytest passes" in full


# -- requeue --------------------------------------------------------------------


def test_requeue_a_cancelled_task_copies_it_and_keeps_dependencies(db, repo, boss):
    a = started_a(db)
    b = queued_task(db, repo, a, plan_first=True)
    mcp_server.cancel_task(b.id)
    db.update_task(b.id, weight="light")
    # A's task is still started, so the dependency is still waiting.
    out = mcp_server.requeue(b.id)
    assert f"as " in out and "waiting on" in out and a in out
    new = [t for t in db.list_tasks(str(repo), state="pending")][-1]
    assert new.id != b.id
    assert (new.task_text, new.done_when, new.files, new.profile, new.branch, new.weight) == (
        b.task_text, b.done_when, b.files, b.profile, b.branch, "light")
    assert tasks._loads(new.depends_on) == [a]
    assert new.plan_first == 1
    assert db.get_task(b.id).state == "cancelled"


def test_requeue_can_override_dependencies(db, repo, boss):
    a = started_a(db)
    b = queued_task(db, repo, a)
    mcp_server.cancel_task(b.id)
    other = asyncio.run(mcp_server.assign("developer", "do C", branch="feat-c"))
    c = re.search(r"Started worker (\S+)", other).group(1)
    out = mcp_server.requeue(b.id, depends_on=[c])
    assert c in out
    new = db.list_tasks(str(repo), state="pending")[-1]
    assert tasks._loads(new.depends_on) == [c]


def test_requeue_with_nothing_to_wait_on_starts_it(db, repo, boss):
    b = queued_task(db, repo, started_a(db))
    mcp_server.cancel_task(b.id)
    out = mcp_server.requeue(b.id, depends_on=[])
    assert "started worker" in out
    assert not db.list_tasks(str(repo), state="pending")


def test_requeue_refuses_a_task_that_is_not_cancelled(db, repo, boss):
    b = queued_task(db, repo, started_a(db))
    out = mcp_server.requeue(b.id)
    assert out.startswith("Error") and "not cancelled" in out
    assert len(db.list_tasks(str(repo), state="pending")) == 1


def test_requeue_refused_by_policy_and_scoped_to_session_repos(db, repo, boss, monkeypatch):
    from brindle import policy

    b = queued_task(db, repo, started_a(db))
    mcp_server.cancel_task(b.id)
    db.update_task(b.id, repo_root="/elsewhere")
    assert mcp_server.requeue(b.id).startswith("Error: No task")
    db.update_task(b.id, repo_root=str(repo))
    monkeypatch.setattr(policy, "check_assign", lambda *a, **k: policy.Decision(False, "no"))
    out = mcp_server.requeue(b.id)
    assert "policy refused" in out
    assert len(db.list_tasks(str(repo), state="pending")) == 0


def test_requeue_unknown_task(db, repo, boss):
    assert mcp_server.requeue("nope").startswith("Error")


def test_requeue_with_a_cancelled_dependency_asks_for_new_ones(db, repo, boss):
    a = started_a(db)
    b = queued_task(db, repo, a)
    c = queued_task(db, repo, "feat-b", branch="feat-c")
    mcp_server.cancel_task(b.id)  # cascades to c
    out = mcp_server.requeue(c.id)
    assert out.startswith("Error") and "depends_on" in out


# -- dry_run --------------------------------------------------------------------


def test_assign_dry_run_starts_and_queues_nothing(db, repo, boss):
    out = asyncio.run(mcp_server.assign(
        "developer", "do A", branch="feat-a", files=["app.py", "nope/**", "src/*.py"],
        dry_run=True))
    assert out.startswith("Dry run")
    assert "profile: developer" in out
    assert "app.py matches 1" in out
    assert "glob 'src/*.py' matches nothing" in out
    assert "Warning: files glob 'nope/**' matches nothing" in out
    assert "queue: would start now" in out
    assert not db.list_tasks(str(repo))
    assert len(db.find_workspaces(str(repo))) == 1


def test_handoff_dry_run_reports_overlap_and_queueing(db, repo, boss):
    a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["app.py"]))
    a_id = re.search(r"Started worker (\S+)", a).group(1)
    out = asyncio.run(mcp_server.handoff(
        "developer", "do B", branch="feat-b", files=["app.py"], depends_on=[a_id], dry_run=True))
    assert f"overlaps with {a_id}" in out
    assert f"would queue until {a_id} merges" in out
    assert len(db.list_tasks(str(repo))) == 1


def test_dry_run_without_files_says_so(db, repo, boss):
    out = asyncio.run(mcp_server.assign("developer", "do A", dry_run=True))
    assert "files: none declared" in out
    assert "overlap: none" in out
