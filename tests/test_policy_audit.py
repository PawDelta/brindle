from dataclasses import replace

import pytest

from brindle import agents, autopilot, codex_hook, policy, profiles, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent
from brindle.pro.team_policy import OrgPolicy, ProPolicy


@pytest.fixture
def org_policy(monkeypatch, repo):
    plugin = ProPolicy(str(repo))
    current = [OrgPolicy("org", 1)]
    monkeypatch.setattr(plugin, "policy", lambda: current[0])
    monkeypatch.setattr(policy, "plugin", lambda *a: plugin)
    return current


@pytest.mark.parametrize("fallback_allowed", [True, False])
@pytest.mark.variant("Completion audits must enforce the policy selector already covered by branch reviews")
def test_audit_respects_policy_and_reviewer_fallbacks(db, repo, monkeypatch, org_policy, fallback_allowed):
    ws = workspaces.adopt_root(db, str(repo))
    root = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "@0", None, 1)
    db.add_agent(root)
    db.add_autopilot(root.id)
    db.update_autopilot(root.id, state="auditing", goal="Finish the feature")
    cfg = RepoConfig(review_profile="reviewer-codex", reviewer_fallbacks=["reviewer"])
    load_profile = profiles.load_profile

    def pinned_profile(name, *a):
        p = load_profile(name, *a)
        return replace(p, model="gpt-6.1-terra") if p.provider == "codex" else p

    monkeypatch.setattr(profiles, "load_profile", pinned_profile)
    monkeypatch.setattr(agents, "load_profile", pinned_profile)
    allowed_model = policy._profile_fields("reviewer", str(repo))[1]
    org_policy[0] = replace(org_policy[0], allowed_models=(allowed_model,) if fallback_allowed else ())
    monkeypatch.setattr(agents, "unusable", lambda *a: None)
    spawned = []

    def spawn(db, ws, profile, **kw):
        spawned.append(profile)
        auditor = replace(root, id="auditor", profile=profile, mode="review", parent_id=root.id)
        db.add_agent(auditor)
        return auditor

    monkeypatch.setattr(agents, "spawn", spawn)
    if fallback_allowed:
        auditor = autopilot.start_audit(db, root.id, ws, cfg)
        assert auditor.profile == "reviewer"
        assert db.get_autopilot(root.id).auditor_id == auditor.id
    else:
        with pytest.raises(agents.AgentError):
            autopilot.start_audit(db, root.id, ws, cfg)
        assert db.get_autopilot(root.id).auditor_id is None
        assert spawned == []
    assert all(policy.assign_decision(cfg, str(repo), name, "audit", "review").allowed for name in spawned)


@pytest.mark.parametrize("configured,cli_default", [
    (None, "gpt-6.1-terra"),
    ("gpt-configured", "gpt-6.1-terra"),
    (None, None),
])
def test_unpinned_codex_uses_installed_default_and_fails_closed(repo, monkeypatch, org_policy,
                                                              configured, cli_default):
    class AppServer:
        def __init__(self, *a):
            pass

        def call(self, method, params):
            if method == "config/read":
                return {"config": {"model": configured}}
            if method == "model/list":
                return {"data": ([{"model": cli_default, "isDefault": True}] if cli_default else []),
                        "nextCursor": None}
            pytest.fail(method)

        def close(self):
            pass

    monkeypatch.setattr(codex_hook, "_AppServer", AppServer)
    profile = replace(profiles.load_profile("reviewer-codex", str(repo)), model=None, env={})
    monkeypatch.setattr(profiles, "load_profile", lambda *a: profile)
    monkeypatch.setattr(autopilot, "effective_profile_model", lambda *a: "")
    org_policy[0] = replace(org_policy[0], allowed_models=("gpt-5.3-codex",))
    cfg = RepoConfig()
    assert not policy.assign_decision(cfg, str(repo), profile.name, "audit", "review").allowed
    expected = configured or cli_default
    assert policy._profile_fields(profile.name, str(repo)) == ("codex", expected)
    if expected:
        org_policy[0] = replace(org_policy[0], allowed_models=(expected,))
        assert policy.assign_decision(cfg, str(repo), profile.name, "audit", "review").allowed
