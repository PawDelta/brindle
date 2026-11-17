"""The daemon's sweeps ask a question about the repo once, not once per checkout.

Whether ``origin/main`` exists, which of ``main`` and ``origin/main`` is
ahead, what origin's default branch is and whether there is an origin at all
are the same in every checkout of a repo, and change only when a ref or the
config does. With sessions at work the sweeps asked git each of them for every
checkout, every 5 seconds, and an antivirus inspects every process
(docs/investigations/process-count.md, S6). So did a session whose goal is
reached while its workers are at work: each sweep judged its readiness again,
asking git about commits and files that hadn't moved."""

import os
import shutil
import subprocess
from collections import Counter

import pytest

from conftest import sh
from brindle import agents, autopilot, git, session_sync, status_cache, tmux
from brindle.daemon import events, session_review
from brindle.db import Agent
from brindle.service import inbox
from test_idle_spawns_daemon import _done_goal, _root
from test_merge_order import _repo_template, quiet_removal, repo, worktree  # noqa: F401 - fixture

REPO_QUESTIONS = ("show-ref", "remote", "symbolic-ref", "for-each-ref", "merge-base")
REPO_NAMES = {"main", "origin/main", "refs/heads", "refs/heads/main", "refs/remotes/origin/main",
              "refs/remotes/origin/HEAD"}


def _repo_question(argv: list[str], names: dict[str, str]) -> str | None:
    """``argv`` as a question about the repo (it names nothing but the base
    branch and origin, by name or by commit), or None: one about a checkout."""
    if argv[0] != "git":
        return None
    args = argv[3:] if argv[1] == "-C" else argv[1:]
    words = [names.get(a, a) for a in args if not a.startswith("-")]
    if words[0] in REPO_QUESTIONS and all(w in REPO_NAMES for w in words[1:]):
        return " ".join(words)
    return None


@pytest.mark.variant("stale-cache guard: a kept git answer must be asked again when a file git reads for it changes, or brindle reports old state")
def test_a_kept_answer_is_asked_again_when_a_file_git_reads_for_it_changes(repo):
    """What is kept must go when anything git reads for it moves, wherever
    that is: a remote named in a file the repo's config includes, and an
    untracked ``.gitattributes`` that changes how a tracked file reads (no
    ref, index or tracked file moves in either)."""
    included = repo.parent / "extra.gitconfig"
    included.write_text('[remote "mirror"]\n\turl = https://example.test/mirror.git\n')
    sh(f"git config include.path {included}", repo)
    assert git.has_remote(repo, "mirror")
    included.write_text("")
    assert not git.has_remote(repo, "mirror")

    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    sh("git -c core.autocrlf=false add crlf.txt && git commit -qm crlf", repo)
    # Its stat no longer matches the index, so git compares its content on every look
    # (it would anyway only while the file is as new as the index).
    os.utime(repo / "crlf.txt", (1_000_000_000, 1_000_000_000))
    assert status_cache.tracked_dirty(repo) == []
    (repo / ".gitattributes").write_text("*.txt text\n")
    assert status_cache.tracked_dirty(repo) == ["crlf.txt"]


def test_a_repo_question_is_asked_once_however_many_sessions_are_at_work(db, repo, tmp_path, _repo_template,
                                                                         monkeypatch):
    """Three sessions in one repo: two whose goal is reached with a worker at
    work (their readiness is judged again whenever a worker's row changes),
    one of them with a merged PR and a sync's merge on top, and one that is
    quiet (the idle sync merges origin/main into it). A round is the daemon's
    sweep, the app's feed, and the open app reading its Inbox and each
    session's goal (which judged the session, and its merged PR, with git on
    every read). origin/main moves once; the round that sees it asks each repo
    question at most once, and the next, the workers still at work, asks none."""
    quiet_removal(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(session_sync.time, "monotonic", lambda: clock[0])
    other = tmp_path / "other"
    shutil.copytree(_repo_template / "proj", other, symlinks=True)
    cfg = other / ".git" / "config"
    cfg.write_text(cfg.read_text().replace(str(_repo_template), str(tmp_path)))

    def main_moves(name: str) -> None:
        (other / name).write_text("main\n")
        sh("git add -A && git commit -qm main && git push -q origin HEAD:main", other)
        sh("git fetch -q origin", repo)

    sessions = {}
    for branch in ("one", "two", "three"):
        sessions[branch] = worktree(db, repo, branch, files={f"{branch}.txt": branch})
        _root(db, sessions[branch], f"lead-{branch}", "idle")
    _done_goal(db, "lead-one")
    # Its PR merged at its commit, and a sync has merged origin/main in since: whether that merge
    # adds anything of the session's own (rev-parse <merge>^{tree}, merge-tree) was asked every time.
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at, merged_at)"
                    " VALUES ('lead-one', 'one', 'https://example.test/pr/1', ?, 1.0, 2.0)",
                    (sh("git rev-parse HEAD", sessions["one"].path),))
    db.conn.execute("INSERT INTO session_pr_prep (root_id, branch, sha, state, title, prepared_at)"
                    " VALUES ('lead-one', 'one', 'old', 'passed', 'One', 1.0)")     # the Inbox asks it too
    main_moves("landed.txt")
    sh("git merge -q --no-ff --no-edit origin/main", sessions["one"].path)
    _done_goal(db, "lead-two")
    panes = tmux.PaneSnapshot(tmux.current_server())
    edited = set()
    for branch in ("one", "two"):
        ws = worktree(db, repo, f"work-{branch}", base=branch, files={"work.txt": branch})
        edited.add(ws.path)
        db.add_agent(Agent(f"worker-{branch}", ws.id, "developer", "claude", f"lead-{branch}", "assign", "working",
                           f"%worker-{branch}", None, 1.0))
        panes[f"%worker-{branch}"] = True
    panes.update({f"%lead-{branch}": True for branch in sessions})
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes)
    monkeypatch.setattr(tmux, "capture", lambda target, **k: "❯ \n")
    monkeypatch.setattr(agents, "is_alive", lambda a, p=None: panes.get(a.tmux_window, False))
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: None)
    monkeypatch.setattr(session_sync, "start_checks", lambda ws, old: None)
    monkeypatch.setattr(session_review, "open_pr_for_head", lambda sha, cwd, root_id=None: None)
    watch, feed = session_review.ReadinessWatch(), events.Feed(db)

    def daemon() -> None:
        watch.sweep(db)
        clock[0] += events.GIT_SECONDS
        feed.tick(clock[0])
        inbox.items(db, str(repo), panes=panes)     # the open app reads its Inbox and each session's goal
        for branch in sessions:
            session_review.goal_phase(db, f"lead-{branch}")
            autopilot.goal_activity(db, f"lead-{branch}")

    def at_work() -> None:
        for branch in ("one", "two"):       # a worker's turn: its row and its files change
            db.conn.execute("UPDATE agents SET status_since = ? WHERE id = ?", (clock[0], f"worker-{branch}"))
            (repo / ".brindle" / "worktrees" / f"work-{branch}" / "work.txt").write_text(f"{clock[0]}\n")

    for _ in range(2):
        daemon()
        at_work()
    main_moves("new.txt")                       # once
    names = {sh("git rev-parse main", repo): "main", sh("git rev-parse origin/main", repo): "origin/main"}
    started, real = [], subprocess.Popen.__init__
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, args, *a, **k: (
        started.append((args, str(k.get("cwd")))), real(self, args, *a, **k))[1])
    daemon()
    asked = Counter(q for argv, _cwd in started if (q := _repo_question(argv, names)))
    assert asked == {"merge-base main origin/main": 1}      # which is ahead: git's to say, once
    # What a merge commit adds doesn't change with main: known since the first sweep that asked.
    assert [argv for argv, _cwd in started if argv[-1].endswith("^{tree}") or "merge-tree" in argv] == []
    at_work()
    started.clear()
    daemon()
    assert [q for argv, _cwd in started if (q := _repo_question(argv, names))] == []
    # Nor is a session whose workers are at work, its own checkout as it was, asked anything again:
    # what git said of its commits and its files stands until they move.
    assert [argv for argv, cwd in started if cwd in (sessions["one"].path, sessions["two"].path)] == []
    # git status runs where files changed since the last look (the workers' worktrees), and nowhere else.
    assert {cwd for argv, cwd in started if "status" in argv} == edited
