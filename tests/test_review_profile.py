"""default_review_profile: which reviewer request_review picks by default.
The baseline reviewer, unless frith Pro's learner picks another that can run
here; frith never reaches for a different model on its own."""

import shutil
import time

import pytest

from frith import agents
from frith.config import RepoConfig
from frith.db import Agent
from frith.native import runner


def worker(provider="claude"):
    return Agent("w1", "ws1", "developer", provider, None, "assign", "idle", "@0", None, time.time())


@pytest.fixture
def which(monkeypatch):
    """Control whether codex is on PATH."""
    state = {"codex": False}
    monkeypatch.setattr(shutil, "which", lambda name: name if state["codex"] and name.endswith("codex") else None)
    return state


@pytest.fixture
def probe(monkeypatch):
    """Control the endpoint probe's answer; records the calls."""
    state = {"result": (True, "model qwen3-coder:30b is available"), "calls": 0}

    def fake_probe(endpoint, timeout=3.0):
        state["calls"] += 1
        result = state["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(runner, "probe", fake_probe)
    return state


class FakeLearner:
    def __init__(self, pick):
        self.pick, self.seen, self.last_reason = pick, None, None

    def suggest(self, info, names, default):
        self.seen = (list(names), default)
        return self.pick or default


@pytest.fixture
def learner(monkeypatch):
    from frith import learning

    box = {"learner": None}
    monkeypatch.setattr(learning, "plugin", lambda cfg, repo_root: box["learner"])
    return box


def test_explicit_review_profile_wins(which, probe, learner, db):
    which["codex"] = True
    learner["learner"] = FakeLearner("reviewer-codex")
    cfg = RepoConfig(review_profile="mine")
    assert agents.default_review_profile(cfg, worker(), db, "/repo") == "mine"
    assert probe["calls"] == 0


def test_without_a_learner_it_is_the_baseline_and_nothing_is_probed(which, probe, learner, db):
    which["codex"] = True
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker(), db, "/repo") == "rev"
    assert agents.default_review_profile(RepoConfig(), worker()) == "reviewer"  # no db: same
    assert probe["calls"] == 0


def test_learner_chooses_among_the_reviewers_that_can_run(which, probe, learner, db):
    which["codex"] = True
    learner["learner"] = fake = FakeLearner("reviewer-codex")
    assert agents.default_review_profile(RepoConfig(), worker(), db, "/repo", "task") == "reviewer-codex"
    assert fake.seen == (["reviewer", "reviewer-codex", "reviewer-local"], "reviewer")


def test_learner_keeping_the_baseline_keeps_it(which, probe, learner, db):
    which["codex"] = True
    learner["learner"] = FakeLearner(None)
    assert agents.default_review_profile(RepoConfig(), worker("codex"), db, "/repo") == "reviewer"


def test_unavailable_reviewers_are_not_offered(which, probe, learner, db):
    probe["result"] = (True, "reachable, but model qwen3-coder:30b is not among: llama3")
    learner["learner"] = fake = FakeLearner(None)
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker(), db, "/repo") == "rev"
    assert fake.seen is None  # one candidate left: the learner isn't even asked


def test_probe_failures_mean_not_available(which, probe, learner, db, monkeypatch):
    learner["learner"] = fake = FakeLearner(None)
    probe["result"] = OSError("boom")
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker(), db, "/repo") == "rev"

    def boom(profile):
        raise ValueError("no endpoint")

    monkeypatch.setattr(runner, "endpoint_for", boom)
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker(), db, "/repo") == "rev"
    assert fake.seen is None
