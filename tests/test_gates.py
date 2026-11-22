import json
from pathlib import Path

import pytest

from conftest import sh
from brindle import autopilot, codemap, gates, workspaces
from brindle.db import Agent


def test_pre_pr_on_a_tree_that_passed_in_full_runs_nothing_again(db, repo, monkeypatch):
    """The scoped run before a PR reuses the full pass the same tree already
    has (here from a gate run), rather than running its narrower command."""
    cmd = "uv run pytest -q"
    for name, text in {".brindle/config.json": json.dumps({"checks": [cmd], "scoped_checks": "infer"}),
                       "src/pkg/__init__.py": "", "src/pkg/mod.py": "X = 1\n",
                       "tests/test_mod.py": "from pkg import mod\n"}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base && git push -q origin main", repo)
    ws = workspaces.create(db, str(repo), "sess").workspace
    (Path(ws.path) / "src/pkg/mod.py").write_text("X = 2\n")
    sh("git commit -qam work", Path(ws.path))
    monkeypatch.setattr(codemap, "graph_path", lambda target: None)
    commands = []
    monkeypatch.setattr(autopilot, "run_check", lambda c, cwd, env, timeout: (commands.append(c), (True, ""))[1])

    assert gates.run_checked(db, ws, cmd, {}, 30, full=True)[0]
    for _ in range(2):
        with gates.pr_checked(db, ws):
            pass
    assert commands == [cmd]


@pytest.mark.variant("a self-narrowed worker check receives BRINDLE_CHANGED_FILES and must not satisfy a full run")
def test_self_narrowed_worker_check_does_not_satisfy_full_run_on_same_tree(db, repo, monkeypatch):
    """A non-pytest check receives BRINDLE_CHANGED_FILES on a worker branch and
    can narrow itself; its pass must not populate the full-run tree cache key."""
    cmd = "check-script --scoped"
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"checks": [cmd]}))
    (repo / "a.txt").write_text("v1\n")
    sh("git add -A && git commit -qm base", repo)
    ws = workspaces.create(db, str(repo), "feat").workspace
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done", "@0", None, 1.0))
    (Path(ws.path) / "a.txt").write_text("v2\n")
    sh("git commit -qam work", Path(ws.path))

    seen = []

    def fake_run(c, cwd, env, timeout, cancel=None):
        seen.append(env.get("BRINDLE_CHANGED_FILES"))
        return (True, "scoped ok") if "BRINDLE_CHANGED_FILES" in env else (False, "full fail")

    monkeypatch.setattr(autopilot, "run_check", fake_run)
    assert gates.run_checked(db, ws, cmd, {}, 30)[0]
    ok, _ = gates.run_checked(db, ws, cmd, {}, 30, full=True)
    assert not ok
    assert seen == ["a.txt", None]


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
