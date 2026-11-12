"""Session branches and concrete file overlap in task coordination."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from brindle import config, gates, git, tasks
from brindle.db import Agent, Task, Workspace


@pytest.fixture(scope="module")
def session_branch_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("session-branches")
    git.run(["init", "-q", "-b", "main"], repo)
    (repo / "base.txt").write_text("base\n")
    git.run(["add", "."], repo)
    commit = ["-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm"]
    git.run([*commit, "base"], repo)
    git.run(["checkout", "-qb", "session"], repo)
    (repo / "shared.py").write_text("shared\n")
    git.run(["add", "."], repo)
    git.run([*commit, "session work"], repo)
    first = git.out(["rev-parse", "HEAD"], repo).strip()
    (repo / "extra.py").write_text("extra\n")
    git.run(["add", "."], repo)
    git.run([*commit, "more session work"], repo)
    return str(repo), first, git.ref_shas(repo)


@pytest.fixture
def coordination(db, monkeypatch):
    def workspace(name, branch, base="main", repo="/repo"):
        ws = Workspace(name, repo, name, "worktree", branch, base, f"{repo}/{name}", None, "", 0)
        db.add_workspace(ws)
        return ws

    a, b, w = workspace("a", "main"), workspace("b", "session"), workspace("w", "feature", "session")
    for name, ws, parent in (("a", a, None), ("b", b, None), ("worker", w, "b")):
        db.add_agent(Agent(name, ws.id, "developer", "claude", parent, "assign" if parent else "interactive",
                           "processing", "", None, 0))
    held = Task("held", a.repo_root, "worker", "b", b.id, "developer", "holder work", "assign", 1,
                w.branch, None, tasks._dumps(["src/shared.py"]), None, "started", 0)
    db.add_task(held)
    changed = {w.id: [], a.id: []}
    monkeypatch.setattr(tasks, "_changed_files", lambda ws: changed.get(ws.id, []))
    monkeypatch.setattr(git, "ref_shas", lambda root: {})
    monkeypatch.setattr(git, "default_branch", lambda root: "main")
    monkeypatch.setattr(git, "merged_into", lambda *args, **kwargs: False)
    monkeypatch.setattr(git, "paths", lambda *args, **kwargs: [f"src/file{i}.py" for i in range(30)])
    return SimpleNamespace(a=a, b=b, w=w, held=held, changed=changed, workspace=workspace)


def test_session_branch_visible_without_worker_and_cached_by_heads(db, session_branch_repo, monkeypatch):
    repo, first, refs = session_branch_repo
    refs = dict(refs)
    latest = refs["refs/heads/session"]
    refs["refs/heads/session"] = first
    a = Workspace("a", repo, "a", "worktree", "main", "main", repo, None, "", 0)
    b = replace(a, id="b", name="b", branch="session")
    for ws in (a, b):
        db.add_workspace(ws)
        # Idle just now: idle for STALLED_AFTER with no process, b couldn't hold anything.
        db.add_agent(Agent(ws.id, ws.id, "supervisor", "claude", None, "interactive", "idle", "", None,
                           tasks.time.time()))
    monkeypatch.setattr(git, "ref_shas", lambda root: refs)
    monkeypatch.setattr(git, "default_branch", lambda root: "main")
    reads = []
    paths = git.paths

    def counted(args, root):
        reads.append(args)
        return paths(args, root)

    monkeypatch.setattr(git, "paths", counted)
    clash = tasks.find_clash(db, a, ["shared.py"], caller_id="a")
    assert clash is not None and clash.holder_root == "b" and clash.cross_session
    assert tasks._resolve_dependency(db, a, clash.holder, [repo])[2].id == b.id
    assert tasks.inspected_clash(db, b, ["shared.py"], caller_id="b") is None
    assert tasks.find_clash(db, a, ["extra.py"], caller_id="a") is None
    assert len([r for r in reads if r[0] == "diff"]) == 1
    refs["refs/heads/session"] = latest
    assert tasks.find_clash(db, a, ["extra.py"], caller_id="a") is not None
    refs["refs/heads/main"] = latest
    assert tasks.find_clash(db, a, ["shared.py"], caller_id="a") is None
    assert len([r for r in reads if r[0] == "diff"]) == 3
    # Attached repositories use their recorded branch, not the root's other repo.
    db.conn.execute("UPDATE workspaces SET repo_root='/elsewhere' WHERE id='b'")
    db.add_session_repo("b", "attached", repo, "session")
    refs["refs/heads/main"] = session_branch_repo[2]["refs/heads/main"]
    assert tasks.find_clash(db, a, ["shared.py"], caller_id="a").holder_root == "b"


@pytest.mark.variant("the gap behind the Nov 6 conflicts: a reported branch dropped out of the forecast just before it merged")
def test_session_branch_forecast_keeps_reported_unmerged(db, coordination, monkeypatch):
    c = coordination
    c.changed[c.a.id] = c.changed[c.w.id] = ["src/shared.py"]
    db.update_agent("worker", result="done", status="done")
    found = tasks.conflict_forecast(db, c.a, db.get_agent("a"))
    assert [(a.id, ws.id, files) for a, ws, files in found] == [("worker", "w", ["src/shared.py"])]
    monkeypatch.setattr(git, "merged_into", lambda *args, **kwargs: True)
    assert tasks.conflict_forecast(db, c.a, db.get_agent("a")) == []


@pytest.mark.variant("Review probe: merge_into=main differs from the caller's inherited session work")
def test_session_branch_overlap_uses_delegate_merge_into_base(db, coordination, monkeypatch):
    from contextlib import nullcontext

    from brindle import agents, identity_lock, pipeline, workspaces

    c = coordination
    db.update_task("held", state="merged")
    db.conn.execute("UPDATE workspaces SET branch='session-a' WHERE id='a'")
    caller_ws = db.get_workspace("a")
    refs = {"refs/heads/main": "main-head", "refs/heads/session-a": "a-merged",
            "refs/heads/session": "b-head"}
    monkeypatch.setattr(config, "load_repo_config", lambda root: config.RepoConfig(merge_into="main"))
    monkeypatch.setattr(git, "ref_shas", lambda root: refs)
    monkeypatch.setattr(git, "paths", lambda args, root:
                        [] if args[-1] == "a-merged...b-head" else ["src/shared.py"])
    monkeypatch.setattr(git, "branch_exists", lambda root, branch: f"refs/heads/{branch}" in refs)
    monkeypatch.setattr(identity_lock, "ensure_workspace", lambda *args: None)
    monkeypatch.setattr(agents, "model_problem", lambda *args: None)
    monkeypatch.setattr(agents, "_check_branch_owner", lambda *args: None)
    monkeypatch.setattr(pipeline, "branch_start_lock", lambda *args: nullcontext())
    monkeypatch.setattr(workspaces, "branch_name", lambda root, branch: branch)
    monkeypatch.setattr(workspaces, "refresh_branch", lambda db, ws: ws)

    class CreateReached(Exception):
        pass

    def create(db, path, branch, base, *, start, **kwargs):
        raise CreateReached(base, start)

    monkeypatch.setattr(workspaces, "create", create)
    # The supervisor starts from configured main, which lacks B's work;
    # sub-workers and workers in an attached session inherit A instead.
    for kind, expected_base in (("supervisor", "main"), ("worker", "session-a"), ("attached", "session-a")):
        if kind == "worker":
            db.add_agent(replace(db.get_agent("a"), id="child", parent_id="a", mode="assign"))
        elif kind == "attached":
            outside = c.workspace("outside", "main", repo="/other-repo")
            db.update_agent("a", workspace_id=outside.id)
            db.add_session_repo("a", "attached", caller_ws.repo_root, caller_ws.branch)
        caller = db.get_agent("child" if kind == "worker" else "a")
        with pytest.raises(CreateReached) as actual:
            agents.delegate(db, caller, caller_ws, "developer", "work", "assign",
                            branch="new", plan_first=False, admit=False)
        assert actual.value.args == (expected_base, expected_base)
        clash = tasks.inspected_clash(db, caller_ws, ["src/shared.py"], caller_id=caller.id)
        if kind == "supervisor":
            assert clash is not None and clash.holder == "workspace:b"
            # An absent configured branch makes delegate use this checkout's
            # HEAD; inspection must use that commit rather than invent a base.
            del refs["refs/heads/main"]
            monkeypatch.setattr(git, "out", lambda args, root: "a-merged")
            with pytest.raises(CreateReached) as fallback:
                agents.delegate(db, caller, caller_ws, "developer", "work", "assign",
                                branch="new", plan_first=False, admit=False)
            assert fallback.value.args == ("main", "HEAD")
            assert tasks.inspected_clash(db, caller_ws, ["src/shared.py"], caller_id=caller.id) is None
            refs["refs/heads/main"] = "main-head"
            # Non-isolated work stays in the caller's checkout regardless of
            # merge_into, as the real delegation path confirms.
            def spawn(db, ws, *args, **kwargs):
                raise CreateReached(ws.id, ws.branch)

            monkeypatch.setattr(agents, "spawn", spawn)
            with pytest.raises(CreateReached) as in_place:
                agents.delegate(db, caller, caller_ws, "developer", "work", "assign",
                                isolate=False, plan_first=False, admit=False)
            assert in_place.value.args == (caller_ws.id, caller_ws.branch)
            pending = replace(c.held, id="in-place", agent_id=None, caller_id=caller.id,
                              caller_ws_id=caller_ws.id, state="pending", isolate=0)
            db.add_task(pending)
            assert tasks.inspected_clash(db, caller_ws, ["src/shared.py"], caller_id=caller.id,
                                         task_id=pending.id) is None
            db.update_task(pending.id, state="cancelled")
        else:
            assert clash is None


def test_broad_glob_requires_real_shared_files(db, coordination):
    from brindle import knowledge

    c = coordination
    assert tasks.find_clash(db, c.a, ["src/**"], caller_id="a") is None
    assert tasks.find_clash(db, c.a, ["src/*.py"], caller_id="a") is None
    db.conn.execute("UPDATE tasks SET files=? WHERE id='held'", (tasks._dumps(["src/**"]),))
    assert tasks.find_clash(db, c.a, ["src/shared.py"], caller_id="a") is None
    c.changed[c.w.id] = ["src/unrelated.py"]
    assert tasks.find_clash(db, c.a, ["src/shared.py"], caller_id="a") is None
    c.changed[c.w.id] += ["src/shared.py"]
    clash = tasks.find_clash(db, c.a, ["src/shared.py"], caller_id="a")
    assert clash.holder == "worker" and clash.shared_files == ("src/shared.py",)
    db.update_agent("worker", result="done", status="done")
    assert tasks.find_clash(db, c.a, ["app/**"], caller_id="a") is None
    assert tasks.find_clash(db, c.a, ["src/**"], caller_id="a").shared_files == (
        "src/shared.py", "src/unrelated.py")
    # Broad queued declarations and intents also need actual branch changes.
    db.conn.execute("UPDATE tasks SET state='pending', agent_id=NULL WHERE id='held'")
    knowledge.append(db, knowledge.repo_key(c.a.repo_root), knowledge.INTENT,
                     {"summary": "other session", "paths": ["src/**"]}, session="b")
    assert tasks.find_clash(db, c.a, ["src/shared.py"], caller_id="a") is None
    # Small globs still reserve concrete files at inspection, before editing.
    db.conn.execute("UPDATE tasks SET files=? WHERE id='held'", (tasks._dumps(["src/shared.py"]),))
    assert tasks.find_clash(db, c.a, ["src/shared.*"], caller_id="a").holder == "task:held"
    later = replace(c.held, id="later", agent_id=None, state="pending", files=tasks._dumps(["src/shared.*"]))
    db.add_task(later)
    assert tasks.inspected_clash(db, c.b, ["src/shared.*"], caller_id="b", task_id="held") is None
    assert tasks.inspected_clash(db, c.b, ["src/shared.*"], caller_id="b", task_id="later").holder == "task:held"


@pytest.mark.parametrize("mode", [
    "warn",
    pytest.param("block", marks=pytest.mark.variant(
        "A repeated detached launch after refusal must not send a second clash notice")),
    "queue",
])
def test_broad_glob_launch_deduplicates_and_preserves_modes(db, coordination, monkeypatch, mode):
    c = coordination
    c.changed[c.w.id] = ["src/shared.py"]
    pending = replace(c.held, id="new", agent_id=None, caller_id="a", caller_ws_id=c.a.id,
                      files=tasks._dumps(["src/**"]), state="pending", task_text="new work")
    db.add_task(pending)
    monkeypatch.setattr(config, "load_repo_config", lambda root: SimpleNamespace(overlap=mode))
    sent = []
    monkeypatch.setattr(gates, "tell", lambda db, to, text: sent.append(to))
    monkeypatch.setattr(tasks.agents, "send_message", lambda *args, **kwargs: None)
    first = tasks._launch_overlap(db, pending, c.a)
    assert first[0] is (mode == "warn")
    assert sorted(sent) == ["a", "b"]
    tasks._launch_overlap(db, db.get_task(pending.id), c.a)
    assert sorted(sent) == ["a", "b"]
    if mode == "warn":
        from brindle import mcp_server

        monkeypatch.setattr(mcp_server, "load_repo_config", config.load_repo_config)
        # The same logical task arriving through assign uses the same notice
        # identity as its detached launch, even without the same task row.
        for _ in range(2):
            reply, warning = mcp_server._route_clash(
                None, db, db.get_agent("a"), c.a, "developer", pending.task_text, "assign",
                isolate=True, branch=None, done_when=None, files=["src/shared.py"],
                depends_on=None, plan_first=None, weight=None, decision={}, why=[])
            assert reply is None and warning is not None
        assert len(sent) == 2
        c.changed[c.w.id].append("src/second.py")
        tasks._launch_overlap(db, pending, c.a)
        assert len(sent) == 4
        db.add_task(replace(pending, id="different", task_text="different work"))
        tasks._launch_overlap(db, db.get_task("different"), c.a)
        assert len(sent) == 6
        db.add_agent(replace(db.get_agent("worker"), id="second-worker"))
        db.update_task("held", agent_id="second-worker")
        tasks._launch_overlap(db, pending, c.a)
        assert len(sent) == 8
    elif mode == "block":
        assert db.get_task(pending.id).state == "cancelled"
    else:
        assert tasks._loads(db.get_task(pending.id).depends_on) == ["worker"]
        # After the worker lands, its session still holds these files until
        # the caller inherits that session. Main deliberately never advances.
        from brindle import identity_lock

        db.update_task("held", state="merged")
        db.update_task(pending.id, depends_on=None)
        db.conn.execute("UPDATE workspaces SET branch='session-a' WHERE id='a'")
        caller_ws = db.get_workspace("a")
        refs = {"refs/heads/main": "main-head", "refs/heads/session-a": "main-head",
                "refs/heads/session": "b-head"}
        monkeypatch.setattr(git, "ref_shas", lambda root: refs)
        monkeypatch.setattr(git, "paths", lambda args, root:
                            [] if args[0] == "ls-files" or args[-1] == "a-merged...b-head"
                            else ["src/shared.py"])

        def ancestry(args, root):
            if args[0] == "cat-file":
                return True
            assert args[:2] == ["merge-base", "--is-ancestor"]
            return refs["refs/heads/session-a"] == "a-merged"

        monkeypatch.setattr(git, "ok", ancestry)
        monkeypatch.setattr(identity_lock, "ensure_workspace", lambda *args: None)
        launch_config = SimpleNamespace(overlap="queue", max_weight=None, merge_into=None)
        monkeypatch.setattr(config, "load_repo_config", lambda root: launch_config)
        monkeypatch.setattr(tasks, "_claim_fair", lambda db, t: (db.claim_task(t.id), None, None))
        monkeypatch.setattr(tasks, "_launch_warnings", lambda *args: [])
        starts = []

        def delegate(db, caller, ws, *args, start=None, **kwargs):
            starts.append(start)
            worker = replace(db.get_agent("worker"), id="released", parent_id="a")
            db.add_agent(worker)
            return worker, c.w

        monkeypatch.setattr(tasks.agents, "delegate", delegate)
        assert not tasks._start_if_ready(db, db.get_task(pending.id), "worker", check_room=False)
        queued = db.get_task(pending.id)
        assert tasks._loads(queued.depends_on) == ["workspace:b"]
        assert not tasks._start_if_ready(db, queued, "workspace:b", check_room=False)
        assert starts == []

        # The holding session is locked to another sign-in: the wait goes on, never cancelled.
        monkeypatch.setattr(identity_lock, "ensure_workspace", lambda db, ws: (_ for _ in ()).throw(
            tasks.agents.AgentError("locked to another sign-in")))
        assert not tasks._start_if_ready(db, queued, "workspace:b", check_room=False)
        assert db.get_task(pending.id).state == "pending"
        refs["refs/heads/session-a"] = "a-merged"
        db.set_merged("b", "b-head")
        assert tasks.unmet_dependencies(db, caller_ws, tasks._loads(queued.depends_on), caller_id="a") == []
        assert tasks._holder_start(db, caller_ws, tasks._loads(queued.depends_on)) is None
        launch_config.overlap = "block"
        assert tasks._launch_overlap(db, queued, caller_ws) == (True, None)
        assert db.get_task(pending.id).state == "pending"
        launch_config.overlap = "queue"
        assert tasks._start_if_ready(db, queued, "workspace:b", check_room=False)
        assert db.get_task(pending.id).state == "started"
        assert starts == [None]
        # Further edits on B must still clash, even on an inherited file.
        db.update_task(pending.id, state="merged")
        refs["refs/heads/session"] = "b-next"
        assert tasks.inspected_clash(db, caller_ws, ["src/shared.py"], caller_id="a").holder == "workspace:b"


@pytest.fixture
def cross_session_wait(db, coordination, monkeypatch):
    """Session a's task on src/shared.py, which session b's unlanded branch
    holds, started through the real dispatch path (``_start_if_ready``)."""
    from brindle import identity_lock

    c = coordination
    db.update_task("held", state="merged")
    db.conn.execute("UPDATE workspaces SET branch='session-a' WHERE id='a'")
    refs = {"refs/heads/main": "cw-main", "refs/heads/session-a": "cw-main", "refs/heads/session": "cw-b"}
    monkeypatch.setattr(git, "ref_shas", lambda root: refs)
    # What each head's diff touches: b synced off the file, then edited it again.
    diffs = {"cw-b": ["src/shared.py"], "cw-b-synced": [], "cw-b-new": ["src/shared.py"], "cw-c": ["src/shared.py"]}
    monkeypatch.setattr(git, "paths", lambda args, root:
                        diffs.get(args[-1].split("...")[-1], []) if args[0] == "diff" else [])
    monkeypatch.setattr(git, "ok", lambda args, root: False)   # a never inherits b's branch
    monkeypatch.setattr(identity_lock, "ensure_workspace", lambda *args: None)
    monkeypatch.setattr(config, "load_repo_config",
                        lambda root: SimpleNamespace(overlap=None, max_weight=None, merge_into=None))
    room = {"why": None}
    monkeypatch.setattr(tasks, "task_wait", lambda db, t: room["why"])
    monkeypatch.setattr(tasks, "_wait_in_background", lambda task_id: None)
    monkeypatch.setattr(tasks, "_note_long_wait", lambda *args: None)
    monkeypatch.setattr(tasks, "_claim_fair", lambda db, t: (db.claim_task(t.id), None, None))
    monkeypatch.setattr(tasks, "_launch_warnings", lambda *args: [])
    monkeypatch.setattr(tasks.agents, "send_message", lambda *args, **kwargs: None)
    told = []
    monkeypatch.setattr(gates, "tell", lambda db, to, text: told.append(to))
    starts = []

    def delegate(db, caller, ws, *args, start=None, **kwargs):
        starts.append(start)
        worker = replace(db.get_agent("worker"), id=f"started-{len(starts)}", parent_id="a")
        db.add_agent(worker)
        return worker, c.w

    monkeypatch.setattr(tasks.agents, "delegate", delegate)
    db.add_task(replace(c.held, id="new", agent_id=None, caller_id="a", caller_ws_id="a",
                        state="pending", task_text="new work"))
    assert not tasks._start_if_ready(db, db.get_task("new"), "its dependencies")
    assert tasks._loads(db.get_task("new").depends_on) == ["workspace:b"]
    # Still held on the next look, and a second identical assign leaves it in place.
    assert tasks.sweep(db) == 0
    assert tasks.room_duplicate(db, "a", "/repo", "new work", c.held.branch).id == "new"
    assert starts == [] and db.get_task("new").state == "pending"
    told.clear()
    return SimpleNamespace(refs=refs, starts=starts, told=told, room=room, coordination=c)


def _land(db, branch="session"):
    """b's PR from ``branch`` merged, at the commit b's branch had when queued."""
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at, merged_at)"
                    " VALUES ('b', ?, ?, 'cw-b', 0, ?)", (branch, f"u-{branch}", tasks.time.time()))


@pytest.mark.parametrize("event", [
    "landed",
    pytest.param("synced", marks=pytest.mark.variant(
        "b's own sync moves its head off the files while a never inherits it: the landing seen "
        "from git when gh's merge record is missing; the queue row above reaches it only via a's inheritance")),
])
def test_cross_session_wait_released_when_other_session_lands(db, cross_session_wait, event):
    w = cross_session_wait
    if event == "landed":   # b's PR merged on main; its branch hasn't synced yet
        _land(db, "unrelated")   # a PR from another of b's branches releases nothing
        assert tasks.sweep(db) == 0
        # Session c, which never landed, holds the file too: b's landing doesn't hide it.
        w.coordination.workspace("c", "session-c")
        db.add_agent(Agent("c", "c", "developer", "claude", None, "interactive", "processing", "", None, 0))
        w.refs["refs/heads/session-c"] = "cw-c"
        _land(db)
        assert tasks.sweep(db) == 0
        assert tasks._loads(db.get_task("new").depends_on) == ["workspace:c"] and w.starts == []
        assert sorted(w.told) == ["a", "c"]   # the new wait's supervisors
        w.told.clear()
        w.refs["refs/heads/session-c"] = "cw-c-synced"
    else:   # b synced main back in: its branch no longer holds the file
        w.refs["refs/heads/session"] = "cw-b-synced"
    assert tasks.sweep(db) == 1
    assert db.get_task("new").state == "started" and len(w.starts) == 1
    assert w.told == []   # a release tells nobody beyond the caller's start note


@pytest.mark.parametrize("event", ["landed", "synced"])
def test_cross_session_wait_requeues_on_new_work_after_release(db, cross_session_wait, event):
    w = cross_session_wait
    if event == "landed":
        _land(db)
        w.refs["refs/heads/session"] = "cw-b-new"   # b edits the file again before the queue sees the landing
    else:
        w.refs["refs/heads/session"] = "cw-b-synced"
    w.room["why"] = "machine busy"   # released, but no room to start yet
    assert tasks.sweep(db) == 0
    assert tasks._loads(db.get_task("new").depends_on) == []
    w.refs["refs/heads/session"] = "cw-b-new"   # b edits the file again before there's room
    w.room["why"] = None
    assert tasks.sweep(db) == 0
    assert tasks._loads(db.get_task("new").depends_on) == ["workspace:b"] and w.starts == []


def test_cross_session_wait_falls_back_after_drift_limit(db, cross_session_wait):
    w = cross_session_wait
    db.conn.execute("UPDATE task_notes SET noted = noted - ? WHERE task_id = 'new'",
                    (tasks.CROSS_SESSION_WAIT_LIMIT + 1,))
    assert tasks.sweep(db) == 1
    assert db.get_task("new").state == "started"
    assert sorted(w.told) == ["a", "b"]   # both supervisors, not the Inbox
