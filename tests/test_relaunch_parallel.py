"""App relaunch: a daemon start brings its sessions back side by side, and
their launches share one model check and preflight per provider and identity."""
import dataclasses
import threading
import time
from collections import Counter

import pytest

from brindle import agents, company_identity, preflight, procs, spawn, tmux, upgrade, workspaces
from brindle.daemon import lifecycle
from brindle.db import Agent
from git_snapshots import fast_host_tools, repo, workspaces_once  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def no_timing_backend(monkeypatch):
    from brindle import debuglog

    monkeypatch.setattr(debuglog, "staff", lambda: False)
    monkeypatch.setattr(debuglog, "_publish_timings", debuglog._write_timings)


class _Launched(Exception):
    """The launch got past its checks to where it would change what runs."""


@pytest.mark.variant("catches sessions recovered one after another and per-agent duplicate checks: timing and call counts, not lines")
def test_recovery_overlaps_sessions_and_checks_each_identity_once(db, repo, tmp_path, monkeypatch):
    """Reported: a relaunch with 3 sessions took the sum of their resumes
    (10s), the one on screen coming back alone first, and every agent ran its
    own model check and preflight. All sessions must be under way at once,
    the viewed one included; launches signed in alike share one check, a
    returning worker of another profile its session's preflight; and an
    identity that differs only in its tmux session's environment, refused,
    is never let through on another's answer."""
    root = workspaces.adopt_root(db, str(repo))
    here = tmux.current_server()
    homes = {"viewed": "home-a", "recent": "home-a", "other": "home-b", "keyed": "home-a"}
    for aid, home in homes.items():
        db.add_workspace(dataclasses.replace(root, id=f"ws-{aid}", name=aid, tmux_session=aid))
        db.add_agent(Agent(aid, f"ws-{aid}", "supervisor", "claude", None, "interactive", "paused", "", None,
                           time.time(), time.time(), "t", tmux_server=here))
        db.update_agent(aid, config_home=str(tmp_path / home))
    db.add_agent(Agent("worker", "ws-viewed", "developer", "claude", "viewed", "assign", "paused", "", None,
                       time.time(), time.time(), "t", tmux_server=here))
    db.update_agent("worker", config_home=str(tmp_path / "home-a"))
    for aid in ("viewed", "recent"):    # the same key stored on two sessions: one identity, two launches to name
        db.set_session_credential(aid, "ANTHROPIC_API_KEY", "test-shared-key")
    lifecycle._record_resume([{"id": aid, "server": here} for aid in homes])
    lifecycle.record_viewed("viewed")
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)
    monkeypatch.setattr(tmux, "capture", lambda *a, **kw: "❯ \n? for shortcuts")
    monkeypatch.setattr(tmux, "inherited_env",
                        lambda session: {"ANTHROPIC_BASE_URL": "https://gateway.test"} if session == "keyed" else {})
    monkeypatch.setattr(spawn, "wait_for_room", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "panes_match", lambda *a, **kw: True)
    monkeypatch.setattr(upgrade, "check_launch", lambda: None)
    monkeypatch.setattr(company_identity, "launch_check", lambda *a, **kw: (None, None, None))

    lock, checks, consents = threading.Lock(), Counter(), {}
    together = threading.Barrier(len(homes))

    def model_problem(name, ws, *, agent, **kw):
        with lock:
            checks["model"] += 1
        return "refused for this gateway" if agent.id == "keyed" else None

    def probe(provider, repo_root, **kw):       # a key the CLI asks about once: its consent names the launch
        with lock:
            checks["preflight"] += 1
        return [preflight.Item("consent", "claude.api_key", "claude", "Use the key?", "",
                               preflight._consent("claude", "claude.api_key", key="fp"))]

    real_blockers = agents.launch_blockers

    def launch_blockers(agent, ws, *a, **kw):
        items = real_blockers(agent, ws, *a, **kw)
        consents[agent.id] = [i.action.body for i in items]
        return items

    def gate_commit(hold):
        raise _Launched

    monkeypatch.setattr(agents, "model_problem", model_problem)
    monkeypatch.setattr(preflight, "check", probe)
    monkeypatch.setattr(agents, "launch_blockers", launch_blockers)
    monkeypatch.setattr(agents, "_gate_commit", gate_commit)

    def resume(db_, root_id, **kw):
        together.wait(5)        # every session's resume under way at once, the viewed one's too
        for a in agents.tree(db_, root_id):
            # The worker's consent stops it; the chat answers its own at its input box.
            with pytest.raises(agents.PreflightBlocked if a.parent_id else _Launched):
                agents._launch(db_, a, db_.get_workspace(a.workspace_id), prompt=None, resume=None,
                               watch_pane=False)

    monkeypatch.setattr(agents, "resume", resume)
    assert sorted(lifecycle.resume_after_start()) == ["other", "recent", "viewed"]     # keyed: refused
    # Model: home-a's supervisors, its developer, home-b, the gateway. Preflight: one per home.
    assert checks == {"model": 4, "preflight": 2}
    # Each consent names its own launch: its profile, and the session whose stored key it gets.
    named = {"id": "claude.api_key", "key": "fp", "repo": str(repo)}
    assert consents == {"viewed": [{**named, "profile": "supervisor", "session": "viewed"}],
                        "recent": [{**named, "profile": "supervisor", "session": "recent"}],
                        "worker": [{**named, "profile": "developer", "session": "viewed"}],
                        "other": [{**named, "profile": "supervisor"}]}
