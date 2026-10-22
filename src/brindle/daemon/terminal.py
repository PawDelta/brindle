"""The daemon's action terminals: a sign-in, ``brindle keys set``, run in a
window of its own tmux session (see :func:`open_action`) for the app to show.
The app attaches its own tmux client to the pane, in a pty, as it does an
agent's window; the daemon only says where it is and types a line the person
gave the app into it (:func:`send_input`).

:func:`reset_window_sizes` undoes, once, what the daemon's old streamed
terminals left behind: brindle windows set to ``window-size manual``."""

from __future__ import annotations

import logging
import re
import secrets
import threading
from pathlib import Path

from brindle import tmux
from brindle.service.dto import ActionTerminalDTO

log = logging.getLogger(__name__)

ACTION_SESSION = "brindle-app"  # the daemon's own tmux session: the app's action terminals
ACTION_TAG = "@brindle_terminal"
ACTION_ID = re.compile(r"t-[0-9a-f]{16}")
WINDOW_SIZE_RESET = "window-size-reset"     # under brindle's home: reset_window_sizes has run
_opening = threading.Lock()


def open_action(kind: str, command: list[str], cwd: str | None = None,
                env: dict[str, str] | None = None, restart: bool = False) -> dict:
    """Run ``command`` in a new window (named ``kind``: the action and what
    it acts on, e.g. "login codex") of the daemon's own tmux session
    (ACTION_SESSION) and return its ActionTerminalDTO: the id POST
    /terminals/{id}/input takes, and the pane (``target``) and tmux
    ``server`` the app's tmux client attaches to. While a window for ``kind``
    is still running, a second press gets that one (``started`` False)
    rather than a second sign-in or upgrade racing it; once it has exited, a
    new one starts. ``restart`` ends that running window (brindle's own,
    never keys typed into it) and starts ``command`` afresh. The person types
    into it there (a sign-in, a secret):
    nothing they type passes through the API. The pane stays after the
    command exits, so its last words can be read."""
    with _opening:      # find-or-create as one step: two presses at once start one
        for pane, name, alive, terminal_id in tmux.tagged_panes(ACTION_SESSION, ACTION_TAG):
            if alive and name == kind:
                if not restart:
                    return _action(terminal_id, False, pane)
                tmux.kill_window(pane)
                break
        terminal_id = "t-" + secrets.token_hex(8)
        cwd = cwd or str(Path.home())
        tmux.ensure_session(ACTION_SESSION, cwd, {})
        pane = tmux.new_window(ACTION_SESSION, kind, cwd, command, env or {}, tag=(ACTION_TAG, terminal_id))
        return _action(terminal_id, True, pane)


def _action(terminal_id: str, started: bool, pane: str) -> dict:
    return ActionTerminalDTO(terminal=terminal_id, started=started, target=pane,
                             server=tmux.current_server()).to_json()


def _running_action(terminal_id: str) -> str | None:
    """The pane of the action terminal ``terminal_id``, while it runs."""
    for pane, _, alive, tag in tmux.tagged_panes(ACTION_SESSION, ACTION_TAG):
        if tag == terminal_id:
            return pane if alive else None
    return None


def send_input(terminal_id: str, text: str, submit: bool = True) -> bool:
    """Type ``text`` (one line, as is) into the action terminal ``terminal_id``
    and press Enter unless ``submit`` is False. False when there is no such
    terminal still running. Only action terminals: an agent's pane is never
    typed into this way."""
    if not ACTION_ID.fullmatch(terminal_id):
        return False
    pane = _running_action(terminal_id)
    if pane is None:
        return False
    tmux.send_keys(pane, "-l", "--", text)
    if submit:
        tmux.send_keys(pane, "Enter")
    return True


def reset_window_sizes() -> int:
    """Once (WINDOW_SIZE_RESET marks it done): give tmux back the size of each
    brindle window the old daemon left at ``window-size manual`` when it
    streamed it, so the terminals showing it size it again. Only brindle's
    own panes (tagged as an agent's or an action terminal's, on any tmux
    server brindle's agents use) and only the option set on that window,
    never a session's or the global one. Returns how many it reset. It is
    marked done only once every such window was read and reset: any tmux
    call that fails (a server that can't be asked, a refused command) leaves
    it to the next daemon start."""
    from brindle import agents
    from brindle.config import brindle_home
    from brindle.db import DB

    marker = brindle_home() / WINDOW_SIZE_RESET
    if marker.exists():
        return 0
    db = DB()
    try:
        servers = {agents.server_of(a) for a in db.list_agents() if a.tmux_window}
    finally:
        db.conn.close()
    reset = 0
    try:
        targets = [(pane, server) for server in {tmux.current_server(), *servers}
                   for pane in tmux.tagged_pane_ids((tmux.AGENT_TAG, ACTION_TAG), server=server)]
        for pane, server in targets:
            if tmux.window_size_option(pane, server=server) == "manual":
                tmux.unset_window_size(pane, server=server)
                reset += 1
    except tmux.TmuxError:
        log.debug("brindle: can't reset window sizes yet", exc_info=True)
        return reset
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    return reset
