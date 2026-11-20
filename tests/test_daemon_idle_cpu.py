"""The daemon and the sidebars stay quiet while nothing changes: a checkout
whose git status can't be read isn't asked again every sweep, and a session
root runs one sidebar, never two."""

from brindle import bus
from brindle.db import Agent, Workspace


def test_a_failing_status_backs_off_instead_of_running_git_every_sweep(db, tmp_path):
    """The owner's daemon sat at 75% CPU: 24 workspaces in benchmark repos the
    OS had half cleaned up (a .git with no HEAD) failed their status on every
    5 s sweep, for every app client, each failure a fork of the whole daemon."""
    from brindle import git
    from brindle.daemon.events import GIT_SECONDS, Feed

    gutted = tmp_path / "bench" / "repo"
    (gutted / ".git").mkdir(parents=True)
    db.add_workspace(Workspace("w", str(gutted), "repo", "repo", "s", None, str(gutted), None, "s", 0))
    looks = []

    def status(path, base):
        looks.append(path)
        if not (gutted / ".git" / "HEAD").exists():
            raise git.GitError("git status: fatal: not a git repository")
        return git.Status("s", None, 0, 0, [], None)

    # Two app windows: each has its own feed, both on the one database.
    feeds = [Feed(db, panes=lambda: [], git_status=status) for _ in range(2)]
    sweeps = range(0, 600, int(GIT_SECONDS))
    for now in sweeps:
        for feed in feeds:
            feed.tick(now)
    assert 1 < len(looks) < 10, f"{len(looks)} looks in {len(sweeps) * len(feeds)} sweeps"

    # Repaired (the repo made again): looked at on the next sweep, not after the wait.
    (gutted / ".git" / "HEAD").write_text("ref: refs/heads/s\n")
    before = len(looks)
    told = feeds[0].tick(600)
    assert len(looks) == before + 1
    assert [m["workspace_id"] for m in told if m["type"] == "git"] == ["w"]
    assert not bus.tail(db, kinds=("session.changed",))


def test_a_second_sidebar_for_a_session_does_not_start_while_one_runs(db, tmp_path, monkeypatch):
    """One `brindle watch --sidebar` per session root: a second (a launch that
    found tmux too slow to say the first was there) closes at once, and the
    root's sidebar row points at the one still running. A tmux that can't say
    whose sidebar a pane is never lets one run unguarded."""
    import os

    import pytest

    from brindle import agents, tmux, watch

    db.add_workspace(Workspace("w", "/repo", "s", "worktree", "s", "main", str(tmp_path), None, "s", 0))
    db.add_agent(Agent("root", "w", "supervisor", "claude", None, "interactive", "idle", "", None, 0))
    monkeypatch.setattr(watch, "_log_to_file", lambda: None)
    monkeypatch.setattr("brindle.spawn.exit_on_hangup", lambda: None)
    monkeypatch.delenv("BRINDLE_SIDEBAR_ROOT", raising=False)
    started, second = [], []

    def dashboard(*a):
        """The first sidebar's screen: while it shows, the launch starts a second pane."""
        started.append(os.environ["TMUX_PANE"])
        if len(started) == 1:
            monkeypatch.setenv("TMUX_PANE", "%2")
            second.append(watch.run("/repo", sidebar=True))

    def tmux_down(*a):
        raise tmux.TmuxError("tmux: timed out")

    monkeypatch.setattr(watch.curses, "wrapper", dashboard)
    tag = {"%1": "root", "%2": "root"}
    for launcher_says_root, tmux_up in ((False, True), (True, False)):
        started.clear(), second.clear()
        db.set_sidebar_pane("root", "%2")           # the launch recorded the second pane
        if launcher_says_root:
            monkeypatch.setenv("BRINDLE_SIDEBAR_ROOT", "root")
        monkeypatch.setattr(agents, "sidebar_root", (lambda pane: tag[pane]) if tmux_up else tmux_down)
        monkeypatch.setattr(tmux, "get_pane_tag", (lambda pane, key: tag[pane]) if tmux_up else tmux_down)
        monkeypatch.setenv("TMUX_PANE", "%1")
        first = watch.run("/repo", sidebar=True)
        assert started == ["%1"], "a second sidebar started while the first ran"
        assert (first, second) == (True, [False])
        assert db.get_sidebar_pane("root") == ("%1" if tmux_up else "%2")

    # No root from the launcher and none from tmux: it doesn't start at all.
    monkeypatch.delenv("BRINDLE_SIDEBAR_ROOT")
    started.clear()
    with pytest.raises(tmux.TmuxError):
        watch.run("/repo", sidebar=True)
    assert started == []
