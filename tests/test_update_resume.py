from dataclasses import replace

from brindle import agents, procs, providers, tmux, workspaces
from brindle.daemon import lifecycle
from brindle.service import sessions as service
from git_snapshots import fast_host_tools, repo  # noqa: F401
from test_sessions import _NoClientTmux, add


def test_update_restores_the_open_session_with_its_live_worker_first(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    other = replace(root, id="other-workspace", name="other", tmux_session="other-session")
    db.add_workspace(other)
    server = _NoClientTmux()
    monkeypatch.setattr(tmux, "_tmux", server)
    monkeypatch.setattr(tmux, "agent_panes", lambda: [
        tmux.AgentPane(pane, row[f"#{{{tmux.AGENT_TAG}}}"], True, True)
        for pane, row in server.panes.items() if row[f"#{{{tmux.AGENT_TAG}}}"]
    ])
    monkeypatch.setattr(procs, "stop", lambda *a, **kw: 0)
    monkeypatch.setattr(service, "cull_detached", lambda: None)
    monkeypatch.setattr(lifecycle, "_after_prompt", lambda *a: None)
    monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: True))
    launched = []

    def launch(db_, agent, ws, **kw):
        pane = server.open(ws.tmux_session, agent.id, (160, 50), agent=agent.id)
        db_.update_agent(agent.id, tmux_window=pane, tmux_server=tmux.current_server())
        db_.set_status(agent.id, "idle")
        launched.append(agent.id)

    monkeypatch.setattr(agents, "_launch", launch)
    # The open chat is older than the other session and has a worker at work.
    for aid, parent in (("open", None), ("worker", "open"), ("other", None)):
        ws = other if aid == "other" else root
        agent = add(db, ws, aid, mode="assign" if parent else "interactive",
                    parent=parent, status="processing")
        launch(db, agent, ws)
    db.set_status("worker", "processing")
    lifecycle.record_viewed("open")
    # A previous recovery left its attempt count behind, though the whole session
    # has since returned to work. An update must give that live session a fresh start.
    lifecycle._record_resume([{"id": "open", "server": tmux.current_server(),
                               "tries": lifecycle.RESUME_TRIES}])
    launched.clear()

    assert set(lifecycle.pause_for_exit()) == {"open", "other"}
    assert {db.get_agent(a).status for a in ("open", "worker", "other")} == {"paused"}
    assert set(lifecycle.resume_after_start()) == {"open", "other"}
    assert launched[0] == "open"
    assert set(launched) == {"open", "worker", "other"}
    assert all(db.get_agent(a).status != "paused" for a in launched)
    assert lifecycle._read_resume() is None
