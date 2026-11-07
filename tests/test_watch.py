import sqlite3

import pytest

from brindle import watch
from brindle.db import DB


def test_background_cull_keeps_one_sweep_in_flight(monkeypatch):
    import threading
    from unittest.mock import Mock

    thread = Mock()
    thread.is_alive.return_value = True
    monkeypatch.setattr(threading, "Thread", lambda **kwargs: thread)
    monkeypatch.setattr(watch, "_cull_thread", None, raising=False)
    watch._cull_in_background()
    watch._cull_in_background()
    assert thread.start.call_count == 1
    thread.is_alive.return_value = False
    watch._cull_in_background()
    assert thread.start.call_count == 2


@pytest.mark.parametrize("newer_schema", [False, True], ids=["older", "newer"])
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
