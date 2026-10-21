"""Finished work must not leave processes or tmux sessions behind."""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from brindle import agents, codex_hook, tmux, workspaces
from brindle.db import Agent, Workspace


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return out.strip().startswith("Z")


def test_an_app_server_that_never_answers_is_gone_with_its_children(tmp_path):
    """The hook trust step gave up on a silent ``codex app-server`` but killed
    only the process it started, leaving whatever that spawned running."""
    pidfile = tmp_path / "child.pid"
    fake = tmp_path / "codex"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import subprocess, sys, threading
        wait = "import threading; threading.Event().wait()"
        child = subprocess.Popen([sys.executable, "-c", wait])
        open({str(pidfile)!r}, "w").write(str(child.pid))
        threading.Event().wait()
    """))
    fake.chmod(0o755)

    with pytest.raises(codex_hook.CodexHookError):
        codex_hook._AppServer(str(fake), [], 3.0)

    child = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while not gone(child) and time.monotonic() < deadline:
        time.monotonic()
    assert gone(child)


def test_closing_the_reviewer_of_a_removed_workspace_ends_its_session(db, repo):
    """The pipeline removes a merged workspace but keeps the session its
    reviewer runs in; closing the reviewer then left the session behind."""
    session = "brindle_proj_feat-done"
    ws = Workspace("proj/feat-done", str(repo), "feat-done", "worktree", "feat-done", "main",
                   str(repo / "gone"), None, session, time.time())
    db.add_workspace(ws)
    tmux.ensure_session(session, str(repo), {})
    pane = tmux._tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:",
                      "cat").stdout.strip()
    db.add_agent(Agent("r1", ws.id, profile="reviewer", provider="claude", parent_id=None,
                       mode="review", status="done", tmux_window=pane, result="ok",
                       created_at=time.time()))

    workspaces.remove(db, ws, keep_session=True)
    assert tmux.has_session(session)
    agents._closing_session("r1").write_text(session)   # as finish_review notes it
    agents.close_removed(db, "r1")

    assert not tmux.has_session(session)
