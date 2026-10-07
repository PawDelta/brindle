"""Managed rollout (Enterprise `managed_rollout`): min_version, required profiles and
rule packs, and the kill switch; none of it applies without the feature."""
import time

import pytest

from brindle import agents, profiles, workspaces
from brindle.db import Agent
from brindle.policy import AssignInfo
from brindle.pro import license, org_profiles, rollout, team_policy
from brindle.pro.team_policy import OrgPolicy, PolicyUnavailable, ProPolicy, parse_policy

ORG = "org_ent1"
PACK = "---\nname: security/org\ndescription: org rules\ndeny_patterns:\n  - eval\\(\n---\nNo eval.\n"
REVIEWER = "---\nname: org-reviewer\ndescription: r\nprovider: claude\n---\nReview.\n"


def item(kind, name, text, pinned=True):
    return org_profiles.Item(kind, name, text, pinned)


@pytest.fixture
def entitled(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == rollout.FEATURE)


@pytest.fixture
def library(monkeypatch):
    lib = {"profile": {"org-reviewer": item("profile", "org-reviewer", REVIEWER)},
           "pack": {"security/org": item("pack", "security/org", PACK)}}
    monkeypatch.setattr(org_profiles, "items", lambda kind: lib[kind])
    return lib


def policy(**over):
    body = {"org_id": ORG, "version": 1, "policy": over}
    return parse_policy(ORG, body)


def serve(monkeypatch, **over):
    p = policy(**over)
    monkeypatch.setattr(team_policy, "current_policy", lambda *a, **k: p)
    return ProPolicy("/work/repo", entitlement=lambda: type(
        "Ent", (), {"features": ["team", rollout.FEATURE], "org_id": ORG})())


def assign():
    return AssignInfo(repo_root="/work/repo", task="t", profile="developer", provider="claude",
                      model="m", actor="user", running_workers=0)


# -- parsing ------------------------------------------------------------------------------------------


def test_the_fields_parse_and_default_to_off():
    p = policy()
    assert (p.min_version, p.required_profiles, p.required_rule_packs, p.kill_switch) == (
        None, (), (), False)
    p = policy(min_version="1.4.0", required_profiles=["org-reviewer"],
               required_rule_packs=["security/org", "security/org"], kill_switch=True)
    assert p.min_version == "1.4.0" and p.required_profiles == ("org-reviewer",)
    assert p.required_rule_packs == ("security/org",) and p.kill_switch is True
    assert p.enforced.kill_switch and p.rollout()["min_version"] == "1.4.0"


@pytest.mark.parametrize("over", [{"min_version": 3}, {"min_version": "latest"},
                                  {"required_profiles": "x"}, {"required_rule_packs": ["Bad Name"]},
                                  {"required_profiles": [1]}, {"kill_switch": "yes"}])
def test_malformed_fields_make_the_policy_unusable(over):
    with pytest.raises(PolicyUnavailable):
        policy(**over)


def test_the_fields_survive_the_cache_round_trip():
    p = policy(min_version="2.0", required_rule_packs=["security/org"], kill_switch=True)
    q = parse_policy(ORG, p.to_json())
    assert q.rollout() == p.rollout()


@pytest.mark.parametrize("a,b,older", [("1.2.3", "1.10.0", True), ("1.10.0", "1.2.3", False),
                                       ("1.4", "1.4.0", False), ("v1.4.0rc1", "1.4.0", False),
                                       ("0.9.9", "1", True)])
def test_versions_compare_numerically(a, b, older):
    assert (rollout.version_tuple(a) < rollout.version_tuple(b)) is older


# -- starting workers -------------------------------------------------------------------------------------


def test_nothing_applies_without_the_feature(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)
    pol = serve(monkeypatch, kill_switch=True, min_version="999")
    assert pol.check_assign(assign()).allowed


def test_the_feature_check_fails_closed(monkeypatch):
    def boom(feature):
        raise RuntimeError("bad license")

    monkeypatch.setattr(license, "has", boom)
    assert not rollout.entitled()
    assert rollout.required_packs("/work/repo") == []


def test_the_kill_switch_refuses_new_workers(entitled, library, monkeypatch):
    d = serve(monkeypatch, kill_switch=True).check_assign(assign())
    assert not d.allowed and "kill switch" in d.reason


def test_a_version_below_min_version_is_refused_with_an_upgrade_message(entitled, library, monkeypatch):
    monkeypatch.setattr("brindle.__version__", "1.2.0")
    d = serve(monkeypatch, min_version="1.3.0").check_assign(assign())
    assert not d.allowed and "1.3.0" in d.reason and "Upgrade" in d.reason
    monkeypatch.setattr("brindle.__version__", "1.3.0")
    assert serve(monkeypatch, min_version="1.3.0").check_assign(assign()).allowed


def test_an_unknown_version_cannot_meet_a_floor(entitled, library, monkeypatch):
    monkeypatch.setattr("brindle.__version__", "unknown")
    assert not serve(monkeypatch, min_version="1.0").check_assign(assign()).allowed


def test_required_items_must_resolve_from_the_org_library(entitled, library, monkeypatch):
    ok = serve(monkeypatch, required_profiles=["org-reviewer"], required_rule_packs=["security/org"])
    assert ok.check_assign(assign()).allowed
    d = serve(monkeypatch, required_profiles=["nope"]).check_assign(assign())
    assert not d.allowed and "nope" in d.reason
    d = serve(monkeypatch, required_rule_packs=["security/other"]).check_assign(assign())
    assert not d.allowed and "security/other" in d.reason


def test_required_items_fail_closed_with_no_library(entitled, monkeypatch):
    monkeypatch.setattr(org_profiles, "items", lambda kind: {})
    assert not serve(monkeypatch, required_rule_packs=["security/org"]).check_assign(assign()).allowed


@pytest.mark.parametrize("mode", ["assign", "handoff"])
def test_assign_and_handoff_share_the_gate(entitled, library, monkeypatch, db, repo, mode):
    import inspect

    from brindle import mcp_server, policy

    pol = serve(monkeypatch, kill_switch=True)
    monkeypatch.setattr(policy, "plugin", lambda cfg, root: pol)
    ws = workspaces.adopt_root(db, str(repo))
    out = mcp_server._policy_refusal(db, None, ws, "developer", "t", mode, None, None, None)
    assert out and out.startswith("Not started") and "kill switch" in out
    # both tools go through that one function
    tool = mcp_server.handoff if mode == "handoff" else mcp_server.assign
    assert f'"{mode}"' in inspect.getsource(tool).replace("'", '"')
    assert "_policy_refusal(" in inspect.getsource(tool)


def test_reviewers_are_blocked_by_the_kill_switch_too(entitled, db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    monkeypatch.setattr(rollout, "current", lambda root=None: policy(kill_switch=True))
    with pytest.raises(agents.AgentError, match="kill switch"):
        agents.request_review(db, None, ws)


def test_reviewers_are_not_blocked_with_the_switch_off(entitled, monkeypatch):
    monkeypatch.setattr(rollout, "current", lambda root=None: policy())
    assert rollout.kill_switch_reason("/work/repo") is None
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert rollout.kill_switch_reason("/work/repo") is None


# -- required packs reach every worker --------------------------------------------------------------------------


def test_required_packs_are_added_to_every_profile(entitled, library, monkeypatch, repo):
    monkeypatch.setattr(rollout, "current", lambda root=None: policy(required_rule_packs=["security/org"]))
    (repo / ".brindle" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".brindle" / "agents" / "dev.md").write_text(
        "---\nname: dev\ndescription: d\nprovider: claude\n---\nWork.\n")
    packs = profiles.load_rule_packs(profiles.load_profile("dev", str(repo)), str(repo))
    assert [p.name for p in packs] == ["security/org"]
    assert packs[0].prompt == "No eval." and packs[0].deny_patterns == ["eval\\("]
    assert packs[0].source == "org rule pack security/org"


def test_a_repo_file_cannot_stand_in_for_a_required_pack(entitled, library, monkeypatch, repo):
    monkeypatch.setattr(rollout, "current", lambda root=None: policy(required_rule_packs=["only/local"]))
    (repo / ".brindle" / "rules" / "only").mkdir(parents=True)
    (repo / ".brindle" / "rules" / "only" / "local.md").write_text("---\n---\nlocal\n")
    (repo / ".brindle" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".brindle" / "agents" / "dev.md").write_text(
        "---\nname: dev\ndescription: d\nprovider: claude\n---\nWork.\n")
    with pytest.raises(KeyError, match="only/local"):
        profiles.load_rule_packs(profiles.load_profile("dev", str(repo)), str(repo))


def test_a_profile_naming_a_required_pack_still_gets_the_org_one(entitled, library, monkeypatch, repo):
    monkeypatch.setattr(rollout, "current", lambda root=None: policy(required_rule_packs=["security/org"]))
    (repo / ".brindle" / "rules" / "security").mkdir(parents=True)
    (repo / ".brindle" / "rules" / "security" / "org.md").write_text("---\n---\nweakened\n")
    (repo / ".brindle" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".brindle" / "agents" / "dev.md").write_text(
        "---\nname: dev\ndescription: d\nprovider: claude\nrules: [security/org]\n---\nWork.\n")
    packs = profiles.load_rule_packs(profiles.load_profile("dev", str(repo)), str(repo))
    assert [(p.name, p.prompt) for p in packs] == [("security/org", "No eval.")]


def test_an_unreadable_policy_does_not_silently_drop_required_packs(entitled, monkeypatch):
    from brindle.policy import deny

    monkeypatch.setattr(rollout, "current", lambda root=None: deny("never fetched"))
    with pytest.raises(KeyError, match="never fetched"):
        rollout.required_packs("/work/repo")

    def boom(root=None):
        raise RuntimeError("x")

    monkeypatch.setattr(rollout, "current", boom)
    with pytest.raises(KeyError):
        rollout.required_packs("/work/repo")


def test_no_extra_packs_without_the_feature(monkeypatch, repo):
    monkeypatch.setattr(license, "has", lambda feature: False)
    (repo / ".brindle" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".brindle" / "agents" / "dev.md").write_text(
        "---\nname: dev\ndescription: d\nprovider: claude\n---\nWork.\n")
    assert profiles.load_rule_packs(profiles.load_profile("dev", str(repo)), str(repo)) == []


# -- the kill switch stops running workers ----------------------------------------------------------------------


@pytest.fixture
def worker(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing",
                       "@1", None, time.time()))
    stopped, sent = [], []
    monkeypatch.setattr(agents, "runs_process", lambda a: True)
    monkeypatch.setattr(agents, "pause_worker", lambda db, a: stopped.append(a.id))
    monkeypatch.setattr(agents, "send_message",
                        lambda db, to, body, sender_id=None, person=False: sent.append((to, body)))
    return stopped, sent


def test_the_sweep_stops_running_workers_when_the_switch_is_on(entitled, db, worker, monkeypatch):
    stopped, sent = worker
    monkeypatch.setattr(rollout, "current", lambda root=None: policy(kill_switch=True))
    assert rollout.sweep(db) == ["stopped worker w1: org kill switch"]
    assert stopped == ["w1"] and sent[0][0] == "boss" and "kill switch" in sent[0][1]


def test_the_sweep_leaves_workers_alone_with_the_switch_off(entitled, db, worker, monkeypatch):
    stopped, sent = worker
    monkeypatch.setattr(rollout, "current", lambda root=None: policy())
    assert rollout.sweep(db) == [] and not stopped and not sent


def test_the_sweep_does_nothing_without_the_feature(db, worker, monkeypatch):
    stopped, _ = worker
    monkeypatch.setattr(license, "has", lambda feature: False)
    monkeypatch.setattr(rollout, "current", lambda root=None: policy(kill_switch=True))
    assert rollout.sweep(db) == [] and not stopped


def test_the_sweep_skips_paused_and_finished_workers(entitled, db, worker, monkeypatch):
    stopped, _ = worker
    db.set_status("w1", "paused")
    monkeypatch.setattr(rollout, "current", lambda root=None: policy(kill_switch=True))
    assert rollout.sweep(db) == [] and not stopped


def test_the_cull_pass_runs_the_sweep(db, monkeypatch):
    from brindle import cull

    calls = []
    monkeypatch.setattr(rollout, "sweep", lambda db, now=None: calls.append(1) or ["x"])
    assert "x" in cull.sweep(db)
    assert calls
