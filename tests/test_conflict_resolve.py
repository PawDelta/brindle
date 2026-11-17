"""Conflict-aware merging, part three: an approved branch whose sync with
its base conflicts goes back to its worker to resolve (a light worker if
that one has finished), is reviewed and checked again, and then merges.
Protected paths are never resolved by a worker."""
import json
from pathlib import Path

import pytest

from conftest import full_text, sh
from brindle import agents, conflicts, gates, pipeline, workspaces
from test_merge_order import _repo_template, add, quiet_removal, repo, worktree  # noqa: F401 - fixture


# -- protected paths -----------------------------------------------------------------


@pytest.mark.parametrize("path, patterns, expected", [
    ("src/app.py", ["app.py"], False),
])
def test_is_protected(path, patterns, expected):
    assert conflicts.is_protected(path, patterns) is expected


# -- the pipeline on a conflicting branch ---------------------------------------------


@pytest.fixture
def conflicted(db, repo, monkeypatch):
    """A busy supervisor; a worker's approved branch that rewrote app.py;
    main that rewrote app.py differently since. The repo's check passes only
    once the resolved file carries both sides."""
    def setup(**config):
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps({
            "review": True, "auto_merge_default_branch": True,
            "checks": ["grep -q mine app.py && grep -q theirs app.py"], **config}))
        root = workspaces.adopt_root(db, str(repo))
        add(db, root, "boss", "interactive", "supervisor", status="processing")
        ws = worktree(db, repo, "feat", files={"app.py": "mine\n"})
        (repo / "app.py").write_text("theirs\n")
        sh("git add -A && git commit -qm theirs", repo)
        add(db, ws, "w1", "assign", parent="boss", status="processing",
            result="rewrote app.py", pipeline="reviewing")
        add(db, ws, "rev0", "review", "reviewer", parent="boss")
        db.add_review(ws.id, gates.head(ws), "rev0", True, "lgtm")
        return ws

    started = []

    def fake_review(db_, caller, ws_, profile=None, focus=None, cfg=None, **kw):
        rid = f"rev{len(started) + 1}"
        add(db_, ws_, rid, "review", "reviewer", parent=caller.id)
        started.append(ws_.id)
        return db_.get_agent(rid)

    monkeypatch.setattr(agents, "request_review", fake_review)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    def detach(argv):
        # The repo has checks, so the review starts from a detached process
        # once they finish: stand in for it.
        if "_review-after-checks" in argv:
            ws_ = db.get_workspace(argv[argv.index("_review-after-checks") + 1])
            fake_review(db, db.get_agent("boss"), ws_)

    monkeypatch.setattr(agents, "_detach", detach)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    quiet_removal(monkeypatch)
    setup.reviews = started
    return setup


def boss_messages(db):
    return [full_text(db, "boss", db.pop_pending("boss").body) for _ in range(db.pending_count("boss"))]


def test_a_resolver_waits_for_room_then_starts_resolving_in_the_worktree(db, repo, conflicted, monkeypatch):
    """Plan 3.5: a pipeline-started worker waits for room like an assigned one."""
    from brindle import spawn, tasks
    from test_merge_order import fake_spawn

    ws = conflicted()
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "w1")
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    load = [14.0]
    monkeypatch.setattr(spawn, "_load", lambda: load[0])
    monkeypatch.setattr(spawn, "_cores", lambda: 10)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    [t] = db.list_tasks(str(repo), state="pending")
    assert "load 14.0 on 10 cores" in t.waiting and t.caller_id == "boss"
    assert [a.id for a in db.list_agents(ws.id) if a.mode == "assign"] == ["w1"]   # nothing started
    [fyi] = boss_messages(db)
    assert "as soon as the machine has room" in fyi and "needs you" not in fyi

    load[0] = 2.0
    assert tasks.start_when_room(db, t.id, poll=0)
    resolver = db.get_agent(db.get_task(t.id).agent_id)
    assert resolver.workspace_id == ws.id and resolver.pipeline == "resolving"
    assert db.get_agent("w1").pipeline is None


def test_a_protected_file_with_a_quoted_name_is_still_protected(db, repo, conflicted, monkeypatch):
    """A conflict in a protected path goes to the supervisor, never to a
    worker. git C-quotes names with spaces or non-ASCII characters in its
    plain output; read raw, the name still matches the protected glob."""
    name = "café lock.py"
    ws = conflicted(protected_paths=["*.py"])
    (Path(ws.path) / name).write_text("mine\n")
    sh("git add -A && git commit -qm quoted", Path(ws.path))
    (repo / name).write_text("theirs\n")
    sh("git add -A && git commit -qm quoted-theirs", repo)
    db.add_review(ws.id, gates.head(ws), "rev0", True, "lgtm")
    delegated = []
    monkeypatch.setattr(agents, "delegate", lambda *a, **k: delegated.append(a))
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    [msg] = boss_messages(db)
    assert "needs you" in msg and "protected path" in msg and name in msg
    assert db.pending_count("w1") == 0 and delegated == []       # the live worker wasn't asked either
    assert db.get_agent("w1").pipeline is None
    assert db.get_workspace(ws.id) is not None and (repo / "app.py").read_text() == "theirs\n"


@pytest.mark.variant("stale-state: an approval of an older commit must not make a moved branch ready")
def test_an_approval_for_a_stale_commit_does_not_make_the_branch_ready(db, repo, conflicted):
    ws = conflicted()
    (Path(ws.path) / "later.py").write_text("x\n")
    sh("git add -A && git commit -qm later", Path(ws.path))     # committed after rev0 read the branch
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    assert db.get_agent("w1").pipeline is None and db.pending_count("w1") == 0
    [msg] = boss_messages(db)
    assert "needs you" in msg and "hasn't been reviewed" in msg
    assert (repo / "app.py").read_text() == "theirs\n"
