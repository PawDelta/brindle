"""What the hosted learner hears beyond the event: a worker that produced
nothing counts as a failure, and a task's real dollars and time (its own and
its reviewers') are in the record."""

import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import (agents, learning, limit_report, mcp_server, pipeline, plugins, pricing, procs,
                     provider_health, rewind, usage, workspaces)
from brindle.config import RepoConfig
from brindle.db import Agent
from brindle.pro import learning as pro_learning

KEY = pro_learning.OrgKey("org_1", "k1", b"0" * 32)


class Recorder(learning.LearningPlugin):
    def __init__(self):
        self.events = []

    def record(self, task, outcome):
        self.events.append((task, outcome))

    def suggest(self, task, candidates, default=None):
        return None


@pytest.fixture
def recorder(monkeypatch):
    plugins.reset()
    p = Recorder()
    monkeypatch.setattr(pro_learning, "CloudLearner", lambda repo_root: p)
    monkeypatch.setattr(plugins, "auto_learning", lambda: plugins.CLOUD)
    yield p
    plugins.reset()


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def add(db, ws, agent_id, mode, status="processing", status_since=None):
    a = Agent(agent_id, ws.id, "developer", "claude", None, mode, status, "", None,
              time.time() - 30, status_since=status_since,
              task="Add export:\n- write the csv writer\n- wire the CLI flag\n- document it")
    db.add_agent(a)
    return a


@pytest.mark.variant("an idle worker with no report is a stalled failure, not a plain removal")
def test_an_idle_worker_without_a_report_is_recorded_as_a_stalled_failure(db, ws, recorder):
    w = add(db, ws, "w1", "assign", status="idle", status_since=time.time() - 20)
    pipeline.note_removed_unmerged(db, ws, worker=w)
    task, outcome = recorder.events[-1]
    assert outcome.event == "removed_unmerged"
    assert outcome.failure == "idle" and outcome.idle_seconds >= 20
    body = pro_learning.record_payload(KEY, "repo", task, outcome, signals=True)
    assert body["stalled"] is True and body["failure"] == "idle"
    assert body["parts"] == 3


@pytest.mark.parametrize("stop, resumed", [
    pytest.param("limit", False, marks=pytest.mark.variant(
        "a usage-limit stop marked by at_limit, never resumed, is the provider's stop at every signal level")),
    pytest.param("overload", False, marks=pytest.mark.variant(
        "a first overload, the provider not yet erroring, is still the provider's stop")),
    pytest.param("limit", True, marks=pytest.mark.variant(
        "back at work once the limit lifted, then abandoned: its own failure again")),
])
def test_a_worker_stopped_for_its_limit_and_removed_unmerged_sends_no_failure(db, ws, recorder, stop, resumed):
    w = add(db, ws, "w1", "assign", status="idle", status_since=time.time() - 20)
    if stop == "limit":
        limit_report.at_limit(db, w, provider_health.Health(provider_health.LIMITED, time.time() + 600))
    else:
        limit_report.on_stop_failure(db, w, rate_limited=False, overloaded=True)
    if resumed:
        db.set_status("w1", "processing")
        db.set_status("w1", "idle")
    pipeline.note_removed_unmerged(db, ws, worker=w)
    sent = [body for task, outcome in recorder.events for level in (0, 1, 2)
            if (body := pro_learning.record_payload(KEY, "repo", task, outcome, signals=level))]
    if resumed:
        assert any(b.get("failure") == "idle" for b in sent)
        return
    assert not [b for b in sent if "failure" in b or "stalled" in b or b["event"] == "removed_unmerged"]


@pytest.mark.variant("a rate-limited park: level 2 sends the interrupted event with its cause and the "
                     "unparked wall time, level 1 sends nothing")
def test_with_signals_2_the_interrupted_event_carries_the_cause(db, ws, recorder):
    w = add(db, ws, "w1", "assign", status="idle", status_since=time.time() - 25)
    limit_report.park(db, w, provider_health.RATE_LIMITED, now=time.time() - 20)
    pipeline.note_removed_unmerged(db, ws, worker=w)
    task, outcome = recorder.events[-1]
    body = pro_learning.record_payload(KEY, "repo", task, outcome, signals=2)
    assert body["event"] == "interrupted" and body["cause"] == "rate_limited"
    assert body["wall_seconds"] < 20            # created 30s ago, parked for the last 20
    assert set(body) <= pro_learning.RECORD_KEYS
    assert pro_learning.record_payload(KEY, "repo", task, outcome, signals=1) is None


@pytest.mark.parametrize("branch, failure", [
    ("untouched", "no_commits"),
    pytest.param("committed", None, marks=pytest.mark.variant(
        "a branch that moved from its recorded start is never claimed as no_commits")),
    pytest.param("merged_by_git", "no record", marks=pytest.mark.variant(
        "level with its base because plain git merged it: no failure is recorded")),
])
def test_removal_records_what_the_worktree_and_reviewers_showed_before_teardown(
        db, repo, recorder, monkeypatch, branch, failure):
    """The real remove_workspace: a reported worker whose branch never moved is
    a no_commits failure (not a merge), one merged by plain git is not, and the
    reviewers' spend survives the deletion of their rows."""
    model = "claude-sonnet-5-5"
    price = pricing.price_for(model)
    root = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", root.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    ws = workspaces.create(db, str(repo), "feat").workspace
    w = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "idle", "@0",
              "done", time.time() - 30, task="the task")
    db.add_agent(w)
    rewind.mark_baseline(w, ws)   # what its session start records
    if branch != "untouched":
        (Path(ws.path) / "new.py").write_text("x = 1\n")
        sh("git add new.py && git commit -qm work", Path(ws.path))
    if branch == "merged_by_git":
        sh(f"git merge -q --ff-only {ws.branch}", Path(repo))
    db.add_agent(Agent("r1", ws.id, "reviewer", "claude", "boss", "review", "idle", "@0",
                       "verdict", time.time() - 10))
    monkeypatch.setattr(usage, "agent_usage",
                        lambda db, a: usage.Usage(500, 100, 0, 0, model) if a.id == "r1" else None)
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(procs, "cwds", dict)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)

    assert mcp_server.remove_workspace(ws.id).startswith("Removed")
    mine = [e for e in recorder.events if e[0].agent_id == "w1"]
    if failure == "no record":   # removed as merged: the learner heard nothing new
        assert not [o for _, o in mine if o.failure or o.event == "removed_unmerged"]
        return
    [(task, outcome)] = mine
    assert outcome.event == "removed_unmerged" and outcome.failure == failure
    assert outcome.review_cost_usd == pytest.approx(price.cost(500, 100, 0, 0))


def test_real_cost_and_time_are_in_the_record(db, ws, recorder, monkeypatch):
    model = "claude-sonnet-5-5"
    price = pricing.price_for(model)
    assert price is not None
    spend = {"w1": usage.Usage(1000, 2000, 0, 0, model),
             "r1": usage.Usage(500, 100, 0, 0, model)}
    monkeypatch.setattr(usage, "agent_usage", lambda db, a: spend.get(a.id))
    w = add(db, ws, "w1", "assign")
    add(db, ws, "r1", "review")
    learning.note(db, RepoConfig(learning="cloud"), w, ws, merged=True, checks_passed=True)
    task, outcome = recorder.events[-1]
    worker_cost = price.cost(1000, 2000, 0, 0)
    review_cost = price.cost(500, 100, 0, 0)
    assert outcome.cost_usd == pytest.approx(worker_cost)
    assert outcome.review_cost_usd == pytest.approx(review_cost)
    assert outcome.failure is None
    body = pro_learning.record_payload(KEY, "repo", task, outcome, signals=True)
    assert body["cost_usd"] == pytest.approx(worker_cost, abs=1e-4)
    assert body["review_cost_usd"] == pytest.approx(review_cost, abs=1e-4)
    assert body["wall_seconds"] >= 30 and body["stalled"] is False
    assert set(body) <= pro_learning.RECORD_KEYS

    spend["w1"] = usage.Usage(1000, 2000, 0, 0, "no-such-model")
    monkeypatch.setattr(pricing, "profile_price", lambda *a, **k: None)
    learning.note(db, RepoConfig(learning="cloud"), w, ws, merged=True)
    body = pro_learning.record_payload(KEY, "repo", *recorder.events[-1], signals=True)
    assert "cost_usd" not in body   # unknown price: left out, never sent as $0
