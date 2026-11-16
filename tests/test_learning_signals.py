"""What the hosted learner hears beyond the event: a worker that produced
nothing counts as a failure, and a task's real dollars and time (its own and
its reviewers') are in the record."""

import time

import pytest

from brindle import learning, pipeline, plugins, pricing, usage, workspaces
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


def test_an_idle_worker_without_a_report_is_recorded_as_a_stalled_failure(db, ws, recorder):
    w = add(db, ws, "w1", "assign", status="idle", status_since=time.time() - 20)
    pipeline.note_removed_unmerged(db, ws, worker=w)
    task, outcome = recorder.events[-1]
    assert outcome.event == "removed_unmerged"
    assert outcome.failure == "idle" and outcome.idle_seconds >= 20
    body = pro_learning.record_payload(KEY, "repo", task, outcome, signals=True)
    assert body["stalled"] is True and body["failure"] == "idle"
    assert body["parts"] == 3


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
