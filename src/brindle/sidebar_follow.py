"""Small runtime for the tmux sidebar-follow hook.

Keep this module independent of the CLI and agent launch stack: tmux invokes
it on every window/session change.
"""

from __future__ import annotations

import contextlib
import fcntl
import time

from brindle import tmux
from brindle.config import brindle_home
from brindle.db import DB, Agent, Workspace

SIDEBAR_COLUMNS = 30
SIDEBAR_TAG = tmux.SIDEBAR_TAG
# Stored in place of a pane id once the person quits the sidebar themselves;
# sidebar_follow then leaves it gone instead of recreating it.
SIDEBAR_DISMISSED = "dismissed"


def root_of(db: DB, agent_id: str) -> str:
    seen: set[str] = set()
    current = agent_id
    while current not in seen:
        seen.add(current)
        agent = db.get_agent(current)
        if agent is None or agent.parent_id is None:
            return current
        current = agent.parent_id
    return current


@contextlib.contextmanager
def _sidebar_lock(root_id: str, timeout: float | None = None):
    lock_dir = brindle_home() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"sidebar-{root_id}.lock", "w") as f:
        if timeout is None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _agent_for_window(db: DB, ws: Workspace, window: str) -> Agent | None:
    # An old paused root can share the workspace (and a stale window id) with
    # the live one: while any agent here is live, only live ones count.
    agents_here = db.list_agents(ws.id)
    agents_here = [a for a in agents_here if a.status not in ("paused", "done")] or agents_here
    for agent in agents_here:
        if agent.tmux_window and tmux.pane_window(agent.tmux_window) == window:
            return agent
    return agents_here[0] if agents_here else None


def _valid_sidebar(pane: str | None, root_id: str) -> bool:
    return (bool(pane) and pane != SIDEBAR_DISMISSED and tmux.window_alive(pane)
            and tmux.get_pane_tag(pane, SIDEBAR_TAG) == root_id)


def _note_sidebar_move(why: str, pane: str, target_pane: str) -> None:
    try:
        src, dst = tmux.pane_session(pane), tmux.pane_session(target_pane)
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {why}: {src} -> {dst} "
                f"(attached: {src}={tmux.session_attached(src or '')}, "
                f"{dst}={tmux.session_attached(dst or '')})\n")
        path = brindle_home() / "sidebar.log"
        lines = path.read_text().splitlines(keepends=True)[-199:] if path.exists() else []
        path.write_text("".join(lines) + line)
    except Exception:
        pass


def _repaint(pane: str) -> None:
    """Have the sidebar redraw itself in full (Ctrl-L, see watch._loop): after a
    client switches sessions, iTerm2 shows the pane mostly blank and without the
    braid image until the sidebar writes it all out again."""
    with contextlib.suppress(tmux.TmuxError):
        tmux.send_keys(pane, "C-l")


def _sidebar_position(ws: Workspace) -> str:
    from brindle.config import load_repo_config

    try:
        return "bottom" if load_repo_config(ws.repo_root).sidebar == "bottom" else "left"
    except ValueError:
        return "left"


def _create_sidebar(db: DB, root_id: str, ws: Workspace, target_pane: str) -> str:
    from brindle.providers import brindle_invocation
    from brindle.workspaces import workspace_env

    env = workspace_env(ws)
    pane = tmux.split_left(target_pane, ws.path, [*brindle_invocation(), "watch", "--sidebar"],
                           env, columns=SIDEBAR_COLUMNS, position=_sidebar_position(ws))
    tmux.keep_sidebar_alive(pane, ws.path, env, brindle_home() / "sidebar.log")
    tmux.set_pane_tag(pane, SIDEBAR_TAG, root_id)
    db.set_sidebar_pane(root_id, pane)
    return pane


def root_for_session(db: DB, session: str) -> str | None:
    """The session root whose chat or worker is in tmux ``session``'s active
    window (or, failing that, any of its agents)."""
    ws = db.workspace_by_tmux_session(session)
    if ws is None:
        return None
    window = tmux.active_window(session)
    agent = _agent_for_window(db, ws, window) if window else None
    return root_of(db, agent.id) if agent else None


def bring_sidebar(db: DB, root_id: str, session: str, *, revive: bool = False) -> bool:
    """Put ``root_id``'s sidebar in ``session``'s active window, wherever it
    is now and whether or not anyone is attached to either session. With
    ``revive`` (the person asked for it), a sidebar that's dead or was quit
    is created fresh. True when the sidebar ends up there."""
    with _sidebar_lock(root_id):
        window = tmux.active_window(session)
        root = db.get_agent(root_id)
        if not window or root is None:
            return False
        sidebar = db.get_sidebar_pane(root_id)
        valid = _valid_sidebar(sidebar, root_id)
        if valid and tmux.pane_window(sidebar) == window:
            return True
        root_ws = db.get_workspace(root.workspace_id)
        if valid:
            target_pane = tmux.agent_pane_in_window(window, sidebar)
            if not target_pane:
                return False
            _note_sidebar_move(f"bring to {session}", sidebar, target_pane)
            tmux.move_pane(sidebar, target_pane, SIDEBAR_COLUMNS,
                           _sidebar_position(root_ws) if root_ws else "left")
            return True
        if revive and root_ws:
            target_pane = tmux.agent_pane_in_window(window, None)
            if target_pane:
                _create_sidebar(db, root_id, root_ws, target_pane)
                return True
        return False


def sidebar_here(db: DB, session: str) -> str:
    """`brindle sidebar`: bring the sidebar of the root ``session`` belongs to
    into ``session``. Returns what happened; raises ValueError if it can't."""
    root_id = root_for_session(db, session)
    if root_id is None:
        raise ValueError(f"tmux session {session} doesn't belong to a brindle session")
    root = db.get_agent(root_id)
    if root is not None and root.status == "paused":
        raise ValueError("this brindle session is paused: resume it first")
    before = db.get_sidebar_pane(root_id)
    was_there = _valid_sidebar(before, root_id) and tmux.pane_session(before) == session
    if not bring_sidebar(db, root_id, session, revive=True):
        raise ValueError("couldn't place the sidebar here (is this window just the sidebar?)")
    window = tmux.active_window(session)
    if window and tmux.window_zoomed(window):
        # A zoomed window hides the sidebar, so unzoom it for the person to see it.
        tmux.unzoom(window)
        return "unzoomed the window: the sidebar is shown now"
    return "the sidebar is already here" if was_there else "sidebar brought here"


def rescue_sidebar(db: DB, ws: Workspace) -> None:
    """Call before ``ws``'s tmux session goes away: if a root's sidebar lives
    in it, move it back to that root's own session first. Never raises."""
    try:
        for agent in db.list_agents(ws.id):
            root_id = root_of(db, agent.id)
            root = db.get_agent(root_id)
            root_ws = db.get_workspace(root.workspace_id) if root else None
            if root_ws is None or root_ws.tmux_session == ws.tmux_session:
                continue
            sidebar = db.get_sidebar_pane(root_id)
            # If the root's own window is gone, bring_sidebar declines and the
            # sidebar goes down with this session: nowhere left to show it.
            if _valid_sidebar(sidebar, root_id) and tmux.pane_session(sidebar) == ws.tmux_session:
                bring_sidebar(db, root_id, root_ws.tmux_session)
    except Exception:  # noqa: BLE001 - cleanup must not fail because of the sidebar
        pass


def _home_session(db: DB, root_id: str, sidebar: str) -> str | None:
    """The brindle session whose agent window ``sidebar`` sits in. Not
    ``tmux.pane_session``: a window the app linked into its own
    ``brindle-view-*`` session reports that one, and that session being
    attached says nothing about the person's terminal on the brindle session."""
    window = tmux.pane_window(sidebar)
    for agent in db.list_agents():
        if (window and agent.tmux_window and root_of(db, agent.id) == root_id
                and tmux.pane_window(agent.tmux_window) == window):
            ws = db.get_workspace(agent.workspace_id)
            if ws is not None:
                return ws.tmux_session
    return tmux.pane_session(sidebar)


def rescue_sidebar_from_window(db: DB, agent: Agent) -> None:
    """Call before ``agent``'s tmux window is killed: if its root's sidebar is
    a pane in that window, move it to a window of the root's session that
    survives (the root's active one, else any other agent's there: a worker
    sharing the root's workspace has its window in the same session). Never
    raises."""
    try:
        if not agent.tmux_window:
            return
        root_id = root_of(db, agent.id)
        root = db.get_agent(root_id)
        root_ws = db.get_workspace(root.workspace_id) if root else None
        if root is None or root_ws is None or agent.id == root_id:
            return
        with _sidebar_lock(root_id):
            sidebar = db.get_sidebar_pane(root_id)
            doomed = tmux.pane_window(agent.tmux_window)
            if not (_valid_sidebar(sidebar, root_id) and doomed
                    and tmux.pane_window(sidebar) == doomed):
                return
            candidates = [tmux.active_window(root_ws.tmux_session)]
            candidates += [tmux.pane_window(a.tmux_window) for a in db.list_agents(root_ws.id)
                           if a.tmux_window and a.status not in ("paused", "done")]
            for window in candidates:
                target_pane = window and window != doomed and tmux.agent_pane_in_window(window, sidebar)
                if target_pane:
                    _note_sidebar_move(f"rescue to {root_ws.tmux_session}", sidebar, target_pane)
                    tmux.move_pane(sidebar, target_pane, SIDEBAR_COLUMNS, _sidebar_position(root_ws))
                    return
    except Exception:  # noqa: BLE001 - cleanup must not fail because of the sidebar
        pass


def sidebar_follow(db: DB, session: str) -> None:
    ws = db.workspace_by_tmux_session(session)
    if ws is None:
        return
    window = tmux.active_window(session)
    if not window:
        return
    agent = _agent_for_window(db, ws, window)
    if agent is None:
        return
    root_id = root_of(db, agent.id)

    # The common path needs no lock or agent/root queries: validate the tag
    # and window in one tmux display-message call and leave the pane alone.
    # Skipping a paused-root check here is harmless: no pane is moved.
    sidebar = db.get_sidebar_pane(root_id)
    if sidebar and sidebar != SIDEBAR_DISMISSED:
        pane_window, pane_tag = tmux.pane_window_tag(sidebar, SIDEBAR_TAG)
        if pane_window == window and pane_tag == root_id:
            _repaint(sidebar)
            return

    with _sidebar_lock(root_id):
        window = tmux.active_window(session)
        if not window:
            return
        agent = _agent_for_window(db, ws, window)
        if agent is None or root_of(db, agent.id) != root_id:
            return
        root = db.get_agent(root_id)
        if root is None or root.status == "paused":
            return
        sidebar = db.get_sidebar_pane(root_id)
        if sidebar is None or sidebar == SIDEBAR_DISMISSED:
            return
        if not _valid_sidebar(sidebar, root_id):
            target_pane = tmux.agent_pane_in_window(window, None)
            root_ws = db.get_workspace(root.workspace_id)
            if target_pane and root_ws:
                _create_sidebar(db, root_id, root_ws, target_pane)
            return
        if tmux.pane_window(sidebar) == window:
            _repaint(sidebar)
            return
        # Never take it out of another session someone still looks at: the
        # person's own switch-client leaves that session first, so it reads
        # unattached by now. A second client (the app's, attached to a
        # worker's session) changing windows must not pull it from their terminal.
        home = _home_session(db, root_id, sidebar)
        if home and home != session and tmux.session_attached(home):
            return
        target_pane = tmux.agent_pane_in_window(window, sidebar)
        if not target_pane:
            return
        root_ws = db.get_workspace(root.workspace_id)
        _note_sidebar_move(f"follow {session}", sidebar, target_pane)
        tmux.move_pane(sidebar, target_pane, SIDEBAR_COLUMNS,
                       _sidebar_position(root_ws) if root_ws else "left")
        _repaint(sidebar)
