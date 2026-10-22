"""Finding your way around the sidebar: the `?` key list, rows that need you
(and `n` to reach them), folding workspaces, `/` to filter, a selection that
follows its agent, and text that fits a 30-column pane. Everything here runs
against ``render`` and ``handle_key``, so no curses screen is needed."""

import collections
import curses
import time
import types

from brindle import autopilot, view, watch, workspaces
from brindle.db import Agent

# The sidebar pane is 30 columns; text starts after the selection bar and
# stops one short of the edge (see watch._loop).
WIDTH = 28


def ws(agents, id="repo/feat", branch="feat", **kw):
    return {"id": id, "name": branch, "branch": branch, "base_branch": "main", "path": "/",
            "ahead": 0, "behind": 0, "dirty": 0, "agents": agents, **kw}


def agent(id, status="idle", profile="developer", **kw):
    return {"id": id, "profile": profile, "provider": "claude", "status": status,
            "mode": "assign", "status_since": 1000.0, "pending": 0, "reported": False,
            "window": "@1", **kw}


def press(state, snap, *keys, sidebar=True, visible=40):
    """Press ``keys`` in order, re-rendering between them like the loop
    does. Returns the last action and the lines it leaves on screen."""
    action = None
    for key in keys:
        lines = watch.render(snap, 1090, WIDTH, state=state)
        action = handle(state, key, lines, visible, sidebar)
    return action, watch.render(snap, 1090, WIDTH, state=state)


def handle(state, key, lines, visible, sidebar):
    return watch.handle_key(state, ord(key) if isinstance(key, str) else key, lines, visible,
                            sidebar)


def selected(state, lines):
    i = watch.selection(state, lines)
    return None if i is None else lines[i]


def selected_agent(state, lines):
    ln = selected(state, lines)
    return ln.agent["id"] if ln and ln.agent else None


# -- 1. `?` and the key hint -------------------------------------------------


def test_question_mark_toggles_the_help_and_any_key_closes_it():
    state = watch.NavState()
    snap = [ws([agent("a1")])]
    press(state, snap, "?")
    assert state.help
    # A key pressed while the help is up only closes it: `q` doesn't quit.
    action, _ = press(state, snap, "q")
    assert not state.help and action is None
    press(state, snap, "?", "?")
    assert not state.help


def test_footer_hint_fits_and_points_at_the_help():
    hint = watch.footer(watch.NavState(), WIDTH)
    assert hint.style == "dim" and hint.text.endswith("? keys") and len(hint.text) <= WIDTH
    assert watch.footer(watch.NavState(), 8).text == "? keys"
    assert watch.footer(watch.NavState(), 3) is None


# -- 2. rows that need you ---------------------------------------------------


def test_under_autopilot_a_report_is_quiet_unpinned_and_skipped_by_n():
    snap = [ws([agent("busy01", "processing"), agent("done01", reported=True),
                agent("wait01", "waiting")], id="w1", autopilot=True),
            ws([agent("sup001", mode="interactive")], id="w0", branch="main", autopilot=True,
               asking="sup001")]
    lines = watch.render(snap, 1090, WIDTH)
    order = [ln.agent["id"] for ln in lines if ln.agent]
    assert order[:3] == ["wait01", "busy01", "done01"]  # the report keeps its place
    done = next(ln for ln in lines if ln.agent and ln.agent["id"] == "done01")
    assert done.text.startswith("  ◇ ") and done.style == "dim" and not done.needs
    sup = next(ln for ln in lines if ln.agent and ln.agent["id"] == "sup001")
    assert sup.text.startswith("  ◆ ") and sup.needs
    state = watch.NavState()
    seen = [selected_agent(state, press(state, snap, "n")[1]) for _ in range(3)]
    assert seen == ["sup001", "wait01", "sup001"]


def test_n_lands_on_a_folded_group_hiding_one_and_says_when_nothing_needs_you():
    snap = [ws([agent("a1")], id="w1", branch="one"),
            ws([agent("w2", "waiting")], id="w2", branch="two")]
    state = watch.NavState(collapsed={"w2"})
    _, lines = press(state, snap, "n")
    assert selected(state, lines).group == "w2"

    quiet = watch.NavState()
    press(quiet, [ws([agent("a1")])], "n")
    assert quiet.notice == "nothing needs you"


# -- 3. groups ----------------------------------------------------------------


def test_enter_on_a_header_folds_it_and_on_an_agent_attaches():
    snap = [ws([agent("a1")], id="w1")]
    state = watch.NavState()
    assert press(state, snap, 10)[0] == "attach"
    press(state, snap, curses.KEY_UP)
    action, _ = press(state, snap, 10)
    assert action is None and state.collapsed == {"w1"}
    # p and x do nothing on a header.
    assert press(state, snap, "p")[0] is None and press(state, snap, "x")[0] is None


# -- 4. `/` filter ------------------------------------------------------------


def test_slash_filters_by_branch_agent_or_profile_as_you_type():
    snap = [ws([agent("a1b2c3", profile="developer")], id="w1", branch="feat/login"),
            ws([agent("d4e5f6", profile="reviewer")], id="w2", branch="fix/crash")]
    state = watch.NavState()
    _, lines = press(state, snap, "/", "l", "o", "g")
    assert state.filtering and state.filter == "log"
    assert [ln.group for ln in lines if ln.group] == ["w1"]

    _, lines = press(state, snap, *[curses.KEY_BACKSPACE] * 3, *"review")
    assert [ln.agent["id"] for ln in lines if ln.agent] == ["d4e5f6"]

    _, lines = press(state, snap, *[127] * 6, *"a1b2")
    assert [ln.agent["id"] for ln in lines if ln.agent] == ["a1b2c3"]

    _, lines = press(state, snap, *[127] * 4, *"zzz")
    assert "Nothing matches" in lines[-1].text


def test_enter_keeps_the_filter_and_esc_clears_it_without_quitting():
    snap = [ws([agent("a1")], id="w1", branch="one"), ws([agent("b1")], id="w2", branch="two")]
    state = watch.NavState()
    press(state, snap, "/", *"two", 10)
    assert not state.filtering and state.filter == "two"
    assert watch.footer(state, WIDTH).text.startswith("/two")
    action, lines = press(state, snap, 27)
    assert action is None and state.filter == ""
    assert len([ln for ln in lines if ln.group]) == 2
    # With no filter left, Esc still doesn't quit the sidebar; only q does.
    assert press(state, snap, 27)[0] is None
    assert press(state, snap, "q")[0] == "quit"


def test_esc_while_typing_clears_the_filter():
    state = watch.NavState()
    action, _ = press(state, [ws([agent("a1")])], "/", *"ab", 27)
    assert action is None and state.filter == "" and not state.filtering


# -- 5. the selection follows its agent --------------------------------------


def test_a_vanished_agent_hands_the_selection_to_its_neighbour():
    state = watch.NavState()
    snap = [ws([agent("a1"), agent("a2"), agent("a3")])]
    press(state, snap, "j")
    lines = watch.render([ws([agent("a1"), agent("a3")])], 1090, WIDTH, state=state)
    assert selected_agent(state, lines) == "a3"


def test_page_keys_move_the_selection_onto_the_new_page():
    snap = [ws([agent(f"a{i}")], id=f"w{i}", branch=f"b{i}") for i in range(10)]
    state = watch.NavState()
    _, lines = press(state, snap, curses.KEY_NPAGE, visible=8)
    i = watch.selection(state, lines)
    assert state.offset > 0 and state.offset <= i < state.offset + 8
    _, lines = press(state, snap, curses.KEY_HOME, visible=8)
    assert state.offset == 0


# -- 6. fitting 30 columns ----------------------------------------------------


def test_nothing_is_wider_than_the_pane_or_split_mid_word():
    snap = [ws([agent("a1b2c3d4", "waiting", profile="very-long-profile-name-for-a-worker",
                      pending=12, tokens="1.2M tokens · $3.40",
                      subagents=[{"id": "s", "agent_type": "general-purpose-explorer",
                                  "started_at": 1000, "ended_at": None}])],
               branch="feat/some-extremely-long-branch-name", ahead=12, behind=3, dirty=40,
               name="root")]
    for state in (watch.NavState(), watch.NavState(collapsed={"repo/feat"})):
        lines = watch.render(snap, 1090, WIDTH, state=state)
        assert all(len(ln.text) <= WIDTH for ln in lines), [ln.text for ln in lines if len(ln.text) > WIDTH]
    # The `?` overlay too, with the sidebar's extra keys and the tmux hints.
    full, side = watch.help_lines(WIDTH), watch.help_lines(WIDTH, in_tmux=True, sidebar=True)
    assert len(side) > len(full) and all(len(ln.text) <= WIDTH for ln in side)
    lines = watch.render(snap, 1090, WIDTH)
    words = {w for ln in lines for w in ln.text.split()}
    # Hyphenated words stay whole instead of breaking at a hyphen.
    assert "general-purpose-explorer" in words
    assert all(not w.endswith("-") for w in words)


# -- the review verdict the markers rely on -----------------------------------


def test_snapshot_carries_the_latest_review_verdict(db, repo):
    w = workspaces.create(db, str(repo), "reviewed").workspace
    entry = lambda: next(e for e in view.snapshot(db, str(repo)) if e["id"] == w.id)  # noqa: E731
    assert entry()["review"] is None
    db.add_review(w.id, "abc123", None, False, "needs tests")
    assert entry()["review"] == "changes"
    db.add_review(w.id, "def456", None, True, "lgtm")
    assert entry()["review"] == "approved"


def test_snapshot_carries_the_sessions_autopilot_and_open_question(db, repo):
    w = workspaces.create(db, str(repo), "auto").workspace
    now = time.time()
    db.add_agent(Agent("root01", w.id, "supervisor", "claude", None, "interactive", "idle",
                       "%991", None, now))
    db.add_agent(Agent("work01", w.id, "developer", "claude", "root01", "assign", "idle",
                       "%992", "done", now))
    entry = lambda: next(e for e in view.snapshot(db, str(repo)) if e["id"] == w.id)  # noqa: E731
    assert (entry()["autopilot"], entry()["asking"]) == (False, None)
    db.add_autopilot("root01")
    assert (entry()["autopilot"], entry()["asking"]) == (True, None)
    autopilot.need_user(db, "root01", "Which database?")
    assert (entry()["autopilot"], entry()["asking"]) == (True, "root01")
    autopilot.set_enabled(db, "root01", False)
    assert entry()["autopilot"] is False


def test_watch_wheel_down_scrolls_under_ncurses_mouse_version_1(db, monkeypatch):
    # macOS ncurses 6.0 with mouse version 1: no BUTTON5_PRESSED, and
    # wheel-down is reported as REPORT_MOUSE_POSITION.
    monkeypatch.delattr(curses, "BUTTON5_PRESSED", raising=False)
    monkeypatch.setattr(curses, "BUTTON4_PRESSED", 0x00080000)
    monkeypatch.setattr(curses, "REPORT_MOUSE_POSITION", 0x08000000)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(curses, "mousemask", lambda mask: (mask, 0))
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 1, 5, 0, 0x08000000))
    monkeypatch.setattr(watch, "_styles", lambda *a: collections.defaultdict(int))
    monkeypatch.setattr(watch, "_start_status", lambda: None)
    monkeypatch.setattr(watch, "_cull_in_background", lambda: None)
    monkeypatch.setattr(watch.tmux, "list_panes", lambda: [])
    many = [ws([agent(f"a{i}")], id=f"repo/b{i}", branch=f"b{i}") for i in range(30)]
    monkeypatch.setattr(watch.view, "snapshot", lambda *a, **k: many)
    monkeypatch.setattr(watch.view, "autopilot_entry", lambda *a, **k: None)
    monkeypatch.setattr(watch.watch_costs, "local", lambda *a, **k: watch.watch_costs.Local())
    monkeypatch.setattr(watch.watch_costs, "Feed", lambda root: types.SimpleNamespace(get=lambda: None))
    # A wheel tick must not ask tmux who shows the pane again (a subprocess per
    # tick made scrolling lag): only the first pass looks, and redraws it all.
    viewers = iter(["", "/dev/ttys001"])
    monkeypatch.setattr(watch.sidebar_logo, "viewer", lambda: next(viewers))

    class Screen:
        """A 12-row pane: a wheel-down, then `q`. Records each frame's text."""
        def __init__(self):
            self.keys, self.frames, self.clears = [curses.KEY_MOUSE, ord("q")], [[]], 0
        def getmaxyx(self):
            return 12, 30
        def getch(self):
            self.frames.append([])
            return self.keys.pop(0)
        def addnstr(self, y, x, text, *rest):
            self.frames[-1].append(text)
        def timeout(self, ms): pass
        def erase(self): pass
        def refresh(self): pass
        def clear(self):
            self.clears += 1

    screen = Screen()
    watch._loop(screen, None)
    assert screen.clears == 1
    # The wheel-down scrolls the top section (Costs) off the pane.
    assert "▾ Costs" in screen.frames[0]
    assert "▾ Costs" not in screen.frames[1]


def _quiet_loop(monkeypatch, snapshot):
    """watch._loop with no tmux, poller or costs behind it."""
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(curses, "mousemask", lambda mask: (mask, 0))
    monkeypatch.setattr(watch, "_styles", lambda *a: collections.defaultdict(int))
    monkeypatch.setattr(watch, "_start_status", lambda: None)
    monkeypatch.setattr(watch, "_cull_in_background", lambda: None)
    monkeypatch.setattr(watch.tmux, "list_panes", lambda: [])
    monkeypatch.setattr(watch.view, "snapshot", snapshot)
    monkeypatch.setattr(watch.view, "autopilot_entry", lambda *a, **k: None)
    monkeypatch.setattr(watch.watch_costs, "local", lambda *a, **k: watch.watch_costs.Local())
    monkeypatch.setattr(watch.watch_costs, "Feed", lambda root: types.SimpleNamespace(get=lambda: None))
    monkeypatch.setattr(watch.sidebar_logo, "viewer", lambda: "")


def test_sidebar_survives_a_resize_that_leaves_a_frame_off_screen_and_an_unexpected_error(
        db, brindle_home, monkeypatch):
    # The desktop app resizes the shared window: a frame drawn past the pane's
    # edge got addnwstr() ERR from curses, which ended the sidebar.
    _quiet_loop(monkeypatch, lambda *a, **k: [ws([agent("a1")])])

    class Screen:
        """Raises like curses past its size. Each key moves it to the next size;
        the 24x30 one shrinks to 24x1 two writes into its frame (mid-paint)."""
        def __init__(self):
            self.sizes = [(24, 30), (24, 1), (1, 30), (24, 30), (24, 1)]
            self.keys = [curses.KEY_RESIZE] * 3 + [-1, ord("q")]
            self.refreshed, self.painted, self.writes = [], False, None
        def getmaxyx(self):
            return self.sizes[0]
        def getch(self):
            self.refreshed.append(self.painted)
            self.painted = False
            if self.keys[0] == curses.KEY_RESIZE:
                self.sizes.pop(0)
                self.writes = 2 if len(self.sizes) == 2 else None
            return self.keys.pop(0)
        def addnstr(self, y, x, text, n, *rest):
            if self.writes is not None:
                self.writes -= 1
                if self.writes < 0:
                    self.sizes.pop(0)
                    self.writes = None
            h, w = self.sizes[0]
            if not (0 <= y < h and 0 <= x < w) or n > w - x:
                raise curses.error("addnwstr() returned ERR")
        def timeout(self, ms): pass
        def erase(self): pass
        def refresh(self):
            self.painted = True
        def clear(self): pass

    screen = Screen()
    watch._loop(screen, None)
    # Every frame at a small size completes; the one resized mid-paint is
    # skipped, and the next tick repaints at the new size.
    assert screen.refreshed == [True, True, True, False, True]

    # An unexpected error in a pass is logged and the next pass draws.
    snapshots = iter([KeyError("boom"), [ws([agent("a1")])]])

    def snapshot(*a, **k):
        if isinstance(got := next(snapshots), Exception):
            raise got
        return got
    _quiet_loop(monkeypatch, snapshot)

    class Screen:
        def __init__(self):
            self.keys, self.text = [-1, ord("q")], []
        def getmaxyx(self):
            return 24, 30
        def getch(self):
            return self.keys.pop(0)
        def addnstr(self, y, x, text, *rest):
            self.text.append(text)
        def timeout(self, ms): pass
        def erase(self): pass
        def refresh(self): pass
        def clear(self): pass

    screen = Screen()
    watch._loop(screen, None)
    assert any("feat" in t for t in screen.text)  # the next pass drew the dashboard
    assert "KeyError: 'boom'" in (brindle_home / "sidebar.log").read_text()
