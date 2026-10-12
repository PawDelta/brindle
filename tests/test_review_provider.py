"""Which reviewer profile request_review uses: an explicit `profile` param,
else the repo's `review_profile` config, else the baseline reviewer unless
brindle Pro's learner picks another reviewer that can run here."""

import asyncio
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, learning, mcp_server, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent
from brindle.native import runner
from brindle.profiles import Profile


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "handoff", "done", "@0", "did stuff", time.time()))
    return ws


def worker():
    return Agent("w1", "ws1", "developer", "claude", None, "assign", "idle", "@0", None, time.time())


def capture_profile(monkeypatch):
    """Record the profile request_review chose instead of starting a reviewer;
    any profile name loads, so fake names only check which one was picked."""
    captured = {}

    def fake_spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review", **kw):
        captured["profile"] = profile
        return Agent("rev1", ws_.id, profile, "claude", parent_id, mode, "starting", "@0", None, time.time())

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "load_profile", lambda name, repo_root=None: Profile(name, "", "claude", ""))
    return captured


@pytest.fixture
def env(monkeypatch):
    """A PATH with claude, and codex when ``env["codex"]``; the local
    reviewer's endpoint answers, and its probes are counted."""
    state = {"codex": False, "probes": 0, "learner": None}

    def which(name):
        base = name.rsplit("/", 1)[-1]
        return f"/usr/bin/{base}" if base == "claude" or (state["codex"] and base == "codex") else None

    def probe(endpoint, timeout=3.0):
        state["probes"] += 1
        return True, "model qwen3-coder:30b is available"

    monkeypatch.setattr(agents.shutil, "which", which)
    monkeypatch.setattr(runner, "probe", probe)
    monkeypatch.setattr(learning, "plugin", lambda cfg, repo_root: state["learner"])
    return state


class FakeLearner:
    def __init__(self, pick):
        self.pick, self.seen, self.last_reason = pick, None, None

    def suggest(self, info, names, default):
        self.seen = (list(names), default)
        return self.pick or default


def test_request_review_uses_config_review_profile_override(db, worker_ws, env, monkeypatch):
    captured = capture_profile(monkeypatch)
    agents.request_review(db, None, worker_ws, cfg=RepoConfig(review_profile="team-reviewer"))
    assert captured["profile"] == "team-reviewer"


def test_request_review_keeps_the_baseline_reviewer_for_a_claude_worker(db, worker_ws, env, monkeypatch):
    env["codex"] = True
    captured = capture_profile(monkeypatch)
    agents.request_review(db, None, worker_ws)
    assert captured["profile"] == "reviewer"


def test_request_review_raises_clearly_for_an_unknown_configured_review_profile(db, worker_ws):
    with pytest.raises(agents.AgentError, match="bogus-profile"):
        agents.request_review(db, None, worker_ws, cfg=RepoConfig(review_profile="bogus-profile"))


def test_mcp_request_review_returns_a_message_instead_of_raising_when_codex_missing(db, worker_ws, env):
    out = asyncio.run(mcp_server.request_review(worker_ws.id, profile="reviewer-codex"))
    assert "codex" in out.lower()


def test_without_a_learner_it_is_the_baseline_and_nothing_is_probed(env, db):
    env["codex"] = True
    assert agents.default_review_profile(RepoConfig(), worker(), db, "/repo") == "reviewer"
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker(), db, "/repo") == "rev"
    assert agents.default_review_profile(RepoConfig(), worker()) == "reviewer"  # no db: same
    assert env["probes"] == 0


def test_learner_chooses_among_the_reviewers_that_can_run(env, db):
    env["codex"] = True
    env["learner"] = fake = FakeLearner("reviewer-codex")
    assert agents.default_review_profile(RepoConfig(), worker(), db, "/repo", "task") == "reviewer-codex"
    assert fake.seen == (["reviewer", "reviewer-codex", "reviewer-local"], "reviewer")
