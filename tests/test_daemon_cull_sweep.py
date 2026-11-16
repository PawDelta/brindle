"""A worker held on its CLI's own approval prompt (Codex with no permission
hook: no permission row, still 'processing') was only ever noticed by the
sidebar's cull sweep. A session with no sidebar (the app alone, --no-watch)
never noticed it, and its supervisor idled. The daemon's watch runs the
sweep too."""

import asyncio
import time
from types import SimpleNamespace

from brindle import agents, tmux
from brindle.db import Agent, Workspace

CODEX_APPROVAL = """\
• Running uv run python -m pytest --version


  Would you like to run the following command?

  Environment: local

  Reason: May I let uv create the project environment outside the sandbox?

  $ uv run python -m pytest --version


› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `uv run` (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""


def a_session(db, tmp_path, monkeypatch, w1_status, w1_provider="codex"):
    """A supervisor and its worker w1, both with live panes on this server."""
    server_name = tmux.current_server()
    ws = Workspace("ws1", str(tmp_path), "feature", "worktree", "feature", "main", str(tmp_path), None,
                   "brindle-ws1", time.time())
    db.add_workspace(ws)
    for id_, parent, provider, mode, status in (("boss", None, "claude", "interactive", "idle"),
                                                ("w1", "boss", w1_provider, "assign", w1_status)):
        db.add_agent(Agent(id_, ws.id, "developer", provider, parent, mode, status, f"%{id_}", None,
                           time.time(), tmux_server=server_name))
    panes = tmux.PaneSnapshot(server_name)
    panes.update({"%boss": True, "%w1": True})
    monkeypatch.setattr(tmux, "list_panes", lambda server=None: panes)
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes=None: {"%boss": "boss", "%w1": "w1"})
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, sender_id=None, **kw: sent.append((to, body)))
    return sent


def resumed(db):
    """w1 came back between the sweep's judgement and its action: a new spell, running."""
    db.conn.execute("UPDATE agents SET status='processing', status_since=? WHERE id='w1'", (time.time() + 5,))
    db.conn.commit()


def test_a_worker_resumed_after_the_sweep_judged_it_leftover_keeps_its_processes(db, tmp_path, monkeypatch):
    from brindle import cull, procs

    a_session(db, tmp_path, monkeypatch, "paused")
    scans = []

    def table():
        scans.append(1)
        if len(scans) == 2:  # the scan for w1's processes, after the sweep judged it paused
            resumed(db)
        return {}

    monkeypatch.setattr(procs, "table", table)
    monkeypatch.setattr(procs, "all_agent_ids", lambda table: ["w1"])
    monkeypatch.setattr(procs, "agent_pids", lambda ids, **kw: {aid: {4242} for aid in ids})
    signalled = []
    monkeypatch.setattr(procs, "terminate", lambda pids, **kw: signalled.extend(pids) or len(pids))
    cull.sweep(db)
    assert 4242 not in signalled


def test_a_worker_that_took_a_turn_after_the_sweep_judged_it_finished_is_not_closed(db, tmp_path, monkeypatch):
    from brindle import cull

    a_session(db, tmp_path, monkeypatch, "idle", "claude")
    db.update_agent("w1", result="done", created_at=time.time() - 7200, status_since=time.time() - 3600)

    def stale_after(db_, a, cache):
        resumed(db)          # judged idle and finished from this row; it takes a turn now
        return 60.0

    monkeypatch.setattr(cull, "_stale_after", stale_after)
    closed = []
    monkeypatch.setattr(agents, "close", lambda db_, aid, panes=None, **kw: closed.append(aid))
    cull.sweep(db)
    assert "w1" not in closed


def test_one_watch_tick_with_no_sidebar_tells_the_supervisor_of_a_codex_approval(db, tmp_path, monkeypatch):
    from brindle.daemon import lifecycle, session_review
    from brindle.upgrade import InstallWatch

    sent = a_session(db, tmp_path, monkeypatch, "processing")
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: CODEX_APPROVAL if target == "%w1" else "")

    # The watch's other checks are not this test's.
    monkeypatch.setattr(session_review.ReadinessWatch, "sweep_safely", lambda self: None)
    monkeypatch.setattr(lifecycle, "_purge_typed_words", lambda: None)
    monkeypatch.setattr(lifecycle, "_keep_codex_hook", lambda settle=0.0: None)
    monkeypatch.setattr(lifecycle, "_sync_shared_docs", lambda: None)
    monkeypatch.setattr(InstallWatch, "changed", lambda self: False)
    now = [0.0]
    server = SimpleNamespace(should_exit=False)

    async def one_minute(seconds):
        now[0] += 60.0
        server.should_exit = True

    asyncio.run(lifecycle._watch(server, None, False, wait=one_minute, clock=lambda: now[0]))

    assert db.get_agent("w1").status == "waiting"
    assert [to for to, body in sent if "w1" in body and "Would you like to run" in body] == ["boss"]
