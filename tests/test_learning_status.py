"""Hosted status and recent local routing are visible without making status required."""
import json
import time

import pytest

from brindle import savings
from brindle import autopilot, learning
from brindle.config import RepoConfig
from brindle.pro import auth, credentials
from brindle.pro.learning import CloudLearner


class StatusTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, form, headers):
        assert isinstance(form, auth.JSONBody)
        self.calls.append((method, url, headers, dict(form)))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def learner(tmp_path, repo, response):
    store = credentials.FileStore(tmp_path / "credentials")
    store.save({"access_token": "test-token", "refresh_token": "test-refresh",
                "access_expires_at": time.time() + 900})
    transport = StatusTransport(response)
    return CloudLearner(str(repo), client=auth.Client("https://example.com", transport),
                        store=store, org=lambda: "test-org", start_thread=False), transport


def test_server_status(db, repo, tmp_path):
    report, transport = learner(tmp_path, repo, (200, {
        "month": "2026-10", "tasks": 12, "learned": 2, "baseline": 10,
        "min_tasks": 5, "exploration": "collecting results", "profiles": [
            {"profile": "cheap", "tasks": 2, "mean": .75},
            {"profile": "developer", "tasks": 10, "mean": .9},
        ],
    }))
    text = report.report()
    assert "This month (2026-10): 12 finished tasks; 2 picked by learning, 10 used the default." in text
    assert "Exploration: collecting results" in text
    assert "cheap: 2 results (below 5 results); mean outcome 0.750" in text
    assert "developer: 10 results; mean outcome 0.900" in text
    method, url, headers, body = transport.calls[0]
    assert method == "POST" and url.endswith("/learning/status")
    assert body == {"org_id": "test-org"}
    assert headers["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize("response", [(404, {}), auth.TransportError("offline"), (500, {}), (200, {})])
def test_status_unavailable_is_one_line(db, repo, tmp_path, response):
    report, _ = learner(tmp_path, repo, response)
    text = report.report()
    assert text.splitlines().count("The server doesn't report status yet.") == 1
    assert "Local picks in this repo" in text


def test_local_picks_keeps_and_candidates(db, repo, tmp_path):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({
        "routing": {"light": ["cheap", "developer"], "medium": ["developer"], "heavy": ["reviewer"]},
        "learning_candidates": ["cheap", "reviewer"],
    }))
    for i in range(25):
        savings.record(db, str(repo), {"profile": "developer", "weight": "light",
                       "weight_routed": True, "learned": i >= 20,
                       "kept_note": "collecting results" if i < 20 else None})
    savings.record(db, str(repo), {"profile": "reviewer", "weight": "heavy", "learned": True})
    savings.record(db, "other-repo", {"profile": "cheap", "weight_routed": True, "learned": True})
    report, _ = learner(tmp_path, repo, (404, {}))
    text = report.report()
    assert "last 20 weight-routed assignments): 5 picked by learning, 15 kept the default" in text
    assert "Kept the default: collecting results (15)" in text
    assert "light: cheap, developer" in text
    assert "medium: developer" in text
    assert "heavy: reviewer" in text
    assert "learning_candidates: cheap, reviewer" in text


def test_empty_local_history(db, repo, tmp_path):
    report, _ = learner(tmp_path, repo, (404, {}))
    assert "last 0 weight-routed assignments): 0 picked by learning, 0 kept the default" in report.report()


def test_older_weight_records_are_visible(db, repo, tmp_path):
    db.add_routing_decision(str(repo), task_id=None, agent_id=None, weight="light",
                            baseline_profile="developer", profile="developer", learned=False)
    report, _ = learner(tmp_path, repo, (404, {}))
    text = report.report()
    assert "last 1 weight-routed assignments): 0 picked by learning, 1 kept the default" in text
    assert "Older records count assignments with a weight; keep reasons weren't saved." in text


def test_weight_routing_records_keep_reason(db, repo, monkeypatch):
    cfg = RepoConfig(routing={"light": ["developer"]})
    monkeypatch.setattr(autopilot, "_unavailable", lambda *args: None)
    monkeypatch.setattr(learning, "choose_why", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(learning, "kept_note", lambda *args: "collecting results")
    decision = {}
    profile, learned, _ = autopilot._route_by_weight(
        db, cfg, str(repo), "light", "task", [], decision)
    decision.update(profile=profile, learned=learned, weight="light")
    savings.record(db, str(repo), decision, agent_id="worker")
    row = db.routing_decision_for_agent("worker")
    assert row.weight_routed == 1
    assert row.kept_note == "collecting results"
    assert row.learned == 0
