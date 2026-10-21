"""#186: an untracked scratch file doesn't stop a committed branch from being reviewed.
#226: a report with nothing to review (no commits, or a review profile) goes straight to the supervisor."""
from pathlib import Path

import pytest

from brindle import agents, gates, pipeline, workspaces
from brindle.config import RepoConfig
from conftest import sh
from test_merge_order import add

REAL_PULLS = agents.pulls_messages   # conftest patches it off for the other tests


@pytest.mark.parametrize("profile, committed, tracked_edit, reviewed", [
    ("developer", True, False, True),
    ("developer", True, True, False),
    ("developer", False, False, False),   # no commits: nothing to review
    ("reviewer-gemini", True, False, False),
])
def test_untracked_scratch_file_does_not_block_review(db, repo, monkeypatch, profile, committed,
                                                      tracked_edit, reviewed):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add(db, ws, "w1", "assign", profile, parent="boss")
    if committed:
        (Path(ws.path) / "new.py").write_text("x = 1\n")
        sh("git add new.py && git commit -qm work", Path(ws.path))
    (Path(ws.path) / "scratch.txt").write_text("notes\n")
    if tracked_edit:
        (Path(ws.path) / "new.py").write_text("edited, not committed\n")
    started = []
    monkeypatch.setattr(pipeline, "load_repo_config", lambda root_: RepoConfig(pipeline=True))
    monkeypatch.setattr(pipeline, "second_profile", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "default_review_profile", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "start_review", lambda *a, **kw: started.append(1))
    # Only the external processes are faked: the supervisor's window, its notice, the detached checks.
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: True)
    monkeypatch.setattr(agents, "pulls_messages", REAL_PULLS)
    monkeypatch.setattr(agents, "_deliver_to_inbox", lambda *a, **kw: True)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)

    agents.report_result(db, "w1", "all findings")
    assert bool(started) is reviewed
    mail = [m.body for m in db.unread("boss")]   # the report reaches the supervisor...
    assert bool(mail) is (not reviewed)          # ...unless the pipeline took it
    assert not tracked_edit or "new.py" in mail[0]
    if profile == "developer" and committed and not tracked_edit:
        assert gates.run(db, ws, RepoConfig(), review_required=False).ok
