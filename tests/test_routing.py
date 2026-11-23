"""Routing by weight: assign/handoff's `weight` picks an available profile for
the tier, after an explicit profile and a milestone's, before the default."""

import asyncio
import json
import time

import pytest

from brindle import agents, autopilot, doctor, learning, mcp_server, plugins, quota, workspaces
from brindle.config import RepoConfig, load_repo_config, user_config_path
from brindle.db import Agent
from test_launch_health import detached, launches  # noqa: F401 - fixtures: the real spawn, no CLI process
from test_merge_order import fake_spawn


class Picker(learning.LearningPlugin):
    def __init__(self, prefer=None, reason="its record is better", note=None):
        self.prefer, self.reason, self.asked, self.defaults = prefer, reason, [], []
        self.note = note

    def record(self, task, outcome):
        pass

    def suggest(self, task, candidates, default=None):
        self.asked.append((task, list(candidates)))
        self.defaults.append(default)
        if self.prefer not in candidates:
            return None
        self.last_reason = self.reason if self.prefer != default else None
        self.last_note = self.note if self.prefer == default else None
        return self.prefer


@pytest.fixture(autouse=True)
def clear_cache():
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


@pytest.fixture(autouse=True)
def everything_available(monkeypatch):
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)


def config(repo, **kw):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(kw))


def choose(db, repo, requested=None, weight=None, why=None):
    return autopilot.choose_profile(db, "boss", str(repo), requested, "task", None, weight, why)


def install(monkeypatch, plugin):
    from brindle.pro import learning as pro_learning

    plugins.reset()
    monkeypatch.setattr(pro_learning, "CloudLearner", lambda repo_root: plugin)


@pytest.mark.parametrize("names,expected", [
    (["missing-worker", "developer"], "developer"),
    pytest.param(["missing-worker"], "dev.gpt", marks=pytest.mark.variant("failure path: an entirely missing routing tier cascades to the next heavier tier and remains a doctor warning")),
])
def test_unknown_routing_profiles_are_skipped_and_diagnosed_by_doctor(db, repo, boss, names, expected):
    path = user_config_path()
    path.write_text(json.dumps({"routing": {"light": names}}))
    why = []
    assert choose(db, repo, weight="light", why=why) == (expected, False)
    assert "missing-worker" not in why[0]
    if len(names) == 1:
        assert "every light candidate was out" in why[0]
    [check] = [c for c in doctor.routing_checks(load_repo_config(repo), str(repo))
               if c.name == "routing light"]
    assert check.level == doctor.WARN
    assert f"no profile missing-worker in {path}, skipped" in check.detail
    assert check.detail.startswith("developer (" if len(names) > 1 else "none configured (")


def test_invalid_weight_is_refused(db, repo, boss):
    with pytest.raises(autopilot.AutopilotError):
        choose(db, repo, weight="enormous")


@pytest.mark.parametrize("weight,candidates,expected", [
    ("light", ["dev.qwen3-coder", "developer"], "developer"),
    pytest.param("medium", ["dev.gpt", "dev.claude-opus"], "dev.gpt",
                 marks=pytest.mark.variant("changing the tier preference ranking changes the routed worker")),
    pytest.param("light", None, "dev.gpt",
                 marks=pytest.mark.variant("light tasks prefer Sol over the available Claude workers")),
])
def test_weight_routing_prefers_available_candidates(db, repo, boss, monkeypatch, weight, candidates, expected):
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 0.0 if provider == "native" else 100.0)
    if weight == "light" and candidates:
        config(repo, routing={weight: candidates})
    why = []
    assert choose(db, repo, weight=weight, why=why)[0] == expected
    if candidates and "dev.qwen3-coder" in candidates:
        assert "local model server not answering" in why[0]
    if weight == "medium":
        from brindle import worker_models

        ranked = list(worker_models.TIER_PREFERENCES[weight])
        first, second = (ranked.index(name) for name in candidates)
        ranked[first], ranked[second] = ranked[second], ranked[first]
        monkeypatch.setitem(worker_models.TIER_PREFERENCES, weight, tuple(ranked))
        assert choose(db, repo, weight=weight)[0] == "dev.claude-opus"


@pytest.mark.parametrize("weight,blocked,expected", [
    ("light", ["dev.gpt"], "dev.claude-haiku"),
    pytest.param("light", ["dev.gpt", "dev.claude-haiku"], "dev.claude-sonnet",
                 marks=pytest.mark.variant("Sonnet takes light work when both Sol and Haiku cannot run")),
    pytest.param("medium", ["dev.gpt"], "dev.claude-sonnet",
                 marks=pytest.mark.variant("medium work falls back to Sonnet when Sol cannot run")),
])
@pytest.mark.variant("availability changes must advance to the next benchmark-ranked worker")
def test_ranked_worker_fallback(db, repo, boss, monkeypatch, weight, blocked, expected):
    unavailable = autopilot._unavailable
    monkeypatch.setattr(autopilot, "_unavailable",
                        lambda name, *args: "unavailable" if name in blocked else unavailable(name, *args))
    assert choose(db, repo, weight=weight)[0] == expected


@pytest.mark.parametrize("unavailable,expected", [
    pytest.param(None, "review.claude-sonnet.high", marks=pytest.mark.variant(
        "with nothing blocking it, the default reviewer is the Sonnet high profile, not the baseline")),
    pytest.param("policy", "reviewer", marks=pytest.mark.variant(
        "an org refusal of the default reviewer must use the available baseline fallback")),
    pytest.param("codex", "reviewer", marks=pytest.mark.variant(
        "a default profile overridden to signed-out Codex must use the available baseline fallback")),
])
def test_default_reviewer_pick(db, repo, boss, monkeypatch, unavailable, expected):
    from brindle import policy

    # Keep same-model avoidance out of this test of the default and its fallbacks.
    db.add_agent(Agent("writer", boss.id, "dev.claude-opus", "claude", None,
                       "assign", "done", "@0", "done", time.time()))
    monkeypatch.setattr(agents, "unusable", lambda provider, env=None:
                        "not signed in" if unavailable == "codex" and provider == "codex" else None)
    monkeypatch.setattr(policy, "check_assign", lambda cfg, root, name, *args:
                        policy.Decision(allowed=not (unavailable == "policy" and name == "review.claude-sonnet.high")))
    if unavailable == "codex":
        path = repo / ".brindle" / "agents" / "review.claude-sonnet.high.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("---\nextends: reviewer\nprovider: codex\n---\n")
    chosen, note = agents._review_choice(db, boss, None, RepoConfig(review_sizing=False))
    assert chosen == expected
    assert (note is not None) == (unavailable is not None)


@pytest.mark.parametrize("weight", [
    pytest.param("medium", marks=pytest.mark.variant("weighted routing uses the eligible profile picked by the learner")),
    pytest.param(None, marks=pytest.mark.variant("without a weight, explicit learner candidates can contain stale ids")),
])
def test_learning_plugin_reorders_the_remaining_candidates(db, repo, boss, monkeypatch, weight):
    picker = Picker(prefer="dev.claude-sonnet")
    install(monkeypatch, picker)
    config(repo, learning="cloud", learning_candidates=["retired-worker", "dev.claude-sonnet"])
    assert choose(db, repo, weight=weight) == ("dev.claude-sonnet", True)
    assert all("retired-worker" not in candidates for _, candidates in picker.asked)


@pytest.mark.parametrize("pinned", [
    False,
    pytest.param(True, marks=pytest.mark.variant(
        "a pinned tier that can't run must refuse, not escape the pin through default_agent")),
])
def test_an_exhausted_cascade_refuses_and_says_why(db, repo, boss, monkeypatch, pinned):
    if pinned:   # medium pinned to Sonnet, which can't run; everything else could
        unavailable = autopilot._unavailable
        monkeypatch.setattr(autopilot, "_unavailable", lambda name, *args: "unavailable"
                            if name == "dev.claude-sonnet" else unavailable(name, *args))
        config(repo, default_agent="dev.gpt", routing={"medium": ["dev.claude-sonnet"]},
               routing_locked={"medium": True})
    else:
        monkeypatch.setattr("shutil.which", lambda cli: None)
        monkeypatch.setattr(quota, "headroom",
                            lambda provider, cfg=None, repo_root=None: 0.0 if provider == "native" else 100.0)
        config(repo, default_agent="reviewer")
    with pytest.raises(autopilot.AutopilotError, match="every (medium|heavy) candidate was out"):
        choose(db, repo, weight="medium" if pinned else "heavy")


def test_assign_reply_names_the_pick_and_stores_the_weight(db, repo, boss, monkeypatch):
    from brindle.model_names import model_name

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 7.0 if provider == "codex" else 100.0)
    config(repo, pipeline=False)
    out = asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="medium"))
    assert "Started worker" in out
    route = next(line for line in out.splitlines() if "→" in line)
    job, worker = route.split("→", 1)
    assert "Medium" in job and "job" in job
    assert model_name("sonnet").display in worker and "dev.claude-sonnet" in worker
    assert "developer-" not in out
    assert "Codex at 93%, skipped dev.gpt" in out
    [t] = db.list_tasks(str(repo), state="started")
    assert t.weight == "medium" and t.profile == "dev.claude-sonnet"

    from brindle import spawn

    monkeypatch.setattr(spawn, "_load", lambda: 14.0)
    monkeypatch.setattr(spawn, "_cores", lambda: 10)
    queued = asyncio.run(mcp_server.assign(
        agent_profile="developer-sonnet", task="do B", branch="feat-b"))
    [pending] = db.list_tasks(str(repo), state="pending")
    assert pending.id in queued and pending.agent_id is None
    assert "dev.claude-sonnet" in queued and model_name("sonnet").display in queued
    assert "developer-sonnet" not in queued


@pytest.mark.parametrize("named,override,expected", [
    ("qa", False, "dev.claude-sonnet"),
    pytest.param("qa", True, "dev.claude-opus", marks=pytest.mark.variant(
        "the agent's own agent_models order replaces the shared table and the learner")),
    pytest.param("", True, "dev.claude-opus", marks=pytest.mark.variant(
        "an unnamed weighted task is done by the repo's default agent, with its own order")),
])
@pytest.mark.variant("an agent task must take its model from the job-size table or its override and record the agent")
def test_assign_routes_an_agent_by_size(db, repo, boss, monkeypatch, launches, detached, named, override, expected):
    from brindle import cascade

    unavailable = autopilot._unavailable
    monkeypatch.setattr(autopilot, "_unavailable", lambda name, *args: "unavailable"
                        if name == "dev.claude-haiku" else unavailable(name, *args))
    monkeypatch.setattr(cascade, "table", lambda: None)   # the tier's own order, unmeasured
    plugin = Picker()
    install(monkeypatch, plugin)
    config(repo, pipeline=False, learning="cloud", default_agent="qa" if not named else "developer",
           routing={"light": ["dev.claude-haiku", "dev.claude-sonnet", "dev.gpt"]},
           **({"agent_models": {"qa": {"light": ["retired-worker", "dev.claude-haiku", "dev.claude-opus"]}}}
              if override else {}))
    out = asyncio.run(mcp_server.assign(agent_profile=named, task="cover the parser", branch="feat-qa",
                                        weight="light"))
    assert "Started worker" in out
    assert "retired-worker" not in out
    [t] = db.list_tasks(str(repo), state="started")
    worker = db.get_agent(t.agent_id)
    assert (t.profile, worker.profile, worker.agent) == (expected, expected, "qa")
    assert len(launches) == 1
    assert db.routing_decision_for_agent(t.agent_id).agent == "qa"
    # The learner weighs the shared table for the agent; an agent's own order is locked.
    assert [task.agent for task, _ in plugin.asked] == ([] if override else ["qa"])


@pytest.mark.parametrize("requested", [
    "custom-opus",
    pytest.param("dev.claude-haiku", marks=pytest.mark.variant(
        "a named model with a weight still runs that exact model")),
])
@pytest.mark.variant("naming a model or a pinned profile must bypass agent routing and run exactly as named")
def test_a_pinned_profile_or_model_runs_as_itself(db, repo, boss, requested):
    path = repo / ".brindle" / "agents" / "custom-opus.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nextends: developer\nmodel: opus\n---\nCareful.\n")
    decision = {}
    assert autopilot.choose_profile(db, "boss", str(repo), requested, "task", None, "light",
                                    None, decision) == (requested, False)
    assert decision["agent"] is None


def test_learning_outcomes_carry_the_weight(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    config(repo, pipeline=False, learning="cloud")

    class Recorder(Picker):
        def __init__(self):
            super().__init__()
            self.events = []

        def record(self, task, outcome):
            self.events.append(task)

    plugin = Recorder()
    install(monkeypatch, plugin)
    asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="heavy"))
    [t] = db.list_tasks(str(repo), state="started")
    worker = db.get_agent(t.agent_id)
    learning.note(db, load_repo_config(repo), worker, boss, merged=True)
    assert plugin.events[0].weight == "heavy"
