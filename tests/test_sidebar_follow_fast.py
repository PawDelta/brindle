"""The tmux follow hook should avoid starting the full CLI."""

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle import sidebar_follow
from brindle.db import Agent, Workspace

ENTRY = Path(__file__).parents[1] / "src" / "brindle" / "__main__.py"


def _run(code, tmp_path):
    env = {**os.environ, "BRINDLE_HOME": str(tmp_path / "brindle-home")}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


def test_sidebar_follow_entry_does_not_import_heavy_modules(tmp_path):
    code = f"""
import runpy, sys
sys.argv = [{str(ENTRY)!r}, '_sidebar-follow', 'missing-session']
runpy.run_path(sys.argv[0], run_name='__main__')
for name in ('brindle.cli', 'brindle.mcp_server', 'typer', 'brindle.providers', 'brindle.workspaces'):
    assert name not in sys.modules, name
"""
    result = _run(code, tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("reported_window", "reported_tag", "should_skip"),
    [("@window", "root", True), ("@other", "root", False),
     ("@window", "different-root", False)],
)
def test_sidebar_follow_fast_skip_requires_matching_window_and_tag(
    monkeypatch, reported_window, reported_tag, should_skip,
):
    ws = SimpleNamespace(id="ws", tmux_session="session")
    agent = SimpleNamespace(id="root", tmux_window="%agent", parent_id=None, status="idle")
    # An older paused root in the same workspace whose stale pane id also
    # resolves to the active window: the live root is the one followed.
    stale = SimpleNamespace(id="stale", tmux_window="%stale", parent_id=None, status="paused")

    class FakeDB:
        def workspace_by_tmux_session(self, session):
            return ws

        def list_agents(self, workspace_id):
            return [stale, agent]

        def get_agent(self, agent_id):
            return agent

        def get_sidebar_pane(self, root_id):
            return "%sidebar"

    calls = []
    monkeypatch.setattr(sidebar_follow.tmux, "active_window", lambda session: "@window")
    monkeypatch.setattr(sidebar_follow.tmux, "pane_window", lambda pane: "@window")

    def pane_window_tag(pane, tag):
        calls.append((pane, tag))
        return reported_window, reported_tag

    monkeypatch.setattr(sidebar_follow.tmux, "pane_window_tag", pane_window_tag)
    sent = []
    monkeypatch.setattr(sidebar_follow.tmux, "send_keys", lambda pane, *keys: sent.append((pane, keys)))
    monkeypatch.setattr(sidebar_follow, "_sidebar_lock", lambda root_id: (_ for _ in ()).throw(
        AssertionError("slow path reached")))
    monkeypatch.setattr(sidebar_follow.tmux, "window_alive", lambda *a: (_ for _ in ()).throw(
        AssertionError("skip path revalidated via tmux")))
    monkeypatch.setattr(sidebar_follow.tmux, "move_pane", lambda *a: (_ for _ in ()).throw(
        AssertionError("skip path moved the sidebar")))

    if should_skip:
        sidebar_follow.sidebar_follow(FakeDB(), "session")
        # A session switch leaves iTerm2 showing a blank sidebar: it must redraw.
        assert sent == [("%sidebar", ("C-l",))]
    else:
        with pytest.raises(AssertionError, match="slow path reached"):
            sidebar_follow.sidebar_follow(FakeDB(), "session")
    assert calls == [("%sidebar", "@brindle_sidebar")]


@pytest.mark.parametrize("shared_session", [False, True], ids=["own session", "shared session"])
def test_sidebar_survives_stopping_the_worker_whose_window_it_sits_in(db, monkeypatch, shared_session):
    """The sidebar followed the person into a worker's window (the root's active
    window too, when the worker shares the root's session); stopping that worker
    closes the window, so the sidebar moves to a surviving root window first and
    its process is not among those stopped."""
    from brindle import agents, procs

    worker_ws, worker_session = ("rootws", "root_s") if shared_session else ("workerws", "worker_s")
    for ws_id, session in {"rootws": "root_s", worker_ws: worker_session}.items():
        db.add_workspace(Workspace(ws_id, "/repo", ws_id, "worktree", ws_id, "main", f"/{ws_id}",
                                   None, session, time.time()))
    db.add_agent(Agent("root1", "rootws", "supervisor", "claude", None, "interactive", "idle",
                       "%root", None, time.time()))
    worker = Agent("w1", worker_ws, "developer", "claude", "root1", "assign", "idle",
                   "%worker", None, time.time())
    db.add_agent(worker)
    db.set_sidebar_pane("root1", "%sidebar")
    windows = {"%root": "@r", "%worker": "@w", "%sidebar": "@w"}   # pane -> window, moved with the sidebar
    pane_pid = {"%worker": 5001, "%sidebar": 5002}
    killed = set()
    tm = sidebar_follow.tmux
    monkeypatch.setattr(tm, "active_window", {"root_s": "@w" if shared_session else "@r",
                                              "worker_s": "@w"}.get)
    monkeypatch.setattr(tm, "pane_window", lambda pane: windows.get(pane, pane))
    monkeypatch.setattr(tm, "window_alive", lambda pane: True)
    monkeypatch.setattr(tm, "get_pane_tag", lambda pane, tag: "root1")
    monkeypatch.setattr(tm, "pane_session", lambda pane: worker_session)
    monkeypatch.setattr(tm, "session_attached", lambda session: False)
    monkeypatch.setattr(tm, "agent_pane_in_window", lambda window, sidebar: "%root" if window == "@r" else None)
    monkeypatch.setattr(tm, "move_pane", lambda pane, target, *a: windows.update({pane: windows[target]}))
    monkeypatch.setattr(agents, "same_server", lambda *a, **k: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a, **k: True)
    monkeypatch.setattr(agents.tmux, "window_pids",
                        lambda w: [pid for pane, pid in pane_pid.items() if windows[pane] == windows[w]])
    monkeypatch.setattr(agents.tmux, "kill_window", lambda w: windows.update(
        {p: "" for p, win in windows.items() if win == windows[w]}))
    monkeypatch.setattr(procs, "table", lambda: {pid: procs.Proc(pid, 1, "sh") for pid in (5001, 5002)})
    monkeypatch.setattr(procs, "terminate", lambda pids, grace=3.0: killed.update(pids) or len(pids))

    agents._stop(db, worker)

    assert windows["%sidebar"] == "@r"
    assert killed == {5001}


@pytest.mark.variant("the app views the root's chat through its own brindle-view session")
def test_sidebar_survives_only_while_the_brindle_session_is_attached(db, monkeypatch):
    """pane_session reports the app's brindle-view-* session for a linked
    window, and that session is attached: the sidebar must still follow the
    person's terminal, which left the root's own (now unattached) session."""
    for ws_id, session in (("rootws", "root_s"), ("workerws", "worker_s")):
        db.add_workspace(Workspace(ws_id, "/repo", ws_id, "worktree", ws_id, "main", f"/{ws_id}",
                                   None, session, time.time()))
    db.add_agent(Agent("root1", "rootws", "supervisor", "claude", None, "interactive", "idle",
                       "%root", None, time.time()))
    db.add_agent(Agent("w1", "workerws", "developer", "claude", "root1", "assign", "idle",
                       "%worker", None, time.time()))
    db.set_sidebar_pane("root1", "%sidebar")
    windows = {"%root": "@r", "%worker": "@w", "%sidebar": "@r"}
    tm = sidebar_follow.tmux
    monkeypatch.setattr(tm, "active_window", {"root_s": "@r", "worker_s": "@w"}.get)
    monkeypatch.setattr(tm, "pane_window", windows.get)
    monkeypatch.setattr(tm, "pane_window_tag", lambda pane, tag: (windows[pane], "root1"))
    monkeypatch.setattr(tm, "window_alive", lambda pane: True)
    monkeypatch.setattr(tm, "get_pane_tag", lambda pane, tag: "root1")
    monkeypatch.setattr(tm, "pane_session", {"%sidebar": "brindle-view-1", "%worker": "worker_s"}.get)
    monkeypatch.setattr(tm, "session_attached", lambda session: session == "brindle-view-1")
    monkeypatch.setattr(tm, "agent_pane_in_window", lambda window, sidebar: "%worker")
    monkeypatch.setattr(tm, "send_keys", lambda *a: None)
    moved = []
    monkeypatch.setattr(tm, "move_pane", lambda pane, target, *a: moved.append((pane, target)))

    sidebar_follow.sidebar_follow(db, "worker_s")

    assert moved == [("%sidebar", "%worker")]


def test_sidebar_left_dead_by_crashes_follows_until_a_restart_by_hand(db, monkeypatch):
    """A crashed sidebar's pane stays (respawning, or capped and showing its
    error): a window switch moves it like a live one rather than starting a
    second sidebar with a fresh restart count. `brindle sidebar` replaces it."""
    for ws_id, session in (("rootws", "root_s"), ("workerws", "worker_s")):
        db.add_workspace(Workspace(ws_id, "/repo", ws_id, "worktree", ws_id, "main", f"/{ws_id}",
                                   None, session, time.time()))
    db.add_agent(Agent("root1", "rootws", "supervisor", "claude", None, "interactive", "idle",
                       "%root", None, time.time()))
    db.add_agent(Agent("w1", "workerws", "developer", "claude", "root1", "assign", "idle",
                       "%worker", None, time.time()))
    db.set_sidebar_pane("root1", "%sidebar")
    windows = {"%root": "@r", "%worker": "@w", "%sidebar": "@r"}
    tm = sidebar_follow.tmux
    monkeypatch.setattr(tm, "active_window", {"root_s": "@r", "worker_s": "@w"}.get)
    monkeypatch.setattr(tm, "pane_window", windows.get)
    monkeypatch.setattr(tm, "pane_window_tag", lambda pane, tag: (windows.get(pane), "root1"))
    monkeypatch.setattr(tm, "window_alive", lambda pane: pane != "%sidebar")
    monkeypatch.setattr(tm, "get_pane_tag", lambda pane, tag: "root1" if pane in windows else None)
    monkeypatch.setattr(tm, "pane_session", lambda pane: "root_s")
    monkeypatch.setattr(tm, "session_attached", lambda session: False)
    monkeypatch.setattr(tm, "agent_pane_in_window", lambda window, sidebar: "%worker")
    monkeypatch.setattr(tm, "send_keys", lambda *a: None)
    monkeypatch.setattr(tm, "move_pane", lambda pane, target, *a: windows.update({pane: windows[target]}))
    monkeypatch.setattr(tm, "kill_pane", lambda pane: windows.pop(pane))
    created = []
    monkeypatch.setattr(sidebar_follow, "_create_sidebar",
                        lambda db, root, ws, target: created.append(target) or "%new")

    sidebar_follow.sidebar_follow(db, "worker_s")

    assert windows["%sidebar"] == "@w" and created == []

    assert sidebar_follow.bring_sidebar(db, "root1", "worker_s", revive=True)
    assert "%sidebar" not in windows and created == ["%worker"]


def test_sidebar_restarts_its_own_pane_after_a_crash_until_the_cap(tmp_path, monkeypatch):
    # A crash (even on startup), or a re-exec into a half-installed engine:
    # tmux's pane-died hook on the sidebar's pane alone runs it again there,
    # until it has been restarted SIDEBAR_MAX_RESTARTS times in a short while.
    from brindle import tmux, workspaces

    calls, opts = tmp_path / "calls", tmp_path / "opts"
    opts.mkdir()
    fake = tmp_path / "tmux"
    # Records each call; keeps the pane's @options as files, so the counter
    # the hook writes is what its next run reads.
    fake.write_text(f"""#!/bin/sh
printf '%s\\0' "$@" >> {calls}; printf '\\036' >> {calls}
for a; do k=$v; v=$a; done
case "$*" in
  *pane-died*) ;;
  *split-window*) echo %7;;
  *show-options*) cat {opts}/$v 2>/dev/null;;
  *set-option*@*) printf %s "$v" > {opts}/$k;;
esac
""")
    fake.chmod(0o755)
    monkeypatch.setenv("BRINDLE_TMUX_BIN", str(fake))
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path))
    monkeypatch.setattr(sidebar_follow, "_sidebar_position", lambda ws: "left")
    monkeypatch.setattr(workspaces, "workspace_env", lambda ws: {"FOO": "x"})
    db = SimpleNamespace(set_sidebar_pane=lambda root, pane: None)

    def made():
        return [c.split("\0")[:-1] for c in calls.read_text().split("\036")[:-1]]

    sidebar_follow._create_sidebar(db, "root", SimpleNamespace(path=str(tmp_path)), "%1")

    split = next(c for c in made() if "split-window" in c)
    flat = [a for c in made() for a in c]
    assert "-g" not in flat
    died, remain, launch = flat.index("pane-died"), flat.index("remain-on-exit"), flat.index("respawn-pane")
    # The pane opens on the stand-in; the sidebar starts there only after
    # its recovery is in place, so a crash on startup is caught too.
    assert split[split.index("--") + 1:] == tmux.SIDEBAR_STAND_IN
    assert remain < died < launch and flat[launch:launch + 4] == ["respawn-pane", "-k", "-t", "%7"]
    assert flat[died - 3:died] == ["-p", "-t", "%7"] and flat[remain - 3:remain + 2] == [
        "-p", "-t", "%7", "remain-on-exit", "failed"]
    # What tmux runs, formats expanded, each time the pane's process exits with status 1.
    shell = shlex.split(flat[died + 1])[2].replace("#{pane_dead_status}", "1").replace("##", "#")
    before = len(made())
    for _ in range(tmux.SIDEBAR_MAX_RESTARTS + 2):
        subprocess.run(["/bin/sh", "-c", shell], check=True)

    respawns = [c[c.index("respawn-pane"):] for c in made()[before:] if "respawn-pane" in c]
    assert respawns == [["respawn-pane", "-t", "%7", "-c", str(tmp_path), "-e", "FOO=x"]] * (
        tmux.SIDEBAR_MAX_RESTARTS)
    assert (opts / tmux.SIDEBAR_RESTARTS).read_text() == str(tmux.SIDEBAR_MAX_RESTARTS)
    assert len((tmp_path / "sidebar.log").read_text().splitlines()) == tmux.SIDEBAR_MAX_RESTARTS + 2
