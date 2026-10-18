"""#186: an untracked scratch file doesn't stop a committed branch from being reviewed."""
from pathlib import Path

import pytest

from brindle import agents, gates, pipeline, workspaces
from brindle.config import RepoConfig
from test_merge_order import add


@pytest.mark.parametrize("tracked_edit, reviewed", [
    (False, True),
    (True, False),
])
def test_untracked_scratch_file_does_not_block_review(db, repo, monkeypatch, tracked_edit, reviewed):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", parent="boss", result="done")
    (Path(ws.path) / "scratch.txt").write_text("notes\n")
    if tracked_edit:
        (Path(ws.path) / "app.py").write_text("edited, not committed\n")
    started = []
    monkeypatch.setattr(pipeline, "load_repo_config", lambda root_: RepoConfig(pipeline=True))
    monkeypatch.setattr(pipeline, "second_profile", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "default_review_profile", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "start_review", lambda *a, **kw: started.append(1))

    assert pipeline.on_report(db, db.get_agent("w1"), ws, "done")
    assert bool(started) is reviewed
    assert gates.run(db, ws, RepoConfig(), review_required=False).ok is reviewed
