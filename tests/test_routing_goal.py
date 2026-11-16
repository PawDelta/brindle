"""Routing goals: the weights routing aims for (cost, speed, first try), set at
the personal < repo < session < task levels, clamped or locked by org policy,
shown by GET/PUT /v1/routing/goal and sent with the learner's suggest."""

import json
import time

import pytest

from brindle import autopilot, config, learning, plugins, quota, workspaces
from brindle.db import Agent
from brindle.pro import learning as pro_learning
from brindle.pro import team_policy
from brindle.pro.team_policy import Access
from test_daemon_actions import _send

KEY = pro_learning.OrgKey("org_1", "k1", b"0" * 32)
CHEAP = {"cost": 0.7, "speed": 0.1, "first_try": 0.2}


class Asked(learning.LearningPlugin):
    """A learner that keeps the default and remembers what it was asked with."""

    def __init__(self):
        self.tasks = []

    def record(self, task, outcome):
        pass

    def suggest(self, task, candidates, default=None):
        self.tasks.append(task)
        return default


@pytest.fixture
def boss(db, repo, monkeypatch):
    """Session ``boss`` in ``repo``, routing two medium candidates through a recording learner."""
    from brindle.pro import learning as pro

    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"learning": "cloud", "routing": {"medium": ["developer", "developer-codex"]}}))
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    asked = Asked()
    plugins.reset()
    monkeypatch.setattr(pro, "CloudLearner", lambda repo_root: asked)
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)
    yield asked
    plugins.reset()


def _asked_with(db, repo, asked, **kw):
    autopilot.choose_profile(db, "boss", str(repo), None, "task", None, "medium", **kw)
    return asked.tasks[-1].weights


def test_the_most_specific_level_wins_and_the_org_clamps_it(db, repo, boss, monkeypatch):
    # Nothing set anywhere: no goal, and the learner is asked exactly as before goals.
    assert _asked_with(db, repo, boss) is None
    config.user_config_path().write_text(json.dumps({"routing_goal": "fast"}))
    assert _asked_with(db, repo, boss) == (0.1, 0.7, 0.2)
    config.set_repo(str(repo), "routing_goal", {"cost": 1})
    assert _asked_with(db, repo, boss) == (1.0, 0.0, 0.0)
    db.set_session_routing_goal("boss", {"cost": 0.2, "speed": 0.1, "first_try": 0.7})
    assert _asked_with(db, repo, boss) == (0.2, 0.1, 0.7)
    assert _asked_with(db, repo, boss, goal="cheap") == tuple(CHEAP.values())
    # An org floor raises cost to 0.5 and rescales the rest; a lock sets its default whatever is asked.
    floor = team_policy.parse_policy("acme", {"version": 1, "policy": {
        "routing_goal": {"min": {"cost": 0.5}}}}).routing_goal
    monkeypatch.setattr(team_policy, "access", lambda repo: Access(org_id="acme", routing_goal=floor))
    assert _asked_with(db, repo, boss, goal="first_try") == (0.5, 0.0625, 0.4375)
    lock = team_policy.parse_policy("acme", {"version": 1, "policy": {
        "routing_goal": {"locked": True, "default": {"speed": 1}}}}).routing_goal
    monkeypatch.setattr(team_policy, "access", lambda repo: Access(org_id="acme", routing_goal=lock))
    assert _asked_with(db, repo, boss, goal="cheap") == (0.0, 1.0, 0.0)


def test_a_goal_put_at_a_level_is_what_get_reports_with_the_triangle(db, repo, boss):
    code, reply = _send("PUT", "/v1/routing/goal", {"repo": str(repo), "scope": "session", "session": "boss",
                                                    "weights": {"cost": 5, "speed": 0.2}})
    assert code == 400, reply
    code, reply = _send("PUT", "/v1/routing/goal", {"repo": str(repo), "scope": "repo",
                                                    "weights": {"cost": 0.5, "speed": 0.2, "first_try": 0.3}})
    assert code == 200, reply
    assert config._read_json(config.scope_paths(str(repo))["repo"])["routing_goal"] == {
        "cost": 0.5, "speed": 0.2, "first_try": 0.3}
    code, got = _send("GET", f"/v1/routing/goal?repo={repo}")
    assert code == 200, got
    assert (got["weights"], got["scope"], got["locked"]) == ({"cost": 0.5, "speed": 0.2, "first_try": 0.3},
                                                            "repo", False)
    # Your routing (brindle B's run, against Fable alone at the centre) lands near 49/10/41 (§6a).
    assert got["preview"]["landed"] == pytest.approx({"cost": 0.49, "speed": 0.10, "first_try": 0.41}, abs=0.01)
    assert {r["name"]: r["measured"] for r in got["preview"]["references"]} == {
        "Fable 5.2 alone": True, "Gemini 4 Argon alone": False, "GPT-6 Astra alone": False, "brindle B": True}
    code, got = _send("PUT", "/v1/routing/goal", {"repo": str(repo), "scope": "session", "session": "boss",
                                                  "weights": {"speed": 1}})
    assert (code, got["scope"], got["weights"]) == (200, "session", {"cost": 0.0, "speed": 1.0, "first_try": 0.0})


def test_an_org_lock_refuses_a_put_and_get_shows_the_org_goal(db, repo, boss, monkeypatch):
    lock = team_policy.parse_policy("acme", {"version": 1, "policy": {
        "routing_goal": {"locked": True, "default": "first_try"}}}).routing_goal
    monkeypatch.setattr(team_policy, "access", lambda repo: Access(org_id="acme", routing_goal=lock))
    code, reply = _send("PUT", "/v1/routing/goal", {"repo": str(repo), "scope": "repo", "weights": {"cost": 1}})
    assert code == 403, reply
    assert "routing_goal" not in config._read_json(config.scope_paths(str(repo))["repo"])
    code, got = _send("GET", f"/v1/routing/goal?repo={repo}")
    assert (got["weights"], got["scope"], got["locked"]) == (
        {"cost": 0.2, "speed": 0.1, "first_try": 0.7}, "org", True)


@pytest.mark.parametrize("weights,signals,sent", [
    ((0.5, 0.2, 0.3), True, {"cost": 0.5, "speed": 0.2, "first_try": 0.3}),
    pytest.param((0.5, 0.2, 0.3), False, None,
                 marks=pytest.mark.variant("an older server rejects unknown keys: no weights without signals")),
    pytest.param(None, True, None, marks=pytest.mark.variant("no goal set: nothing sent, the server's default applies")),
])
def test_the_goal_reaches_the_suggest_payload_only_when_set_and_taken(weights, signals, sent):
    task = learning.TaskInfo(repo_root="/r", task="fix the login bug", weight="medium", weights=weights)
    body = pro_learning.suggest_payload(KEY, "repo", task, ["developer", "developer-codex"], signals=signals)
    assert body.get("weights") == sent
