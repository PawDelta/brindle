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
    # No other provider can take the task over (brindle.handover, tested in
    # test_handover.py): a full window pauses, whatever CLIs this machine has.
    from brindle import handover

    monkeypatch.setattr(handover, "replacement", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "boss" and a.status != "paused")
    monkeypatch.setattr(agents, "_stop", lambda db_, a: stopped.append(a.id))
    return ws, stopped


def use(percent):
    autopilot.record_usage({"rate_limits": {"five_hour": {"used_percentage": percent, "resets_at": RESET}}})


def inbox(db, agent_id):
    return [r["body"] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (agent_id,))]


@pytest.mark.variant("the brief's required case: the hook itself, at a full window, reports in the same call "
                     "with branch, commits and reset time; the sweep-path test reaches at_limit another way")
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


def rate_limited_then(db, monkeypatch, percent_later):
    """w1's turn stops on a 429 at 40% use; the usage reads ``percent_later``
    by the time the rate limit lifts. Returns what was sent to whom."""
    from brindle import limit_report

    use(40)
    agents.handle_hook(db, "w1", "stop-failure", {"error": "rate_limit", "error_type": "rate_limit"})
    use(percent_later)
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **kw: sent.append((to, body)))
    return limit_report, sent, time.time() + provider_health.RATE_LIMIT_SECONDS + 1


def test_a_turn_run_since_the_429_drops_its_rerun(db, session, monkeypatch):
    limit_report, sent, later = rate_limited_then(db, monkeypatch, 40)
    stop = db.get_agent("w1").status_since
    db.set_status("w1", "processing")                 # its supervisor sent it a new turn,
    db.update_agent("w1", status="idle", status_since=stop + 1)   # which finished before the sweep
    assert limit_report.rerun_due(db, now=later) == [] and sent == []


@pytest.mark.variant("race: a new turn starts while the sweep reads provider health, after its stop check")
def test_a_turn_started_during_the_health_read_gets_no_rerun(db, session, monkeypatch):
    limit_report, sent, later = rate_limited_then(db, monkeypatch, 40)
    health_of = limit_report.health_of

    def new_turn_meanwhile(db_, agent, now=None):
        db.set_status("w1", "processing")
        return health_of(db_, agent, now)

    monkeypatch.setattr(limit_report, "health_of", new_turn_meanwhile)
    assert limit_report.rerun_due(db, now=later) == [] and sent == []


def test_a_window_filled_since_the_429_is_reported_not_run_again(db, session, monkeypatch):
    _, stopped = session
    limit_report, sent, later = rate_limited_then(db, monkeypatch, 100)
    assert limit_report.rerun_due(db, now=later)
    assert stopped == ["w1"] and [to for to, _ in sent] == ["boss"] and "notes.txt" in sent[0][1]


def test_repeated_overloads_report_the_worker_in_full(db, session):
    ws, stopped = session
    for i in range(provider_health.ERROR_COUNT):
        db.update_agent("w1", status="processing", status_since=NOW + i)
        agents.handle_hook(db, "w1", "stop-failure", {"error": "overloaded_error (529)"})
    assert stopped == [] and db.get_autopilot("boss").state == "running"
    assert any("notes.txt" in body and ws.branch in body for body in inbox(db, "boss"))
