"""assign/handoff's `files` (declared scope, overlap warnings) and
`depends_on` (queue until an earlier task's branch merges, cancel if its
workspace is removed unmerged instead); list_tasks(full=true), requeue, and
assign/handoff's dry_run."""

import asyncio
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import full_text, sh
from fast_repo import build_repo, copy_tree, moved, no_screen_gap, repo, repo_template, scratch_db  # noqa: F401
from brindle import agents, bus, mcp_server, tasks, workspaces
from brindle.db import DB, Agent, Workspace
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
    (repo / ".brindle" / "config.json").write_text('{"overlap": "warn", "pipeline": false, "review": false}')
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


@pytest.fixture(scope="module")
def overlap_template(tmp_path_factory):
    """Reported worktrees on ``session`` (w0, and ``landed``, merged into it),
    made once; ``start`` is ``session`` as w3 is cut from it."""
    base = tmp_path_factory.mktemp("overlap-template")
    repo_ = build_repo(base)
    sh("git branch session && git update-ref refs/remotes/origin/feat-w3 main", repo_)
    start = sh("git rev-parse session", repo_)
    made = {}
    with scratch_db(tmp_path_factory.mktemp("overlap-template-home")) as db_:
        for name in ("w0", "landed"):
            made[name] = workspaces.create(db_, str(repo_), f"feat-{name}", base="session",
                                           run_setup=False).workspace
            commit_file(Path(made[name].path), f"{name}.py")
        sh("git checkout -q session && git merge -q --no-ff feat-landed -m land && git checkout -q main", repo_)
    return base, made, start


@pytest.fixture(scope="module")
def session_b_template(tmp_path_factory):
    """The repo with root B's ``session-b`` worktree, made once."""
    base = tmp_path_factory.mktemp("session-b-template")
    repo_ = build_repo(base)
    with scratch_db(tmp_path_factory.mktemp("session-b-template-home")) as db_:
        return base, workspaces.create(db_, str(repo_), "session-b", run_setup=False).workspace


@pytest.fixture
def repo(request, repo_template, tmp_path):
    """conftest's repo; with root B's worktree for the tests that use ``two_sessions``."""
    if "two_sessions" in request.fixturenames:
        return copy_tree(request.getfixturevalue("session_b_template")[0], tmp_path)
    return copy_tree(repo_template, tmp_path)


@pytest.mark.slow("The overlap check under test runs ~50 real git reads over three worktrees")
@pytest.mark.parametrize("w3_branch", [
    "feat-w3",
    pytest.param("origin/feat-w3", marks=pytest.mark.variant(
        "review finding: a local branch named origin/x must not read as origin's merged x")),
])
def test_overlap_check_git_calls_bounded(db, tmp_path, overlap_template, monkeypatch, w3_branch):
    """Reported-unmerged worktrees cost a few cheap git reads each, never a
    fetch or the cherry/patch-id fallback, and nothing again once decided; a
    branch merged into its session branch has landed."""
    template, made, start = overlap_template
    repo = copy_tree(template, tmp_path)
    root = workspaces.adopt_root(db, str(repo))
    for name in ("w0", "w3", "landed"):
        if name == "w3":
            ws = workspaces.create(db, str(repo), w3_branch, base="session", start=start,
                                   run_setup=False).workspace
            commit_file(Path(ws.path), "w3.py")
        else:
            ws = moved(made[name], template, tmp_path)
            db.add_workspace(ws)
        db.add_agent(Agent(name, ws.id, "developer", "claude", None, "assign", "done", "@1", "done",
                           time.time()))
        db.add_task(tasks.Task(f"t-{name}", str(repo), name, None, root.id, "developer", "work",
                               "assign", 1, ws.branch, None, None, None, "started", time.time()))
    calls = []
    real = tasks.git.run_env
    monkeypatch.setattr(tasks.git, "run_env", lambda args, *a, **k: calls.append(args) or real(args, *a, **k))

    assert tasks.find_clash(db, root, ["w3.py"]).holder == "w3"
    assert tasks.find_clash(db, root, ["landed.py"]) is None
    verbs = [c[0] for c in calls]
    assert "fetch" not in verbs and "cherry" not in verbs and "patch-id" not in verbs
    assert len(calls) <= 8 * 3 * 2, verbs    # 8 per worktree per check
    calls.clear()
    assert tasks.find_clash(db, root, ["w3.py"]).holder == "w3"
    assert "merge-tree" not in {c[0] for c in calls}    # decided once per (branch, target) sha pair


@pytest.fixture
def two_sessions(db, repo, session_b_template, monkeypatch):
    """Roots A (main checkout) and B (its own session branch) on one repo,
    overlap unset; B's worker runs on src/x.py. Returns B's worker id."""
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"pipeline": false, "review": false}')
    root_a = workspaces.adopt_root(db, str(repo))
    template, made = session_b_template
    root_b = moved(made, template, repo.parent)
    db.add_workspace(root_b)
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


@pytest.mark.variant("Board incident: bare task IDs must dispatch like pinned task references, even after cleanup")
@pytest.mark.parametrize("event", ["sweep", "merged", "removed"])
def test_task_id_dependency_dispatches(db, dependency_rows, event):
    caller, boss, workspace, launched = dependency_rows
    upstream_ws = workspace("upstream", branch="feature", tracked=True)
    [upstream] = db.list_tasks(boss.repo_root)
    if event == "sweep":
        tasks.on_merged(db, upstream_ws)
        db.delete_workspace(upstream_ws.id)
    pending = tasks.enqueue(db, caller, boss, "developer", "dependent", "assign",
                            isolate=True, branch="dependent", done_when=None, files=None, depends_on=[upstream.id])
    if event == "sweep":
        assert tasks.sweep(db) == 1
    else:
        assert tasks.sweep(db) == 0
        finish = tasks.on_merged if event == "merged" else tasks.on_removed_unmerged
        finish(db, upstream_ws)
    result = db.get_task(pending.id)
    assert result.state == ("cancelled" if event == "removed" else "started")
    assert launched == ([] if event == "removed" else [result.agent_id])
    assert tasks.known_dependency(db, boss.repo_root, upstream.id)


def test_failed_mcp_removal_keeps_dependencies_pending(db, dependency_rows, monkeypatch):
    caller, boss, workspace, _ = dependency_rows
    upstream = workspace("upstream", branch="feature", tracked=True)
    db.conn.execute("UPDATE workspaces SET kind='worktree' WHERE id=?", (upstream.id,))
    Path(upstream.path).mkdir(parents=True)
    pending = tasks.enqueue(db, caller, boss, "developer", "dependent", "assign",
                            isolate=True, branch="dependent", done_when=None, files=None,
                            depends_on=[db.list_tasks(boss.repo_root)[0].id])
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (caller, boss))
    monkeypatch.setattr(mcp_server.git, "ahead_behind", lambda *a: (0, 1))
    def refuse(*a, **k):
        raise workspaces.WorkspaceError("disk full")
    monkeypatch.setattr(workspaces, "remove", refuse)
    mcp_server.remove_workspace(upstream.id)
    assert db.get_task(pending.id).state == "pending"
    assert not bus.tail(db, kinds=("pipeline.removed",))
    monkeypatch.setattr(workspaces, "remove", lambda db, ws, **k:
                        (db.delete_workspace(ws.id) or workspaces.Removed(False, None, None)))
    mcp_server.remove_workspace(upstream.id)
    assert db.get_task(pending.id).state == "cancelled"
    assert bus.tail(db, kinds=("pipeline.removed",))[0]["agent_id"] is not None


@pytest.mark.parametrize(("event", "upstream_kind"), [
    ("removed", "task"), ("requeue_merged", "task"), ("requeue_pending", "workspace"),
])
def test_same_branch_dependency_does_not_resolve_to_itself(db, dependency_rows, monkeypatch, event, upstream_kind):
    caller, boss, workspace, launched = dependency_rows
    upstream = workspace("engine-worker", "app" if upstream_kind == "local_workspace" else "engine",
                         "shared", tracked=upstream_kind == "task")
    if upstream_kind == "worker":
        fake_spawn(db, upstream, "developer", parent_id=caller.id)
    monkeypatch.setattr(tasks.git, "ok", lambda cmd, path=None:
                        bool(db.merged_sha(upstream.id)) and cmd[:3] == ["merge-base", "--is-ancestor", "shared"])
    db.add_session_repo(caller.id, "engine", upstream.repo_root)
    assert tasks.unmet_dependencies(db, boss, ["shared"], caller.id) == ["shared"]
    second = (workspace("second", "engine", "second", tracked=upstream_kind != "workspace")
              if event == "requeue_pending" else None)
    deps = ["shared"] + (["second" if upstream_kind == "workspace" else agents.workspace_worker(db, second).id]
                         if second else [])
    pending = tasks.enqueue(db, caller, boss, "developer", "app", "assign",
                            isolate=True, branch="shared", done_when=None, files=None, depends_on=deps)
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
            if upstream_kind == "workspace":
                db.update_task(pending.id, state="held")
    if event in ("removed", "requeue_removed"):
        tasks.on_removed_unmerged(db, upstream)
        db.delete_workspace(upstream.id)
        assert db.get_task(pending.id).state == "cancelled"
        assert launched == []
    else:
        db.set_merged(upstream.id, "merged-sha")
        if second and upstream_kind == "workspace":
            db.set_merged(second.id, "second-sha")
            original_tx = db.tx

            @contextmanager
            def interleaved_tx():
                monkeypatch.setattr(db, "tx", original_tx)
                tasks.on_merged(db, second)
                with original_tx() as connection:
                    yield connection

            monkeypatch.setattr(db, "tx", interleaved_tx)
        if event == "sweep":
            assert tasks.sweep(db) == 1
        else:
            tasks.on_merged(db, upstream)
        if second:
            assert db.get_task(pending.id).state == ("held" if upstream_kind == "workspace" else "pending")
            assert launched == []
            db.delete_workspace(upstream.id)
            if upstream_kind == "workspace":
                db.delete_workspace(second.id)
            db.update_task(pending.id, state="pending")
            if upstream_kind == "workspace":
                assert tasks.sweep(db) == 1
            else:
                tasks.on_merged(db, second)
        assert db.get_task(pending.id).state == "started"
        tasks.on_merged(db, upstream)
        assert tasks.sweep(db) == 0
        assert launched == [db.get_task(pending.id).agent_id]


@pytest.mark.parametrize("finish", ["cancelled"])
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
        assert tasks.set_dependencies(db, replacement.id, [upstream.id], tasks._loads(replacement.depends_on))
        tasks.cancel(db, caller, upstream.id)
        assert db.get_task(replacement.id).state == "cancelled" and launched == []
    else:
        tasks.start_queued(db, upstream)
        tasks.on_merged(db, engine)
        assert db.get_task(replacement.id).state == "started"
        assert len(launched) == 2


@pytest.mark.variant("An accepted exact worker ID must still dispatch after its repository is detached")
@pytest.mark.parametrize("event", ["merged", "removed"])
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


@pytest.mark.variant("#183 failure path: a start_queued exception must cancel the queued task and tell its caller")
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

    before = db.list_tasks(str(repo))
    workers_before = db.list_agents()
    asyncio.run(mcp_server.assign("developer", "do D", branch="feat-d",
                                  depends_on=["feat-b"]))
    assert db.list_tasks(str(repo)) == before
    assert db.list_agents() == workers_before
    out = mcp_server.requeue(c_task.id)
    assert out.startswith("Error")
    assert db.list_tasks(str(repo)) == before


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


def queued_task(db, repo, worker_id, branch="feat-b", **kw):
    out = asyncio.run(mcp_server.assign(
        "developer", "the full brief for B", branch=branch, depends_on=[worker_id],
        done_when="pytest passes", files=["src/b.py"], **kw))
    assert "Queued task" in out, out
    return db.list_tasks(str(repo), state="pending")[-1]


def started_a(db):
    return started_worker_id(asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a")))


# -- requeue --------------------------------------------------------------------


def test_requeue_a_cancelled_task_copies_it_and_keeps_dependencies(db, repo, boss):
    a = started_a(db)
    b = queued_task(db, repo, a, plan_first=True)
    before = db.list_tasks(str(repo))
    assert mcp_server.requeue("nope").startswith("Error")
    assert mcp_server.requeue(b.id).startswith("Error")
    assert db.list_tasks(str(repo)) == before
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


# -- replying before the start ---------------------------------------------------


@pytest.mark.parametrize("tool, start", [
    ("handoff", "detached"), ("assign", "behind a worker"), ("assign", "capped"),
])
def test_assign_returns_before_spawn(db, repo, boss, monkeypatch, tool, start):
    """Under load every git and tmux step of a start waits for room, and
    assigns ran past the client's 120 s (2026-11-03): assign (and handoff)
    reply with the task id first, and admission, the worktree and the launch
    happen in the detached start afterwards. Repeated before that start
    runs, it queues nothing more, and another task on the same files is
    refused (overlap "block"); a start that beat the reply is named in it,
    never waited for. Behind a running worker, the git look at what it
    changed waits for the start too, which still refuses the overlap it
    finds; a team's worker cap holds for tasks both queued before either
    start ran, even when the first finishes between the second's reads; and
    a dependency's worktree waits for the start for its identity lookup."""
    from brindle import identity_lock, policy, spawn

    (repo / ".brindle" / "config.json").write_text('{"overlap": "block", "pipeline": false, "review": false}')
    events, helpers = [], []
    real_delegate, real_workers = agents.delegate, policy.worker_agents

    def slow_delegate(*a, **k):   # admission, worktree, setup and the tmux launch
        events.append("spawn")
        if start != "capped":
            return real_delegate(*a, **k)
        if events.count("spawn") > 1:
            pytest.fail("a second worker started past the cap of 1")
        [starting] = db.list_tasks(str(repo), state="starting")
        launched = []

        def finish_between(db_, root):   # this start finishes between the other start's two reads
            workers = real_workers(db_, root)
            if not launched:
                try:   # an error here would read as an unknown count, which the cap refuses anyway
                    launched.append(real_delegate(*a, **k))
                    mine = real_workers(db, root)   # its worker made, its task not naming it yet: counted once
                    counted = len(mine) + tasks.launching_workers(db, root, mine)
                    db.update_task(starting.id, agent_id=launched[0][0].id, state="started")
                except Exception as e:  # noqa: BLE001
                    pytest.fail(f"the first launch failed: {e!r}")
                if counted != 1:
                    pytest.fail(f"the first launch counted {counted} times")
            return workers

        monkeypatch.setattr(policy, "worker_agents", finish_between)
        for other_task in db.list_tasks(str(repo), state="pending"):   # its own helper's connection
            assert not tasks.start_when_room(DB(), other_task.id)
        monkeypatch.setattr(policy, "worker_agents", real_workers)
        return launched[0]

    def call(task: str = "do A", files: tuple[str, ...] = ("app.py",), deps: list[str] | None = None) -> str:
        return asyncio.run(getattr(mcp_server, tool)("developer", task, files=list(files), depends_on=deps))

    if start in ("behind a worker", "after a dependency"):
        worker = started_worker_id(call("do W", ()))
    if start == "behind a worker":   # its branch has changed app.py, which only git can tell
        monkeypatch.setattr(tasks, "_changed_files", lambda ws: events.append("git") or ["app.py"])
    if start == "after a dependency":   # who holds its worktree: sign-in probes, up to a minute
        monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: events.append("identity"))
    if start == "capped":   # as an org's max_parallel_workers of 1 decides
        monkeypatch.setattr(policy, "check_assign", lambda *a, running_workers=None, **k: (
            policy.allow() if running_workers == 0 else policy.deny("at most 1 parallel worker")))
    monkeypatch.setattr(agents, "delegate", slow_delegate)
    monkeypatch.setattr(agents, "wait_for_result", lambda *a, **k: pytest.fail("the reply waited for the worker"))
    if start == "at once":   # conftest's start runs as the reply is released
        out = call()
        [t] = db.list_tasks(str(repo))
        assert events == ["spawn"] and f'wait_for_worker(agent_id="{t.agent_id}")' in out, out
        return
    monkeypatch.setattr(tasks, "start_in_background", helpers.append, raising=False)
    monkeypatch.setattr(spawn, "room", lambda: 0.0)   # the machine at 0% idle
    if start == "after a dependency":
        out = call(deps=[worker])
        events.append("reply")
        [t] = [x for x in db.list_tasks(str(repo)) if x.task_text == "do A"]
        assert t.id in out and events == ["reply"] and helpers == [], out
        monkeypatch.setattr(spawn, "room", lambda: 4.0)
        assert not tasks.start_when_room(db, t.id)   # still waits for it to merge
        assert events[:2] == ["reply", "identity"] and "spawn" not in events
        return
    out = call()
    events.append("reply")
    again = call()   # the same task, before its start has run
    other = call("do B")   # another task on the same files, before either start has run
    [t] = [x for x in db.list_tasks(str(repo)) if x.task_text == "do A"]
    assert t.id in out and t.id in again and events == ["reply"] and helpers == [t.id], (out, again)
    assert other.startswith("Not started") and f"queued task {t.id}" in other, other
    monkeypatch.setattr(spawn, "room", lambda: 4.0)
    if start == "behind a worker":
        assert not tasks.start_when_room(db, t.id)
        assert events == ["reply", "git"] and db.get_task(t.id).state == "cancelled"
        return
    if start == "capped":
        c = call("do C", ("c.py",))   # accepted: no worker is at work yet
        [t_c] = [x for x in db.list_tasks(str(repo)) if x.task_text == "do C"]
        assert t_c.id in c and helpers == [t.id, t_c.id], c
    assert tasks.start_when_room(db, t.id)   # what the detached helper runs once there's room
    assert events == ["reply", "spawn"] and db.get_task(t.id).agent_id
    if start == "capped":
        assert db.get_task(t_c.id).state == "cancelled" and "at most 1" in tasks.cancel_reason(db, t_c.id)
        assert policy.running_workers(db, str(repo)) == 1


@pytest.mark.parametrize("when", [
    "while it works",
    pytest.param("while its reply is written", marks=pytest.mark.variant(
        "race: cancellation notification arrives after the handler returned, while stdout is blocked")),
    "client gone",
    pytest.param("written", marks=pytest.mark.variant(
        "real transport: draining the blocked pipe releases the promised task exactly once")),
])
def test_cancelled_assign_never_launches(db, repo, boss, monkeypatch, when):
    """The client gave up on an assign (TaskStop, a disconnect) before brindle
    answered it, through the real stdio transport and MCP dispatcher: what it
    queued is cancelled, and no worker ever starts for it later. Past the
    handler, the response waits behind the client's full stdout pipe (a
    slow reader); only a response actually written releases the task."""
    import anyio

    from brindle import autopilot, mcp_stdio, upgrade
    from test_mcp_stale_engine import INIT, _answers, _send

    monkeypatch.setattr(upgrade, "preload", lambda: None)
    monkeypatch.setattr(upgrade, "install", SimpleNamespace(changed=lambda: False))
    monkeypatch.delenv(mcp_stdio.HANDOVER_ENV, raising=False)
    inside, go, cancelled, registered, settled = (threading.Event() for _ in range(5))
    launched, seen = [], []
    real = (autopilot.choose_profile, tasks.answered, tasks.drop_unanswered, mcp_server._Request.cancel,
            mcp_stdio.after_reply)

    def held(*a, **k):
        inside.set()
        go.wait()
        return real[0](*a, **k)

    monkeypatch.setattr(autopilot, "choose_profile", held)
    monkeypatch.setattr(tasks, "answered", lambda *a: (real[1](*a), settled.set()))
    monkeypatch.setattr(tasks, "drop_unanswered", lambda *a: (real[2](*a), settled.set()))
    monkeypatch.setattr(mcp_server._Request, "cancel", lambda self: (real[3](self), cancelled.set()))
    monkeypatch.setattr(mcp_stdio, "after_reply", lambda rid, settle: registered.set() or real[4](rid, settle))
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: launched.append("spawn") or fake_spawn(*a, **k))
    monkeypatch.setattr(tasks, "start_in_background", launched.append)
    cancel = {"method": "notifications/cancelled", "params": {"requestId": 1}}
    call = {"id": 1, "method": "tools/call",
            "params": {"name": "assign", "arguments": {"agent_profile": "developer", "task": "do A"}}}

    def fill(fd: int) -> int:   # the client's stdout pipe, full: it isn't reading
        os.set_blocking(fd, False)
        filled = 0
        for size in (65536, 4096, 512, 1):
            while True:
                try:
                    filled += os.write(fd, b" " * size)
                except BlockingIOError:
                    break
        os.set_blocking(fd, True)
        return filled

    def slow_client(from_client: int, from_server: int, filled: int) -> None:
        registered.wait()   # the handler is done; its response waits for room in the pipe
        seen.append(DB().list_tasks(str(repo))[0].state)   # this thread's own connection
        if when == "while its reply is written":
            _send(from_client, cancel)
        elif when == "client gone":
            os.close(from_client)
        if when != "written":
            settled.wait(30)   # bounds a server whose blocked write never reads the cancel
        while filled:   # read what filled the pipe: now there's room for the response
            filled -= len(os.read(from_server, filled))

    async def client():
        to_server, from_client = os.pipe()
        from_server, to_client = os.pipe()
        conn = mcp_stdio.Connection(to_server, to_client, wants_reload=lambda reloaded: False,
                                    replace=lambda: False)
        async with anyio.create_task_group() as tg:
            tg.start_soon(conn.serve, mcp_server._run_server)
            _send(from_client, INIT, {"method": "notifications/initialized"})
            await _answers(from_server, 0)
            if when == "while it works":
                _send(from_client, call)
                await anyio.to_thread.run_sync(inside.wait)
                _send(from_client, cancel)
                await anyio.to_thread.run_sync(cancelled.wait)
                go.set()
            else:
                go.set()
                filled = fill(to_client)
                _send(from_client, call)
                await anyio.to_thread.run_sync(slow_client, from_client, from_server, filled)
                assert seen == [tasks.UNANSWERED]   # nothing released while the reply is unwritten
            await anyio.to_thread.run_sync(settled.wait)
            if when != "client gone":
                os.close(from_client)

    anyio.run(client)
    [t] = db.list_tasks(str(repo))
    if when == "written":
        assert t.state == "pending" and launched == [t.id]
        return
    tasks.sweep(db)
    assert db.get_task(t.id).state == "cancelled" and launched == []


# -- waiting for room ------------------------------------------------------------


@pytest.mark.parametrize("state", ["pending", "starting"])
def test_repeating_an_assign_queued_for_room_never_starts_a_second_worker(db, repo, boss, monkeypatch, state):
    """The load falls before the queued task's helper looks again, and the
    supervisor assigns the same task: the queued one starts (or, already
    being started, is left alone), never a second worker beside it."""
    from brindle import spawn

    load = [14.0]
    monkeypatch.setattr(spawn, "_load", lambda: load[0])
    monkeypatch.setattr(spawn, "_cores", lambda: 10)
    assert asyncio.run(mcp_server.assign("developer", "do A")).startswith("Queued task")
    [t] = db.list_tasks(str(repo))
    db.update_task(t.id, state=state)
    load[0] = 2.0
    out = asyncio.run(mcp_server.assign("developer", "do A"))
    workers = [a for a in db.list_agents() if a.mode == "assign"]
    assert len(db.list_tasks(str(repo))) == 1 and t.id in out, out
    if state == "pending":
        assert [w.id for w in workers] == [db.get_task(t.id).agent_id]
    else:
        assert workers == [] and db.get_task(t.id).state == "starting"


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

