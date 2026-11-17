"""Merges into one base branch run one at a time (#197), an approved
branch whose merge checks fail is retried once before the supervisor is told,
with the whole failure (#186), every reviewer's standing verdict decides a
merge (#216), and an approval leads to a merge or a clear message (#217)."""
import json
import threading
from contextlib import contextmanager

import pytest

from conftest import full_text, sh
from brindle import agents, gates, pipeline, workspaces
from brindle.db import DB
from test_merge_order import _repo_template, add, quiet_removal, repo, worktree  # noqa: F401 - fixture
from test_pipeline_idempotent import drain, keep_worktree


@pytest.fixture
def approved(db, repo, monkeypatch):
    """test_pipeline_idempotent's ``approved`` (a piped branch both rev0 and
    rev1 approved, and a count of gate runs), on a worktree made in one git run."""
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"review": True, "auto_merge_default_branch": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")  # busy: messages queue
    ws = worktree(db, repo, "feat", files={"new.py": "x = 1\n"})
    add(db, ws, "w1", "assign", parent="boss", status="processing")
    db.update_agent("w1", result="added new.py", pipeline="reviewing")
    for rid in ("rev0", "rev1"):
        add(db, ws, rid, "review", "reviewer", parent="boss", status="processing")
        db.add_review(ws.id, gates.head(ws), rid, True, "lgtm")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    quiet_removal(monkeypatch)
    runs = []
    real_run = gates.run
    monkeypatch.setattr(gates, "run", lambda *a, **k: (runs.append(1), real_run(*a, **k))[1])
    return ws, runs


@pytest.mark.variant("race from a live incident (#197): two merges into one base; the only test of the ordering")
def test_a_merge_waits_for_another_merge_into_the_same_base(db, repo, monkeypatch):
    """#197: the supervisor's merge_workspace of one branch ran its gates while
    the pipeline merged another into the same base, and git then failed with
    "MERGE_HEAD exists". The second merge waits for the first, sync to merge."""
    workspaces.adopt_root(db, str(repo))
    quiet_removal(monkeypatch)
    spaces = [worktree(db, repo, name, files={f"{name}.py": "x = 1\n"}) for name in ("one", "two")]
    in_gates, release = threading.Event(), threading.Event()
    events = []
    real_run, real_lock = gates.run, pipeline.base_lock

    def slow_first(db_, ws_, *a, **k):
        events.append(f"{ws_.branch} gates")
        if ws_.id == spaces[0].id:
            in_gates.set()
            assert release.wait(10)
        return real_run(db_, ws_, *a, **k)

    @contextmanager
    def watched(repo_root, base, wait=True):
        me = threading.current_thread().name
        if base != "main":
            with real_lock(repo_root, base, wait):
                yield
            return
        if me == "two":   # the first merge is in its gates: main must be held
            try:
                with real_lock(repo_root, base, wait=False):
                    events.append("two found main free")
            except BlockingIOError:
                events.append("two waits")
            release.set()
        with real_lock(repo_root, base, wait):
            try:
                yield
            finally:
                events.append(f"{me} done")   # still holding main

    monkeypatch.setattr(gates, "run", slow_first)
    monkeypatch.setattr(pipeline, "base_lock", watched)
    out = {}

    def merge(ws):
        out[ws.branch] = pipeline.merge(DB(), None, ws)   # each in its own process, in production

    first = threading.Thread(target=merge, args=(spaces[0],), name="one")
    second = threading.Thread(target=merge, args=(spaces[1],), name="two")
    first.start()
    try:
        assert in_gates.wait(10)
        second.start()
    finally:
        release.wait(10)
        release.set()
        first.join(20)
        second.join(20)
    # Waiting for the merge into main that's under way, before its own sync.
    assert events == ["one gates", "two waits", "one done", "two gates", "two done"]
    assert out["one"].startswith("Merged one") and out["two"].startswith("Merged two")
    assert {"one.py", "two.py"} <= set(sh("git ls-tree --name-only HEAD", repo).split())


@pytest.mark.parametrize("case", ["rejected while the checks ran"])
def test_issue216_each_reviewers_standing_verdict_decides_the_merge(db, approved, monkeypatch, repo, case):
    """rev0 and rev1 approved; then rev1 asks for changes on the same commit.
    (Which later review replaces a rejection: test_escapes, through real dispatch.)"""
    ws, _runs = approved
    sha = gates.head(ws)
    if case == "rejected while the checks ran":
        (repo / ".brindle" / "config.json").write_text(json.dumps({"review": True, "checks": ["true"]}))
        real = gates.run_checked
        monkeypatch.setattr(gates, "run_checked", lambda *a, **k: (
            db.add_review(ws.id, sha, "rev1", False, "scope sign-off missing"), real(*a, **k))[1])
    else:
        db.add_review(ws.id, sha, "rev1", False, "scope sign-off missing")
        db.add_review(ws.id, sha, "rev0", True, "lgtm")
    out = pipeline.merge(db, None, ws)
    assert out.startswith("Not merged") and "new.py" not in sh("git ls-tree --name-only HEAD", repo), out


@pytest.mark.parametrize("state", ["done", "still running", "pipeline off"])
def test_issue217_approval_of_a_never_reported_branch_merges_or_says_so(db, approved, monkeypatch, repo, state):
    """The worker had no brindle connection, so the supervisor requested the review."""
    ws, _runs = approved
    keep_worktree(monkeypatch)
    db.update_agent("w1", result=None, pipeline=None)
    if state != "still running":
        db.set_status("w1", "done")
    if state == "pipeline off":
        (repo / ".brindle" / "config.json").write_text(json.dumps({"review": True, "pipeline": False}))
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    bodies = drain(db)
    assert ("new.py" in sh("git ls-tree --name-only HEAD", repo)) is (state == "done")
    if state == "done":
        assert any("Merged feat" in b for b in bodies)
    else:
        assert any(f'This needs you: merge it with merge_workspace("{ws.id}")' in b for b in bodies)


@pytest.mark.parametrize("fails", [2])
def test_an_approved_branch_whose_checks_fail_is_retried_once(db, approved, monkeypatch, repo, fails):
    ws, _runs = approved
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"review": True, "auto_merge_default_branch": True, "checks": ["true"]}))
    monkeypatch.setattr(workspaces, "remove", lambda *a, **k: (_ for _ in ()).throw(
        workspaces.WorkspaceError("kept")))
    runs = []
    real_run = gates.run

    def flaky(*a, **k):
        runs.append(1)
        if len(runs) <= fails:
            return gates.Report().fail("Check failed in feat:\n1 failed\nFAILED tests/test_x.py::t")
        return real_run(*a, **k)

    monkeypatch.setattr(gates, "run", flaky)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    bodies = []
    while (msg := db.pop_pending("boss")):
        bodies.append(msg.body)
    assert len(runs) == 2
    # #217: told once that the merge waits on the checks, so it never looks stuck.
    assert sum("merge is waiting on the repo's checks" in b for b in bodies) == 1
    if fails == 1:
        assert "new.py" in sh("git ls-tree --name-only HEAD", repo)
        assert any("Merged feat" in b for b in bodies)
    else:
        [body] = [b for b in bodies if "couldn't be merged" in b]
        assert f'merge_workspace("{ws.id}")' in body   # in the one line the supervisor sees
        assert "FAILED tests/test_x.py::t" in full_text(db, "boss", body)
