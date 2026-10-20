"""Conflict-aware merging, parts one and two: predict conflicts from what
running branches actually changed, and merge ready branches in the order
with the fewest overlapping hunks, syncing the rest onto the new tip."""
import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, conflicts, gates, mcp_server, pipeline, workspaces
from brindle.db import Agent


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", result=None,
        pipeline=None, **kw):
    """An agent row with no process behind it (the orchestration tests share this)."""
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", result,
                       time.time(), **kw))
    if pipeline:   # add_agent writes the core columns only
        db.update_agent(agent_id, pipeline=pipeline)


# -- hunks and their overlap -----------------------------------------------------


def test_parse_hunks_does_not_take_a_body_line_for_a_file_header():
    """A removed line starting `-- ` (an SQL comment, a Lua comment, a yaml
    document marker `---`) prints as `--- ...` inside the hunk; the hunks
    after it still belong to the real file."""
    patch = ("diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n"
             "@@ -1 +1 @@\n--- old comment\n+-- new comment\n"
             "@@ -5,2 +5,2 @@\n-+++ marker\n++++ other\n-x\n+y\n"
             "@@ -9 +9 @@\n-z\n+w\n"
             "diff --git a/r.sql b/r.sql\n--- a/r.sql\n+++ b/r.sql\n@@ -2 +2 @@\n-a\n+b\n")
    assert conflicts.parse_hunks(patch) == {"q.sql": [(1, 1), (5, 6), (9, 9)], "r.sql": [(2, 2)]}


def test_changed_files_keeps_unusual_names_as_themselves(db, repo):
    ws = workspaces.create(db, str(repo), "names").workspace
    name = "café x.py"
    (Path(ws.path) / name).write_text("x\n")
    sh("git add -A && git commit -qm names", Path(ws.path))
    (Path(ws.path) / "sp ace.txt").write_text("y\n")            # untracked
    assert sorted(conflicts.changed_files(ws)) == [name, "sp ace.txt"]
    assert conflicts.hunks(ws) == {name: [(0, 0)]}


# -- predicting conflicts while workers run ----------------------------------------


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    """Stands in for agents.spawn: a worker row, no CLI process (shared too)."""
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


@pytest.fixture
def two_workers(db, repo, monkeypatch):
    """A busy supervisor with two assigned workers, each on its own branch."""
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"pipeline": False}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    ids = []
    for name in ("a", "b"):
        out = asyncio.run(mcp_server.assign("developer", f"task {name}", branch=f"feat-{name}"))
        ids.append(re.search(r"Started worker (\S+)", out).group(1))
    wss = [db.get_workspace(db.get_agent(i).workspace_id) for i in ids]
    return ids, wss


def edit(ws, name, text, commit=True):
    (Path(ws.path) / name).write_text(text)
    if commit:
        sh(f"git add -A && git commit -qm {name}", Path(ws.path))


def test_the_warning_is_sent_once_per_pair_until_more_files_overlap(db, two_workers):
    (wa, wb), (wsa, wsb) = two_workers
    edit(wsa, "app.py", "a\n")
    edit(wsb, "app.py", "b\n")
    agents.handle_hook(db, wa, "stop", {})
    agents.handle_hook(db, wb, "stop", {})          # the other side of the same pair
    agents.handle_hook(db, wa, "stop", {})          # another turn, same overlap
    assert db.pending_count("boss") == 1
    edit(wsa, "shared.py", "a\n")
    edit(wsb, "shared.py", "b\n")
    agents.handle_hook(db, wb, "stop", {})
    assert db.pending_count("boss") == 2
    db.pop_pending("boss")
    assert "shared.py" in db.pop_pending("boss").body


# -- merging ready branches in order -------------------------------------------------


@pytest.fixture
def piped(db, repo, monkeypatch):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"review": True, "auto_merge_default_branch": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    started = []

    def fake_review(db_, caller, ws_, profile=None, focus=None, cfg=None, **kw):
        add(db_, ws_, f"rev{len(started)}", "review", "reviewer", parent=caller.id)
        started.append(ws_.id)
        return db_.get_agent(f"rev{len(started) - 1}")

    monkeypatch.setattr(agents, "request_review", fake_review)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_detach", lambda argv: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    return root


def ready_branch(db, repo, name, worker_id, files: dict[str, str]):
    """A worker's branch with ``files`` committed, approved and marked ready."""
    ws = workspaces.create(db, str(repo), name).workspace
    for f, text in files.items():
        (Path(ws.path) / f).write_text(text)
    sh("git add -A && git commit -qm work", Path(ws.path))
    add(db, ws, worker_id, "assign", parent="boss", status="processing",
        result=f"did {name}", pipeline="ready")
    add(db, ws, f"rev-{worker_id}", "review", "reviewer", parent="boss")
    db.add_review(ws.id, gates.head(ws), f"rev-{worker_id}", True, f"lgtm {name}")
    return ws


def merged_subjects(repo):
    return sh("git log --merges --format=%s", repo).splitlines()


def test_one_approval_merges_every_ready_branch_least_entangled_first(db, repo, piped):
    # a and c both rewrite app.py's first line; b only adds a file. Created
    # a, b, c: the oldest-first order would merge a first; the entanglement
    # order merges b first, then a, then c.
    a = ready_branch(db, repo, "feat-a", "wa", {"app.py": "print('a')\n", "a.py": "a\n"})
    b = ready_branch(db, repo, "feat-b", "wb", {"b.py": "b\n"})
    c = ready_branch(db, repo, "feat-c", "wc", {"app.py": "print('c')\n"})
    assert [ws.name for _w, ws in pipeline.merge_order(pipeline.ready_branches(db, str(repo), "main"))] \
        == ["feat-b", "feat-a", "feat-c"]

    assert pipeline.on_review(db, db.get_agent("rev-wa"), a, True, "lgtm feat-a") is True

    into_main = [s for s in merged_subjects(repo) if s.startswith("Merge branch 'feat-")]
    assert into_main[::-1] == ["Merge branch 'feat-b'", "Merge branch 'feat-a'"]   # oldest first
    assert "a.py" in sh("git ls-tree --name-only HEAD", repo) and "b.py" in sh("git ls-tree --name-only HEAD", repo)
    assert db.get_workspace(a.id) is None and db.get_workspace(b.id) is None
    # c conflicts with a on app.py once a is in: it isn't merged, its worker is asked to resolve.
    assert db.get_workspace(c.id) is not None and db.get_agent("wc").pipeline == "resolving"
    fix = db.pop_pending("wc")
    assert fix and "git merge main" in fix.body and "app.py" in fix.body
    bodies = [db.pop_pending("boss").body for _ in range(db.pending_count("boss"))]
    assert sum("Merged feat-b into main" in b_ for b_ in bodies) == 1
    assert sum("Merged feat-a into main" in b_ for b_ in bodies) == 1
    assert any("feat-c" in b_ and "conflicts" in b_ and "wc" in b_ for b_ in bodies)
    assert not any("needs you" in b_ for b_ in bodies)
