"""Small runtime for the tmux sidebar-follow hook.

Keep this module independent of the CLI and agent launch stack: tmux invokes
it on every window/session change.
"""

from __future__ import annotations

import contextlib
import fcntl
import time

from copse import tmux
from copse.config import copse_home
from copse.db import DB, Agent, Workspace

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
    lock_dir = copse_home() / "locks"
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
    agents_here = db.list_agents(ws.id)
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
        path = copse_home() / "sidebar.log"
        lines = path.read_text().splitlines(keepends=True)[-199:] if path.exists() else []
        path.write_text("".join(lines) + line)
    except Exception:
        pass


def _sidebar_position(ws: Workspace) -> str:
    from copse.config import load_repo_config

    try:
        return "bottom" if load_repo_config(ws.repo_root).sidebar == "bottom" else "left"
    except ValueError:
        return "left"


def _create_sidebar(db: DB, root_id: str, ws: Workspace, target_pane: str) -> str:
    from copse.providers import copse_invocation
    from copse.workspaces import workspace_env

    pane = tmux.split_left(target_pane, ws.path, [*copse_invocation(), "watch", "--sidebar"],
                           workspace_env(ws), columns=SIDEBAR_COLUMNS,
                           position=_sidebar_position(ws))
    tmux.set_pane_tag(pane, SIDEBAR_TAG, root_id)
    db.set_sidebar_pane(root_id, pane)
    return pane


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
            return
        if not tmux.session_attached(session):
            home = tmux.pane_session(sidebar)
            if home and home != session and tmux.session_attached(home):
                return
        target_pane = tmux.agent_pane_in_window(window, sidebar)
        if not target_pane:
            return
        root_ws = db.get_workspace(root.workspace_id)
        _note_sidebar_move(f"follow {session}", sidebar, target_pane)
        tmux.move_pane(sidebar, target_pane, SIDEBAR_COLUMNS,
                       _sidebar_position(root_ws) if root_ws else "left")
