"""assign/handoff's `files` (declared scope, overlap warnings) and
`depends_on` (queue until an earlier task's branch merges, cancel if its
workspace is removed unmerged instead); list_tasks(full=true), requeue, and
assign/handoff's dry_run."""

import asyncio
import re
import time
from pathlib import Path

import pytest

from conftest import full_text, sh
from brindle import agents, bus, mcp_server, tasks, workspaces
from brindle.db import Agent, Workspace
from test_merge_order import fake_spawn


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture
def boss(db, repo, monkeypatch):
    """A supervisor caller adopted on the main checkout, so mcp_server tools
    that need `_caller` resolve without touching the real process cwd."""
    # These tests exercise overlap *warnings*; the default now refuses overlaps.
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"overlap": "warn", "pipeline": false}')
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


def started_worker_id(reply: str) -> str:
    m = re.search(r"Started worker (\S+)", reply)
    assert m, reply
    return m.group(1)


def commit_file(path: Path, name: str, content: str = "x\n") -> None:
    (path / name).write_text(content)
    sh(f"git add -A && git commit -qm {name}", path)


# -- overlap warnings -----------------------------------------------------------


def test_overlap_warning_matches_recursive_globs(db, repo, boss):
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/**/a.py"]))
    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          files=["src/nested/dir/a.py"]))
    assert "Warning" in out_b


def test_overlap_warning_names_other_session_intent(db, repo, boss):
    from brindle import knowledge
    key = knowledge.repo_key(repo)
    knowledge.append(db, key, knowledge.INTENT, {"summary": "rework x\nmore", "paths": ["src/x.py"]},
                     session="sessB")
    warning = tasks.overlap_warning(db, boss, ["src/x.py"])
    assert "sessB" in warning and "rework x" in warning and "more" not in warning
    assert tasks.overlap_warning(db, boss, ["src/y.py"]) is None
    knowledge.append(db, key, knowledge.INTENT, {"summary": "mine", "paths": ["src/y.py"]}, session="boss")
    assert tasks.overlap_warning(db, boss, ["src/y.py"]) is None    # same session: ignored
    knowledge.append(db, key, knowledge.PAUSE, {}, session="sessB")
    assert tasks.overlap_warning(db, boss, ["src/x.py"]) is None    # paused intents don't warn


def test_overlapping_tasks_are_blocked(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", root.id, "supervisor", "claude", None, "interactive", "idle", "@0", None,
                       time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(tasks, "find_clash", lambda db_, ws_, files, text=None, caller_id=None:
                        tasks.Clash("overlaps with w0 (feat/a) on ledger.py", "w0", "boss"))
    delegated = []
    monkeypatch.setattr(agents, "delegate", lambda *a, **k: delegated.append(a))
    out = asyncio.run(mcp_server.assign("developer", "do it", files=["ledger.py"]))
    assert out.startswith("Not started") and "depends_on" in out and delegated == []


@pytest.fixture
def other_session_task(db, repo, monkeypatch):
    """A reported worker on another session's base branch whose branch isn't
    merged yet; returns a function to record its task."""
    root = workspaces.adopt_root(db, str(repo))
    other_ws = workspaces.create(db, str(repo), "fix-197", run_setup=False).workspace
    db.add_agent(Agent("w197", other_ws.id, "developer", "claude", None, "assign", "done",
                       "@1", "fixed it", time.time()))
    monkeypatch.setattr(tasks.git, "merged_into_default", lambda root_, branch: False)
    monkeypatch.setattr(tasks, "_changed_files", lambda w: ["src/brindle/tasks.py"])
    db.conn.execute("UPDATE workspaces SET base_branch='brindle/session-14' WHERE id=?", (other_ws.id,))

    def record(text="fix #197 overlap", files=None):
        db.add_task(tasks.Task("t197", str(repo), "w197", None, root.id, "developer", text,
                               "assign", 1, other_ws.branch, None, tasks._dumps(files), None,
                               "started", time.time()))
        return root

    return record


def test_cross_session_same_issue_number_warns_without_files(db, other_session_task):
    root = other_session_task()
    warning = tasks.overlap_warning(db, root, None, "Please fix #197 again")
    assert warning and "#197" in warning and "fix-197" in warning
    assert tasks.overlap_warning(db, root, None, "Fix #198") is None


def test_cross_session_merged_branch_is_not_warned(db, other_session_task, monkeypatch):
    root = other_session_task()
    warning = tasks.overlap_warning(db, root, ["src/brindle/tasks.py"])    # on changed files alone
    assert warning and "fix-197" in warning and "brindle/session-14" in warning
    assert tasks.overlap_warning(db, root, ["src/brindle/tasks.py"], "fix #197")
    monkeypatch.setattr(tasks.git, "merged_into_default", lambda root_, branch: True)
    assert tasks.overlap_warning(db, root, ["src/brindle/tasks.py"], "fix #197") is None


@pytest.fixture
def two_sessions(db, repo, monkeypatch):
    """Roots A (main checkout) and B (its own session branch) on one repo,
    overlap unset; B's worker runs on src/x.py. Returns B's worker id."""
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"pipeline": false}')
    root_a = workspaces.adopt_root(db, str(repo))
    root_b = workspaces.create(db, str(repo), "session-b", run_setup=False).workspace
    for name, ws in (("rootA", root_a), ("rootB", root_b)):
        db.add_agent(Agent(name, ws.id, "supervisor", "claude", None, "interactive", "processing",
                           "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "rootB")
    b = started_worker_id(asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                                        files=["src/x.py"])))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "rootA")
    return b


def test_cross_session_clash_queues_and_notifies(db, repo, two_sessions):
    b = two_sessions
    out = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/x.py"]))
    assert out.startswith("Queued task") and b in out, out
    [pending] = db.list_tasks(str(repo), state="pending")
    assert tasks._loads(pending.depends_on) == [b]
    for root in ("rootA", "rootB"):
        assert "src/x.py" in full_text(db, root, db.pop_pending(root).body)
    # A second trigger leaves the queued task and B's running work in place.
    again = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/x.py"]))
    assert pending.id in again
    assert [t.id for t in db.list_tasks(str(repo), state="pending")] == [pending.id]
    assert db.get_agent(b).status == "processing"
    assert [t.agent_id for t in db.list_tasks(str(repo), state="started")] == [b]


def test_queued_task_starts_from_holder_merge(db, repo, two_sessions, monkeypatch):
    b = two_sessions
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/x.py"]))
    ws_b = db.get_workspace(db.get_agent(b).workspace_id)
    (Path(ws_b.path) / "src").mkdir()
    commit_file(Path(ws_b.path), "src/x.py")
    starts = []
    real = agents.delegate

    def recording(*a, start=None, **k):
        starts.append(start)
        return real(*a, start=start, **k)

    monkeypatch.setattr(agents, "delegate", recording)
    monkeypatch.setenv("BRINDLE_AGENT_ID", "rootB")
    out = asyncio.run(mcp_server.merge_workspace(ws_b.id))
    assert out.startswith("Merged"), out
    assert starts == [db.merged_sha(ws_b.id)]
    [a_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-a"]
    assert a_task.state == "started"
    assert [e["kind"] for e in bus.tail(db, kinds=("task.queued", "task.started"))
            if e["task_id"] == a_task.id] == ["task.queued", "task.started"]


# -- dependencies: queue until merge, then auto-start --------------------------


def test_queued_task_stays_queued_if_only_some_dependencies_merged(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    out_c = asyncio.run(mcp_server.assign("developer", "do C", branch="feat-c"))
    worker_c = started_worker_id(out_c)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")

    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                  depends_on=[worker_a, worker_c]))

    asyncio.run(mcp_server.merge_workspace(ws_a.id))  # only one of two deps merges

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "pending"


@pytest.fixture
def dependency_rows(db, tmp_path, monkeypatch):
    """Task dispatch with database workspaces and a fake worker launch."""
    def workspace(name, repo="app", branch="main", *, tracked=False):
        root = str(tmp_path / repo)
        ws = Workspace(name, root, name, "main", branch, "main", root, None, "", 1)
        db.add_workspace(ws)
        if tracked:
            worker = fake_spawn(db, ws, "developer", parent_id=caller.id)
            tasks.record_started(db, ws, worker, "developer", name, "assign",
                                 isolate=True, branch=branch, done_when=None, files=None, depends_on=None)
        return ws

    boss = workspace("boss")
    caller = fake_spawn(db, boss, "supervisor", mode="interactive")
    launched = []

    def delegate(db, caller, ws, profile, task_text, mode, **kwargs):
        worker = fake_spawn(db, ws, profile, parent_id=caller.id, mode=mode)
        launched.append(worker.id)
        return worker, ws

    monkeypatch.setattr(agents, "delegate", delegate)
    monkeypatch.setattr(agents, "send_message", lambda *a, **kw: None)
    monkeypatch.setattr(tasks.git, "ok", lambda *a, **kw: False)
    return caller, boss, workspace, launched


@pytest.mark.parametrize("event", ["merged", "sweep", "removed", "requeue_merged", "requeue_pending", "requeue_removed"])
def test_same_branch_dependency_does_not_resolve_to_itself(db, dependency_rows, event):
    caller, boss, workspace, launched = dependency_rows
    upstream = workspace("engine-worker", "engine", "shared", tracked=True)
    db.add_session_repo(caller.id, "engine", upstream.repo_root)
    assert tasks.unmet_dependencies(db, boss, ["shared"], caller.id) == ["shared"]
    pending = tasks.enqueue(db, caller, boss, "developer", "app", "assign",
                            isolate=True, branch="shared", done_when=None, files=None, depends_on=["shared"])
    assert tasks.sweep(db) == 0
    if event.startswith("requeue_"):
        tasks.cancel(db, caller, pending.id)
        if event == "requeue_merged":
            tasks.on_merged(db, upstream)
        tasks.requeue(db, caller, boss, pending.id)
        [pending] = [t for t in db.list_tasks(boss.repo_root) if t.state != "cancelled"]
        if event == "requeue_pending":
            assert pending.state == "pending" and launched == []
            assert tasks.sweep(db) == 0
    if event in ("removed", "requeue_removed"):
        tasks.on_removed_unmerged(db, upstream)
        assert db.get_task(pending.id).state == "cancelled"
        assert launched == []
    else:
        if event == "sweep":
            db.set_merged(upstream.id, "merged-sha")
            assert tasks.sweep(db) == 1
        else:
            tasks.on_merged(db, upstream)
        assert db.get_task(pending.id).state == "started"
        tasks.on_merged(db, upstream)
        assert tasks.sweep(db) == 0
        assert launched == [db.get_task(pending.id).agent_id]


@pytest.mark.parametrize("finish", ["merged", "cancelled"])
def test_requeue_preserves_an_upstream_that_has_no_worker_yet(db, dependency_rows, finish):
    caller, boss, workspace, launched = dependency_rows
    engine = workspace("engine", "engine")
    db.add_session_repo(caller.id, "engine", engine.repo_root)
    upstream, dependent = [
        tasks.enqueue(db, caller, ws, "developer", ws.name, "assign", isolate=True,
                      branch="shared", done_when=None, files=None, depends_on=deps)
        for ws, deps in ((engine, []), (boss, ["shared"]))
    ]
    tasks.cancel(db, caller, dependent.id)
    tasks.requeue(db, caller, boss, dependent.id)
    [replacement] = db.list_tasks(boss.repo_root, state="pending")
    assert launched == []
    if finish == "cancelled":
        tasks.cancel(db, caller, upstream.id)
        assert db.get_task(replacement.id).state == "cancelled" and launched == []
    else:
        tasks.start_queued(db, upstream)
        tasks.on_merged(db, engine)
        assert db.get_task(replacement.id).state == "started"
        assert len(launched) == 2


@pytest.mark.parametrize("event", ["merged", "removed"])
@pytest.mark.variant("An accepted exact worker ID must still dispatch after its repository is detached")
def test_exact_worker_dependency_dispatches_after_repo_detach(db, dependency_rows, event):
    caller, boss, workspace, launched = dependency_rows
    upstream = workspace("engine-worker", "engine", "feature", tracked=True)
    db.add_session_repo(caller.id, "engine", upstream.repo_root)
    worker = agents.workspace_worker(db, upstream)
    pending = tasks.enqueue(db, caller, boss, "developer", "app", "assign",
                            isolate=True, branch="app", done_when=None, files=None, depends_on=[worker.id])
    db.remove_session_repo(caller.id, "engine")
    assert tasks.unmet_dependencies(db, boss, [worker.id], caller.id) == [worker.id]
    if event == "removed":
        tasks.on_removed_unmerged(db, upstream)
        db.delete_workspace(upstream.id)
        assert db.get_task(pending.id).state == "cancelled" and launched == []
    else:
        tasks.on_merged(db, upstream)
        assert db.get_task(pending.id).state == "started"
        tasks.on_merged(db, upstream)
        assert launched == [db.get_task(pending.id).agent_id]


@pytest.mark.variant("Workspace cleanup between two upstream merges must retain the first worker's dependency identity")
def test_queued_task_starts_when_first_dep_workspace_deleted_and_second_merges(db, dependency_rows):
    """A retained task satisfies the first worker dependency after workspace cleanup."""
    caller, boss, workspace, launched = dependency_rows
    ws_a, ws_c = [workspace(n, branch=f"feat-{n}", tracked=True) for n in ("a", "c")]
    workers = [agents.workspace_worker(db, ws).id for ws in (ws_a, ws_c)]
    b_task = tasks.enqueue(db, caller, boss, "developer", "do B", "assign",
                           isolate=True, branch="feat-b", done_when=None, files=None, depends_on=workers)
    tasks.on_merged(db, ws_a)
    db.delete_workspace(ws_a.id)
    assert db.get_agent(workers[0]) is None
    assert db.get_task(b_task.id).state == "pending" and launched == []
    tasks.on_merged(db, ws_c)
    b_task = db.get_task(b_task.id)
    assert b_task.state == "started" and b_task.agent_id
    assert launched == [b_task.agent_id]


def test_a_stranded_queued_task_starts_at_the_supervisors_stop_and_only_once(db, repo, boss, monkeypatch):
    """A merged without on_merged ever running (cut off, or merged by hand)."""
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    worker_a = started_worker_id(asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a")))
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))
    db.set_merged(ws_a.id, "abc123")

    boss_agent = db.get_agent("boss")
    agents._stop_decision(db, boss_agent, {})
    agents._stop_decision(db, boss_agent, {})

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "started" and b_task.agent_id
    assert len([a for a in db.list_agents() if a.parent_id == "boss"]) == 2   # A and B, B once


def test_a_queued_task_that_fails_to_start_tells_its_caller_why(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    worker_a = started_worker_id(asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a")))
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))

    def boom(db_, task):
        raise agents.AgentError("no such profile")
    monkeypatch.setattr(tasks, "start_queued", boom)
    asyncio.run(mcp_server.merge_workspace(ws_a.id))

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "cancelled"
    assert "no such profile" in full_text(db, "boss", db.pop_pending("boss").body)


# -- cancellation on unmerged removal --------------------------------------------


def test_removing_a_fully_merged_workspace_does_not_cancel_dependents(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")

    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))
    asyncio.run(mcp_server.merge_workspace(ws_a.id))  # merges and auto-starts B first

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "started"  # already started; removing A now must not touch it

    ws_a = db.get_workspace(ws_a.id)
    mcp_server.remove_workspace(ws_a.id, force=True)

    b_task = db.get_task(b_task.id)
    assert b_task.state == "started"


def test_cancellation_cascades_to_a_task_depending_on_the_cancelled_one(db, repo, boss, monkeypatch):
    """A unmerged -> B depends_on=[A] queued -> C depends_on=["feat-b"]
    queued -> removing A unmerged must cancel both B and C, not just B."""
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")  # ahead of main: "unmerged"

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          depends_on=[worker_a]))
    assert "Queued task" in out_b
    out_c = asyncio.run(mcp_server.assign("developer", "do C", branch="feat-c",
                                          depends_on=["feat-b"]))
    assert "Queued task" in out_c

    mcp_server.remove_workspace(ws_a.id, force=True)

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    [c_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-c"]
    assert b_task.state == "cancelled"
    assert c_task.state == "cancelled"
    assert [(e["kind"], e["task_id"]) for e in bus.tail(db, kinds=("task.cancelled",))] == [
        ("task.cancelled", b_task.id), ("task.cancelled", c_task.id)]

    first = db.pop_pending("boss")
    second = db.pop_pending("boss")
    assert first is not None and second is not None
    bodies = first.body + second.body
    assert "Cancelled" in first.body and "Cancelled" in second.body
    assert worker_a in bodies and b_task.id in bodies


def test_new_task_depending_on_an_already_cancelled_task_fails_clearly(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")  # ahead of main: "unmerged"
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=["feat-a"]))
    mcp_server.remove_workspace(ws_a.id, force=True)  # cancels B

    out_d = asyncio.run(mcp_server.assign("developer", "do D", branch="feat-d",
                                          depends_on=["feat-b"]))
    assert "cancelled" in out_d.lower() and "feat-b" in out_d
    # D was neither started nor queued: unmet_dependencies failed before either.
    assert not any(t.branch == "feat-d" for t in db.list_tasks(str(repo)))


# -- cancel_task ------------------------------------------------------------------


def _queue_chain(db, repo):
    """A started (files declared so it's recorded), B queued on A, C queued on B."""
    worker_a = started_worker_id(asyncio.run(
        mcp_server.assign("developer", "do A", branch="feat-a", files=["a.py"])))
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))
    asyncio.run(mcp_server.assign("developer", "do C", branch="feat-c", depends_on=["feat-b"]))
    by_branch = {t.branch: t for t in db.list_tasks(str(repo))}
    return by_branch["feat-a"], by_branch["feat-b"], by_branch["feat-c"]


def test_cancel_task_refuses_unknown_started_and_someone_elses_tasks(db, repo, boss, monkeypatch):
    a, b, _ = _queue_chain(db, repo)
    assert mcp_server.cancel_task("nope").startswith("Error")

    out = mcp_server.cancel_task(a.id)
    assert out.startswith("Error") and "not queued" in out
    assert db.get_task(a.id).state == "started"

    db.add_agent(Agent("other", boss.id, "supervisor", "claude", None, "interactive",
                       "processing", "@1", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "other")
    out = mcp_server.cancel_task(b.id)
    assert out.startswith("Error") and "isn't yours" in out
    assert db.get_task(b.id).state == "pending"


# -- a missing add_dirs entry reaches the supervisor ---------------------------


def _missing_add_dir(repo):
    (repo / ".brindle" / "config.json").write_text(
        '{"overlap": "warn", "pipeline": false, "add_dirs": ["/no/such/cache"]}')


def test_handoff_reply_names_a_missing_add_dir(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    _missing_add_dir(repo)
    out = asyncio.run(mcp_server.handoff("developer", "do A", branch="feat-a", wait_seconds=0))
    assert "Warning: add_dirs names /no/such/cache" in out


def test_a_queued_task_starting_names_a_missing_add_dir(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))
    _missing_add_dir(repo)

    asyncio.run(mcp_server.merge_workspace(ws_a.id))

    msg = db.pop_pending("boss")
    body = full_text(db, "boss", msg.body)
    assert "Started" in body and "add_dirs names /no/such/cache" in body


def queued_task(db, repo, worker_id, branch="feat-b", **kw):
    out = asyncio.run(mcp_server.assign(
        "developer", "the full brief for B", branch=branch, depends_on=[worker_id],
        done_when="pytest passes", files=["src/b.py"], **kw))
    assert "Queued task" in out, out
    return db.list_tasks(str(repo), state="pending")[-1]


def started_a(db):
    return started_worker_id(asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a")))


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
    assert "as " in out and "waiting on" in out and a in out
    new = [t for t in db.list_tasks(str(repo), state="pending")][-1]
    assert new.id != b.id
    assert (new.task_text, new.done_when, new.files, new.profile, new.branch, new.weight) == (
        b.task_text, b.done_when, b.files, b.profile, b.branch, "light")
    assert tasks._loads(new.depends_on) == [a]
    assert new.plan_first == 1
    assert db.get_task(b.id).state == "cancelled"


def test_requeue_with_nothing_to_wait_on_starts_it(db, repo, boss):
    b = queued_task(db, repo, started_a(db))
    mcp_server.cancel_task(b.id)
    out = mcp_server.requeue(b.id, depends_on=[])
    assert "started worker" in out
    assert not db.list_tasks(str(repo), state="pending")


def test_requeue_refuses_an_unknown_task_or_one_that_is_not_cancelled(db, repo, boss):
    assert mcp_server.requeue("nope").startswith("Error")
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
    out = asyncio.run(mcp_server.assign("developer", "do A", dry_run=True))
    assert "files: none declared" in out and "overlap: none" in out


def test_handoff_dry_run_reports_overlap_and_queueing(db, repo, boss):
    a_id = started_worker_id(asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["app.py"])))
    out = asyncio.run(mcp_server.handoff(
        "developer", "do B", branch="feat-b", files=["app.py"], depends_on=[a_id], dry_run=True))
    assert f"overlaps with {a_id}" in out
    assert f"would queue until {a_id} merges" in out
    assert len(db.list_tasks(str(repo))) == 1
