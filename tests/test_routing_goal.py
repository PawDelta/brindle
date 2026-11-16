"""Routing goals: the weights routing aims for (cost, speed, first try), set at
the personal < repo < session < task levels, clamped or locked by org policy,
shown by GET/PUT /v1/routing/goal, ordering routing by the shipped priors and
sent with the learner's suggest."""

import json
import time

import pytest

from brindle import autopilot, config, learning, plugins, quota, routing_goal, workspaces
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


def _session(db, repo, monkeypatch, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)


@pytest.fixture
def boss(db, repo, monkeypatch):
    """Session ``boss`` in ``repo``, routing two medium candidates through a recording learner."""
    from brindle.pro import learning as pro

    _session(db, repo, monkeypatch, learning="cloud", routing={"medium": ["developer", "developer-codex"]})
    asked = Asked()
    plugins.reset()
    monkeypatch.setattr(pro, "CloudLearner", lambda repo_root: asked)
    yield asked
    plugins.reset()


def _policy(monkeypatch, goal: dict) -> None:
    parsed = team_policy.parse_policy("acme", {"version": 1, "policy": {"routing_goal": goal}}).routing_goal
    monkeypatch.setattr(team_policy, "access", lambda repo: Access(org_id="acme", routing_goal=parsed))


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
    # An org floor raises cost to 0.5 and keeps the others' ratio.
    _policy(monkeypatch, {"min": {"cost": 0.5}})
    assert _asked_with(db, repo, boss, goal="first_try") == (0.5, 0.0625, 0.4375)
    # A ceiling and floors together: a bound held early is released when the sum needs it.
    _policy(monkeypatch, {"min": {"speed": 0.4, "first_try": 0.3}, "max": {"cost": 0.5}})
    assert _asked_with(db, repo, boss, goal={"cost": 0.9, "speed": 0.1}) == (0.3, 0.4, 0.3)
    # A lock sets its default, inside its own bounds, whatever is asked.
    _policy(monkeypatch, {"locked": True, "default": "cheap", "min": {"first_try": 0.8}})
    assert _asked_with(db, repo, boss, goal="fast") == (0.175, 0.025, 0.8)


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
    assert {r["name"]: r["measured"] for r in got["preview"]["references"]} == {
        "Fable 5.2 alone": True, "Gemini 4 Argon alone": True, "GPT-6 Astra alone": True, "brindle B": True}
    code, got = _send("PUT", "/v1/routing/goal", {"repo": str(repo), "scope": "session", "session": "boss",
                                                  "weights": {"speed": 1}})
    assert (code, got["scope"], got["weights"]) == (200, "session", {"cost": 0.0, "speed": 1.0, "first_try": 0.0})


def test_an_org_lock_refuses_a_put_and_get_shows_the_org_goal(db, repo, boss, monkeypatch):
    # Bounds without a default: nothing set below is Balanced within them, as the org's goal.
    _policy(monkeypatch, {"min": {"first_try": 0.8}})
    code, got = _send("GET", f"/v1/routing/goal?repo={repo}")
    assert (got["weights"], got["scope"], got["locked"]) == ({"cost": 0.1, "speed": 0.1, "first_try": 0.8},
                                                            "org", False)
    _policy(monkeypatch, {"locked": True, "default": "first_try"})
    code, reply = _send("PUT", "/v1/routing/goal", {"repo": str(repo), "scope": "repo", "weights": {"cost": 1}})
    assert code == 403, reply
    assert "routing_goal" not in config._read_json(config.scope_paths(str(repo))["repo"])
    code, got = _send("GET", f"/v1/routing/goal?repo={repo}")
    assert (got["weights"], got["scope"], got["locked"]) == (
        {"cost": 0.2, "speed": 0.1, "first_try": 0.7}, "org", True)


# developer-sonnet: dear and quick; developer-codex: cheap, slow and likelier to merge first time.
TRADEOFF = {
    "token_mix": {"input": 0.8, "output": 0.2}, "default_mix": {"medium": 1.0},
    "tiers": {t: {"mtok": 1.0, "minutes": 30, "first_try": 0.3, "p": 0.8}
              for t in ("light", "medium", "heavy", "advanced")},
    "profiles": {"developer-sonnet": {"medium": {"usd": 4.0, "minutes": 10, "first_try": 0.3}},
                 "developer-codex": {"medium": {"usd": 1.0, "minutes": 40, "first_try": 0.6}}},
}


@pytest.fixture
def offline(db, repo, monkeypatch):
    """Session ``boss`` without hosted learning, medium routing sonnet then codex, on TRADEOFF priors."""
    _session(db, repo, monkeypatch, learning="off", routing={"medium": ["developer-sonnet", "developer-codex"]})
    plugins.reset()
    monkeypatch.setattr(routing_goal, "priors", lambda: TRADEOFF)
    yield
    plugins.reset()


@pytest.mark.parametrize("goal,picked", [
    (None, "developer-sonnet"),
    ("cheap", "developer-codex"),
    pytest.param("fast", "developer-sonnet",
                 marks=pytest.mark.variant("the opposite trade-off keeps the quick candidate first")),
])
def test_without_hosted_learning_the_goal_orders_the_tier_by_the_priors(db, repo, offline, goal, picked):
    kw = {"goal": goal} if goal else {}
    assert autopilot.choose_profile(db, "boss", str(repo), None, "task", None, "medium", **kw)[0] == picked


def _medium(preview: dict) -> dict:
    return {r["kind"]: r for r in preview["roles"]}["medium"]


def test_the_preview_reflects_this_repos_routing_and_goal(db, repo, offline):
    cheap = routing_goal.preview(CHEAP, str(repo), db)
    fast = routing_goal.preview(routing_goal.PRESETS["fast"], str(repo), db)
    assert (_medium(cheap)["profile"], _medium(cheap)["previous"]) == ("developer-codex", "developer-sonnet")
    assert fast["you"]["usd"] / cheap["you"]["usd"] == pytest.approx(4.0, rel=0.01)
    # The point and the table share one yardstick: the shipped Fable alone reference.
    fable = next(r for r in cheap["references"] if r["name"] == "Fable 5.2 alone")
    assert cheap["landed"] == pytest.approx(routing_goal.place(cheap["you"], fable), abs=0.01)
    # A candidate routing can't run is never proposed: the preview's pick is dispatch's.
    config.set_repo(str(repo), "exclude_providers", ["codex"])
    proposed = _medium(routing_goal.preview(CHEAP, str(repo), db))["profile"]
    assert proposed == "developer-sonnet" == autopilot.choose_profile(
        db, "boss", str(repo), None, "task", None, "medium", goal="cheap")[0]


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
