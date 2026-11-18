"""The goal route binds external PRs for the real sidebar."""

import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from brindle import autopilot, git, spawn, status_cache, workspaces
from brindle.daemon import routes_read, session_review as review
from brindle.db import Agent, Workspace


@pytest.fixture
def tracking(db, tmp_path, monkeypatch, real_github_lookups):
    ws = Workspace("home", str(tmp_path), "session", "worktree", "session", "main",
                   str(tmp_path), None, "session", 0)
    db.add_workspace(ws)
    db.add_agent(Agent("root", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, 0))
    db.add_autopilot("root")
    monkeypatch.setattr(autopilot, "goal_activity", lambda *args: {})
    monkeypatch.setattr(workspaces, "refresh_branch", lambda db, ws: ws)
    monkeypatch.setattr(status_cache, "read_head", lambda path: "session-head")
    monkeypatch.setattr(review.session_sync, "drifting", lambda *args: False)
    for name in ("_pr_cache", "_pr_flights", "_head_cache", "_head_roots"):
        monkeypatch.setattr(review, name, {})
    for name in ("_pr_closed", "_pr_changed", "_head_flights", "_head_changed"):
        monkeypatch.setattr(review, name, set())

    def submit(fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    monkeypatch.setattr(review._pr_pool, "submit", submit)
    monkeypatch.setattr(workspaces.shutil, "which", lambda name: "/bin/gh")
    request = Request({"type": "http", "query_string": f"repo={ws.repo_root}&root=root".encode()})
    return ws, lambda: routes_read.goal.__wrapped__(db, request)


def test_goal_tracks_pr_on_fresh_branch_with_copied_changes(db, tracking, monkeypatch):
    ws, goal = tracking
    git.run(["init", "-b", "main"], ws.path)
    git.run(["config", "user.name", "Test"], ws.path)
    git.run(["config", "user.email", "test@example.com"], ws.path)
    file = Path(ws.path) / "file"
    binary = Path(ws.path) / "binary"
    repeated = Path(ws.path) / "settings.py"
    block = "    a = 1\n    b = 2\n    c = 3\n    enabled = False\n    d = 4\n    e = 5\n    return enabled\n"
    repeated.write_text(f"def production():\n{block}\n\ndef development():\n{block}")
    binary.write_bytes(b"\x00base")
    lines = [f"line {i}\n" for i in range(40)]
    file.write_text("".join(lines))
    git.run(["add", "."], ws.path)
    git.run(["commit", "-m", "base"], ws.path)
    git.run(["checkout", "-b", "session"], ws.path)
    lines[25] = "session work\n"
    file.write_text("".join(lines))
    file.chmod(0o755)
    binary.write_bytes(b"\x00session")
    repeated.write_text(repeated.read_text().replace("enabled = False", "enabled = True", 1))
    git.run(["commit", "-am", "session work"], ws.path)
    session_head = git.head(ws.path)
    git.run(["checkout", "main"], ws.path)
    lines[25] = "line 25\n"
    lines[2] = "independent main work\n"
    file.write_text("".join(lines))
    git.run(["commit", "-am", "newer main"], ws.path)
    base = git.head(ws.path)
    git.run(["checkout", "-b", "fresh-from-main"], ws.path)
    git.run(["cherry-pick", session_head], ws.path)
    copied_head = git.head(ws.path)
    monkeypatch.setattr(status_cache, "read_head", lambda path: session_head)
    pr = {"number": 458, "url": "https://github.com/o/r/pull/458", "headRefName": "fresh-from-main",
          "headRefOid": copied_head, "baseRefOid": base}
    unrelated = {**pr, "number": 999, "url": "https://github.com/o/r/pull/999", "headRefOid": base}
    candidates = [unrelated, pr]
    real_read = spawn.read

    def read(args, **kwargs):
        if args[0] != "gh":
            return real_read(args, **kwargs)
        if args[:3] == ["gh", "pr", "view"]:
            payload = {"state": "OPEN", "mergeable": "MERGEABLE",
                       "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        else:
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 30
            payload = [] if "--search" in args else candidates[:limit]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(spawn, "read", read)
    # Newer main changed the same file, so the raw diff's blob IDs differ.
    goal()  # starts discovery without waiting for gh
    dto = goal()
    assert dto["pr_url"] == pr["url"]
    assert (dto["pr_state"]["number"], dto["pr_state"]["state"], dto["pr_state"]["done"],
            dto["pr_state"]["total"]) == (458, "ready", 1, 1)
    assert db.conn.execute("SELECT head_ref FROM session_pr_heads").fetchone()[0] == "fresh-from-main"
    assert db.conn.execute("SELECT pushed_sha FROM session_prs").fetchone()[0] == copied_head
    # A match beyond gh's first page is found, but another equivalent PR
    # beyond that page prevents adopting a match on the first page.
    candidates[:] = [unrelated] * 30 + [pr]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path)["url"] == pr["url"]
    candidates[:] = [pr] + [unrelated] * 30 + [{**pr, "url": "https://github.com/o/r/pull/459"}]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) == {}
    candidates[:] = [pr, {**pr, "headRefOid": "unavailable-object"}]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) is None
    candidates[:] = [unrelated]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) == {}
    # Same text edits must not conceal different binary data or file modes.
    binary.write_bytes(b"\x00different")
    git.run(["commit", "-am", "different binary"], ws.path)
    candidates[:] = [{**pr, "headRefOid": git.head(ws.path)}]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) == {}

    # Identical edits in distinct repeated blocks are different work, even
    # when every line of the three-line diff context is identical.
    git.run(["checkout", "-b", "different-block", copied_head], ws.path)
    repeated.write_text(f"def production():\n{block}\n\ndef development():\n{block.replace('enabled = False', 'enabled = True')}")
    git.run(["commit", "-am", "change development instead of production"], ws.path)
    candidates[:] = [{**pr, "headRefOid": git.head(ws.path)}]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) == {}
    git.run(["checkout", "--detach", copied_head], ws.path)
    binary.write_bytes(b"\x00session")
    file.chmod(0o644)
    git.run(["commit", "-am", "different mode"], ws.path)
    candidates[:] = [{**pr, "headRefOid": git.head(ws.path)}]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) == {}
    file.chmod(0o755)
    file.write_text(file.read_text().replace("session work", "    session work"))
    git.run(["commit", "-am", "different indentation"], ws.path)
    candidates[:] = [{**pr, "headRefOid": git.head(ws.path)}]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) == {}

    # If a candidate's newer base shifts these locations, do not establish
    # uniqueness by ignoring that candidate, even beside a known match.
    git.run(["checkout", "-b", "shifted-main", base], ws.path)
    file.write_text("inserted on main\n" + file.read_text())
    git.run(["commit", "-am", "shift main locations"], ws.path)
    shifted_base = git.head(ws.path)
    git.run(["cherry-pick", session_head], ws.path)
    candidates[:] = [pr, {**pr, "baseRefOid": shifted_base, "headRefOid": git.head(ws.path)}]
    assert workspaces.open_pull_request_for_commit(session_head, ws.path) is None


def test_pull_request_no_match_reuses_immutable_patches(tmp_path, monkeypatch, real_github_lookups):
    monkeypatch.setattr(workspaces.shutil, "which", lambda name: "/bin/gh")
    monkeypatch.setattr(workspaces, "_pr_patches", {}, raising=False)
    monkeypatch.setattr(workspaces, "_pr_no_matches", {}, raising=False)
    candidates = [{"number": 1, "baseRefOid": "base", "headRefOid": "other"}]
    diffs = []
    patches = []
    real_patch = workspaces._pr_patch

    def patch(base, head, cwd):
        patches.append((base, head))
        return real_patch(base, head, cwd)

    def read(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps([] if "--search" in args else candidates))

    def run(args, cwd):
        diffs.append(args[-1])
        return SimpleNamespace(stdout=f"diff --git a/file b/file\n+{args[-1]}\n")

    monkeypatch.setattr(spawn, "read", read)
    monkeypatch.setattr(workspaces, "_pr_patch", patch)
    monkeypatch.setattr(git, "run", run)
    monkeypatch.setattr(git, "out", lambda args, cwd: "base")
    assert workspaces.open_pull_request_for_commit("session", str(tmp_path)) == {}
    assert len(diffs) == 2
    diffs.clear()
    patches.clear()
    assert workspaces.open_pull_request_for_commit("session", str(tmp_path)) == {}
    assert diffs == []
    assert patches == []

    # A changed candidate set must be checked, reusing the session's patch.
    candidates.append({"number": 2, "baseRefOid": "base", "headRefOid": "session"})
    assert workspaces.open_pull_request_for_commit("session", str(tmp_path))["number"] == 2
    assert patches
    assert diffs == []


def test_closed_pr_is_not_readopted_from_cached_head(db, tracking, monkeypatch):
    ws, goal = tracking
    url = "https://github.com/o/r/pull/457"
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at)"
                    " VALUES ('root', 'session', ?, 'session-head', 1)", (url,))
    review.note_head_pr("session-head", ws.path, {"url": url, "headRefName": "old"})
    monkeypatch.setattr(workspaces, "pull_request_checks", lambda *args: {"state": "CLOSED"})
    clock = [1.0]
    monkeypatch.setattr(review.time, "monotonic", lambda: clock[0])

    def closing_during_lookup(*args):
        clock[0] = 2.0
        review.refresh_pr_states(db)
        return {"url": url, "headRefName": "old"}

    monkeypatch.setattr(workspaces, "open_pull_request_for_commit", closing_during_lookup)
    review._read_head_pr(("session-head", ws.path))
    assert goal()["pr_url"] is None
    # A lookup already in flight at retirement can still finish with the old PR.
    review.note_head_pr("session-head", ws.path, {"url": url, "headRefName": "old"})
    assert goal()["pr_url"] is None
    assert db.conn.execute("SELECT url FROM session_prs").fetchone()[0] is None
    # A later, fresh GitHub lookup can confirm the person reopened that PR.
    clock[0] = 3.0
    monkeypatch.setattr(workspaces, "open_pull_request_for_commit", lambda *args: {"url": url})
    monkeypatch.setattr(workspaces, "pull_request_checks", lambda *args: {"state": "OPEN"})
    review._read_head_pr(("session-head", ws.path))
    assert goal()["pr_url"] == url
    assert goal()["pr_state"]["number"] == 457
