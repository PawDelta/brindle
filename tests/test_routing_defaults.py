"""The shipped routing: every tier starts on a profile that loads, the light
tier on developer-light, and a task with no weight still reaches the learner."""

import pytest

from brindle import learning, plugins, workspaces
from brindle.config import DEFAULT_ROUTING, WEIGHTS, RepoConfig
from brindle.profiles import load_profile
from brindle.pro import learning as pro_learning


class Recorder(learning.LearningPlugin):
    def __init__(self):
        self.asked = []

    def record(self, task, outcome):
        pass

    def suggest(self, task, candidates, default=None):
        self.asked.append((list(candidates), default))
        return None


@pytest.fixture(autouse=True)
def clear_cache():
    plugins.reset()
    yield
    plugins.reset()


@pytest.mark.parametrize("weight", WEIGHTS)
def test_every_tiers_first_profile_loads(weight, repo):
    assert load_profile(DEFAULT_ROUTING[weight][0], str(repo)).name == DEFAULT_ROUTING[weight][0]


def test_light_starts_on_developer_light():
    assert DEFAULT_ROUTING["light"][0] == "developer-light"
    assert DEFAULT_ROUTING["light"][1:] == ["developer", "developer-codex", "developer-local"]
    p = load_profile("developer-light")
    assert p.model == "haiku" and p.effort == "low"


def test_heavy_starts_on_developer_with_developer_heavy_next():
    assert DEFAULT_ROUTING["heavy"][:2] == ["developer", "developer-heavy"]


def test_an_unweighted_task_offers_the_learner_the_medium_candidates(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    p = Recorder()
    monkeypatch.setattr(pro_learning, "CloudLearner", lambda repo_root: p)
    cfg = RepoConfig(learning="cloud")
    assert learning.choose(db, cfg, ws.repo_root, "fix a bug") is None
    assert p.asked == [(DEFAULT_ROUTING["medium"], DEFAULT_ROUTING["medium"][0])]
