"""Automatic supervisor restarts require an enterprise admin's dangerous opt-in."""

import json
from types import SimpleNamespace

import pytest

from brindle import agents, config, supervisor_state, workspaces
from brindle.pro import license, rollout, team_policy
from fast_repo import repo, repo_template  # noqa: F401
from test_efficiency_supervisor import transcript
from test_merge_order import add


@pytest.fixture
def supervisor(db, repo, tmp_path, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", session_ref="old-chat")
    detached = []
    monkeypatch.setattr(agents, "_detach_orphan", lambda argv: detached.append(argv))
    path = transcript(tmp_path, 60_000, "Keep working on the goal.")
    return db.get_agent("boss"), {"transcript_path": str(path)}, detached


def serve_policy(monkeypatch, body, *, enterprise=True):
    features = {"team", rollout.FEATURE} if enterprise else {"team"}
    ent = SimpleNamespace(features=features, org_id="org_restart")
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    monkeypatch.setattr(license, "has", lambda feature: feature in features)
    monkeypatch.setattr(team_policy, "current_policy", lambda *a, **kw: team_policy.parse_policy(
        ent.org_id, {"version": 1, "policy": body, "effective": {}}))


@pytest.mark.variant("real org-policy and entitlement resolution must reach restart dispatch, beyond the mocked restart fixtures")
def test_org_policy_starts_restart_past_limit(db, supervisor, tmp_path, monkeypatch):
    serve_policy(monkeypatch, {"dangerous": {"supervisor_restart_tokens": 50_000}})
    root, payload, detached = supervisor
    small = transcript(tmp_path, 20_000, "Keep working on the goal.")
    assert not supervisor_state.on_turn_end(db, root, {"transcript_path": str(small)})
    assert supervisor_state.on_turn_end(db, root, payload)
    assert detached and db.restart_holding(root.id)


@pytest.mark.variant("local config bypass attempts must fail through real dispatch, even where seed tests cover the same policy branches")
@pytest.mark.parametrize("policy", [
    "absent",
    pytest.param("zero", marks=pytest.mark.variant("an explicit org disable must not fall back to local config")),
    "unentitled",
    "unavailable",
])
def test_local_config_cannot_enable_restart(db, repo, supervisor, monkeypatch, policy):
    # All three ordinary config layers request a restart; none is authoritative.
    config.user_config_path().write_text(json.dumps({"supervisor_restart_tokens": 50_000}))
    directory = repo / ".brindle"
    directory.mkdir(exist_ok=True)
    for name in ("config.json", "config.local.json"):
        (directory / name).write_text(json.dumps({"supervisor_restart_tokens": 50_000}))
    body = {} if policy == "absent" else {
        "dangerous": {"supervisor_restart_tokens": 0 if policy == "zero" else 50_000}}
    serve_policy(monkeypatch, body, enterprise=policy != "unentitled")
    if policy == "unavailable":
        def unavailable(*a, **kw):
            raise team_policy.PolicyUnavailable("no cached policy")
        monkeypatch.setattr(team_policy, "current_policy", unavailable)
    root, payload, detached = supervisor
    assert not supervisor_state.on_turn_end(db, root, payload)
    assert detached == [] and not db.restart_holding(root.id)
