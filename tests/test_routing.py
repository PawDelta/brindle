"""Routing by weight: assign/handoff's `weight` picks an available profile for
the tier, after an explicit profile and a milestone's, before the default."""

import asyncio
import json
import time

import pytest

from brindle import agents, autopilot, doctor, learning, mcp_server, plugins, quota, workspaces
from brindle.config import DEFAULT_ROUTING, load_repo_config, user_config_path
from brindle.db import Agent


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


def test_defaults(repo):
    assert load_repo_config(repo).routing == DEFAULT_ROUTING


def test_config_overrides_one_tier_and_keeps_the_rest(repo):
    config(repo, routing={"heavy": ["developer"], "bogus": ["x"]})
    r = load_repo_config(repo).routing
    assert r["heavy"] == ["developer"]
    assert r["light"] == DEFAULT_ROUTING["light"] and "bogus" not in r


def test_routing_merges_user_repo_and_local_per_tier(repo, tmp_path):
    user = user_config_path()
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(json.dumps({"routing": {"light": ["developer-codex"]}}))
    cfg = load_repo_config(repo)
    assert cfg.routing == {**DEFAULT_ROUTING, "light": ["developer-codex"]}
    assert cfg.routing_sources["light"] == str(user)

    config(repo, routing={"light": ["developer"], "medium": ["developer-heavy"]})
    cfg = load_repo_config(repo)
    assert cfg.routing == {**DEFAULT_ROUTING, "light": ["developer"], "medium": ["developer-heavy"]}
    assert cfg.routing_sources["light"] == str(repo / ".brindle" / "config.json")

    local = repo / ".brindle" / "config.local.json"
    local.write_text(json.dumps({"routing": {"light": ["developer-local"]}}))
    cfg = load_repo_config(repo)
    assert cfg.routing == {**DEFAULT_ROUTING, "light": ["developer-local"], "medium": ["developer-heavy"]}
    assert cfg.routing_sources == {
        "light": str(local), "medium": str(repo / ".brindle" / "config.json"),
        "heavy": "DEFAULT_ROUTING", "advanced": "DEFAULT_ROUTING",
    }
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    assert load_repo_config(other_repo).routing == {**DEFAULT_ROUTING, "light": ["developer-codex"]}


def test_repo_and_local_keep_untouched_user_tiers(repo):
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text(json.dumps({"routing": {"light": ["developer-codex"]}}))
    config(repo, routing={"medium": ["developer-heavy"]})
    (repo / ".brindle" / "config.local.json").write_text(
        json.dumps({"routing": {"heavy": ["developer-local"]}}))
    assert load_repo_config(repo).routing == {
        "light": ["developer-codex"], "medium": ["developer-heavy"], "heavy": ["developer-local"],
        "advanced": DEFAULT_ROUTING["advanced"],
    }


@pytest.mark.parametrize("layer", ["user", "repo", "local"])
@pytest.mark.parametrize("names", [["missing-worker", "developer"], ["missing-worker"]])
def test_unknown_routing_profiles_are_skipped_and_reported(db, repo, boss, layer, names):
    path = {"user": user_config_path(), "repo": repo / ".brindle" / "config.json",
            "local": repo / ".brindle" / "config.local.json"}[layer]
    path.write_text(json.dumps({"routing": {"light": names}}))
    why = []
    assert choose(db, repo, weight="light", why=why) == ("developer", False)
    assert f"no profile missing-worker in {path}, skipped missing-worker" in why[0]
    if len(names) == 1:
        assert "every light candidate was out" in why[0] and "using developer" in why[0]
    [check] = [c for c in doctor.routing_checks(load_repo_config(repo), str(repo))
               if c.name == "routing light"]
    assert check.level == doctor.WARN
    assert f"no profile missing-worker in {path}, skipped" in check.detail
    assert check.detail.startswith("developer (" if len(names) > 1 else "none configured (")


@pytest.mark.parametrize("layer", ["default", "user", "repo", "local"])
def test_doctor_prints_effective_routing_and_each_tiers_source(repo, monkeypatch, layer):
    config(repo)
    path = {"user": user_config_path(), "repo": repo / ".brindle" / "config.json",
            "local": repo / ".brindle" / "config.local.json"}.get(layer)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"routing": {"light": ["developer-codex"]}}))
    monkeypatch.setattr(doctor, "_tool", lambda *a, **k: doctor.Check(doctor.OK, a[0], "stub"))
    monkeypatch.setattr(doctor, "native_checks", lambda root: [])
    # Exercise the section in the full report, not just its helper.
    checks = [c for c in doctor.checks(str(repo)) if c.name.startswith("routing ")]
    assert len(checks) == len(DEFAULT_ROUTING)
    assert all(c.level == doctor.OK for c in checks)
    rendered = doctor.render(checks)
    for tier in DEFAULT_ROUTING:
        names = ["developer-codex"] if tier == "light" and path else DEFAULT_ROUTING[tier]
        source = str(path) if tier == "light" and path else "DEFAULT_ROUTING"
        assert f"{' -> '.join(names)} (from {source})" in rendered
        assert f"routing {tier}" in rendered


def test_weight_picks_the_first_candidate(db, repo, boss):
    why = []
    assert choose(db, repo, weight="advanced", why=why) == ("developer-fable", False)
    assert len(why) == 1 and why[0].startswith("weight advanced -> developer-fable")
    assert choose(db, repo, weight="reasoning")[0] == "developer-fable"
    assert choose(db, repo, weight="complex")[0] == "developer-fable"
    assert choose(db, repo, weight="heavy")[0] == "developer-heavy"
    # No tier prefers another provider for its own sake: the baselines are
    # Claude profiles; Codex and the local model are learner candidates.
    assert choose(db, repo, weight="medium")[0] == "developer"
    assert choose(db, repo, weight="light")[0] == "developer-light"


def test_no_weight_keeps_the_default_agent(db, repo, boss):
    config(repo, default_agent="developer-codex")
    assert choose(db, repo) == ("developer-codex", False)


def test_invalid_weight_is_refused(db, repo, boss):
    with pytest.raises(autopilot.AutopilotError):
        choose(db, repo, weight="enormous")


def test_precedence_explicit_then_milestone_then_weight(db, repo, boss):
    assert choose(db, repo, "reviewer", "heavy")[0] == "reviewer"
    db.add_autopilot("boss", enabled=True)
    autopilot.set_goal(db, "boss", "g", [("m1", "true", "", "developer-local")])
    assert choose(db, repo, None, "heavy")[0] == "developer-local"
    milestone = db.milestones("boss")[0]
    db.record_check(milestone.id, True, "", "sha")
    assert choose(db, repo, None, "heavy")[0] == "developer-heavy"


def test_weight_routing_beats_learning_candidates(db, repo, boss, monkeypatch):
    install(monkeypatch, Picker(prefer="developer-local"))
    config(repo, learning="cloud", learning_candidates=["developer-local"])
    assert choose(db, repo, weight="heavy")[0] == "developer-heavy"


def test_missing_cli_is_skipped(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None if cli == "codex" else f"/usr/bin/{cli}")
    why = []
    assert choose(db, repo, weight="medium", why=why)[0] == "developer"
    assert "codex isn't installed, skipped developer-codex" in why[0]


def test_limited_provider_is_skipped(db, repo, boss, monkeypatch):
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 7.0 if provider == "codex" else 100.0)
    why = []
    assert choose(db, repo, weight="medium", why=why)[0] == "developer"
    assert len(why) == 1 and "weight medium -> developer" in why[0] and "Codex at 93%, skipped developer-codex" in why[0]


def test_provider_below_the_usage_limit_is_kept(db, repo, boss, monkeypatch):
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 30.0)
    why = []
    assert choose(db, repo, weight="medium", why=why)[0] == "developer"
    assert "skipped" not in why[0]  # Codex stays a candidate


def test_native_down_is_skipped(db, repo, boss, monkeypatch):
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 0.0 if provider == "native" else 100.0)
    config(repo, routing={"light": ["developer-local", "developer"]})
    why = []
    assert choose(db, repo, weight="light", why=why)[0] == "developer"
    assert "local model server not answering" in why[0]


def test_learning_plugin_reorders_the_remaining_candidates(db, repo, boss, monkeypatch):
    plugin = Picker(prefer="developer-codex")
    install(monkeypatch, plugin)
    config(repo, learning="cloud")
    why = []
    assert choose(db, repo, weight="medium", why=why) == ("developer-codex", True)
    task, candidates = plugin.asked[0]
    assert candidates == ["developer", "developer-codex", "developer-heavy"] and task.weight == "medium"
    assert "learning picked it: its record is better" in why[0]
    assert plugin.defaults == ["developer"]


def test_learning_keeping_the_default_is_not_credited(db, repo, boss, monkeypatch):
    plugin = Picker(prefer="developer")
    install(monkeypatch, plugin)
    config(repo, learning="cloud")
    why = []
    assert choose(db, repo, weight="medium", why=why) == ("developer", False)
    assert "learning picked it" not in why[0] and "learning kept it" not in why[0]


def test_the_learners_note_on_a_kept_default_is_shown(db, repo, boss, monkeypatch):
    plugin = Picker(prefer="developer", note="not enough similar tasks yet")
    install(monkeypatch, plugin)
    config(repo, learning="cloud")
    why = []
    assert choose(db, repo, weight="medium", why=why) == ("developer", False)
    assert "learning kept it: not enough similar tasks yet" in why[0]


def test_one_candidate_never_asks_the_learner(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None if cli == "codex" else f"/usr/bin/{cli}")
    plugin = Picker(prefer="developer")
    install(monkeypatch, plugin)
    config(repo, learning="cloud", routing={"medium": ["developer", "developer-codex"]})
    assert choose(db, repo, weight="medium") == ("developer", False)
    assert plugin.asked == []


def test_learning_only_sees_available_candidates(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None if cli == "codex" else f"/usr/bin/{cli}")
    plugin = Picker(prefer="developer-codex")
    install(monkeypatch, plugin)
    config(repo, learning="cloud", routing={"medium": ["developer", "developer-codex"]})
    assert choose(db, repo, weight="medium") == ("developer", False)
    assert plugin.asked == []     # a single candidate: nothing to choose, no server call


def test_all_out_falls_back_to_the_default_agent_and_says_why(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None)
    config(repo, default_agent="reviewer")
    why = []
    assert choose(db, repo, weight="heavy", why=why) == ("reviewer", False)
    assert "every heavy candidate was out" in why[0] and "using reviewer" in why[0]


# -- the assign/handoff reply, and the weight stored for learning -----------------------


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


def test_assign_reply_names_the_pick_and_stores_the_weight(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 7.0 if provider == "codex" else 100.0)
    config(repo, pipeline=False)
    out = asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="medium"))
    assert "Started worker" in out
    assert "weight medium -> developer" in out and "Codex at 93%, skipped developer-codex" in out
    [t] = db.list_tasks(str(repo), state="started")
    assert t.weight == "medium" and t.profile == "developer"


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
