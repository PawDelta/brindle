"""Conflict-aware merging, parts one and two: predict conflicts from what
running branches actually changed, and merge ready branches in the order
with the fewest overlapping hunks, syncing the rest onto the new tip."""
import asyncio
import json
import re
import shutil
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, config, conflicts, disk, gates, git, mcp_server, pipeline, services, tmux, workspaces
from brindle.db import Agent, Workspace


# -- fast git state, shared with the other merge and pipeline test files ------------


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    """conftest's ``repo``, built with git once per test process."""
    base = tmp_path_factory.mktemp("repo-template")
    origin, work = base / "origin.git", base / "proj"
    work.mkdir()
    (work / "app.py").write_text("print('hi')\n")
    (work / ".gitignore").write_text(".env\n")
    (work / ".env").write_text("SECRET=1\n")
    sh(f"git init -q --bare -b main {origin} && git init -q -b main && git add -A"
       " && git -c user.name=t -c user.email=t@example.com commit -qm init"
       f" && git remote add origin {origin} && git push -q -u origin main && git remote set-head origin main",
       work)
    return base


@pytest.fixture
def repo(tmp_path, _repo_template):
    """conftest's ``repo`` (main with one commit, pushed to a bare origin),
    copied from the template instead of rebuilt with nine git runs."""
    for name in ("origin.git", "proj"):
        shutil.copytree(_repo_template / name, tmp_path / name, symlinks=True)
    cfg = tmp_path / "proj" / ".git" / "config"
    cfg.write_text(cfg.read_text().replace(str(_repo_template), str(tmp_path)))
    return tmp_path / "proj"


def worktree(db, repo, branch: str, base: str = "main", files: dict[str, str] | None = None) -> Workspace:
    """What ``workspaces.create`` leaves (the worktree, its base and its row),
    in one shell run instead of a dozen git runs, with ``files`` committed.
    ``create`` itself is covered where it is the behaviour (assign, sessions)."""
    root = str(repo)
    path = Path(root) / ".brindle" / "worktrees" / branch
    sh(f"git worktree add -q --no-track -b {branch} {path} {base}"
       f" && git config branch.{branch}.{git.BASE_CONFIG_KEY} {base}", repo)
    if files:
        for f, text in files.items():
            (path / f).write_text(text)
        sh("git add -A && git commit -qm work", path)
    with open(Path(root) / ".git" / "info" / "exclude", "a") as fh:
        fh.write("/.brindle/worktrees/\n")
    slug = workspaces._db_slug(db, root)
    ws = Workspace(f"{slug}/{branch}", root, branch, "worktree", branch, base, str(path),
                   workspaces._next_port_base(db), workspaces._session_name(root, branch, slug), time.time())
    db.add_workspace(ws)
    return ws


def quiet_removal(monkeypatch):
    """``workspaces.remove`` without its machine-wide looks: the process
    table and tmux panes (``disk.live_in``), docker and the tmux session.
    The real ones run in test_history's merge_workspace tests."""
    monkeypatch.setattr(disk, "live_in", lambda *a, **k: False)
    monkeypatch.setattr(services, "down", lambda ws, cfg=None: [])
    monkeypatch.setattr(tmux, "kill_session", lambda name: None)


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", result=None,
        pipeline=None, **kw):
    """An agent row with no process behind it (the orchestration tests share this)."""
    # tmux allocates numeric pane ids; a fake must never alias the keeper.
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, f"%fake-{agent_id}", result,
                       time.time(), **kw))
    if pipeline:   # add_agent writes the core columns only
        db.update_agent(agent_id, pipeline=pipeline)


# -- hunks and their overlap -----------------------------------------------------


@pytest.mark.variant("past bug: a hunk body line starting `--- ` was read as a file header")
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
    name = "café x.py"
    ws = worktree(db, repo, "names", files={name: "x\n"})
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


def test_the_warning_is_sent_once_per_pair_until_more_files_overlap(db, two_workers, monkeypatch):
    (wa, wb), (wsa, wsb) = two_workers
    real_snapshot = agents.snapshot_turn   # rewind's turn snapshots: wa's two turns are enough
    monkeypatch.setattr(agents, "snapshot_turn", lambda db_, a: real_snapshot(db_, a) if a.id == wa else None)
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
    monkeypatch.setattr(pipeline.autopilot, "_unavailable", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_detach", lambda argv: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    quiet_removal(monkeypatch)
    # The config isn't committed, so `git show main:.brindle/config.json` fails
    # on every read and the working tree's file is what counts: read it directly.
    monkeypatch.setattr(config, "_read_shared", lambda root, branch: config._read_json(
        root / config.CONFIG_DIR / config.CONFIG_FILE))
    return root


def ready_branch(db, repo, name, worker_id, files: dict[str, str]):
    """A worker's branch with ``files`` committed, approved and marked ready."""
    ws = worktree(db, repo, name, files=files)
    add(db, ws, worker_id, "assign", parent="boss", status="processing",
        result=f"did {name}", pipeline="ready")
    add(db, ws, f"rev-{worker_id}", "review", "reviewer", parent="boss")
    db.add_review(ws.id, gates.head(ws), f"rev-{worker_id}", True, f"lgtm {name}")
    return ws


def merged_subjects(repo):
    return sh("git log --merges --format=%s", repo).splitlines()


@pytest.mark.slow("the one end-to-end run of the merge order: three real worktrees, two merges and a "
                  "conflicting sync, ~125 git runs; 11s when the gate's machine is at load 200")
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
