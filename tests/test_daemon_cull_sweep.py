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


def test_one_watch_tick_with_no_sidebar_tells_the_supervisor_of_a_codex_approval(db, tmp_path, monkeypatch):
    from brindle.daemon import lifecycle, session_review
    from brindle.upgrade import InstallWatch

    server_name = tmux.current_server()
    ws = Workspace("ws1", str(tmp_path), "feature", "worktree", "feature", "main", str(tmp_path), None,
                   "brindle-ws1", time.time())
    db.add_workspace(ws)
    for id_, parent, provider, mode, status in (("boss", None, "claude", "interactive", "idle"),
                                                ("w1", "boss", "codex", "assign", "processing")):
        db.add_agent(Agent(id_, ws.id, "developer", provider, parent, mode, status, f"%{id_}", None,
                           time.time(), tmux_server=server_name))
    panes = tmux.PaneSnapshot(server_name)
    panes.update({"%boss": True, "%w1": True})
    monkeypatch.setattr(tmux, "list_panes", lambda server=None: panes)
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: CODEX_APPROVAL if target == "%w1" else "")
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes=None: {"%boss": "boss", "%w1": "w1"})
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, sender_id=None, **kw: sent.append((to, body)))

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
