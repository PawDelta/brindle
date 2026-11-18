"""The daemon's sweeps start no process while nothing has changed.

The daemon runs ``ReadinessWatch.sweep`` every 5 seconds and, for an open
app, the events feed's git sweep; the cull runs once a minute. On a repo with
many paused and done sessions an unchanged round used to start git for many of
them, and an antivirus inspects every process (docs/investigations/process-count.md)."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, autopilot, cull, git, session_sync, tmux, workspaces
from brindle.daemon import events, session_review
from brindle.db import Agent, Workspace
from test_merge_order import _repo_template, quiet_removal  # noqa: F401 - fixture

_BRANCHES = {"live": "main", "parked": "main", "finished": "main", "messy": "main", "clash": "main",
             "feat": "live", "still": "live", "quiet": "live"}


@pytest.fixture(scope="session")
def _sessions_template(tmp_path_factory, _repo_template):
    """The repo with every session's worktree already made (committed work, the
    unsaved file, the conflicting change), built with git once per test process."""
    base = tmp_path_factory.mktemp("sessions-template")
    for name in ("origin.git", "proj"):
        shutil.copytree(_repo_template / name, base / name, symlinks=True)
    proj = base / "proj"
    cfg = proj / ".git" / "config"
    cfg.write_text(cfg.read_text().replace(str(_repo_template), str(base)))
    work = proj / ".brindle" / "worktrees"
    for branch, from_ in _BRANCHES.items():
        sh(f"git worktree add -q --no-track -b {branch} {work / branch} {from_}"
           f" && git config branch.{branch}.{git.BASE_CONFIG_KEY} {from_}", proj)
    for branch, files in (("live", {"live.txt": "live"}), ("parked", {"parked.txt": "parked"}),
                          ("finished", {"finished.txt": "finished"}), ("messy", {"messy.txt": "messy"}),
                          ("clash", {"app.py": "session\n"}), ("feat", {"feat.txt": "feat"})):
        for f, text in files.items():
            (work / branch / f).write_text(text)
        # A session-scoped fixture gets no per-test git identity; CI has no global one.
        sh("git add -A && git -c user.name=brindle-test -c user.email=test@brindle.invalid commit -qm work",
           work / branch)
    (work / "messy" / "unsaved.txt").write_text("unsaved\n")
    with open(proj / ".git" / "info" / "exclude", "a") as fh:
        fh.write("/.brindle/worktrees/\n")
    return base


def _session_repo(tmp_path, template):
    """The template copied, with the absolute paths git keeps in it pointed at the copy."""
    for name in ("origin.git", "proj"):
        shutil.copytree(template / name, tmp_path / name, symlinks=True)
    proj = tmp_path / "proj"
    old = str(template)
    files = [proj / ".git" / "config", *(proj / ".git" / "worktrees").glob("*/gitdir"),
             *(proj / ".brindle" / "worktrees").glob("*/.git")]
    for f in files:
        f.write_text(f.read_text().replace(old, str(tmp_path)))
    return proj


def _adopt(db, repo, branch: str) -> Workspace:
    """The row ``workspaces.create`` leaves for a worktree the template already made."""
    root = str(repo)
    slug = workspaces._db_slug(db, root)
    ws = Workspace(f"{slug}/{branch}", root, branch, "worktree", branch, _BRANCHES[branch],
                   str(repo / ".brindle" / "worktrees" / branch), workspaces._next_port_base(db),
                   workspaces._session_name(root, branch, slug), time.time())
    db.add_workspace(ws)
    return ws


def _root(db, ws, root_id: str, status: str, mode: str = "interactive") -> None:
    db.add_agent(Agent(root_id, ws.id, mode if mode == "coordinator" else "supervisor", "claude", None, mode,
                       status, f"%{root_id}", None, time.time()))


def _done_goal(db, root_id: str, audit_sha: str | None = None) -> None:
    db.add_autopilot(root_id)
    autopilot.set_goal(db, root_id, "goal", [("m", "true", None)])
    db.conn.execute("UPDATE milestones SET status = 'passed' WHERE root_id = ?", (root_id,))
    db.conn.execute("UPDATE autopilot SET state = 'done', audit_sha = ? WHERE root_id = ?", (audit_sha, root_id))


@pytest.mark.slow("builds ten real worktrees and settles two real sweeps (~130 git processes, 15s on the macOS CI "
                  "runner) so the third sweep can prove it starts none; faking git would defeat what it asserts")
def test_an_unchanged_sweep_starts_no_process(db, tmp_path, _repo_template, _sessions_template, monkeypatch):
    """Sessions in every state the owner's machine had: a paused session on the
    repo's own checkout with its goal reached (readiness asked git every sweep),
    a session whose merge of origin/main conflicts (tried again every sweep) and
    one with unsaved work (git status every sweep), paused, done and live ones,
    the app's feed on a checkout with no base branch (git.status every 5 s), and
    a worker the cull already told about (counted again every minute)."""
    quiet_removal(monkeypatch)
    repo = _session_repo(tmp_path, _sessions_template)
    clock = [1000.0]
    monkeypatch.setattr(session_sync.time, "monotonic", lambda: clock[0])
    home = workspaces.adopt_root(db, str(repo))
    _root(db, home, "coord-paused", "paused", "coordinator")
    _root(db, home, "coord-done", "done", "coordinator")
    _root(db, home, "home-lead", "paused")
    _done_goal(db, "home-lead", audit_sha=sh("git rev-parse HEAD", repo))
    for branch, root, status in (("live", "lead", "idle"), ("parked", "parked-lead", "paused"),
                                 ("finished", "done-lead", "done"), ("messy", "messy-lead", "idle")):
        _root(db, _adopt(db, repo, branch), root, status)
    _done_goal(db, "lead")
    _root(db, _adopt(db, repo, "clash"), "clash-lead", "idle")
    other = tmp_path / "other"
    shutil.copytree(_repo_template / "proj", other, symlinks=True)
    cfg = other / ".git" / "config"
    cfg.write_text(cfg.read_text().replace(str(_repo_template), str(tmp_path)))
    (other / "app.py").write_text("main\n")
    sh("git commit -qam main && git push -q origin HEAD:main", other)
    feat = _adopt(db, repo, "feat")
    db.add_agent(Agent("worker", feat.id, "developer", "claude", "lead", "assign", "idle", "%worker", None, 1.0))
    still = _adopt(db, repo, "still")       # idle, nothing committed: nothing to tell
    db.add_agent(Agent("idler", still.id, "developer", "claude", "lead", "assign", "idle", "%idler", None, 1.0))
    quiet = _adopt(db, repo, "quiet")       # Codex: idle is judged by its pane's last output
    db.add_agent(Agent("codex", quiet.id, "developer", "codex", "lead", "assign", "idle", "%codex", None, 1.0))
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({"%lead": True, "%clash-lead": True, "%messy-lead": True, "%worker": True, "%idler": True,
                  "%codex": True})
    panes.activity.update({pane: time.time() - 600 for pane in panes})     # nothing printed for 10 minutes
    read = []
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: panes)
    monkeypatch.setattr(tmux, "capture", lambda target, **k: read.append(target) or "❯ \n")
    monkeypatch.setattr(agents, "is_alive", lambda a, p=None: panes.get(a.tmux_window, False))
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: None)
    monkeypatch.setattr(session_review, "open_pr_for_head", lambda sha, cwd, root_id=None: None)
    watch, feed = session_review.ReadinessWatch(), events.Feed(db)

    def daemon() -> None:
        watch.sweep(db)
        clock[0] += events.GIT_SECONDS
        feed.tick(clock[0])

    for _ in range(2):                # the first sync's merge is found synced, the clash settled
        daemon()
        cull.sweep(db)
    assert db.mark("worker", f"commits:{sh('git rev-parse HEAD', Path(feat.path))}") == "told"
    started, real = [], subprocess.Popen.__init__
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, args, *a, **k: (
        started.append(args), real(self, args, *a, **k))[1])
    daemon()
    assert started == []
    read.clear()
    cull.sweep(db)                    # its process table and pane listing are its job; git isn't,
    assert [args for args in started if args[0] == "git" or "display-message" in args] == []
    assert read == []                 # nor reading screens nothing has printed to since the last read
