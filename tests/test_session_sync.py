"""brindle.session_sync: a session branch keeps up with origin/main. A merge
into it, a session resuming and a merged session PR bring origin/main in when
that's clean; a conflict blocks merges into it until a branch resolves it, and
a branch behind origin/main gets no PR."""

import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, config, gates, git, pipeline, session_sync, workspaces
from brindle.daemon import session_review
from brindle.db import Agent
from brindle.service import inbox
from brindle.service import sessions as svc


@pytest.fixture
def told(monkeypatch):
    """What brindle said to the session's supervisor."""
    said = []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, **kw: said.append((to, body)))
    monkeypatch.setattr(session_sync, "_detach", lambda args: said.append(("detached", args)))
    return said


@pytest.fixture
def sess(db, repo, tmp_path, told):
    """Session ``lead`` on branch ``sess`` (cut from main) in its own checkout."""
    sh(f"git worktree add -q -b sess {tmp_path / 'sess'} main", repo)
    ws = workspaces.adopt_root(db, str(tmp_path / "sess"))
    db.add_agent(Agent("lead", ws.id, "supervisor", "claude", None, "interactive", "idle", "%9", None, time.time()))
    return ws


@pytest.fixture
def other(repo, tmp_path):
    """Another clone, where commits land on origin/main without the session."""
    path = tmp_path / "other"
    sh(f"git clone -q {repo.parent / 'origin.git'} {path}", tmp_path)
    return path


def commit(path: Path, name: str, text: str, push: bool = False) -> str:
    (path / name).write_text(text)
    sh(f"git add {name} && git commit -qm {name}", path)
    if push:
        sh("git push -q origin HEAD:main", path)
    return sh("git rev-parse HEAD", path)


def worker(db, repo, name: str):
    ws = workspaces.create(db, str(repo), name, base="sess", fetch=False).workspace
    commit(Path(ws.path), f"{name}.txt", name)
    return ws


def contains(path, sha: str) -> bool:
    return git.ok(["merge-base", "--is-ancestor", sha, "HEAD"], path)


def test_a_merge_into_the_session_tells_the_supervisor_how_far_behind_main_it_is(db, repo, sess, other, told):
    commit(Path(sess.path), "app.py", "session\n")
    commit(other, "app.py", "main\n", push=True)
    main = commit(other, "hot.py", "fix\n", push=True)
    (Path(sess.path) / "app.py").write_text("unsaved\n")   # the supervisor is mid-edit: nothing is merged
    feat = worker(db, repo, "feat")

    pipeline.merge(db, None, feat)

    [(to, text)] = [m for m in told if m[0] == "lead"]
    assert "2 commit(s)" in text and "files both changed: app.py" in text and "uncommitted" in text
    assert not contains(sess.path, main)


def test_a_clean_sync_merges_main_in_and_checks_what_it_brought(db, repo, sess, other, told):
    config.set_local(sess.repo_root, "checks", ["test -f hot.py"])
    before = commit(Path(sess.path), "a.py", "a\n")
    main = commit(other, "hot.py", "fix\n", push=True)
    feat = worker(db, repo, "feat")

    out = pipeline.merge(db, None, feat)

    assert out.startswith("Merged"), out
    parents = sh("git rev-list --parents -n 1 HEAD~1", Path(sess.path)).split()[1:]
    assert parents == [before, main]       # a merge commit of origin/main, then the worker's merge
    assert any(to == "lead" and "brindle merged origin/main into sess" in text for to, text in told)
    [args] = [a for to, a in told if to == "detached"]
    assert session_sync.run_checks(db, args[-2], args[-1]).startswith("The checks passed")


def test_a_conflicting_sync_blocks_merges_until_a_branch_resolves_it(db, repo, sess, other, told):
    commit(Path(sess.path), "app.py", "session\n")
    main = commit(other, "app.py", "main\n", push=True)
    feat = worker(db, repo, "feat")

    out = pipeline.merge(db, None, feat)

    assert out.startswith("Not merged: sess must be synced with origin/main") and "app.py" in out
    assert not contains(sess.path, main) and sh("git status --porcelain", Path(sess.path)) == ""
    [item] = [i for i in inbox.items(db, str(repo), panes={}) if i.kind == "sync_conflict"]
    assert item.root_id == "lead" and "app.py" in item.detail
    fix = workspaces.create(db, str(repo), "fix", base="sess", fetch=False).workspace
    sh("git merge -q origin/main || true", Path(fix.path))
    commit(Path(fix.path), "app.py", "both\n")
    assert pipeline.merge(db, None, fix).startswith("Merged")
    assert contains(sess.path, main) and session_sync.merge_block(db, feat) is None


def test_a_session_behind_main_gets_no_pr(db, repo, sess, other):
    commit(Path(sess.path), "app.py", "session\n")
    commit(other, "app.py", "main\n", push=True)
    sh("git fetch -q origin", Path(sess.path))

    assert any(r.startswith("origin/main has 1 commit(s) sess lacks")
               for r in session_review.reasons(db, db.get_agent("lead"), sess))
    with pytest.raises(workspaces.WorkspaceError, match="not opening a PR: origin/main"):
        with gates.pr_checked(db, sess):
            pass


def test_resuming_a_session_merges_origin_main_in_first(db, repo, sess, other, monkeypatch):
    db.set_status("lead", "paused")
    commit(Path(sess.path), "a.py", "a\n")
    main = commit(other, "hot.py", "fix\n", push=True)
    resumed = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
    monkeypatch.setattr(agents, "resume", lambda db_, rid, **kw: resumed.append(contains(sess.path, main)) or [])

    got = svc.continue_(db, workspaces.adopt_root(db, str(repo)), "lead")

    assert got.state == "resumed" and resumed == [True]
    assert any(n.text.startswith("brindle merged origin/main into sess") for n in got.notes)


def test_a_squashed_session_pr_syncs_back_without_conflicting(db, repo, sess, other, monkeypatch):
    path = Path(sess.path)
    pushed = commit(path, "app.py", "v1\n")
    sh("git push -q origin sess", path)
    sh("git fetch -q origin sess && git merge -q --squash origin/sess && git commit -qm squash", other)
    sh("git push -q origin main", other)
    squash = sh("git rev-parse HEAD", other)
    main = commit(other, "hot.py", "fix\n", push=True)
    commit(path, "app.py", "v2\n")          # the session carries on editing the same line
    monkeypatch.setattr(workspaces, "pull_request_merge_commit", lambda url, cwd: squash)

    out = session_sync.on_pr_merged(db, sess, "https://github.com/o/r/pull/7", pushed)

    assert out.state == "synced", out.text
    assert contains(path, main) and (path / "app.py").read_text() == "v2\n"
