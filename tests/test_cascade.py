"""The cascade (brindle.cascade): one ordered list of runnable candidates for
every role, from the role's pool, ordered by the measured table."""

import json
import sys
import time
from pathlib import Path

import pytest

from brindle import agents, autopilot, cascade, plugins, policy, providers, quota, routing_goal, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent
from brindle.profiles import load_profile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

PROVIDERS = ("claude", "codex", "antigravity", "opencode")
TIERS = ("light", "medium", "heavy", "advanced")
ROLES = (*TIERS, "reviewer", "supervisor")


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    # No local model server answering: native candidates are out.
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 0.0 if provider == "native" else 100.0)
    plugins.reset()
    yield ws
    plugins.reset()


def config(repo, **kw):
    (repo / ".brindle" / "config.json").write_text(json.dumps(kw))


def only(monkeypatch, installed: str) -> None:
    """Only ``installed``'s CLI can run here."""
    def unusable(provider, env=None):
        return None if provider == installed else f"{provider} isn't installed"

    monkeypatch.setattr(providers, "unusable", unusable)
    monkeypatch.setattr(agents, "unusable", unusable)
    monkeypatch.setattr(providers, "signed_out", lambda provider, env=None: None)
    monkeypatch.setattr(agents, "model_problem", lambda name, ws, **kw: None)   # provisioning: not under test


@pytest.mark.variant("each provider alone must staff every role: a role with no cross-provider fallback "
                     "only shows in the full provider-by-role grid")
def test_every_role_finds_a_profile_on_the_one_installed_provider(db, repo, boss, monkeypatch):
    # The owner's supervisor runs on Claude; with Claude gone the supervisor
    # takes another provider's built-in profile.
    config(repo, supervisor_profile="supervisor")
    grid = {}
    for installed in PROVIDERS:
        only(monkeypatch, installed)
        for tier in TIERS:
            name, _ = autopilot.choose_profile(db, "boss", str(repo), None, "task", None, tier)
            grid[installed, tier] = load_profile(name, str(repo)).provider
        try:
            name, _ = agents._review_choice(db, boss, None, RepoConfig(review_sizing=False))
            grid[installed, "reviewer"] = load_profile(name, str(repo)).provider
        except agents.AgentError as e:
            grid[installed, "reviewer"] = str(e)
        chosen = agents.supervisor_choice(str(repo))
        grid[installed, "supervisor"] = chosen.provider if chosen else None
    assert grid == {(p, role): p for p in PROVIDERS for role in ROLES}


def _table(codex_runs: int) -> dict:
    tiers = {t: {"p": 0.8, "first_try": 0.3, "minutes": 30, "mtok": 0.4} for t in TIERS}
    return {"version": 2, "measured": "2026-11-15", "source": "test", "min_runs": 5,
            "token_mix": {"input": 0.8, "output": 0.2}, "tiers": tiers, "handover": {},
            "profiles": {
                "developer-sonnet": {"medium": {"p": 0.8, "first_try": 0.3, "usd": 4.0, "minutes": 40,
                                                "runs": 50}},
                "developer-codex": {"medium": {"p": 0.9, "first_try": 0.5, "usd": 1.0, "minutes": 20,
                                               "runs": codex_runs}}}}


def _missing():
    raise FileNotFoundError("routing_priors.json")


def test_the_measured_table_orders_runnable_candidates_and_unmeasured_keep_tier_order(db, repo, boss,
                                                                                       monkeypatch):
    # Both run; tier order puts Sonnet first, the table measures Codex better.
    config(repo, learning="off", routing={"medium": ["developer-sonnet", "developer-codex"]})
    picks = []
    for priors, goal in [(lambda: _table(50), None), (lambda: _table(2), None),
                         # an explicit goal orders by the same validated table: an unmeasured cell still can't win
                         (lambda: _table(2), "balanced"),
                         # a missing or older table is no evidence: tier order, never a failed route
                         (_missing, "balanced"), (lambda: {**_table(50), "version": 1}, None)]:
        monkeypatch.setattr(routing_goal, "priors", priors)
        picks.append(autopilot.choose_profile(db, "boss", str(repo), None, "task", None, "medium", goal=goal)[0])
    # Under min_runs Codex counts as not measured: tier order wins.
    assert picks == ["dev.gpt"] + ["dev.claude-sonnet"] * 4


def test_measured_history_under_an_old_id_counts_for_its_renamed_profile():
    # _table keys its measured cells by the old id; the renamed profile reads them.
    table = cascade.parse(_table(50))
    assert table.cell("dev.gpt", "medium")["runs"] == 50
    assert table.cell("developer-codex", "medium")["runs"] == 50


@pytest.mark.parametrize("old_first", [True, False])
def test_mixed_old_and_new_id_history_keeps_both_halves_in_either_order(old_first):
    old = {"developer-codex": {"medium": {"p": 0.9, "first_try": 0.5, "usd": 1.0, "minutes": 20, "runs": 50}}}
    new = {"dev.gpt": {"medium": {"p": 0.6, "first_try": 0.5, "usd": 1.0, "minutes": 20, "runs": 2}}}
    raw = _table(50)
    raw["profiles"] = {**old, **new} if old_first else {**new, **old}

    cell = cascade.parse(raw).cell("dev.gpt", "medium")
    assert cell["runs"] == 52
    assert cell["p"] == pytest.approx((0.9 * 50 + 0.6 * 2) / 52)


def test_a_table_built_from_records_with_near_zero_minutes_still_orders(db, repo, boss, monkeypatch):
    import build_routing_priors

    def run(worker, seconds, usd):
        return {"worker": worker, "tier": "medium", "valid": True, "pass_rate": 1.0, "cost_usd": usd,
                "seconds": seconds}

    # Codex runs round to 0.0 minutes; Sonnet's are slow and dear.
    records = [run("developer-codex", 1, 0.5)] * 5 + [run("developer-sonnet", 1200, 4.0)] * 5
    table = build_routing_priors.build(records, _table(50), measured="2026-11-17")
    assert table["profiles"]["developer-codex"]["medium"]["minutes"] == 0
    monkeypatch.setattr(routing_goal, "priors", lambda: table)
    config(repo, learning="off", routing={"medium": ["developer-sonnet", "developer-codex"]})
    assert autopilot.choose_profile(db, "boss", str(repo), None, "task", None, "medium")[0] == "dev.gpt"


@pytest.mark.parametrize("refused,reason", [
    pytest.param({"dev.gpt"}, "", marks=pytest.mark.variant(
        "a denial with no reason text must still count as a denial, not an approval")),
    pytest.param(None, "nothing today", marks=pytest.mark.variant(
        "every candidate refused: routing must refuse as policy does, not return a forbidden profile")),
])
def test_a_candidate_the_policy_refuses_is_replaced_by_one_it_allows(db, repo, boss, monkeypatch, refused, reason):
    class Refuse(policy.PolicyPlugin):
        def check_assign(self, info):
            no = refused is None or info.profile in refused
            return policy.Decision(allowed=False, reason=reason) if no else policy.allow()

        def check_merge(self, info):
            return policy.allow()

    monkeypatch.setattr(plugins, "entry_points", lambda group: [_EP(lambda repo_root: Refuse())]
                        if group == plugins.POLICY else [])
    why = []
    if refused is None:
        with pytest.raises(autopilot.AutopilotError, match="the repo's policy refused it: nothing today"):
            autopilot.choose_profile(db, "boss", str(repo), None, "task", None, "medium", why)
        return
    assert autopilot.choose_profile(db, "boss", str(repo), None, "task", None, "medium", why)[0] \
        == "dev.claude-sonnet"
    assert "refused by policy" in why[0] and "skipped dev.gpt" in why[0]


class _EP:
    name = "pro"

    def __init__(self, factory):
        self._factory = factory

    def load(self):
        return self._factory


@pytest.mark.parametrize("tier_out,default", [
    pytest.param(True, "dev.claude-opus", marks=pytest.mark.variant(
        "with the tier's own candidate out, the learner's default is the first replacement, not the unrunnable pick")),
    pytest.param(False, "dev.claude-sonnet", marks=pytest.mark.variant(
        "a runnable tier must not hide unmeasured replacements from the learner's evidence")),
])
def test_the_learner_chooses_among_every_replacement(db, repo, boss, monkeypatch, tier_out, default):
    from brindle.pro import learning as pro_learning
    from test_routing import Picker

    if tier_out:
        unavailable = autopilot._unavailable
        monkeypatch.setattr(autopilot, "_unavailable", lambda name, *args: "unavailable"
                            if name == "dev.claude-sonnet" else unavailable(name, *args))
    config(repo, learning="cloud", routing={"medium": ["developer-sonnet"]})   # an old id in config still routes
    monkeypatch.setattr(routing_goal, "priors", lambda: {**_table(50), "profiles": {}})   # nothing measured
    picker = Picker(prefer="dev.gpt")
    plugins.reset()
    monkeypatch.setattr(pro_learning, "CloudLearner", lambda repo_root: picker)
    # The default is the tier's (or, with it out, the first replacement); the learner favours a later one.
    assert autopilot.choose_profile(db, "boss", str(repo), None, "task", None, "medium") \
        == ("dev.gpt", True)
    assert picker.defaults == [default] and "dev.gpt" in picker.asked[0][1]
