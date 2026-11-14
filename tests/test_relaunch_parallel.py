"""App relaunch: a daemon start brings its sessions back side by side, and
their launches share one model check and preflight per provider and identity."""
import dataclasses
import threading
import time
from collections import Counter

import pytest

from brindle import agents, company_identity, procs, spawn, tmux, upgrade, workspaces
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


def test_recovery_overlaps_sessions_and_checks_each_identity_once(db, repo, tmp_path, monkeypatch):
    """Reported: a relaunch with 3 sessions took the sum of their resumes
    (10s), the one on screen coming back alone first, and every agent ran its
    own model check and preflight. All three must be under way at once, the
    viewed one included, and the two signed in alike share one check."""
    root = workspaces.adopt_root(db, str(repo))
    here = tmux.current_server()
    homes = {"viewed": tmp_path / "home-a", "recent": tmp_path / "home-a", "other": tmp_path / "home-b"}
    for aid, home in homes.items():
        db.add_workspace(dataclasses.replace(root, id=f"ws-{aid}", name=aid, tmux_session=aid))
        db.add_agent(Agent(aid, f"ws-{aid}", "supervisor", "claude", None, "interactive", "paused", "", None,
                           time.time(), time.time(), "t", tmux_server=here))
        db.update_agent(aid, config_home=str(home))
    lifecycle._record_resume([{"id": aid, "server": here} for aid in homes])
    lifecycle.record_viewed("viewed")
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)
    monkeypatch.setattr(tmux, "capture", lambda *a, **kw: "❯ \n? for shortcuts")
    monkeypatch.setattr(spawn, "wait_for_room", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "panes_match", lambda *a, **kw: True)
    monkeypatch.setattr(upgrade, "check_launch", lambda: None)
    monkeypatch.setattr(company_identity, "launch_check", lambda *a, **kw: (None, None, None))

    lock, checks, overlapped = threading.Lock(), Counter(), []
    together = threading.Barrier(len(homes))

    def counted(kind):
        def check(*a, **kw):
            with lock:
                checks[kind] += 1
            return None if kind == "model" else []
        return check

    def gate_commit(hold):
        raise _Launched

    monkeypatch.setattr(agents, "model_problem", counted("model"))
    monkeypatch.setattr(agents, "launch_blockers", counted("preflight"))
    monkeypatch.setattr(agents, "_gate_commit", gate_commit)

    def resume(db_, root_id, **kw):
        together.wait(5)        # every session's resume under way at once, the viewed one's too
        overlapped.append(root_id)
        a = db_.get_agent(root_id)
        with pytest.raises(_Launched):
            agents._launch(db_, a, db_.get_workspace(a.workspace_id), prompt=None, resume=None, watch_pane=False)

    monkeypatch.setattr(agents, "resume", resume)
    assert sorted(lifecycle.resume_after_start()) == sorted(homes)
    assert sorted(overlapped) == sorted(homes)
    assert checks == {"model": 2, "preflight": 2}    # once for each of the two homes, not for each of the three
