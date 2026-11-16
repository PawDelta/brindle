"""A worker held on a permission ask only the person can answer does nothing
until they do: its supervisor, idle or about to stop, must hear of it rather
than wait on a result that never comes (bench Stage 3 arm B sat 90 minutes)."""

import json
import time

import pytest

from brindle import agents, autopilot, cull
from brindle.config import set_local
from brindle.db import Agent
from test_autopilot import root, with_goal  # noqa: F401 - fixtures
from test_permission_routing import ask, person_answers, team, waiting  # noqa: F401 - fixtures

NOTICE = "waiting for the person"


def notices(team):
    return [m for m in team.sent if NOTICE in m]


@pytest.mark.parametrize("command, window_runs_out", [
    ("deploy prod", False),     # person-only: the person's from the start
    ("make build", True),       # the supervisor's first, until its window runs out
])
@pytest.mark.variant("person-only commands skip the supervisor's hold; ordinary ones wake it only after the window")
def test_a_request_left_to_the_person_wakes_the_idle_supervisor(db, team, monkeypatch, command, window_runs_out):
    set_local(team.ws.repo_root, "permission_supervisor_hold_seconds", 30)
    db.set_status("boss", "idle")
    steps = [lambda holder: setattr(team.clock, "now", team.clock.now + 31)] if window_runs_out else []

    def supervisor_told(holder):
        [notice] = notices(team)
        assert "w1" in notice and command in notice and "answer_permission" not in notice
        person_answers("allow_once")(holder)
    waiting(monkeypatch, *steps, supervisor_told)
    assert ask(db, command) == "allow"
    assert len(notices(team)) == 1      # once per wait, however many looks the hold takes


def held_for_person(db, ws_id="ws1"):
    db.add_agent(Agent("w1", ws_id, "developer", "claude", "boss", "assign", "waiting", "%w1", None, time.time()))
    db.add_permission_request("w1", "r1", json.dumps({"tool": "Bash", "kind": "bash", "provider": "claude",
                                                      "command": "deploy prod"}), "why", None, "hook",
                              hold_until=time.time() + 3600)
    return db.get_agent("w1")


def test_a_supervisor_cannot_stop_on_a_worker_held_for_the_person(db, root, monkeypatch):
    agent, ws = root
    with_goal(db)
    worker = held_for_person(db, ws.id)
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid, **kw: [worker])
    out = autopilot.on_stop(db, agent, {})
    assert out and out["decision"] == "block"
    assert "w1" in out["reason"] and "deploy prod" in out["reason"]


def test_the_stuck_sweep_tells_at_once_about_a_worker_held_for_the_person(db, team, monkeypatch):
    sent = []
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes: {})
    monkeypatch.setattr(agents, "owns_pane", lambda db, a, owners: True)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, sender_id=None: sent.append(body))
    monkeypatch.setattr(cull.tmux, "capture", lambda *a, **k: "• Working (12s)")
    db.add_permission_request("w1", "r1", json.dumps({"tool": "Bash", "kind": "bash", "provider": "claude",
                                                      "command": "deploy prod"}), "why", None, "hook",
                              hold_until=time.time() + 3600)
    db.set_status("w1", "waiting")
    since = db.get_agent("w1").status_since
    assert cull.note_stuck(db, since + 1, {}) != [] and len(sent) == 1
