"""Merges into one base branch run one at a time (#197), an approved
branch whose merge checks fail is retried once before the supervisor is told,
with the whole failure (#186), every reviewer's standing verdict decides a
merge (#216), and an approval leads to a merge or a clear message (#217)."""
import json
import threading
from pathlib import Path

import pytest

from conftest import full_text, sh
from brindle import gates, pipeline, workspaces
from brindle.db import DB
from test_merge_order import add
from test_pipeline_idempotent import approved, drain, keep_worktree  # noqa: F401 - fixture


@pytest.mark.variant("race from a live incident (#197): two merges into one base; the only test of the ordering")
def test_a_merge_waits_for_another_merge_into_the_same_base(db, repo, monkeypatch):
    """#197: the supervisor's merge_workspace of one branch ran its gates while
    the pipeline merged another into the same base, and git then failed with
    "MERGE_HEAD exists". The second merge waits for the first, sync to merge."""
    workspaces.adopt_root(db, str(repo))
    spaces = []
    for name in ("one", "two"):
        ws = workspaces.create(db, str(repo), name).workspace
        (Path(ws.path) / f"{name}.py").write_text("x = 1\n")
        sh(f"git add {name}.py && git commit -qm {name}", Path(ws.path))
        spaces.append(ws)
    in_gates, second_in_gates, release = threading.Event(), threading.Event(), threading.Event()
    real_run = gates.run

    def slow_first(db_, ws_, *a, **k):
        if ws_.id == spaces[0].id:
            in_gates.set()
            assert release.wait(10)
        else:
            second_in_gates.set()
        return real_run(db_, ws_, *a, **k)

    monkeypatch.setattr(gates, "run", slow_first)
    out = {}

    def merge(ws):
        out[ws.branch] = pipeline.merge(DB(), None, ws)   # each in its own process, in production

    first = threading.Thread(target=merge, args=(spaces[0],))
    first.start()
    try:
        assert in_gates.wait(10)
        second = threading.Thread(target=merge, args=(spaces[1],))
        second.start()
        # Waiting for the merge into main that's under way, before its own sync.
        assert not second_in_gates.wait(1)
    finally:
        release.set()
        first.join(20)
        second.join(20)
    assert out["one"].startswith("Merged one") and out["two"].startswith("Merged two")
    assert {"one.py", "two.py"} <= set(sh("git ls-tree --name-only HEAD", repo).split())


@pytest.mark.parametrize("case", ["rejected while the checks ran", "rejected, then another approved",
                                  "rejected, then superseded"])
def test_issue216_each_reviewers_standing_verdict_decides_the_merge(db, approved, monkeypatch, repo, case):
    """rev0 and rev1 approved; then rev1 asks for changes on the same commit."""
    ws, _runs = approved
    sha = gates.head(ws)
    if case == "rejected while the checks ran":
        (repo / ".brindle" / "config.json").write_text(json.dumps({"review": True, "checks": ["true"]}))
        real = gates.run_checked
        monkeypatch.setattr(gates, "run_checked", lambda *a, **k: (
            db.add_review(ws.id, sha, "rev1", False, "scope sign-off missing"), real(*a, **k))[1])
    else:
        db.add_review(ws.id, sha, "rev1", False, "scope sign-off missing")
        if case == "rejected, then superseded":   # a review requested again after it
            add(db, ws, "rev2", "review", "reviewer", parent="boss", status="processing")
        db.add_review(ws.id, sha, "rev2" if "superseded" in case else "rev0", True, "lgtm")
    out = pipeline.merge(db, None, ws)
    merged = "new.py" in sh("git ls-tree --name-only HEAD", repo)
    assert merged == (case == "rejected, then superseded"), out
    assert out.startswith("Merged") == merged


@pytest.mark.parametrize("still_running", [False, True])
def test_issue217_approval_of_a_never_reported_branch_merges_or_says_so(db, approved, monkeypatch, repo,
                                                                          still_running):
    """The worker had no brindle connection, so the supervisor requested the review."""
    ws, _runs = approved
    keep_worktree(monkeypatch)
    db.update_agent("w1", result=None, pipeline=None)
    if not still_running:
        db.set_status("w1", "done")
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    bodies = drain(db)
    assert ("new.py" in sh("git ls-tree --name-only HEAD", repo)) is not still_running
    if still_running:
        assert any(f'This needs you: merge it with merge_workspace("{ws.id}")' in b for b in bodies)
    else:
        assert any("Merged feat" in b for b in bodies)


@pytest.mark.parametrize("fails", [1, 2])
def test_an_approved_branch_whose_checks_fail_is_retried_once(db, approved, monkeypatch, repo, fails):
    ws, _runs = approved
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"review": True, "auto_merge_default_branch": True, "checks": ["true"]}))
    db.set_check_duration(ws.repo_root, "true", 600.0)   # a suite that takes minutes
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
