import sqlite3

import pytest

from brindle import watch
from brindle.db import DB


def test_background_cull_keeps_one_sweep_in_flight(monkeypatch):
    """One sweep at a time in a sidebar, and one a minute between all the
    sidebars: each used to sweep on its own clock."""
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock

    clock = [2_000_000_000.0]
    monkeypatch.setattr(watch, "time", SimpleNamespace(time=lambda: clock[0]))
    thread = Mock()
    thread.is_alive.return_value = True
    monkeypatch.setattr(threading, "Thread", lambda **kwargs: thread)
    monkeypatch.setattr(watch, "_cull_thread", None, raising=False)
    watch._cull_in_background()
    watch._cull_in_background()
    assert thread.start.call_count == 1
    thread.is_alive.return_value = False
    clock[0] += 30                        # another sidebar's minute is up: this one's sweep counts
    monkeypatch.setattr(watch, "_cull_thread", None)
    watch._cull_in_background()
    assert thread.start.call_count == 1
    clock[0] += watch.CULL_SECONDS - 30
    watch._cull_in_background()
    assert thread.start.call_count == 2


@pytest.fixture(scope="module")
def four_sessions(tmp_path_factory):
    """A repo's main checkout and three session worktrees, each with a
    tracked file below its root; built once, with its own git identity.
    s1 has a build tree a tracked src/.gitignore ignores and empty nested
    folders, s2 a commit its upstream (``upstream/review``, on another
    remote) lacks and a log file ignored by the excludes file (a symlink)
    that a config included by an escaped path names, s3 a v4 index."""
    from brindle import git

    base = tmp_path_factory.mktemp("sessions")
    with pytest.MonkeyPatch.context() as mp:
        for role in ("AUTHOR", "COMMITTER"):
            mp.setenv(f"GIT_{role}_NAME", "t")
            mp.setenv(f"GIT_{role}_EMAIL", "t@example.com")
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            mp.delenv(k, raising=False)
        main = base / "proj"
        (main / "src").mkdir(parents=True)
        (main / "src" / "mod.py").write_text("x = 1\n")
        (main / "src" / ".gitignore").write_text("build/\n")
        git.run(["init", "-q", "-b", "main"], main)
        git.run(["add", "-A"], main)
        git.run(["commit", "-qm", "init"], main)
        (base / "excludes.target").write_text("*.log\n")
        (base / "excludes").symlink_to(base / "excludes.target")
        included = base / 'extra "q".gitconfig'       # git writes its path escaped
        included.write_text(f"[core]\n\texcludesFile = {base / 'excludes'}\n")
        git.run(["config", "include.path", str(included)], main)
        for i in (1, 2, 3):
            git.run(["worktree", "add", "-q", "-b", f"brindle/session-{i}", str(base / f"s{i}")], main)
        (base / "s1" / "src" / "build" / "out").mkdir(parents=True)
        (base / "s1" / "src" / "build" / "out" / "app.bin").write_text("built\n")
        (base / "s1" / "empty" / "nested" / "deep").mkdir(parents=True)
        s2 = base / "s2"
        (s2 / "src" / "two.py").write_text("y = 1\n")
        (s2 / "src" / "logs").mkdir()
        (s2 / "src" / "logs" / "debug.log").write_text("log\n")
        git.run(["add", "-A"], s2)
        git.run(["commit", "-qm", "two"], s2)
        for key, value in (("remote.upstream.url", str(base / "elsewhere.git")),
                           ("remote.upstream.fetch", "+refs/heads/*:refs/remotes/upstream/*"),
                           ("branch.brindle/session-2.remote", "upstream"),
                           ("branch.brindle/session-2.merge", "refs/heads/review")):
            git.run(["config", key, value], s2)
        git.run(["update-ref", "refs/remotes/upstream/review", "HEAD~1"], s2)
        git.run(["update-index", "--index-version", "4"], base / "s3")
    return [main, *(base / f"s{i}" for i in (1, 2, 3))]


def _sidebar(db, paths, monkeypatch):
    """The sessions in ``db``, a fake clock for the view, and a list every
    process brindle starts is recorded in."""
    from types import SimpleNamespace

    from brindle import agents, spawn, status_cache, tmux, view
    from brindle.db import Agent, Workspace

    root = str(paths[0])
    for i, path in enumerate(paths):
        kind, base = ("main", None) if i == 0 else ("worktree", "main")
        db.add_workspace(Workspace(f"w{i}", root, f"w{i}", kind, f"b{i}", base, str(path), None, "", 0))
        db.add_agent(Agent(f"lead{i}", f"w{i}", "supervisor", "claude", None, "interactive", "idle",
                           f"%{i}", None, 0))
    panes = tmux.PaneSnapshot(tmux.current_server())
    panes.update({f"%{i}": True for i in range(len(paths))})
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db, a, samples=1: a)
    clock = [2_000_000_000.0]
    monkeypatch.setattr(view, "time", SimpleNamespace(time=lambda: clock[0]))
    status_cache.clear()
    started: list = []
    guard = spawn._guard
    monkeypatch.setattr(spawn, "_guard", lambda argv: (started.append(argv), guard(argv))[1])

    def refresh():
        clock[0] += watch.IDLE_SECONDS
        return {ws["id"]: ws for ws in view.snapshot(db, root, panes=panes)}
    return refresh, started


def test_an_idle_sidebar_starts_no_git_until_something_changes(db, four_sessions, monkeypatch):
    """Forty idle minutes of refreshes (eight safety looks) of four quiet
    sessions, counted from just after the sidebar's first look, start no
    process: the safety look used to run git status and rev-list in every
    worktree, in every sidebar. What git would see still shows: an upstream
    on another remote moving at once; at the next safety look, a file new in
    an old empty folder deep down, an edit below the root in a v4 index
    (which compresses its paths), a folder no longer ignored and a file new
    in it afterwards, and a file no longer ignored once the excludes file is
    edited through its symlink, though the first look couldn't learn which
    files those are (git busy). The global config is /dev/null, which
    every process writing to it touches: still nothing to ask git."""
    import os
    from pathlib import Path

    from brindle import git, status_cache

    s1, s2, s3 = four_sessions[1:]
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    refresh, started = _sidebar(db, four_sessions, monkeypatch)
    first = refresh()
    started.clear()
    for _ in range(int(2400 / watch.IDLE_SECONDS)):
        Path(os.devnull).write_text("noise\n")     # another process's output, thrown away
        shown = refresh()
    assert started == []
    assert shown == first
    assert status_cache.cached_status(s2, "main", now=0).unpushed == 1

    def safety_look():
        for _ in range(int(status_cache.TTL / watch.IDLE_SECONDS)):
            shown = refresh()
        return [shown[f"w{i}"]["dirty"] for i in (1, 2, 3)]

    new, edited = s1 / "empty" / "nested" / "deep" / "new.txt", s3 / "src" / "mod.py"
    rules, built = s1 / "src" / ".gitignore", s1 / "src" / "build" / "out" / "two.bin"
    excludes = s1.parent / "excludes"
    try:
        git.run(["update-ref", "refs/remotes/upstream/review", "HEAD"], s2)   # a fetch, say
        refresh()
        assert status_cache.cached_status(s2, "main", now=0).unpushed == 0
        new.write_text("new\n")
        edited.write_text("x = 2\n")                 # the roots and their entries are untouched
        assert safety_look() == [1, 0, 1]
        rules.write_text("")                         # src/build/ is no longer ignored ...
        assert safety_look()[0] == 3                 # ... (.gitignore, build/out/app.bin)
        built.write_text("built\n")                  # ... and a file new in it shows too
        assert safety_look()[0] == 4
        status_cache.clear()                         # a sidebar opening while git is busy for s2's config
        busy, run = [True], git.run

        def busy_once(args, cwd, *a, **k):
            if args[:2] == ["config", "--list"] and str(cwd) == str(s2) and busy[0]:
                busy[0] = False
                raise git.GitBusy("no room")
            return run(args, cwd, *a, **k)
        monkeypatch.setattr(git, "run", busy_once)
        refresh()
        excludes.write_text("")
        assert safety_look()[1] == 1                 # src/logs/debug.log: git looked, no list being known
        started.clear()
        assert safety_look()[1] == 1 and started == []   # the list known again: a quiet look asks nothing
    finally:
        for gone in (new, built):
            gone.unlink(missing_ok=True)
        edited.write_text("x = 1\n")
        rules.write_text("build/\n")
        excludes.write_text("*.log\n")
        git.run(["update-ref", "refs/remotes/upstream/review", "HEAD~1"], s2)


@pytest.mark.parametrize("newer_schema", [pytest.param(False, marks=pytest.mark.variant(
    "a legacy agents table must migrate without losing rows and remain writable after reopening")), True],
    ids=["older", "newer"])
def test_old_databases_are_migrated(tmp_path, newer_schema):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    if newer_schema:
        con.execute("PRAGMA user_version=2147483647")
        with pytest.raises(RuntimeError):
            DB(str(path))
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2147483647
        assert con.execute("SELECT name FROM sqlite_master").fetchall() == []
        con.close()
        return
    con.executescript("""
        CREATE TABLE agents (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, profile TEXT NOT NULL,
          provider TEXT NOT NULL, parent_id TEXT, mode TEXT NOT NULL, status TEXT NOT NULL,
          tmux_window TEXT NOT NULL, result TEXT, created_at REAL NOT NULL);
        INSERT INTO agents VALUES ('a','w','p','claude',NULL,'assign','idle','@1',NULL,7.0);
    """)
    con.commit(); con.close()
    db = DB(str(path))
    a = db.get_agent("a")
    assert a.status_since is None and a.status == "idle"
    db.set_status("a", "processing")
    assert db.get_agent("a").status_since is not None
    db.conn.close()
    reopened = DB(str(path))
    assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] > 0
    assert reopened.get_agent("a").status == "processing"
    reopened.conn.close()


class _Screen:
    """Records what the header draws; like curses, refuses to write past the right edge."""

    def __init__(self, w: int):
        self.w, self.cells = w, {}

    def addstr(self, y, x, s, attr=0):
        assert x + len(s) <= self.w, (x, s, self.w)
        for i, ch in enumerate(s):
            self.cells[(y, x + i)] = (ch, attr)

    def addnstr(self, y, x, s, n, attr=0):
        self.addstr(y, x, s[:max(0, n)], attr)

    def row(self, y):
        return "".join(self.cells.get((y, x), (" ", 0))[0] for x in range(self.w)).rstrip()


STYLES = {"accent": 1, "bold": 2, "dim": 3, "strand_o": 4, "strand_p": 5,
          "strand_g": 6, "strand_b": 7}


def test_header_leaves_the_braid_cells_to_the_image_or_draws_the_lens():
    # No sixel (braid=0), or too narrow for the name beside the image: the lens,
    # in the same three rows, with the name beside it.
    for w, braid in ((30, 0), (14, 8)):
        screen = _Screen(w)
        assert watch._draw_logo(screen, w, STYLES, braid) == watch._header_rows(18, w, braid) == 4
        assert "╭" in screen.row(0) and "┿" in screen.row(1) and "╰" in screen.row(2)
    screen = _Screen(30)
    watch._draw_logo(screen, 30, STYLES, 0)
    assert screen.row(1).index("brindle") == screen.row(2).index("v") == 10
    screen = _Screen(30)
    assert watch._draw_logo(screen, 30, STYLES, braid=8) == watch._header_rows(18, 30, 8) == 4
    assert not [cell for cell in screen.cells if cell[0] < 3 and cell[1] <= 8]
    assert screen.row(1).index("brindle") == screen.row(2).index("v") == 10
    assert screen.row(3).lstrip().startswith("─")


def test_errors_go_to_a_log_file_not_over_the_screen(brindle_home, capsys):
    import logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        watch._log_to_file()
        try:
            raise TypeError("boom")
        except TypeError:
            logging.getLogger("brindle.view").exception("brindle: couldn't read usage")
        for h in root.handlers:
            h.flush()
    finally:
        for h in root.handlers[len(handlers):]:
            h.close()
        root.handlers[:], root.level = handlers, level
    assert "TypeError: boom" in (brindle_home / "watch.log").read_text()
    assert "boom" not in capsys.readouterr().err


def test_idle_sidebar_ticks_do_no_work(brindle_home, monkeypatch):
    """An idle minute (30 ticks 2 s apart, a minute rollover, one outside commit) skips quiet ticks."""
    import collections
    import time as t
    from types import SimpleNamespace

    from brindle import watch_costs
    n, now, seen, db = collections.Counter(), [1_700_000_000 - 1_700_000_000 % 60 + 31.0], [], DB()
    hit = lambda name, result=None: lambda *a, **k: (n.update([name]), result and result())[1]  # noqa: E731
    class Scr:
        timeout = clear = lambda *a: None
        getmaxyx = lambda self: (30, 60)  # noqa: E731

        def getch(self):
            seen.append(n["snapshot"])
            now[0] += watch.REFRESH_SECONDS
            if len(seen) == 3:  # another process commits
                with sqlite3.connect(db.path) as other:
                    other.execute("CREATE TABLE poke (x)")
            return ord("q") if len(seen) > 30 else -1

    for target, value in {
        "curses.curs_set": hit("x"), "curses.mousemask": hit("x"), "brindle.watch._styles": lambda bg=None: {},
        "brindle.watch._start_status": lambda: None, "brindle.watch._cull_in_background": hit("x"),
        "brindle.watch.DB": hit("db", lambda: db), "brindle.watch.render": hit("render", list),
        "brindle.watch_costs.local": hit("spend", watch_costs.Local),
        "brindle.upgrade.InstallWatch": lambda: SimpleNamespace(changed=lambda: False),
        "brindle.tmux.list_panes": hit("panes", dict), "brindle.view.snapshot": hit("snapshot", list),
        "brindle.view.autopilot_entry": hit("x"), "brindle.sidebar_logo.viewer": hit("viewer", str),
        "brindle.watch._paint": hit("paint"),
        "brindle.watch.time": SimpleNamespace(time=lambda: now[0], localtime=t.localtime,
                                              strftime=lambda f, s=None: t.strftime(f, s or t.localtime(now[0]))),
    }.items():
        monkeypatch.setattr(target, value)
    watch._loop(Scr(), None)
    assert n["db"] == 1 and n["paint"] == 2  # one open; first frame + minute rollover
    assert max(n["snapshot"], n["spend"], n["render"], n["viewer"]) <= 8  # each was 31 before
    assert seen[2] == seen[1] and seen[3] == seen[2] + 1  # idle: nothing; the commit: a refresh
