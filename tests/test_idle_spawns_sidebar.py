import os
import subprocess
from types import SimpleNamespace

from brindle import watch


class _Done(BaseException):
    """Ends the sidebar loop (it carries on past any Exception)."""


def test_an_unchanged_sidebar_refresh_starts_no_process(db, tmp_path, monkeypatch):
    """The sidebar loop itself, with nothing changing between refreshes: a
    session root, a worker waiting on the person in a worktree whose base
    branch was deleted, and a terminal that can't show the braid. Each idle
    refresh used to list the panes, read the waiting worker's screen, ask
    which terminal shows the sidebar and whether it is home (three tmux
    calls), recheck the braid, and run git status and a failing rev-list
    three times over: about 60 processes a minute per sidebar."""
    from brindle import agents, autopilot, git, sidebar_logo, tmux, view
    from brindle.db import Agent, Workspace

    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")
    main, wt = tmp_path / "proj", tmp_path / "wt"
    main.mkdir()
    (main / "a.txt").write_text("a\n")
    git.run(["init", "-q", "-b", "main"], main)
    git.run(["add", "-A"], main)
    git.run(["commit", "-qm", "init"], main)
    git.run(["worktree", "add", "-q", "-b", "feat", str(wt)], main)
    server = tmux.current_server()
    db.add_workspace(Workspace("w0", str(main), "w0", "main", "main", None, str(main), None, "", 0))
    db.add_workspace(Workspace("w1", str(main), "w1", "worktree", "feat", "gone", str(wt), None, "", 0))
    db.add_agent(Agent("lead", "w0", "supervisor", "claude", None, "interactive", "idle", "%1", None, 0,
                       tmux_server=server))
    db.add_agent(Agent("work", "w1", "developer", "claude", "lead", "assign", "waiting", "%2", None, 0,
                       tmux_server=server))
    autopilot.set_goal(db, "lead", "Ship it", [("Tests pass", "false", None)])
    db.record_check(db.milestones("lead")[0].id, False, "nope")   # the goal line asks what's in flight

    fake = tmp_path / "bin" / "tmux"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\ncase \"$*\" in\n"
                    "  *list-panes*) printf '@1\\t%%1\\t0\\t\\t\\t\\t\\n@2\\t%%2\\t0\\t\\t\\t\\t\\n"
                    "@1\\t%%9\\t0\\t\\t\\t\\t\\n' ;;\n"
                    "  *client_tty*) echo /dev/ttys001 ;;\n"
                    "  *session_name*) echo brindle_s ;;\n"
                    "  *'#{session_attached}'*) echo 1 ;;\n"
                    "  *capture-pane*) echo 'Do you want to proceed?' ;;\n"
                    "  *'#{pane_dead}'*) echo 0 ;;\n"
                    "esac\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("BRINDLE_TMUX_BIN", str(fake))
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(agents, "sidebar_root", lambda pane: "lead")
    monkeypatch.setattr(watch, "_cull_in_background", lambda: None)   # one a minute between sidebars
    monkeypatch.setattr(watch, "_start_status", lambda: None)
    monkeypatch.setattr(watch.watch_costs, "Feed", lambda root: SimpleNamespace(get=lambda: None))
    for name in ("curs_set", "mousemask"):
        monkeypatch.setattr(watch.curses, name, lambda *a: None)
    monkeypatch.setattr(watch.curses, "has_colors", lambda: False)
    clock = [2_000_000_000.0]
    monkeypatch.setattr(watch, "time", SimpleNamespace(time=lambda: clock[0], strftime=lambda f: "12:00"))
    monkeypatch.setattr(sidebar_logo, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    refreshes, started = [], []
    snapshot = view.snapshot
    monkeypatch.setattr(view, "snapshot", lambda *a, **k: refreshes.append(1) or snapshot(*a, **k))
    pilots, entry = [], view.autopilot_entry
    monkeypatch.setattr(view, "autopilot_entry", lambda *a, **k: pilots.append(entry(*a, **k)) or pilots[-1])
    popen = subprocess.Popen.__init__
    monkeypatch.setattr(subprocess.Popen, "__init__",
                        lambda self, args, *a, **k: started.append(args) or popen(self, args, *a, **k))

    def failed(what):
        raise AssertionError(what)     # a failed pass reads tmux again: it mustn't hide
    monkeypatch.setattr(watch, "_log_error", failed)

    def no_key():
        if len(refreshes) == 3:
            started.clear()           # counted from just after the third refresh ...
        if len(refreshes) == 5:
            raise _Done               # ... across the next two
        clock[0] += watch.REFRESH_SECONDS
        return -1

    class Screen:
        getch = staticmethod(no_key)

        def getmaxyx(self):
            return 24, 40

        def __getattr__(self, name):   # drawing: nothing to see
            return lambda *a: None

    try:
        watch._loop(Screen(), str(main), sidebar=True, logo=sidebar_logo.Braid((0, 0, 0), (8, 16)))
    except _Done:
        pass
    assert len(refreshes) == 5
    assert pilots[-1]["milestones"][0]["status"] == "waiting"   # the worker is in flight
    assert started == []
