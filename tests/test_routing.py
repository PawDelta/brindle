"""Routing by weight: assign/handoff's `weight` picks an available profile for
the tier, after an explicit profile and a milestone's, before the default."""

import asyncio
import json
import time

import pytest

from brindle import agents, autopilot, doctor, learning, mcp_server, plugins, quota, workspaces
from brindle.config import DEFAULT_ROUTING, load_repo_config, user_config_path
from brindle.db import Agent
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


@pytest.mark.parametrize("names", [["missing-worker", "developer"], ["missing-worker"]])
def test_unknown_routing_profiles_are_skipped_and_reported(db, repo, boss, names):
    path = user_config_path()
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


@pytest.mark.parametrize("layer", ["default", "local"])
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
    assert choose(db, repo, weight="reasoning")[0] == "developer-fable"   # an alias of advanced


def test_invalid_weight_is_refused(db, repo, boss):
    with pytest.raises(autopilot.AutopilotError):
        choose(db, repo, weight="enormous")


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


def test_all_out_falls_back_to_the_default_agent_and_says_why(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None)
    config(repo, default_agent="reviewer")
    why = []
    assert choose(db, repo, weight="heavy", why=why) == ("reviewer", False)
    assert "every heavy candidate was out" in why[0] and "using reviewer" in why[0]


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
