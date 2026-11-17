import pytest

from brindle import gates, workspaces
from brindle.db import Agent


@pytest.mark.parametrize("slot, legacy, approves", [("", True, True)])
def test_same_slot_approval_supersedes_after_reviewers_are_deleted(db, repo, monkeypatch,
                                                                slot, legacy, approves):
    ws = workspaces.adopt_root(db, str(repo))
    sha = gates.head(ws)
    for rid, started, recorded, approved in (("old", 1, 2, False), ("new", 3, 4, True)):
        db.add_agent(Agent(
            id=rid, workspace_id=ws.id, profile="reviewer", provider="codex",
            parent_id=None, mode="review", status="done", tmux_window="", result=None, created_at=started,
        ))
        db.update_agent(rid, review_slot="" if rid == "old" else slot)
        monkeypatch.setattr("brindle.db.time.time", lambda: recorded)
        db.add_review(ws.id, sha, rid, approved, "")
    if legacy:
        db.conn.execute("ALTER TABLE reviews DROP COLUMN review_slot")
        db.conn.execute("ALTER TABLE reviews DROP COLUMN reviewer_created_at")
        db._migrate()
    for rid in ("old", "new"):
        db.delete_agent(rid)
    review = gates.verdict(db, ws.id, sha)
    assert bool(review.approved) is approves
