import pytest

from brindle import gates, git, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent


def test_check_summary_fail_fast(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    cfg = RepoConfig(checks=["cheap", "expensive"])
    calls = []

    def run(db, ws, cmd, *args):
        calls.append(cmd)
        return cmd != "cheap", "failed" if cmd == "cheap" else "ok"

    monkeypatch.setattr(gates, "_run_queued", run)
    summary = gates.check_summary(db, ws, cfg)
    assert calls == ["cheap"]
    assert gates.summary_failed(summary)
    assert "`expensive` (skipped: earlier check failed)" in summary
    assert db.get_check(ws.id, gates.head(ws), "expensive") is None
    tree = git.out(["rev-parse", "HEAD^{tree}"], ws.path)
    assert db.get_tree_check(ws.repo_root, tree, "expensive") is None
    assert gates.run_checked(db, ws, "expensive", {}, cfg.check_timeout)[0]
    assert calls == ["cheap", "expensive"]


@pytest.mark.parametrize("slot, legacy, approves", [("", False, True), (None, False, False),
                                                  ("second", False, False), ("", True, True)])
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
