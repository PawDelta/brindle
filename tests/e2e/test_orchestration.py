"""E2E of brindle's MCP orchestration against a throwaway git repo.

The real ``mcp_server`` tool functions run; only the CLI processes are faked
(workers are records that commit in their worktree and call ``report_result``,
reviewers are records that call ``submit_review``). No network."""

import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, autopilot, mcp_server, pipeline, rewind, workspaces
from brindle.db import Agent


def run(coro):
    return asyncio.run(coro)


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


def configure(repo, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status != "rewound")
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    monkeypatch.setattr(pipeline, "_detach", lambda argv: None)


@pytest.fixture
def boss(db, repo, monkeypatch):
    """The supervisor: an agent on the main checkout that every tool call runs as."""
    configure(repo, review=True, auto_merge_default_branch=True, overlap="warn")
    root = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", root.id, "supervisor", "claude", None, "interactive",
                       "processing", "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return root


@pytest.fixture
def reviewers(db, monkeypatch):
    """Reviews start without a process; the list holds the reviewers started."""
    started: list[Agent] = []

    def fake_review(db_, caller, ws, profile=None, focus=None, cfg=None):
        a = Agent(f"rev{len(started)}", ws.id, "reviewer", "claude", caller.id, "review",
                  "processing", "@r", None, time.time())
        db_.add_agent(a)
        started.append(a)
        return a

    monkeypatch.setattr(agents, "request_review", fake_review)
    return started


def as_agent(monkeypatch, agent_id):
    monkeypatch.setenv("BRINDLE_AGENT_ID", agent_id)


def started_id(reply: str) -> str:
    m = re.search(r"Started worker (\S+)", reply)
    assert m, reply
    return m.group(1)


def ws_of(db, repo, branch):
    return next(w for w in db.find_workspaces(str(repo)) if w.branch == branch)


def commit(ws, name, text="x\n"):
    (Path(ws.path) / name).write_text(text)
    sh(f"git add -A && git commit -qm {name}", Path(ws.path))


def worker_reports(db, monkeypatch, worker_id, result="done"):
    """What a worker does last: call report_result as itself."""
    as_agent(monkeypatch, worker_id)
    try:
        return mcp_server.report_result(result)
    finally:
        as_agent(monkeypatch, "boss")


def reviewer_approves(monkeypatch, reviewer_id, approved=True, summary="lgtm"):
    as_agent(monkeypatch, reviewer_id)
    try:
        return mcp_server.submit_review(approved, summary)
    finally:
        as_agent(monkeypatch, "boss")


def main_files(repo):
    return sh("git ls-tree --name-only HEAD", repo).splitlines()


# -- assign / handoff / report_result --------------------------------------------------


def test_assign_isolate_gives_the_worker_its_own_worktree_and_branch(db, repo, boss):
    out = run(mcp_server.assign("developer", "add a.py", branch="feat-a"))
    wid = started_id(out)
    ws = ws_of(db, repo, "feat-a")
    assert ws.kind == "worktree" and ws.path != str(repo) and Path(ws.path).is_dir()
    assert ws.base_branch == "main"
    assert db.get_agent(wid).workspace_id == ws.id
    assert db.get_agent(wid).parent_id == "boss"
    assert "feat-a" in mcp_server.list_agents() and wid in mcp_server.list_agents()


def test_assign_without_isolate_shares_the_checkout(db, repo, boss):
    wid = started_id(run(mcp_server.assign("developer", "look around", isolate=False)))
    assert db.get_workspace(db.get_agent(wid).workspace_id).path == str(repo)


def test_blank_task_is_refused(db, repo, boss):
    assert run(mcp_server.assign("developer", "  ")) == "Give the worker a task."
    assert run(mcp_server.handoff("developer", "")) == "Give the worker a task."


def test_report_result_with_the_pipeline_off_goes_to_the_supervisor(db, repo, boss, monkeypatch):
    configure(repo, pipeline=False)
    wid = started_id(run(mcp_server.assign("developer", "add a.py", branch="feat-a")))
    commit(ws_of(db, repo, "feat-a"), "a.py")
    out = worker_reports(db, monkeypatch, wid, "added a.py")
    assert "sent to your supervisor" in out
    assert db.get_agent(wid).result == "added a.py"
    msg = db.pop_pending("boss")
    assert msg and "added a.py" in msg.body and "feat-a" in msg.body


def test_handoff_returns_the_result_once_the_worker_reports(db, repo, boss, monkeypatch):
    configure(repo, pipeline=False)
    monkeypatch.setattr(agents, "wait_for_result", lambda db_, wid, secs: db_.get_agent(wid).result)
    orig = agents.spawn

    def spawn_and_report(db_, ws, profile, **kw):
        a = orig(db_, ws, profile, **kw)
        commit(ws, "h.py")
        db_.set_result(a.id, "wrote h.py")
        return a

    monkeypatch.setattr(agents, "spawn", spawn_and_report)
    out = run(mcp_server.handoff("developer", "write h.py", branch="feat-h", wait_seconds=5))
    assert "finished" in out and "wrote h.py" in out and "1 commit(s) ahead of main" in out
    assert "h.py" in out


def test_handoff_that_is_still_running_says_so(db, repo, boss):
    out = run(mcp_server.handoff("developer", "slow", branch="feat-s", wait_seconds=0))
    assert "still running" in out and "wait_for_worker" in out


# -- the pipeline: review -> checks -> merge -> removal -----------------------------------


def test_pipeline_review_checks_merge_and_removal(db, repo, boss, reviewers, monkeypatch):
    configure(repo, review=True, auto_merge_default_branch=True, checks=["test -f new.py"])
    wid = started_id(run(mcp_server.assign("developer", "add new.py", branch="feat-p")))
    ws = ws_of(db, repo, "feat-p")
    commit(ws, "new.py")

    out = worker_reports(db, monkeypatch, wid, "added new.py")
    assert "having your branch reviewed" in out
    assert db.get_agent(wid).pipeline == "reviewing" and len(reviewers) == 1
    assert db.pending_count("boss") == 0                      # nothing to bother the supervisor with

    out = reviewer_approves(monkeypatch, reviewers[0].id)
    assert "takes it from here" in out
    assert "new.py" in main_files(repo)                         # merged
    assert db.get_workspace(ws.id) is None and not Path(ws.path).exists()   # worktree removed
    assert db.get_agent(wid) is None
    assert "feat-p" not in sh("git branch --list", repo)        # merged branch deleted
    msg = db.pop_pending("boss")
    assert msg and "Merged feat-p into main" in msg.body and "added new.py" in msg.body


def test_failed_checks_leave_the_branch_unmerged_and_need_the_supervisor(db, repo, boss, reviewers,
                                                                          monkeypatch):
    configure(repo, review=True, auto_merge_default_branch=True, checks=["test -f never_there.py"])
    wid = started_id(run(mcp_server.assign("developer", "add new.py", branch="feat-f")))
    ws = ws_of(db, repo, "feat-f")
    commit(ws, "new.py")
    worker_reports(db, monkeypatch, wid, "added new.py")

    reviewer_approves(monkeypatch, reviewers[0].id)

    assert "new.py" not in main_files(repo)
    assert db.get_workspace(ws.id) is not None and Path(ws.path).is_dir()
    assert db.get_agent(wid).pipeline is None
    bodies = []
    while (m := db.pop_pending("boss")) is not None:
        bodies.append(m.body)
    text = "\n".join(bodies)
    assert "couldn't be merged" in text and "This needs you" in text and "feat-f" in text


def test_changes_requested_are_sent_back_to_the_worker(db, repo, boss, reviewers, monkeypatch):
    wid = started_id(run(mcp_server.assign("developer", "add new.py", branch="feat-r")))
    ws = ws_of(db, repo, "feat-r")
    commit(ws, "new.py")
    worker_reports(db, monkeypatch, wid)
    reviewer_approves(monkeypatch, reviewers[0].id, approved=False, summary="add a test")
    assert db.get_agent(wid).pipeline == "fixing"
    assert "add a test" in db.pop_pending(wid).body
    assert "new.py" not in main_files(repo)
    # The fix is reported, reviewed again and merged.
    commit(ws, "test_new.py")
    worker_reports(db, monkeypatch, wid, "added the test")
    assert len(reviewers) == 2
    reviewer_approves(monkeypatch, reviewers[1].id)
    assert {"new.py", "test_new.py"} <= set(main_files(repo))


def test_merge_workspace_by_hand_runs_the_review_gate(db, repo, boss, reviewers, monkeypatch):
    configure(repo, review=True, pipeline=False)
    wid = started_id(run(mcp_server.assign("developer", "add new.py", branch="feat-m")))
    ws = ws_of(db, repo, "feat-m")
    commit(ws, "new.py")
    worker_reports(db, monkeypatch, wid)
    assert "Not merged" in run(mcp_server.merge_workspace(ws.id))     # no approval yet
    assert "new.py" not in main_files(repo)
    run(mcp_server.request_review(ws.id))
    reviewer_approves(monkeypatch, reviewers[0].id)
    out = run(mcp_server.merge_workspace(ws.id))
    assert out.startswith("Merged feat-m into main")
    assert "new.py" in main_files(repo)
    assert "Removed" in mcp_server.remove_workspace(ws.id)
    assert db.get_workspace(ws.id) is None


# -- depends_on: queue, then auto-start after the merge ------------------------------------


def test_depends_on_queues_then_starts_from_the_merged_base(db, repo, boss, reviewers, monkeypatch):
    configure(repo, review=True, auto_merge_default_branch=True, overlap="warn")
    a = started_id(run(mcp_server.assign("developer", "do A", branch="feat-a")))
    ws_a = ws_of(db, repo, "feat-a")
    commit(ws_a, "a_out.txt", "from A\n")

    out = run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[a]))
    assert out.startswith("Queued task") and a in out
    assert "feat-b" in mcp_server.list_tasks()
    assert not any(w.branch == "feat-b" for w in db.find_workspaces(str(repo)))

    worker_reports(db, monkeypatch, a)
    reviewer_approves(monkeypatch, reviewers[0].id)            # pipeline merges A

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "started" and b_task.agent_id
    ws_b = db.get_workspace(db.get_agent(b_task.agent_id).workspace_id)
    assert (Path(ws_b.path) / "a_out.txt").exists()            # cut from main after A merged
    bodies = []
    while (m := db.pop_pending("boss")) is not None:
        bodies.append(m.body)
    assert any("Started" in b and b_task.agent_id in b for b in bodies)


# -- set_goal / check_milestone / get_progress --------------------------------------------


def test_goal_tools_need_autopilot(db, repo, boss):
    assert "Autopilot isn't on" in mcp_server.set_goal("g", [{"title": "m", "check": "true"}])
    assert "Autopilot isn't on" in mcp_server.get_progress()
    assert "Autopilot isn't on" in run(mcp_server.check_milestone())


def test_set_goal_get_progress_and_check_milestone(db, repo, boss, monkeypatch):
    db.add_autopilot("boss")
    out = mcp_server.set_goal("ship it", [
        {"title": "has a.py", "check": "test -f a.py"},
        {"title": "has b.py", "check": "test -f b.py"},
    ])
    assert "ship it" in out and "has a.py" in out and "has b.py" in out
    assert [m.title for m in db.milestones("boss")] == ["has a.py", "has b.py"]
    assert "passed" not in {m.status for m in db.milestones("boss")}
    assert "has a.py" in mcp_server.get_progress()

    # The tool starts the checks detached (the test's _detach is a no-op) and returns at once.
    monkeypatch.setattr(autopilot, "_detach", lambda argv: None)
    assert run(mcp_server.check_milestone()).startswith("Checking")
    assert db.get_autopilot("boss").checking_since

    # What the detached `_check-milestones` runs, in the checkout where merges land.
    (repo / "a.py").write_text("")
    sh("git add -A && git commit -qm a", repo)
    root = db.get_workspace(db.get_agent("boss").workspace_id)
    text = autopilot.check_milestones(db, "boss", root, None)
    assert "has a.py" in text
    by_title = {m.title: m for m in db.milestones("boss")}
    assert by_title["has a.py"].status == "passed"
    assert by_title["has b.py"].status != "passed"
    progress = mcp_server.get_progress()
    assert "has a.py" in progress and "has b.py" in progress

    # A milestone that keeps its title and check keeps its result when the goal is re-set.
    mcp_server.set_goal("ship it", [{"title": "has a.py", "check": "test -f a.py"},
                                    {"title": "has c.py", "check": "test -f c.py"}])
    assert {m.title: m.status for m in db.milestones("boss")}["has a.py"] == "passed"


def test_set_goal_rejects_a_milestone_without_a_title(db, repo, boss):
    db.add_autopilot("boss")
    assert mcp_server.set_goal("g", [{"title": " ", "check": "true"}]) == "Every milestone needs a title."


# -- rewind ----------------------------------------------------------------------------------


def turn(db, wid, ws, name, text):
    (Path(ws.path) / name).write_text(text)
    agents.handle_hook(db, wid, "stop", {"stop_hook_active": True})


def test_rewind_agent_restores_the_turn_and_starts_a_fresh_worker(db, repo, boss):
    wid = started_id(run(mcp_server.assign("developer", "build the thing", branch="feat-w")))
    ws = ws_of(db, repo, "feat-w")
    turn(db, wid, ws, "one.txt", "1\n")
    turn(db, wid, ws, "two.txt", "2\n")
    sh("git add -A && git commit -qm wip", ws.path)
    turn(db, wid, ws, "three.txt", "3\n")
    assert [n for n, _ in rewind.turns(str(repo), wid)] == [1, 2, 3]
    assert "turn   1" in mcp_server.agent_turns(wid) and "one.txt" in mcp_server.agent_turns(wid)

    out = run(mcp_server.rewind_agent(wid, 1, note="skip two.txt"))
    assert out.startswith(f"Rewound {wid} to turn 1.")

    assert (Path(ws.path) / "one.txt").exists()
    assert not (Path(ws.path) / "two.txt").exists() and not (Path(ws.path) / "three.txt").exists()
    assert sh("git log --oneline main..HEAD", ws.path) == ""            # the commit is undone
    assert db.get_agent(wid).status == "rewound"
    [fresh] = [a for a in db.list_agents(ws.id) if a.id != wid]
    assert fresh.parent_id == "boss" and fresh.task == "build the thing"
    assert f"{fresh.id} " in out
    # The fresh worker can be rewound again, to a turn it inherited.
    assert [n for n, _ in rewind.turns(str(repo), fresh.id)] == [1]
    assert "has no turn 2" in run(mcp_server.rewind_agent(fresh.id, 2))


def test_rewind_agent_refuses_what_it_cannot_do(db, repo, boss, monkeypatch):
    assert run(mcp_server.rewind_agent("nope", 1)) == "Not rewound: no agent nope."
    wid = started_id(run(mcp_server.assign("developer", "t", branch="feat-x")))
    assert "no turn snapshots" in run(mcp_server.rewind_agent(wid, 1))
    ws = ws_of(db, repo, "feat-x")
    turn(db, wid, ws, "a.txt", "a\n")
    # Someone else's worker is not for any other agent to rewind.
    db.add_agent(Agent("other", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@9", None, time.time()))
    as_agent(monkeypatch, "other")
    assert "isn't yours to rewind" in run(mcp_server.rewind_agent(wid, 1))
    as_agent(monkeypatch, "boss")
    assert "has no turn 7" in run(mcp_server.rewind_agent(wid, 7))
    assert (Path(ws.path) / "a.txt").exists()


# -- conflict-aware merging -----------------------------------------------------------------------


def two_branches_on_one_file(db, repo, monkeypatch, protected=None, **extra):
    cfg = {"review": True, "auto_merge_default_branch": True, "overlap": "warn", **extra}
    if protected:
        cfg["protected_paths"] = protected
    configure(repo, **cfg)
    (repo / "shared.txt").write_text("one\ntwo\nthree\n")
    sh("git add -A && git commit -qm shared", repo)
    sh("git push -q origin main", repo)
    ids = {}
    for branch, line in (("feat-1", "ONE\ntwo\nthree\n"), ("feat-2", "uno\ntwo\nthree\n")):
        ids[branch] = started_id(run(mcp_server.assign("developer", f"edit shared on {branch}",
                                                        branch=branch)))
        commit(ws_of(db, repo, branch), "shared.txt", line)
    return ids


def test_two_branches_touching_one_file_conflict_and_the_second_goes_to_a_resolver(
        db, repo, boss, reviewers, monkeypatch):
    ids = two_branches_on_one_file(db, repo, monkeypatch)
    ws1, ws2 = ws_of(db, repo, "feat-1"), ws_of(db, repo, "feat-2")
    from brindle import conflicts
    assert conflicts.shared_files(conflicts.changed_files(ws1), conflicts.changed_files(ws2)) == ["shared.txt"]
    assert conflicts.overlapping_hunks(conflicts.hunks(ws1), conflicts.hunks(ws2)) == 1

    worker_reports(db, monkeypatch, ids["feat-1"], "edited shared")
    worker_reports(db, monkeypatch, ids["feat-2"], "edited shared too")
    reviewer_approves(monkeypatch, reviewers[0].id)
    assert sh("git show HEAD:shared.txt", repo) == "ONE\ntwo\nthree"
    assert db.get_workspace(ws1.id) is None

    reviewer_approves(monkeypatch, reviewers[1].id)
    # feat-2 conflicts with main now: not merged, its (still running) worker is asked to resolve.
    assert sh("git show HEAD:shared.txt", repo) == "ONE\ntwo\nthree"
    assert db.get_workspace(ws2.id) is not None
    assert db.get_agent(ids["feat-2"]).pipeline == "resolving"
    assert sh("git status --porcelain", ws2.path) == ""                  # the sync was aborted cleanly
    task = db.pop_pending(ids["feat-2"])
    assert task and "Merge conflict" in task.body and "shared.txt" in task.body


def test_manual_merge_of_a_conflicting_branch_names_the_files(db, repo, boss, monkeypatch):
    ids = two_branches_on_one_file(db, repo, monkeypatch, pipeline=False, review=False)
    ws1, ws2 = ws_of(db, repo, "feat-1"), ws_of(db, repo, "feat-2")
    assert run(mcp_server.merge_workspace(ws1.id)).startswith("Merged")
    # A branch behind its base isn't synced under a worker that's still at work on it.
    assert "is still working on feat-2" in run(mcp_server.merge_workspace(ws2.id))
    worker_reports(db, monkeypatch, ids["feat-2"])
    out = run(mcp_server.merge_workspace(ws2.id))
    assert out.startswith("Not merged") and "conflicts with main in: shared.txt" in out
    assert sh("git status --porcelain", ws2.path) == ""
    assert sh("git show HEAD:shared.txt", repo) == "ONE\ntwo\nthree"


def test_a_conflict_in_a_protected_path_goes_to_the_person(db, repo, boss, reviewers, monkeypatch):
    ids = two_branches_on_one_file(db, repo, monkeypatch, protected=["shared.txt"])
    ws2 = ws_of(db, repo, "feat-2")
    worker_reports(db, monkeypatch, ids["feat-1"])
    worker_reports(db, monkeypatch, ids["feat-2"])
    reviewer_approves(monkeypatch, reviewers[0].id)
    reviewer_approves(monkeypatch, reviewers[1].id)
    assert db.get_agent(ids["feat-2"]).pipeline is None
    assert db.pending_count(ids["feat-2"]) == 0
    bodies = []
    while (m := db.pop_pending("boss")) is not None:
        bodies.append(m.body)
    text = "\n".join(bodies)
    assert "protected path" in text and "This needs you" in text and "shared.txt" in text
    assert db.get_workspace(ws2.id) is not None


def test_ready_branches_merge_the_least_conflicting_first(db, repo, boss, monkeypatch):
    configure(repo, overlap="warn")
    (repo / "shared.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
    sh("git add -A && git commit -qm shared", repo)
    for branch, name, text in (("clash-a", "shared.txt", "ONE\ntwo\nthree\nfour\nfive\nsix\n"),
                               ("clean", "own.txt", "mine\n"),
                               ("clash-b", "shared.txt", "uno\ntwo\nthree\nfour\nfive\nsix\n")):
        run(mcp_server.assign("developer", branch, branch=branch))
        commit(ws_of(db, repo, branch), name, text)
    ready = [(None, ws_of(db, repo, b)) for b in ("clash-a", "clean", "clash-b")]
    order = pipeline.merge_order(ready)
    assert [ws.branch for _w, ws in order][0] == "clean"
