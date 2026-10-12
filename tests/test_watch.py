import sqlite3

from brindle import watch
from brindle.db import DB


def test_empty_state_renders_without_agents():
    lines = watch.render([], now=0)
    assert lines and not any(ln.agent for ln in lines)


def test_ago():
    assert [watch.ago(s) for s in (5, 125, 3720, 90000)] == ["5s", "2m", "1h02m", "1d"]


def test_scrolling_keeps_the_selection_in_view():
    # scroll_into_view(offset, index, visible, total) -> new offset
    assert watch.scroll_into_view(10, index=2, visible=5, total=20) == 2     # up to it
    assert watch.scroll_into_view(0, index=19, visible=5, total=20) == 15    # down, clamped
    assert watch.scroll_into_view(0, index=0, visible=5, total=3) == 0
    # content_layout(total, height, offset) -> (rows, more above, more below)
    assert watch.content_layout(total=11, height=10, offset=0) == (9, False, True)
    # nearest_visible_row(rows, offset, visible) -> index of the row to select
    assert watch.nearest_visible_row([2, 30], offset=10, visible=5) == 0
    assert watch.nearest_visible_row([2, 30], offset=25, visible=5) == 1
    assert watch.nearest_visible_row([], offset=5, visible=10) == 0


def test_old_databases_are_migrated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
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


STYLES = {"accent": 1, "bold": 2, "dim": 3, "coat": 4, "streak": 5}


def test_logo_fits_narrow_panes():
    for w in range(3, 30):
        watch._draw_logo(_Screen(w), w, STYLES)
        watch._draw_compact_logo(_Screen(w), w, STYLES)


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
