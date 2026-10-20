"""The one sidebar pane: following the person between brindle windows and
sessions, coming back after it disappears without the person asking it to,
and getting a stranded one back (see agents.sidebar_follow,
agents._ensure_sidebar, agents.dismiss_sidebar, agents._sidebar_lock,
tmux.move_pane, `brindle sidebar`).

Real tmux on the suite's private socket (see conftest.py), since the
behaviour under test is genuine tmux pane/hook plumbing.
"""

import fcntl
import threading
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, tmux, workspaces
from brindle.cli import app
from brindle.config import brindle_home
from brindle.db import Agent, Workspace
from brindle.sidebar_follow import sidebar_here

ROOT_SESSION = "brindle_recovertest"
WORKER_SESSION = "brindle_recovertest_worker"


def make_workspace(db, tmp_path, name, session, repo_root=None):
    path = tmp_path / name
    path.mkdir()
    ws = Workspace(
        id=name, repo_root=repo_root or str(tmp_path), name=name, kind="worktree",
        branch=name, base_branch="main", path=str(path), port_base=None,
        tmux_session=session, created_at=time.time(),
    )
    db.add_workspace(ws)
    return ws


def fake_agent(db, ws, window, agent_id, parent=None, mode="interactive", status="idle"):
    a = Agent(agent_id, ws.id, "supervisor", "claude", parent, mode, status, window, None, time.time())
    db.add_agent(a)
    return a


def window_panes(session, window):
    return tmux._tmux("list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}").stdout.split()


def make_window(session, name):
    """A window with one plain shell pane, standing in for an agent's pane."""
    proc = tmux._tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:", "-n", name)
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def inert_sidebar(monkeypatch):
    """The sidebar pane runs a stand-in, not `brindle watch --sidebar`: the
    real dashboard's cull sweep runs outside this process's fakes, finds
    Claude signed out under the temporary config dir and pauses the fake
    workers, closing their windows (and a sidebar in one) mid-test."""
    from brindle import providers

    monkeypatch.setattr(providers, "brindle_invocation", lambda: ["sh", "-c", "exec sleep 3600"])


@pytest.fixture
def session(tmp_path):
    name = "brindle_persisttest"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def wait_dead(pane):
    deadline = time.time() + 5
    while time.time() < deadline and tmux.window_alive(pane):
        time.sleep(0.05)
    assert not tmux.window_alive(pane)


def test_sidebar_killed_with_a_workers_session_comes_back_in_the_root_session(db, tmp_path):
    """The sidebar follows the person into a worker's session; closing that
    session (remove_workspace, a finished worker) takes the sidebar with it.
    Coming back to the supervisor's session must bring it back."""
    root_s, worker_s = "brindle_persist_root", "brindle_persist_worker"
    tmux.ensure_session(root_s, str(tmp_path), {})
    tmux.ensure_session(worker_s, str(tmp_path), {})
    try:
        root_pane = make_window(root_s, "supervisor")
        worker_pane = make_window(worker_s, "worker")
        root_ws = make_workspace(db, tmp_path, "rootws", root_s)
        worker_ws = make_workspace(db, tmp_path, "workerws", worker_s)
        fake_agent(db, root_ws, root_pane, "root1")
        fake_agent(db, worker_ws, worker_pane, "w1", parent="root1", mode="assign")
        tmux._tmux("select-window", "-t", f"{root_s}:supervisor")
        tmux._tmux("select-window", "-t", f"{worker_s}:worker")
        agents._ensure_sidebar(db, "root1", root_ws, root_pane)
        sidebar = db.get_sidebar_pane("root1")

        agents.sidebar_follow(db, worker_s)  # the person switched to the worker
        assert tmux.pane_window(sidebar) == tmux.pane_window(worker_pane)

        tmux.kill_session(worker_s)
        wait_dead(sidebar)
        agents.sidebar_follow(db, root_s)  # ...and back (or re-attached)

        new = db.get_sidebar_pane("root1")
        assert new != sidebar and tmux.window_alive(new)
        assert tmux.pane_window(new) == tmux.pane_window(root_pane)
    finally:
        tmux.kill_session(root_s)
        if tmux.has_session(worker_s):
            tmux.kill_session(worker_s)


def test_a_dismissed_sidebar_stays_gone_until_a_relaunch(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    agents.dismiss_sidebar(db, sidebar)  # the person pressed q
    tmux.kill_pane(sidebar)
    wait_dead(sidebar)
    assert db.get_sidebar_pane("root1") == agents.SIDEBAR_DISMISSED

    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    assert window_panes(session, "winA") == [win_a]

    # `brindle` / `brindle continue` launches it again.
    agents._ensure_sidebar(db, "root1", ws, win_a)
    new = db.get_sidebar_pane("root1")
    assert new != agents.SIDEBAR_DISMISSED and tmux.window_alive(new)
    assert len(window_panes(session, "winA")) == 2


def test_dismiss_ignores_panes_that_are_not_the_recorded_sidebar(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    agents.dismiss_sidebar(db, None)  # a plain `brindle watch` outside tmux
    agents.dismiss_sidebar(db, win_a)  # a plain `brindle watch` in some pane
    # An old sidebar pane still tagged for root1 but since replaced.
    stale = make_window(session, "stale")
    tmux.set_pane_tag(stale, agents.SIDEBAR_TAG, "root1")
    agents.dismiss_sidebar(db, stale)

    assert db.get_sidebar_pane("root1") == sidebar


def test_relaunch_over_a_row_whose_pane_no_longer_exists(db, tmp_path, session):
    """A tmux server restart (or the pane simply gone) leaves a row naming
    a pane id nothing has: both paths treat it as dead, not as present."""
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    db.set_sidebar_pane("root1", "%99999")

    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    first = db.get_sidebar_pane("root1")
    assert first != "%99999" and tmux.window_alive(first)

    agents._ensure_sidebar(db, "root1", ws, win_a)
    assert db.get_sidebar_pane("root1") == first
    assert len(window_panes(session, "winA")) == 2


def test_follow_moves_a_sidebar_left_alone_in_its_window(db, tmp_path, session):
    """Whatever it sat beside exited (e.g. `exit` in the shell window), so
    the sidebar is the only pane there: switching away must still bring it
    along rather than leave it stranded out of sight."""
    win_a = make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    fake_agent(db, ws, win_b, "w1", parent="root1", mode="assign")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")
    tmux.kill_pane(win_a)
    wait_dead(win_a)

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") == sidebar  # the same process, moved
    assert tmux.pane_window(sidebar) == tmux.pane_window(win_b)
    assert "winA" not in tmux.windows(session)


def _hold_lock(root_id, seconds, held):
    lock = brindle_home() / "locks" / f"sidebar-{root_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        held.set()
        time.sleep(seconds)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def test_launch_lock_wait_is_bounded(db, tmp_path, session, monkeypatch):
    monkeypatch.setattr(agents, "SIDEBAR_LOCK_TIMEOUT", 0.1)
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    held = threading.Event()
    holder = threading.Thread(target=_hold_lock, args=("root1", 1.0, held))
    holder.start()
    held.wait(5)
    try:
        start = time.monotonic()
        with pytest.raises(BlockingIOError):
            agents._ensure_sidebar(db, "root1", ws, win_a)
        assert time.monotonic() - start < 0.9
    finally:
        holder.join()


# -- following the person (serial: real hooks fire across sessions) ----------


@pytest.mark.serial
def test_sidebar_follow_never_creates_one(db, tmp_path, session):
    """Only _open_window (via _ensure_sidebar) creates a root's first
    sidebar; follow only restores one a root already had, so a `--no-watch`
    root never gets one."""
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    tmux._tmux("select-window", "-t", f"{session}:winA")

    agents.sidebar_follow(db, "no-such-session")  # unknown session: no crash
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") is None
    assert len(window_panes(session, "winA")) == 1


@pytest.mark.serial
def test_sidebar_follow_stays_with_the_person_when_an_unwatched_session_changes_window(
        db, tmp_path, session, monkeypatch):
    """A worker's session changes its own active window at launch (its
    placeholder shell window closes once the agent's window is up) and the
    follow hook fires with nobody attached to it. The sidebar must stay in
    the session the person is actually looking at."""
    worker_session = "brindle_worker_followtest"
    tmux.ensure_session(worker_session, str(tmp_path), {})
    try:
        root_win = make_window(session, "root")
        worker_win = make_window(worker_session, "worker")
        root_ws = make_workspace(db, tmp_path, "rootws", session)
        worker_ws = make_workspace(db, tmp_path, "workerws", worker_session)
        fake_agent(db, root_ws, root_win, "root1")
        fake_agent(db, worker_ws, worker_win, "w1", parent="root1", mode="assign")
        agents._ensure_sidebar(db, "root1", root_ws, root_win)
        sidebar = db.get_sidebar_pane("root1")

        attached = {session: True, worker_session: False}
        monkeypatch.setattr(tmux, "session_attached", lambda name: attached.get(name, False))
        tmux._tmux("select-window", "-t", f"{worker_session}:worker")
        agents.sidebar_follow(db, worker_session)
        assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)

        # Once the person switches into the worker's session, it follows.
        attached[worker_session] = True
        agents.sidebar_follow(db, worker_session)
        assert tmux.pane_window(sidebar) == tmux.pane_window(worker_win)

        # With no client anywhere (tests, headless runs) it follows the windows as before.
        attached[session] = attached[worker_session] = False
        tmux._tmux("select-window", "-t", f"{session}:root")
        agents.sidebar_follow(db, session)
        assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)
    finally:
        tmux.kill_session(worker_session)


@pytest.mark.serial
def test_second_supervisor_in_the_same_repo_gets_its_own_sidebar(db, tmp_path):
    """Keyed by session root, not repo_root: a second supervisor working in
    the same repo must not steal the first one's sidebar."""
    repo_root = str(tmp_path)
    session_a, session_b = "brindle_followtest_a", "brindle_followtest_b"
    tmux.ensure_session(session_a, str(tmp_path), {})
    tmux.ensure_session(session_b, str(tmp_path), {})
    try:
        win_a = make_window(session_a, "agentA")
        win_b = make_window(session_b, "agentB")
        ws_a = make_workspace(db, tmp_path, "wsA", session_a, repo_root=repo_root)
        ws_b = make_workspace(db, tmp_path, "wsB", session_b, repo_root=repo_root)
        fake_agent(db, ws_a, win_a, "rootA")
        fake_agent(db, ws_b, win_b, "rootB")

        agents._ensure_sidebar(db, "rootA", ws_a, win_a)
        agents._ensure_sidebar(db, "rootB", ws_b, win_b)

        sidebar_a = db.get_sidebar_pane("rootA")
        sidebar_b = db.get_sidebar_pane("rootB")
        assert sidebar_a is not None and sidebar_b is not None
        assert sidebar_a != sidebar_b
        assert len(window_panes(session_a, "agentA")) == 2
        assert len(window_panes(session_b, "agentB")) == 2

        agents.pause(db, "rootA")

        assert not tmux.window_alive(sidebar_a)
        assert db.get_sidebar_pane("rootA") is None
        # rootB's sidebar is untouched.
        assert tmux.window_alive(sidebar_b)
        assert db.get_sidebar_pane("rootB") == sidebar_b
    finally:
        tmux.kill_session(session_a)
        tmux.kill_session(session_b)


@pytest.mark.serial
@pytest.mark.variant("incident: a reused pane id must never be killed as the sidebar")
def test_pause_never_kills_an_untagged_pane_it_mistakes_for_the_sidebar(db, tmp_path):
    """A stale DB row can point at a pane id tmux has since reused for
    something else entirely (a tmux server restart starts pane ids over).
    Pausing must not kill that pane just because the DB row still names it.
    Put the imposter in a wholly separate session so pausing root1's own
    session (which always closes entirely) can't kill it for that reason
    instead of proving the tag check."""
    session_a, session_b = "brindle_followtest_pause_a", "brindle_followtest_pause_b"
    tmux.ensure_session(session_a, str(tmp_path), {})
    tmux.ensure_session(session_b, str(tmp_path), {})
    try:
        win_a = make_window(session_a, "winA")
        ws = make_workspace(db, tmp_path, "winA", session_a)
        fake_agent(db, ws, win_a, "root1")

        imposter = make_window(session_b, "imposter")
        db.set_sidebar_pane("root1", imposter)

        agents.pause(db, "root1")

        assert tmux.window_alive(imposter)  # never touched
        assert db.get_agent("root1").status == "paused"
        assert db.get_sidebar_pane("root1") == imposter  # untouched, stale row kept
    finally:
        if tmux.has_session(session_a):
            tmux.kill_session(session_a)
        tmux.kill_session(session_b)


@pytest.mark.serial
def test_follow_skips_a_paused_root(db, tmp_path, session):
    """The tombstone pause() sets (status="paused") before closing windows:
    a follow call that lands after that must not touch anything."""
    win_a = make_window(session, "winA")
    make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1", status="paused")

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") is None
    assert len(window_panes(session, "winB")) == 1


@pytest.mark.serial
@pytest.mark.variant("incident: tmux 3.7 answers nothing for a bare =name target")
def test_session_attached_reads_a_real_tmux_session(tmp_path, monkeypatch):
    """session_attached's own tmux call answers "0" for a detached session,
    not nothing: tmux 3.7 prints nothing for display-message on a bare
    "=name" target, so a session someone was attached to also read as
    unattached and the sidebar wandered off to any worker session that
    changed windows."""
    name = "brindle_attached_probe"
    tmux.ensure_session(name, str(tmp_path), {})
    real, answers = tmux._tmux, []

    def recording(*args, **kw):
        proc = real(*args, **kw)
        if args[0] == "display-message":
            answers.append(proc.stdout.strip())
        return proc

    monkeypatch.setattr(tmux, "_tmux", recording)
    try:
        assert tmux.session_attached(name) is False
        assert answers == ["0"]
        assert tmux.session_attached("brindle_no_such_session") is False
    finally:
        real("kill-session", "-t", f"={name}", check=False)


# -- getting a stranded sidebar back ------------------------------------------


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


def test_sidebar_zoom_sidebar_here_unzooms_the_window(stranded):
    db, root_win, _, _ = stranded
    tmux._tmux("split-window", "-t", root_win)  # zoom needs a second pane
    tmux._tmux("resize-pane", "-Z", "-t", root_win)
    assert tmux.window_zoomed(root_win)
    assert sidebar_here(db, ROOT_SESSION) == "unzoomed the window: the sidebar is shown now"
    assert not tmux.window_zoomed(root_win)


def test_sidebar_here_restarts_a_quit_sidebar(stranded):
    db, root_win, _, sidebar = stranded
    agents.dismiss_sidebar(db, sidebar)
    tmux.kill_pane(sidebar)
    sidebar_here(db, ROOT_SESSION)
    new = db.get_sidebar_pane("root1")
    assert new != sidebar and tmux.window_alive(new)
    assert tmux.pane_session(new) == ROOT_SESSION


def test_sidebar_command_reports_failure(brindle_home):
    result = CliRunner().invoke(app, ["sidebar", "--session", "no-such-session"])
    assert result.exit_code != 0


def test_removing_a_workspace_rescues_the_sidebar(stranded):
    db, root_win, worker_ws, sidebar = stranded
    worker_ws.kind = "main"  # no real git repo here; this path kills the session just the same
    workspaces.remove(db, worker_ws, force=True)
    assert tmux.window_alive(sidebar)
    assert tmux.pane_session(sidebar) == ROOT_SESSION


def test_a_stranded_sidebar_comes_home_to_the_attached_root_session(stranded, monkeypatch):
    """The hook that should have brought it back was missed: the sidebar's own
    loop moves it to the session the person is attached to, and only then."""
    db, root_win, _, sidebar = stranded
    attached = {ROOT_SESSION: False, WORKER_SESSION: False}
    monkeypatch.setattr(tmux, "session_attached", lambda name: attached.get(name, False))
    assert not agents.sidebar_come_home(db, "root1", sidebar)   # nobody anywhere: stay
    attached[WORKER_SESSION] = True
    assert not agents.sidebar_come_home(db, "root1", sidebar)   # the person is right here
    attached[WORKER_SESSION], attached[ROOT_SESSION] = False, True
    assert agents.sidebar_come_home(db, "root1", sidebar)
    assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)
