"""Which reviewer profile request_review uses: an explicit `profile` param,
else the repo's `review_profile` config, else the baseline reviewer unless
brindle Pro's learner picks another reviewer that can run here."""

import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, git, learning, local_setup, pipeline, workspaces
from brindle.config import RepoConfig, load_repo_config
from brindle.db import Agent, Workspace
from brindle.native import runner
from brindle.profiles import compose, load_profile


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "handoff", "done", "@0", "did stuff", time.time()))
    return ws


def worker():
    return Agent("w1", "ws1", "developer", "claude", None, "assign", "idle", "@0", None, time.time())


@pytest.fixture
def env(monkeypatch):
    """A PATH with claude, and codex when ``env["codex"]``; the local
    reviewer's endpoint answers and its model fits in memory, and its
    probes are counted."""
    state = {"codex": False, "probes": 0, "learner": None}

    def which(name):
        base = name.rsplit("/", 1)[-1]
        return f"/usr/bin/{base}" if base == "claude" or (state["codex"] and base == "codex") else None

    def probe(endpoint, timeout=3.0):
        state["probes"] += 1
        return True, "model qwen3-coder:30b is available"

    monkeypatch.setattr(agents.shutil, "which", which)
    monkeypatch.setattr(runner, "probe", probe)
    monkeypatch.setattr(local_setup, "memory_problem", lambda profile: None)
    monkeypatch.setattr(learning, "plugin", lambda cfg, repo_root: state["learner"])
    return state


class FakeLearner:
    def __init__(self, pick):
        self.pick, self.seen, self.info, self.last_reason = pick, None, None, None

    def suggest(self, info, names, default):
        self.seen = (list(names), default)
        self.info = info
        return self.pick or default


def test_request_review_raises_clearly_for_an_unknown_configured_review_profile(db, worker_ws):
    with pytest.raises(agents.AgentError, match="bogus-profile"):
        agents.request_review(db, None, worker_ws, cfg=RepoConfig(review_profile="bogus-profile"))


def test_without_a_learner_it_is_the_baseline_and_nothing_is_probed(env, db):
    env["codex"] = True
    assert agents.default_review_profile(RepoConfig(), worker(), db, "/repo") == "review.claude-sonnet.high"
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker(), db, "/repo") == "rev"
    assert agents.default_review_profile(RepoConfig(), worker()) == "review.claude-sonnet.high"  # no db: same
    assert env["probes"] == 0


def test_learner_chooses_among_the_reviewers_that_can_run(env, db):
    env["codex"] = True
    env["learner"] = fake = FakeLearner("review.gpt")
    db.add_routing_decision("/repo", task_id=None, agent_id="w1", weight="heavy",
                            baseline_profile="dev.claude-opus", profile="dev.claude-opus", learned=False)
    assert agents.default_review_profile(RepoConfig(), worker(), db, "/repo", "task") == "review.gpt"
    assert fake.seen == (["review.claude-sonnet.high", "review.gpt", "review.qwen3-coder"], "review.claude-sonnet.high")
    assert fake.info.weight == "heavy"


@pytest.mark.parametrize("alternative,small,models,expected", [
    (True, False, "override", "dev.claude-sonnet"),
    (False, False, "override", "dev.claude-opus"),
    (True, True, None, "review.claude-sonnet.low"),
    (True, False, None, "review.claude-sonnet.high"),
    (True, False, "empty", "dev.claude-sonnet"),
    (True, False, "partial", "dev.claude-sonnet"),
    (True, False, "recorded", "dev.claude-sonnet"),
])
def test_review_size_pool_avoids_writers_model_when_possible(env, db, tmp_path, monkeypatch,
                                                           alternative, small, models, expected):
    ws = Workspace("ws1", str(tmp_path), "feat", "branch", "feat", "main", str(tmp_path), None, "", 0)
    db.add_workspace(ws)
    writer = worker()
    writer.profile = "dev.claude-opus"
    writer.weight = None if models == "recorded" else "heavy"
    db.add_agent(writer)
    if models == "recorded":
        db.add_routing_decision(ws.repo_root, task_id=None, agent_id=writer.id, weight="heavy",
                                baseline_profile=writer.profile, profile=writer.profile, learned=False)
    settings = {"routing": {"heavy": ["dev.claude-opus", "dev.claude-sonnet"]}}
    if models is not None:
        orders = ({"heavy": ["dev.claude-opus", "dev.claude-sonnet"]}
                  if models in {"override", "recorded"} else {})
        if models == "partial":
            orders["light"] = ["review.gpt.low"]
        settings["agent_models"] = {"reviewer": orders}
    config_dir = tmp_path / ".brindle"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps(settings))
    cfg = load_repo_config(tmp_path)
    monkeypatch.setattr(git, "out", lambda *a: f"{1 if small else 500}\t0\tsrc/main.py\0")
    can_run = agents._reviewer_can_run

    def available(name, workspace, config):
        if not alternative and name != "dev.claude-opus":
            raise agents.AgentError("model unavailable")
        can_run(name, workspace, config)

    monkeypatch.setattr(agents, "_reviewer_can_run", available)
    chosen, why = agents._review_choice(db, ws, None, cfg)
    assert chosen == expected
    reviewer = compose("reviewer", chosen, ws.repo_root)
    assert (reviewer.model == load_profile(writer.profile).model) is (not alternative)
    if not alternative:
        assert why is not None and "model" in why
    if models is not None:
        failed_reviewer = worker()
        failed_reviewer.mode = "review"
        assert pipeline._fallback_reviewer(cfg, failed_reviewer, ws.repo_root,
                                           db=db, ws=ws, worker=writer)[0] == expected


def test_request_review_launches_developer_model_with_reviewer_role(env, db, worker_ws, monkeypatch):
    launched = []

    def launch(database, reviewer, workspace, **kwargs):
        stored = database.get_agent(reviewer.id)
        launched.append((stored.agent, agents._profile_for(database, stored, workspace)))

    monkeypatch.setattr(agents, "_launch", launch)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    reviewer = agents.request_review(db, None, worker_ws, "dev.claude-opus",
                                    cfg=RepoConfig(review_sizing=False))
    assert reviewer.agent == "reviewer"
    role, profile = launched[0]
    assert role == "reviewer"
    expected = compose("reviewer", "dev.claude-opus", worker_ws.repo_root)
    assert profile.model == expected.model
    assert expected.prompt in profile.prompt
    assert profile.allowed_tools == expected.allowed_tools
    assert load_profile("dev.claude-opus").prompt not in profile.prompt
