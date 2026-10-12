"""Selecting and copying chat text (issue #12): pane-scoped mouse copy to the
system clipboard, the sidebar hide/show toggle, and the bottom layout."""

import uuid

import pytest

from brindle import tmux, watch


@pytest.fixture
def session(tmp_path):
    name = f"sel-{uuid.uuid4().hex[:6]}"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_clipboard_command(monkeypatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda n: f"/bin/{n}" if n in ("wl-copy", "xclip") else None)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert tmux.clipboard_command() == "wl-copy"
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert tmux.clipboard_command() == "xclip -selection clipboard -i"
    monkeypatch.setattr(tmux.shutil, "which", lambda n: None)
    assert tmux.clipboard_command() is None


def _zoomed(window: str) -> str:
    return tmux._tmux("display-message", "-p", "-t", window, "#{window_zoomed_flag}").stdout.strip()


def test_toggle_sidebar_hides_and_restores(session, tmp_path):
    chat = tmux.new_window(session, "chat", str(tmp_path), ["sleep", "60"], {})
    side = tmux.split_left(chat, str(tmp_path), ["sleep", "60"], {})
    window = tmux.pane_window(side)
    tmux._tmux("select-pane", "-t", side)  # focus is in the sidebar, as when h is pressed
    tmux.toggle_sidebar(side)
    assert _zoomed(window) == "1"
    assert tmux._tmux("display-message", "-p", "-t", window, "#{pane_id}").stdout.strip() == chat
    tmux.toggle_sidebar(side)
    assert _zoomed(window) == "0"
    assert tmux.window_alive(side)  # still running the whole time


def test_bottom_layout_puts_sidebar_under_the_chat(session, tmp_path):
    chat = tmux.new_window(session, "chat", str(tmp_path), ["sleep", "60"], {})
    side = tmux.split_left(chat, str(tmp_path), ["sleep", "60"], {}, columns=30, position="bottom")

    def geom(p):
        return tuple(int(x) for x in tmux._tmux(
            "display-message", "-p", "-t", p, "#{pane_left} #{pane_top} #{pane_width}").stdout.split())

    (cl, ct, cw), (sl, st, sw) = geom(chat), geom(side)
    assert st > ct and sl == cl and sw == cw


def test_h_key_returns_hide_only_in_the_sidebar():
    state = watch.NavState()
    assert watch.handle_key(state, ord("h"), [], 10, sidebar=True) == "hide"
    assert watch.handle_key(state, ord("h"), [], 10, sidebar=False) is None
