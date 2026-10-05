"""Getting a stranded sidebar back (copse-agents #54): `copse sidebar`,
rescuing it before a worker's session goes away, and the come-home net."""

import time

import pytest
from typer.testing import CliRunner

from copse import agents, doctor, tmux, workspaces
from copse.cli import app
from copse.db import DB, Agent, Workspace
from copse.sidebar_follow import rescue_sidebar, sidebar_here

ROOT_SESSION = "copse_recovertest"
WORKER_SESSION = "copse_recovertest_worker"


@pytest.fixture
def db(copse_home):
    return DB()


def make_workspace(db, tmp_path, name, session):
    path = tmp_path / name
    path.mkdir()
    ws = Workspace(
        id=name, repo_root=str(tmp_path), name=name, kind="worktree", branch=name,
        base_branch="main", path=str(path), port_base=None, tmux_session=session,
        created_at=time.time(),
    )
    db.add_workspace(ws)
    return ws


def fake_agent(db, ws, window, agent_id, parent=None):
    db.add_agent(Agent(agent_id, ws.id, "supervisor", "claude", parent, "interactive", "idle",
                       window, None, time.time()))


def make_window(session, name):
    return tmux._tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:",
                      "-n", name).stdout.strip()


@pytest.fixture
def stranded(db, tmp_path):
    """The root's sidebar sits in a worker's session, nobody attached."""
    tmux.ensure_session(ROOT_SESSION, str(tmp_path), {})
    tmux.ensure_session(WORKER_SESSION, str(tmp_path), {})
    root_win = make_window(ROOT_SESSION, "root")
    worker_win = make_window(WORKER_SESSION, "worker")
    root_ws = make_workspace(db, tmp_path, "rootws", ROOT_SESSION)
    worker_ws = make_workspace(db, tmp_path, "workerws", WORKER_SESSION)
    fake_agent(db, root_ws, root_win, "root1")
    fake_agent(db, worker_ws, worker_win, "w1", parent="root1")
    agents._ensure_sidebar(db, "root1", root_ws, worker_win)
    sidebar = db.get_sidebar_pane("root1")
    assert tmux.pane_session(sidebar) == WORKER_SESSION
    tmux._tmux("select-window", "-t", f"{ROOT_SESSION}:root")
    yield db, root_win, worker_ws, sidebar
    tmux.kill_session(ROOT_SESSION)
    tmux.kill_session(WORKER_SESSION)


def test_sidebar_here_pulls_it_from_another_session(stranded):
    db, root_win, _, sidebar = stranded
    assert sidebar_here(db, ROOT_SESSION) == "sidebar brought here"
    assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)
    assert db.get_sidebar_pane("root1") == sidebar
    assert sidebar_here(db, ROOT_SESSION) == "the sidebar is already here"


def test_sidebar_here_restarts_a_quit_sidebar(stranded):
    db, root_win, _, sidebar = stranded
    agents.dismiss_sidebar(db, sidebar)
    tmux.kill_pane(sidebar)
    sidebar_here(db, ROOT_SESSION)
    new = db.get_sidebar_pane("root1")
    assert new != sidebar and tmux.window_alive(new)
    assert tmux.pane_session(new) == ROOT_SESSION


def test_sidebar_here_rejects_an_unknown_session(db):
    with pytest.raises(ValueError):
        sidebar_here(db, "no-such-session")


def test_sidebar_command_reports_failure(copse_home):
    result = CliRunner().invoke(app, ["sidebar", "--session", "no-such-session"])
    assert result.exit_code != 0
    assert "sidebar" in CliRunner().invoke(app, ["sidebar", "--help"]).output


def test_rescue_moves_the_sidebar_home_before_the_session_dies(stranded):
    db, root_win, worker_ws, sidebar = stranded
    rescue_sidebar(db, worker_ws)
    assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)
    tmux.kill_session(WORKER_SESSION)
    assert tmux.window_alive(sidebar)


def test_removing_a_workspace_rescues_the_sidebar(stranded):
    db, root_win, worker_ws, sidebar = stranded
    worker_ws.kind = "main"  # no real git repo here; this path kills the session just the same
    workspaces.remove(db, worker_ws, force=True)
    assert tmux.window_alive(sidebar)
    assert tmux.pane_session(sidebar) == ROOT_SESSION


def test_come_home_when_attached_to_root_and_sidebar_is_unwatched(stranded, monkeypatch):
    db, root_win, _, sidebar = stranded
    monkeypatch.setattr(tmux, "session_attached", lambda name: name == ROOT_SESSION)
    assert agents.sidebar_come_home(db, "root1", sidebar)
    assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)


def test_come_home_works_even_if_the_root_window_has_no_matching_agent(stranded, monkeypatch):
    """Straight into the attached session: the agent-for-window lookup that
    sidebar_follow does needn't succeed."""
    db, root_win, _, sidebar = stranded
    db.conn.execute("UPDATE agents SET tmux_window = 'gone' WHERE id = 'root1'")
    db.conn.commit()
    monkeypatch.setattr(tmux, "session_attached", lambda name: name == ROOT_SESSION)
    assert agents.sidebar_come_home(db, "root1", sidebar)
    assert tmux.pane_session(sidebar) == ROOT_SESSION


def test_prefix_s_pulls_a_sidebar_that_lives_elsewhere(stranded):
    tmux.bind_session_keys(ROOT_SESSION)
    line = next(ln for ln in tmux._tmux("list-keys", "-T", "prefix").stdout.splitlines()
                if " S " in ln and "copse" in ln)
    assert "sidebar" in line and "--session" in line and "resize-pane -Z" in line


def test_doctor_mentions_the_command(copse_home):
    assert any("copse sidebar" in c.detail for c in doctor.checks(None))
