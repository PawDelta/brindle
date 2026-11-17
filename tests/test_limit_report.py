import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, autopilot, provider_health, subscription_usage, workspaces
from brindle.db import Agent

NOW = time.time()
# quota drops windows already past their reset: keep it ahead of the real clock.
RESET = NOW + 30 * 86400


@pytest.fixture
def session(db, repo, monkeypatch):
    """An autopilot supervisor, live but unreachable (its messages queue),
    and a live Claude worker on its own branch with one commit."""
    monkeypatch.setattr(subscription_usage, "identity", lambda: "test-subscription")
    root = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", root.id, "supervisor", "claude", None, "interactive",
                       "processing", "@0", None, NOW))
    ws = workspaces.create(db, str(repo), "feat/parse").workspace
    (Path(ws.path) / "parse.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm 'Add the flag parser'", Path(ws.path))
    (Path(ws.path) / "notes.txt").write_text("half done\n")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "@1",
                       None, NOW, task="Parse the flags", done_when="pytest tests/test_parse.py passes"))
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Goal", [("M1", "true", None)])
    stopped = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "boss" and a.status != "paused")
    monkeypatch.setattr(agents, "_stop", lambda db_, a: stopped.append(a.id))
    return ws, stopped


def use(percent):
    autopilot.record_usage({"rate_limits": {"five_hour": {"used_percentage": percent, "resets_at": RESET}}})


def inbox(db, agent_id):
    return [r["body"] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (agent_id,))]


def test_full_window_pauses_the_worker_and_reports_it_at_once(db, session):
    ws, stopped = session
    use(100)
    agents.handle_hook(db, "w1", "stop-failure", {"error": "rate_limit", "error_type": "rate_limit"})
    assert stopped == ["w1"] and db.get_autopilot("boss").state == "usage_paused"
    [report] = inbox(db, "boss")
    assert ws.branch in report and "Add the flag parser" in report and "notes.txt" in report
    from brindle import limit_report

    assert limit_report.when(RESET) in report


def test_a_429_short_of_the_limit_pauses_nothing_and_runs_the_turn_again(db, session, monkeypatch):
    _, stopped = session
    use(40)
    agents.handle_hook(db, "w1", "stop-failure", {"error": "rate_limit", "error_type": "rate_limit"})
    assert stopped == [] and db.get_autopilot("boss").state == "running"
    assert db.get_agent("w1").status == "idle" and inbox(db, "boss") == []
    from brindle import limit_report

    sent = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **kw: sent.append(to))
    assert limit_report.rerun_due(db, now=time.time()) == []          # still rate limited
    later = time.time() + provider_health.RATE_LIMIT_SECONDS + 1
    assert limit_report.rerun_due(db, now=later) and sent == ["w1"]
    assert limit_report.rerun_due(db, now=later) == [] and sent == ["w1"]   # once
