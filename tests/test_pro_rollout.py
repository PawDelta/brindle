"""Managed rollout (Enterprise `managed_rollout`): min_version, required profiles and
rule packs, and the kill switch; none of it applies without the feature."""
import time

import pytest

from brindle import agents, profiles, workspaces
from brindle.db import Agent
from brindle.policy import AssignInfo
from brindle.pro import license, org_profiles, rollout, team_policy
from brindle.pro.team_policy import PolicyUnavailable, ProPolicy, parse_policy

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


@pytest.mark.parametrize("over", [{"min_version": "latest"}, {"required_profiles": "x"},
                                  {"kill_switch": "yes"}])
def test_malformed_fields_make_the_policy_unusable(over):
    with pytest.raises(PolicyUnavailable):
        policy(**over)


# -- starting workers -------------------------------------------------------------------------------------


def test_the_feature_check_fails_closed(monkeypatch):
    def boom(feature):
        raise RuntimeError("bad license")

    monkeypatch.setattr(license, "has", boom)
    assert not rollout.entitled()
    assert rollout.required_packs("/work/repo") == []


def test_an_unknown_version_cannot_meet_a_floor(entitled, library, monkeypatch):
    monkeypatch.setattr("brindle.__version__", "unknown")
    assert not serve(monkeypatch, min_version="1.0").check_assign(assign()).allowed


@pytest.mark.variant("enforcement: the org kill switch must refuse new work")
def test_the_kill_switch_refuses_an_assign(entitled, library, monkeypatch, db, repo):
    from brindle import mcp_server, policy

    pol = serve(monkeypatch, kill_switch=True)
    monkeypatch.setattr(policy, "plugin", lambda cfg, root: pol)
    ws = workspaces.adopt_root(db, str(repo))
    out = mcp_server._policy_refusal(db, None, ws, "developer", "t", "assign", None, None, None)
    assert out and out.startswith("Not started") and "kill switch" in out


# -- required packs reach every worker --------------------------------------------------------------------------


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


def test_the_cull_pass_runs_the_sweep(db, monkeypatch):
    from brindle import cull

    calls = []
    monkeypatch.setattr(rollout, "sweep", lambda db, now=None: calls.append(1) or ["x"])
    assert "x" in cull.sweep(db)
    assert calls
