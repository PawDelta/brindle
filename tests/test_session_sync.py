"""brindle.session_sync: a session branch keeps up with origin/main. A merge
into it, a session resuming and a merged session PR bring origin/main in when
that's clean; a conflict blocks merges into it until a branch resolves it, and
a branch behind origin/main gets no PR."""

import shutil
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, autopilot, bus, config, gates, git, pipeline, session_sync, status_cache, tasks, workspaces
from brindle.daemon import session_review
from brindle.db import Agent, Workspace
from brindle.service import inbox
from brindle.service import sessions as svc
from test_merge_order import _repo_template, quiet_removal, repo, worktree  # noqa: F401 - fixture


@pytest.fixture
def told(monkeypatch):
    """What brindle said to the session's supervisor."""
    said = []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, **kw: said.append((to, body)))
    monkeypatch.setattr(session_sync, "_detach", lambda args: said.append(("detached", args)))
    return said


@pytest.fixture
def sess(db, repo, tmp_path, told, monkeypatch):
    """Session ``lead`` on branch ``sess`` (cut from main) in its own checkout."""
    quiet_removal(monkeypatch)
    sh(f"git worktree add -q -b sess {tmp_path / 'sess'} main", repo)
    ws = workspaces.adopt_root(db, str(tmp_path / "sess"))
    db.add_agent(Agent("lead", ws.id, "supervisor", "claude", None, "interactive", "idle", "%9", None, time.time()))
    return ws


@pytest.fixture
def other(repo, tmp_path, _repo_template):
    """Another clone, where commits land on origin/main without the session:
    the template's checkout, a clone of the same origin, pointed at this one."""
    path = tmp_path / "other"
    shutil.copytree(_repo_template / "proj", path, symlinks=True)
    cfg = path / ".git" / "config"
    cfg.write_text(cfg.read_text().replace(str(_repo_template), str(tmp_path)))
    return path


def commit(path: Path, name: str, text: str, push: bool = False) -> str:
    (path / name).write_text(text)
    sh(f"git add {name} && git commit -qm {name}", path)
    if push:
        sh("git push -q origin HEAD:main", path)
    return sh("git rev-parse HEAD", path)


def worker(db, repo, name: str):
    return worktree(db, repo, name, base="sess", files={f"{name}.txt": name})


def contains(path, sha: str) -> bool:
    return git.ok(["merge-base", "--is-ancestor", sha, "HEAD"], path)


def test_idle_sync_quiet_session(db, repo, sess, other, told, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(session_sync.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(session_review.spawn, "room", lambda: 0)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status != "done")
    db.conn.execute("UPDATE workspaces SET base_branch = 'main' WHERE id = ?", (sess.id,))
    main = commit(other, "hot.py", "fix\n", push=True)
    feat = worker(db, repo, "feat")
    db.add_agent(Agent("worker", feat.id, "developer", "claude", "lead", "assign", "working", "%10", None, 0))
    sibling = worktree(db, repo, "another", base="main")
    owner = Workspace("owner", "/elsewhere", "owner", "root", "main", None, "/elsewhere", None, "", 0)
    db.add_workspace(owner)
    db.add_agent(Agent("other-lead", owner.id, "supervisor", "claude", None, "interactive", "processing",
                       "%11", None, 0))
    db.add_session_repo("other-lead", "attached", sibling.repo_root, sibling.branch)
    watch = session_review.ReadinessWatch()
    fetched = []
    fetch = session_sync.fetch

    def fetching(root, default):
        fetched.append(clock[0])
        return fetch(root, default)

    monkeypatch.setattr(session_sync, "fetch", fetching)
    watch.sweep(db)                         # a live worker holds the session
    assert not contains(sess.path, main)
    db.set_status("worker", "done")
    db.update_agent("worker", pipeline="ready")
    watch.sweep(db)                         # even a finished worker can have a queued merge
    assert not contains(sess.path, main)
    db.update_agent("worker", pipeline=None)
    db.set_status("lead", "processing")
    watch.sweep(db)
    assert not contains(sess.path, main)
    db.set_status("lead", "idle")
    dirty = Path(sess.path) / "unsaved.txt"
    dirty.write_text("unsaved\n")
    watch.sweep(db)
    assert not contains(sess.path, main)
    dirty.unlink()
    base_lock = pipeline.base_lock

    @contextmanager
    def busy(*args, **kwargs):
        raise BlockingIOError
        yield

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "base_lock", busy)
        watch.sweep(db)
    assert not contains(sess.path, main)
    assert pipeline.base_lock is base_lock
    watch.sweep(db)
    assert contains(sess.path, main)
    assert len([m for m in told if m[0] == "lead"]) == 1
    assert len([m for m in told if m[0] == "detached"]) == 1
    assert not contains(sibling.path, main)   # its owner is busy in another repo
    db.set_status("other-lead", "idle")
    watch.sweep(db)
    assert contains(sibling.path, main)       # attached checkouts have no local root agent
    assert len([m for m in told if m[0] == "other-lead"]) == 1
    assert len([m for m in told if m[0] == "detached"]) == 2
    assert len(fetched) == 1
    clock[0] += 301
    watch.sweep(db)
    assert len(fetched) == 2
    assert len([m for m in told if m[0] == "lead"]) == 1
    assert len([m for m in told if m[0] == "other-lead"]) == 1


@pytest.mark.variant("the idle git storm measured live: 14 git calls per session every 5 s")
def test_a_sweep_starts_no_git_for_a_session_whose_refs_have_not_moved(db, repo, sess, other, monkeypatch):
    """The daemon sweeps every few seconds: an idle, synced session costs no
    process until its HEAD or origin/main moves, and is synced again then;
    origin/main moving just as a sync finishes is never taken as synced."""
    import subprocess

    clock = [1000.0]
    monkeypatch.setattr(session_sync.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(session_review.spawn, "room", lambda: 0)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
    db.conn.execute("UPDATE workspaces SET base_branch = 'main' WHERE id = ?", (sess.id,))
    late, sync = [], session_sync.sync

    def another_fetch_lands_as_it_finishes(db_, ws, **kw):
        out = sync(db_, ws, **kw)
        if not late:
            late.append(commit(other, "late.py", "late\n", push=True))
            session_sync.fetch(ws.repo_root, "main")
        return out

    monkeypatch.setattr(session_sync, "sync", another_fetch_lands_as_it_finishes)
    watch = session_review.ReadinessWatch()
    watch.sweep(db)
    watch.sweep(db)                         # no fetch due: origin/main as that one left it
    assert contains(sess.path, late[0])
    watch.sweep(db)
    started, real = [], subprocess.Popen.__init__
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, args, *a, **k: (
        started.append(args), real(self, args, *a, **k))[1])
    watch.sweep(db)
    assert started == []
    main = commit(other, "hot.py", "fix\n", push=True)
    clock[0] += session_sync.IDLE_FETCH_SECONDS
    watch.sweep(db)
    assert contains(sess.path, main)


def test_idle_sync_worker_goes_live_before_merge(db, told, monkeypatch):
    sess = Workspace("session", "/repo", "session", "worktree", "sess", "main", "/repo/session", None, "", 0)
    owner = Workspace("owner", "/primary", "owner", "root", "main", None, "/primary", None, "", 0)
    db.add_workspace(sess)
    db.add_workspace(owner)
    db.add_agent(Agent("lead", owner.id, "supervisor", "claude", None, "interactive", "idle", "%9", None, 0))
    db.add_session_repo("lead", "attached", sess.repo_root, sess.branch)
    db.add_agent(Agent("worker", sess.id, "developer", "claude", "lead", "assign", "done", "%10", None, 0))
    monkeypatch.setattr(session_sync.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status != "done")
    monkeypatch.setattr(session_sync, "upstream", lambda ws: "main")
    monkeypatch.setattr(session_sync, "_remote_default", lambda root: "main")
    monkeypatch.setattr(session_sync, "fetch", lambda *args: True)
    monkeypatch.setattr(session_sync, "drift", lambda *args: session_sync.Drift("origin/main", "main-sha", 1, []))
    monkeypatch.setattr(git, "dirty_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(status_cache, "cached_status", lambda path, base: git.Status("session", base, 0, 0, [], None))
    monkeypatch.setattr(git, "out", lambda *args: "head-sha")
    monkeypatch.setattr(pipeline, "base_lock", lambda *args, **kwargs: nullcontext())
    merged = []
    monkeypatch.setattr(git, "merge_local_base", lambda *args, **kwargs: merged.append(args) or ("new-sha", []))
    entered = []
    waking = ["worker"]

    @contextmanager
    def starting_worker(path, **kwargs):
        entered.append(path)
        db.set_status(waking[0], "processing")
        yield

    monkeypatch.setattr(git, "checkout_lock", starting_worker)
    session_sync.refresh_idle_sessions(db, {})
    assert entered == [sess.path]
    assert merged == []
    assert not told
    db.set_status("worker", "done")
    waking[0] = "lead"                      # its remote owner can also begin a turn after planning
    session_sync.refresh_idle_sessions(db, {})
    assert entered == [sess.path, sess.path]
    assert merged == [] and not told


def test_a_merge_into_the_session_tells_the_supervisor_how_far_behind_main_it_is(db, repo, sess, other, told):
    commit(Path(sess.path), "app.py", "session\n")
    commit(other, "app.py", "main\n", push=True)
    main = commit(other, "hot.py", "fix\n", push=True)
    (Path(sess.path) / "app.py").write_text("unsaved\n")   # the supervisor is mid-edit: nothing is merged
    feat = worker(db, repo, "feat")

    pipeline.merge(db, None, feat)

    [(to, text)] = [m for m in told if m[0] == "lead"]
    assert "2 commit(s)" in text and "files both changed: app.py" in text and "uncommitted" in text
    assert not contains(sess.path, main)


@pytest.mark.parametrize("trigger, diverged", [("merge", True), ("merge", False), ("resume", True)],
                         ids=["diverged", "linear", "resume"])
def test_a_clean_sync_merges_main_in_and_checks_what_it_brought(db, repo, sess, other, told, monkeypatch,
                                                                 trigger, diverged):
    config.set_local(sess.repo_root, "checks", ["test -f hot.py"])
    if trigger == "resume":
        db.set_status("lead", "paused")
        # Cut, as sessions once were, from another branch: it still keeps up with main.
        db.conn.execute("UPDATE workspaces SET base_branch = 'feat/old' WHERE id = ?", (sess.id,))
    before = commit(Path(sess.path), "a.py", "a\n") if diverged else sh("git rev-parse HEAD", Path(sess.path))
    main = commit(other, "hot.py", "fix\n", push=True)

    if trigger == "resume":
        resumed = []
        monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
        monkeypatch.setattr(agents, "resume", lambda db_, rid, **kw: resumed.append(contains(sess.path, main)) or [])
        got = svc.continue_(db, workspaces.adopt_root(db, str(repo)), "lead")
        assert got.state == "resumed" and resumed == [True]   # main is merged in before the session resumes
        assert any(n.text.startswith("brindle merged origin/main into sess") for n in got.notes)
        synced = "HEAD"
    else:
        out = pipeline.merge(db, None, worker(db, repo, "feat"))
        assert out.startswith("Merged"), out
        assert any(to == "lead" and "brindle merged origin/main into sess" in text for to, text in told)
        synced = "HEAD~1"                  # a merge commit of origin/main, then the worker's merge

    parents = sh(f"git rev-list --parents -n 1 {synced}", Path(sess.path)).split()[1:]
    assert parents == [before, main]
    [args] = [a for to, a in told if to == "detached"]
    assert session_sync.run_checks(db, args[-2], args[-1]).startswith("The checks passed")


def test_drift_flag_sync_resolver_once_only(db, repo, sess, other, told, monkeypatch):
    monkeypatch.setattr(autopilot, "choose_profile", lambda *a, **kw: ("developer", False))
    commit(Path(sess.path), "app.py", "session\n")
    main = commit(other, "app.py", "main\n", push=True)
    feat = worker(db, repo, "feat")
    approved = Agent("approved", feat.id, "developer", "claude", "lead", "assign", "done", "%12",
                     "Ready for merge", time.time())
    db.add_agent(approved)
    db.update_agent(approved.id, pipeline="ready")
    db.add_review(feat.id, gates.head(feat), "reviewer", True, "Approved")

    out = pipeline._finish_locked(db, db.get_agent(approved.id), feat, None)

    assert out.startswith("Not merged: sess must be synced with origin/main") and "app.py" in out
    assert not contains(sess.path, main) and sh("git status --porcelain", Path(sess.path)) == ""
    [event] = bus.tail(db, kinds=("conflict",))
    assert event["workspace_id"] == sess.id and event["payload"]["files"] == ["app.py"]
    [resolver] = db.list_tasks()
    assert resolver.caller_id == "lead" and resolver.caller_ws_id == sess.id
    assert resolver.isolate and resolver.state == "pending"
    assert not inbox.items(db, str(repo), panes={})
    assert session_sync._quiet(db, sess)       # the blocked merge must not hold its resolver off
    session_sync.sync(db, sess)
    assert [t.id for t in db.list_tasks()] == [resolver.id]
    # Only the refusal caused by session sync waits; unrelated failures still surface.
    waiting = db.get_agent(approved.id).pipeline
    db.update_agent(approved.id, pipeline=None, merge_failed="Unrelated merge failure")
    assert any(i.agent_id == approved.id and i.kind == "merge_failed"
               for i in inbox.items(db, str(repo), panes={}))
    db.update_agent(approved.id, pipeline=waiting, merge_failed=None)
    fix = workspaces.create(db, str(repo), "fix", base="sess", fetch=False).workspace   # sess isn't on origin
    sh("git merge -q origin/main || true", Path(fix.path))
    commit(Path(fix.path), "app.py", "both\n")
    db.add_agent(Agent("resolver", fix.id, "developer", "claude", "lead", "assign", "done", "%10",
                       "Resolved", time.time()))
    db.update_task(resolver.id, agent_id="resolver", state="started")
    assert pipeline.merge(db, None, fix).startswith("Merged")
    assert contains(sess.path, main) and session_sync.merge_block(db, feat) is None
    assert db.get_task(resolver.id).state == "merged"
    assert session_sync.needs_you(db, sess) is None
    assert any(a.id == approved.id for a, _ws in pipeline.ready_branches(db, str(repo), "sess"))
    monkeypatch.setattr(pipeline, "_still_in_use", lambda *args: "fixture")
    assert pipeline._finish(db, db.get_agent(approved.id), feat, None).startswith("Merged")
    assert db.merged_sha(feat.id)


@pytest.fixture
def sync_resolver_session(db, monkeypatch, told):
    ws = Workspace("session", "/repo", "session", "worktree", "sess", "main", "/repo/session", None, "", 0)
    db.add_workspace(ws)
    db.add_agent(Agent("lead", ws.id, "supervisor", "claude", None, "interactive", "idle", "%9", None, 0))
    monkeypatch.setattr(session_sync, "upstream", lambda ws: "main")
    monkeypatch.setattr(session_sync, "fetch", lambda *a: True)
    monkeypatch.setattr(session_sync, "default_branch", lambda *a: "main")
    monkeypatch.setattr(session_sync, "drift", lambda *a: session_sync.Drift("origin/main", "origin-sha", 1, []))
    monkeypatch.setattr(git, "dirty_files", lambda *a, **kw: [])
    monkeypatch.setattr(git, "out", lambda *a: "head-sha")
    monkeypatch.setattr(git, "ok", lambda *a: False)
    monkeypatch.setattr(git, "merge_local_base", lambda *a, **kw: (None, ["app.py"]))
    monkeypatch.setattr(pipeline, "base_lock", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(git, "checkout_lock", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(autopilot, "choose_profile", lambda *a, **kw: ("developer", False))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status != "done")
    return ws


def test_sync_resolver_no_inbox_while_resolving(db, sync_resolver_session, monkeypatch):
    ws = sync_resolver_session
    db.add_agent(Agent("busy", ws.id, "developer", "claude", "lead", "assign", "working", "%10", None, 0))
    historical = tasks.enqueue(db, db.get_agent("lead"), ws, "developer",
                               "Merge origin/main and resolve app.py", "assign", isolate=True,
                               branch=None, done_when=None, files=["app.py"], depends_on=None)
    db.update_task(historical.id, state="merged")
    older = tasks.enqueue(db, db.get_agent("lead"), ws, "developer",
                          f"Merge origin/main and resolve app.py at {'a' * 40}", "assign", isolate=True,
                          branch=None, done_when=None, files=["app.py"], depends_on=None)
    db.update_task(older.id, state="held")
    enqueue = tasks.enqueue

    def enqueue_during_sweep(*args, **kwargs):
        task = enqueue(*args, **kwargs)
        assert tasks.sweep(db) == 0       # no dispatch before its quietness constraint is attached
        return task

    monkeypatch.setattr(tasks, "enqueue", enqueue_during_sweep)
    session_sync.sync(db, ws)
    [resolver] = [t for t in db.list_tasks() if t.id not in (historical.id, older.id)]
    monkeypatch.setattr(tasks, "task_wait", lambda *a: None)
    monkeypatch.setattr(tasks, "_launch_overlap", lambda *a: (True, None))
    monkeypatch.setattr(tasks, "_claim_fair", lambda db, t: (tasks.move(db, t.id, "pending", "starting"), None, None))
    monkeypatch.setattr(tasks, "_launch_warnings", lambda *a: [])
    launched = []

    def delegate(db, caller, caller_ws, profile, text, mode, **kw):
        launched.append((caller.id, caller_ws.branch, kw["isolate"]))
        worker = Agent("resolver", ws.id, profile, "claude", caller.id, mode, "working", "%11", None, 1)
        db.add_agent(worker)
        return worker, ws

    monkeypatch.setattr(agents, "delegate", delegate)
    db.update_task(resolver.id, waiting="load")
    with pytest.raises(ValueError):
        tasks.start_now(db, db.get_agent("lead"), resolver.id)
    assert tasks.sweep(db) == 0
    assert not [i for i in inbox.items(db, ws.repo_root, panes={}) if i.kind == "sync_conflict"]
    db.set_status("busy", "done")
    db.set_status("lead", "processing")
    assert tasks.sweep(db) == 0
    db.set_status("lead", "idle")
    assert tasks.sweep(db) == 1
    assert launched == [("lead", "sess", True)]
    assert db.get_task(resolver.id).state == "started"
    assert not inbox.items(db, ws.repo_root, panes={})
    session_sync.sync(db, ws)
    assert len(db.list_tasks()) == 3


def test_sync_resolver_inbox_after_failure(db, sync_resolver_session, monkeypatch):
    ws = sync_resolver_session
    # A supervisor's own resolver must be reused, including after it fails.
    resolver = tasks.enqueue(db, db.get_agent("lead"), ws, "developer",
                             "Resolve the conflict by merging `origin/main` into the session branch",
                             "assign", isolate=True, branch=None, done_when=None, files=["app.py"], depends_on=None)
    db.add_agent(Agent("resolver", ws.id, "developer", "claude", "lead", "assign", "working", "%10", None, 1))
    db.update_task(resolver.id, agent_id="resolver", state="started")
    session_sync.sync(db, ws)
    assert len(db.list_tasks()) == 1
    assert session_sync.needs_you(db, ws) is None
    db.update_agent("resolver", status="done", result="Resolved; awaiting review")
    assert session_sync.needs_you(db, ws) is None
    db.update_agent("resolver", pipeline="reviewing")
    pipeline._send_back(db, ws, db.get_agent("resolver"), config.RepoConfig(), "Unresolved conflict",
                        True, subject="Review", verb="requested changes", what="review", sender_id=None)
    assert any(i.kind == "sync_conflict" and i.root_id == "lead"
               for i in inbox.items(db, ws.repo_root, panes={}))
    monkeypatch.setattr(git, "ahead_behind", lambda *a: (0, 1))
    monkeypatch.setattr(pipeline, "_start_review", lambda *a: None)
    agents.report_result(db, "resolver", "New resolution ready for review")
    assert db.get_agent("resolver").pipeline == "reviewing"
    assert not [i for i in inbox.items(db, ws.repo_root, panes={}) if i.kind == "sync_conflict"]

    def failed_review(db, cfg, parent, worker, workspace, result, report):
        pipeline._send_back(db, workspace, worker, cfg, "Unresolved conflict", True,
                            subject="Review", verb="requested changes", what="review", sender_id=None)

    monkeypatch.setattr(pipeline, "_start_review", failed_review)
    agents.report_result(db, "resolver", "Another resolution ready for review")
    assert session_sync.needs_you(db, ws)
    agents.report_result(db, "resolver", "Another resolution ready for review")
    assert session_sync.needs_you(db, ws)      # replaying an accepted report cannot undo its escalation
    db.set_status("resolver", "failed")
    [item] = [i for i in inbox.items(db, ws.repo_root, panes={}) if i.kind == "sync_conflict"]
    assert item.root_id == "lead"
    db.update_task(resolver.id, state="cancelled")
    assert session_sync.needs_you(db, ws)
    session_sync.sync(db, ws)
    assert len(db.list_tasks()) == 1
    monkeypatch.setattr(session_sync, "drift", lambda *a: session_sync.Drift("origin/main", "next-sha", 1, []))
    session_sync.sync(db, ws)
    assert len(db.list_tasks()) == 2
    assert session_sync.needs_you(db, ws) is None

    def refuse_profile(*args, **kwargs):
        raise autopilot.AutopilotError("No permitted profile")

    monkeypatch.setattr(autopilot, "choose_profile", refuse_profile)
    monkeypatch.setattr(session_sync, "drift", lambda *a: session_sync.Drift("origin/main", "refused-sha", 1, []))
    session_sync.sync(db, ws)
    assert len(db.list_tasks()) == 2
    assert session_sync.needs_you(db, ws)
    session_sync.sync(db, ws)
    assert len(db.list_tasks()) == 2
    # Without an owner, nothing can be queued and the conflict needs a person.
    db.update_agent("lead", status="done")
    monkeypatch.setattr(session_sync, "drift", lambda *a: session_sync.Drift("origin/main", "ownerless-sha", 1, []))
    session_sync.sync(db, ws)
    assert session_sync.needs_you(db, ws)


@pytest.fixture(scope="module")
def drift_flag_repo(tmp_path_factory, _repo_template):
    path = tmp_path_factory.mktemp("drift-flag") / "repo"
    shutil.copytree(_repo_template / "proj", path)
    git.run(["checkout", "-b", "session"], str(path))
    for i in range(60):
        (path / f"file-{i}").write_text("work\n")
    git.run(["add", "-A"], str(path))
    with pytest.MonkeyPatch.context() as patch:
        # Module scope runs before the per-test identity in conftest: CI has no global git user.
        for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            patch.setenv(key, "t")
        for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            patch.setenv(key, "t@example.com")
        patch.setenv("GIT_COMMITTER_DATE", "1700000000 +0000")
        git.run(["commit", "-m", "session work"], str(path))
        (path / "file-0").write_text("more work\n")
        git.run(["add", "-A"], str(path))
        patch.setenv("GIT_COMMITTER_DATE", "1700003600 +0000")
        git.run(["commit", "-m", "newer session work"], str(path))
    return path


@pytest.mark.variant("the boundary: exactly 48 h and 60 files is not drift; read through the goal route the sidebar uses")
def test_drift_flag_crossings_and_goal_dto(db, drift_flag_repo, tmp_path, told, monkeypatch):
    from starlette.requests import Request
    from brindle.daemon import routes_read

    path = tmp_path / "repo"
    shutil.copytree(drift_flag_repo, path)
    ws = workspaces.adopt_root(db, str(path))
    db.add_agent(Agent("lead", ws.id, "supervisor", "claude", None, "interactive", "idle", "%9", None, 0))
    autopilot.set_goal(db, "lead", "Ship", [("Ship work", "true", None)])
    monkeypatch.setattr(autopilot, "goal_activity", lambda *a: {})
    monkeypatch.setattr(session_review, "open_pr_for_head", lambda *a: None)
    monkeypatch.setattr(session_review.spawn, "room", lambda: 0)
    oldest = 1700000000
    clock = [oldest + 48 * 3600]
    monkeypatch.setattr(session_sync.time, "time", lambda: clock[0])
    request = Request({"type": "http", "query_string": f"repo={ws.repo_root}&root=lead".encode()})

    def flag():
        session_review.ReadinessWatch().sweep(db)
        return routes_read.goal.__wrapped__(db, request)["drifting"]

    assert flag() is False                 # exactly 48 hours and 60 files
    clock[0] += 1
    assert flag() is True
    assert flag() is True and len(told) == 1
    # Landing the session's work clears the flag and rearms the notice.
    git.run(["update-ref", "refs/remotes/origin/main", "HEAD"], ws.path)
    assert flag() is False
    # Main's old history doesn't age fresh session work; only unique commits count.
    for i in range(61):
        (path / f"file-{i}").write_text("new work\n")
    git.run(["add", "-A"], ws.path)
    git.run(["commit", "-m", "more session work"], ws.path)
    clock[0] = int(git.out(["log", "-1", "--format=%ct"], ws.path))
    assert flag() is True and len(told) == 2
    assert flag() is True and len(told) == 2
    assert [e["payload"]["drifting"] for e in bus.tail(db, kinds=("session.pr_drift",))] == [True, False, True]


def test_drift_flag_squash_landing_clears_covered_history_and_rearms(db, drift_flag_repo, tmp_path, told,
                                                                  monkeypatch):
    path = tmp_path / "repo"
    shutil.copytree(drift_flag_repo, path)
    ws = workspaces.adopt_root(db, str(path))
    db.add_agent(Agent("lead", ws.id, "supervisor", "claude", None, "interactive", "idle", "%9", None, 0))
    clock = [1700000000 + 48 * 3600 + 1]
    monkeypatch.setattr(session_sync.time, "time", lambda: clock[0])

    def landing(covers):
        squash = git.out(["commit-tree", f"{covers}^{{tree}}", "-p", "origin/main", "-m", "squash"], ws.path)
        git.run(["update-ref", "refs/remotes/origin/main", squash], ws.path)
        assert session_sync.record_squash(db, ws, squash, covers)
        assert session_sync._sync_locked(db, ws, "main").state == "synced"

    def oldest():
        session_sync.refresh_drift_flags(db)
        return db.conn.execute("SELECT oldest_at FROM session_drift").fetchone()[0]

    assert oldest() == 1700000000 and session_sync.drifting(db, "lead")
    covers = git.out(["rev-parse", "HEAD"], ws.path)
    # A recorded landing whose tree doesn't carry the work cannot clear it.
    unrelated = git.out(["commit-tree", "origin/main^{tree}", "-p", "origin/main", "-m", "unrelated"], ws.path)
    git.run(["update-ref", "refs/remotes/origin/main", unrelated], ws.path)
    assert session_sync.record_squash(db, ws, unrelated, covers)
    assert oldest() == 1700000000 and session_sync.drifting(db, "lead")
    landing(covers)
    assert git.out(["diff", "origin/main", "HEAD"], ws.path) == ""
    assert oldest() is None and not session_sync.drifting(db, "lead")

    # Fresh work after landing ages from its own commit, then notifies again.
    with monkeypatch.context() as patch:
        patch.setenv("GIT_COMMITTER_DATE", "1700200000 +0000")
        first = commit(path, "file-0", "next PR\n")
    clock[0] = 1700200000 + 48 * 3600
    assert oldest() == 1700200000 and not session_sync.drifting(db, "lead")
    notices = len(told)
    clock[0] += 1
    assert oldest() == 1700200000 and session_sync.drifting(db, "lead")
    assert len(told) == notices + 1
    oldest()
    assert len(told) == notices + 1

    # A second landing covers only that PR, preserving work made before sync.
    with monkeypatch.context() as patch:
        patch.setenv("GIT_COMMITTER_DATE", "1700400000 +0000")
        commit(path, "file-0", "later unlanded work\n")
    landing(first)
    clock[0] = 1700400000 + 48 * 3600
    assert oldest() == 1700400000 and not session_sync.drifting(db, "lead")
    notices = len(told)
    clock[0] += 1
    assert oldest() == 1700400000 and session_sync.drifting(db, "lead")
    assert len(told) == notices + 1


@pytest.mark.variant("a conflict in the pipeline's own merge (not session sync) still records a conflict event")
def test_drift_flag_pipeline_conflict_event(db, repo, monkeypatch):
    from brindle.workspaces import SyncResult

    ws = worktree(db, repo, "conflicting", files={"app.py": "work\n"})
    monkeypatch.setattr(workspaces, "sync_with_base", lambda ws: SyncResult("conflict", conflicts=["app.py"]))
    pipeline.merge(db, None, ws)
    [event] = bus.tail(db, kinds=("conflict",))
    assert event["workspace_id"] == ws.id and event["payload"]["files"] == ["app.py"]


def test_drift_flag_coordinator_overlap_event(db, repo, sess, monkeypatch):
    from brindle import knowledge, tasks

    monkeypatch.setattr(knowledge, "intents", lambda *a: [
        {"status": "live", "session_root": "other", "paths": ["app.py", "other.py"], "summary": "Other work"}])
    clash = tasks.inspected_clash(db, sess, ["*.py"], caller_id="lead")
    assert clash is not None
    tasks.notify_clash(db, "lead", clash, "warned")
    [event] = bus.tail(db, kinds=("overlap",))
    assert event["root_id"] == "lead" and event["payload"]["files"] == ["app.py", "other.py"]


def test_a_session_behind_main_gets_no_pr(db, repo, sess, other):
    commit(Path(sess.path), "app.py", "session\n")
    commit(other, "app.py", "main\n", push=True)
    sh("git fetch -q origin", Path(sess.path))

    assert any(r.startswith("origin/main has 1 commit(s) sess lacks")
               for r in session_review.reasons(db, db.get_agent("lead"), sess))
    with pytest.raises(workspaces.WorkspaceError, match="not opening a PR: origin/main"):
        with gates.pr_checked(db, sess):
            pass


def test_a_squashed_session_pr_syncs_back_once_its_landing_is_known(db, repo, sess, other, monkeypatch):
    path, url = Path(sess.path), "https://github.com/o/r/pull/7"
    pushed = commit(path, "app.py", "v1\n")
    sh("git push -q origin sess", path)
    sh("git fetch -q origin sess && git merge -q --squash origin/sess && git commit -qm squash", other)
    sh("git push -q origin main", other)
    squash = sh("git rev-parse HEAD", other)
    main = commit(other, "hot.py", "fix\n", push=True)
    commit(path, "app.py", "v2\n")          # the session carries on editing the same line
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at) VALUES (?, ?, ?, ?, ?)",
                    ("lead", "sess", url, pushed, time.time()))
    # The forge doesn't say at first, past the point where the sync stops waiting on it.
    lookups = [None] * (session_sync.MAX_LANDING_LOOKUPS + 1) + [squash]
    monkeypatch.setattr(workspaces, "pull_request_merged", lambda u, cwd: True)
    monkeypatch.setattr(workspaces, "pull_request_merge_commit", lambda u, cwd: lookups.pop(0))
    monkeypatch.setattr(session_review, "MERGE_POLL_SECONDS", 0.0)

    session_review.poll_merged(db)          # merged; how it landed isn't known: nothing merged yet
    assert not contains(path, main) and session_sync.conflict(db, sess.repo_root, "sess") is None
    for _ in range(session_sync.MAX_LANDING_LOOKUPS + 1):
        session_review.poll_merged(db)

    assert lookups == [] and session_sync.unlanded(db) == []
    assert contains(path, main) and (path / "app.py").read_text() == "v2\n"


def test_a_refused_fetch_tells_the_supervisor_once_until_origin_answers_again(db, repo, sess, told, tmp_path):
    origin = sh("git remote get-url origin", repo)
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text('#!/bin/sh\necho "Permission denied (publickey)." >&2\nexit 255\n')
    fake_ssh.chmod(0o755)
    sh(f"git config core.sshCommand {fake_ssh}", repo)

    def fetch_from(url: str) -> list:
        sh(f"git remote set-url origin {url}", repo)
        session_sync.fetch(str(repo), "main")
        for _ in range(2):      # the daemon's sweep, round after round
            session_sync.tell_refused_fetches(db)
        return [text for to, text in told if to == "lead"]

    [first] = fetch_from("ssh://example.invalid/x")
    assert str(repo) in first
    assert len(fetch_from(origin)) == 1
    assert len(fetch_from("ssh://example.invalid/x")) == 2


@pytest.mark.parametrize("reachable", [True, False], ids=["local-main-ahead", "origin-unreachable"])
def test_a_new_session_branch_starts_only_at_origin_main_as_just_fetched(db, repo, reachable):
    origin = sh("git rev-parse origin/main", repo)
    commit(repo, "local.py", "unpushed\n")   # local main has a commit of its own
    if not reachable:
        sh(f"git remote set-url origin {repo.parent / 'gone.git'}", repo)

    if not reachable:
        with pytest.raises(workspaces.WorkspaceError, match="couldn't fetch origin/main"):
            svc.session_worktree(db, workspaces.adopt_root(db, str(repo)))
        return
    created = svc.session_worktree(db, workspaces.adopt_root(db, str(repo)))
    assert sh("git rev-parse HEAD", Path(created.path)) == origin
