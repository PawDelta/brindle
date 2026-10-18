"""Merges into one base branch run one at a time (#197), and an approved
branch whose merge checks fail is retried once before the supervisor is told,
with the whole failure (#186)."""
import threading
from pathlib import Path

import pytest

from conftest import full_text, sh
from brindle import gates, pipeline, workspaces
from brindle.db import DB
from test_pipeline_idempotent import approved  # noqa: F401 - fixture


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


@pytest.mark.parametrize("fails", [1, 2])
def test_an_approved_branch_whose_checks_fail_is_retried_once(db, approved, monkeypatch, repo, fails):
    ws, _runs = approved
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
    if fails == 1:
        assert "new.py" in sh("git ls-tree --name-only HEAD", repo)
        assert any("Merged feat" in b for b in bodies)
    else:
        [body] = [b for b in bodies if "couldn't be merged" in b]
        assert f'merge_workspace("{ws.id}")' in body   # in the one line the supervisor sees
        assert "FAILED tests/test_x.py::t" in full_text(db, "boss", body)
