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
        db.add_agent(Agent(ws.id, ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, 0))
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
