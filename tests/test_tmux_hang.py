"""A hung tmux server never hangs brindle, never reads as "no panes", and
nothing in brindle ever stops the default server.

The incident: a `brindle` launched inside a tmux pane with $TMUX unset
(`env -u TMUX brindle`, from another agent's shell) attached a client to the
default server from within one of that server's own panes. From then on
every tmux command on the server hung: `tmux ls`, brindle's own
`has-session` and `new-session` (three `brindle` starts in a row hung there,
rows with no pane), `brindle doctor`, the sidebar. After 17 minutes the only
way out was `pkill tmux`, which took every session on the server with it,
another brindle session's supervisor included. Had a hung server read as
an empty pane list, the cull would have killed every agent's processes too.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest
import typer

from brindle import agents, cli, cull, tmux, workspaces
from brindle.db import Agent
from test_cull import alive, launch_as, proc_cleanup  # noqa: F401 (fixture)


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    """A `tmux` on PATH that logs its argv and then does what ``body`` says;
    returns the log path. Undone with the test, before conftest's teardown
    talks to the real tmux."""
    def make(body: str):
        bin_ = tmp_path / "bin"
        bin_.mkdir(exist_ok=True)
        log = tmp_path / "tmux.log"
        script = bin_ / "tmux"
        script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n{body}\n')
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
        return log
    return make


@pytest.fixture
def hung_tmux(fake_tmux, monkeypatch):
    """Every tmux command blocks, as a wedged server's clients do, with
    brindle's patience cut to a fraction of a second."""
    monkeypatch.setenv("BRINDLE_TMUX_TIMEOUT", "0.3")
    return fake_tmux("exec sleep 30")


def add(db, ws, agent_id, **kw):
    fields = dict(profile="developer", provider="claude", parent_id=None, mode="assign",
                  status="idle", tmux_window="%999", result=None, created_at=time.time() - 7200)
    fields.update(kw)
    db.add_agent(Agent(agent_id, ws.id, **fields))


# -- every tmux call has a timeout ---------------------------------------------------


def test_a_hung_server_fails_fast_with_a_remedy(hung_tmux):
    t0 = time.time()
    with pytest.raises(tmux.TmuxTimeout) as e:
        tmux.ensure_session("brindle_hung", "/tmp", {})
    assert time.time() - t0 < 5
    assert "not responding" in str(e.value) and "kill-server" in str(e.value)
    log = hung_tmux.read_text()
    assert "has-session" in log and "new-session" not in log  # it stopped at the first one


def test_a_hung_server_is_not_an_empty_pane_list(hung_tmux):
    # An empty snapshot means "no server": every agent would look stopped.
    with pytest.raises(tmux.TmuxTimeout):
        tmux.list_panes()
    with pytest.raises(tmux.TmuxTimeout):
        tmux.list_sessions()
    with pytest.raises(tmux.TmuxTimeout):
        tmux.window_alive("%1")
    with pytest.raises(tmux.TmuxTimeout):
        tmux.server_homes("brindle-elsewhere")


# -- a hung server is never a verdict on any agent -------------------------------------


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def test_sweep_and_pause_on_a_hung_server_stop_nothing(db, ws, proc_cleanup, hung_tmux):
    add(db, ws, "abc12345", mode="interactive", status="processing", profile="supervisor")
    p = launch_as("abc12345")
    proc_cleanup.append(p)
    with pytest.raises(tmux.TmuxTimeout):
        cull.sweep(db)
    cull.sweep_quietly(db)
    with pytest.raises(tmux.TmuxTimeout):
        agents.pause(db, "abc12345")
    time.sleep(0.5)
    assert alive(p.pid), "its process was stopped on no evidence"
    a = db.get_agent("abc12345")
    assert a.status == "processing" and a.dismissed_at is None


def test_orphan_servers_leaves_a_hung_server_and_its_socket_alone(monkeypatch):
    monkeypatch.setattr(tmux, "other_servers", lambda prefix="brindle-": ["brindle-hung"])

    def hung(name):
        raise tmux.TmuxTimeout("no answer")

    monkeypatch.setattr(tmux, "server_homes", hung)
    touched = []
    monkeypatch.setattr(tmux, "reap_server", lambda name: touched.append(("reap", name)))
    monkeypatch.setattr(tmux, "remove_socket", lambda name: touched.append(("rm", name)))
    notes = cull.orphan_servers()
    assert touched == []
    assert any("brindle-hung" in n and "answering" in n for n in notes)


@pytest.mark.parametrize("denied", [
    pytest.param("connect", marks=pytest.mark.variant("a sandbox that denies connecting keeps a live server's socket")),
    pytest.param("metadata", marks=pytest.mark.variant("a sandbox that also denies reading the socket's metadata")),
    pytest.param(None, marks=pytest.mark.variant("a live server whose list-sessions failed for another reason")),
])
def test_orphan_servers_keeps_the_socket_of_a_live_server_it_could_not_list(monkeypatch, tmp_path, denied):
    # A sandboxed process can't connect to a live server ("Operation not
    # permitted"), so its list-sessions fails and server_homes reads None.
    # Deleting the socket then orphaned the live server: the next tmux call
    # started a second one, and a benchmark run's supervisor dropped out of sight.
    import errno
    import socket

    monkeypatch.chdir(tmp_path)              # relative paths: a Unix socket path must stay short
    monkeypatch.setenv("TMUX_TMPDIR", ".")
    sockets = tmux.socket_dir()
    sockets.mkdir()
    monkeypatch.setattr(tmux, "server_homes", lambda name: None)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(sockets / "brindle-stale"))
    stale.close()                            # its file stays, nobody listening
    real_stat = os.stat

    class Sandboxed(socket.socket):
        def connect(self, address):
            if str(address).endswith("brindle-live"):
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return super().connect(address)

    def no_metadata(path, *a, **kw):
        if str(path).endswith("brindle-live"):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_stat(path, *a, **kw)

    with socket.socket(socket.AF_UNIX) as live:
        live.bind(str(sockets / "brindle-live"))
        live.listen()
        with monkeypatch.context() as sandbox:
            if denied == "connect":
                sandbox.setattr(socket, "socket", Sandboxed)
            elif denied == "metadata":
                sandbox.setattr(os, "stat", no_metadata)
            cull.orphan_servers()
        assert (sockets / "brindle-live").exists()
    assert not (sockets / "brindle-stale").exists()


# -- nothing in brindle stops the default server ---------------------------------------


def test_kill_server_never_targets_the_default_server(fake_tmux, monkeypatch, tmp_path):
    log = fake_tmux("exit 0")
    monkeypatch.delenv("BRINDLE_TMUX_SOCKET", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    sockets = tmux.socket_dir()
    sockets.mkdir(parents=True)
    for name in ("default", "brindle-old", "brindle-mine"):
        (sockets / name).touch()

    with pytest.raises(tmux.TmuxError):
        tmux.kill_server()                 # no private socket selected
    with pytest.raises(tmux.TmuxError):
        tmux.reap_server("default")
    with pytest.raises(tmux.TmuxError):
        tmux.reap_server("")
    tmux.remove_socket("default")
    assert (sockets / "default").exists()
    assert not log.exists() or "kill-server" not in log.read_text()

    # A private server is still ours to stop, socket and all.
    tmux.reap_server("brindle-old")
    assert not (sockets / "brindle-old").exists()
    assert "-L brindle-old kill-server" in log.read_text()
    monkeypatch.setenv("BRINDLE_TMUX_SOCKET", "brindle-mine")
    tmux.kill_server()
    assert not (sockets / "brindle-mine").exists()
    assert "-L brindle-mine kill-server" in log.read_text()


# -- from inside a pane, brindle switches the client rather than nesting one -----------


def test_from_inside_a_pane_brindle_switches_the_client(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("TMUX", raising=False)        # as `env -u TMUX brindle` leaves it
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(tmux, "pane_session", lambda pane: "some_session" if pane == "%7" else None)
    monkeypatch.setattr(tmux, "has_session", lambda s: True)
    monkeypatch.setattr(tmux, "select_window", lambda t: None)
    ran = []
    clients = "/dev/ttys004\n"                       # the client on this pane's session

    def fake(*args, **kw):
        ran.append(args)
        return subprocess.CompletedProcess(args, 0, clients if args[0] == "list-clients" else "", "")

    monkeypatch.setattr(tmux, "_tmux", fake)
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, *a, **k: ran.append(tuple(argv)))
    assert tmux.inside_this_server()
    cli._attach(ws, "%3")
    assert ran[-1] == ("switch-client", "-c", "/dev/ttys004", "-t", "%3")

    # No client on the caller's session: never switch someone else's.
    clients = ""
    ran.clear()
    cli._attach(ws, "%3")
    assert not any(a[0] == "switch-client" for a in ran)

    # No controlling terminal (stdin /dev/null in a script): the client on
    # this pane's session is the person's, so don't switch it or attach.
    clients = "/dev/ttys001\n"
    ran.clear()
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    cli._attach(ws, "%3")
    assert not any(a[0] in ("switch-client", "attach-session") for a in ran)


def test_attach_fails_fast_on_a_hung_server(db, repo, hung_tmux):
    ws = workspaces.adopt_root(db, str(repo))
    t0 = time.time()
    with pytest.raises((SystemExit, typer.Exit)):
        cli._attach(ws)
    assert time.time() - t0 < 5


