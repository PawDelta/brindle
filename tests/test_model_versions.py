"""Route and launch pinned profiles against the user's model catalog."""

import json

import pytest

from brindle import agents, autopilot, model_availability, provider_health, workspaces
from brindle.pro import team_policy
from brindle.profiles import load_profile
from test_launch_health import detached, flag, launches  # noqa: F401


@pytest.fixture
def routing(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    monkeypatch.setattr(provider_health, "check", lambda *a, **kw: provider_health.Health("ok"))
    monkeypatch.setattr("brindle.quota.known_headroom", lambda *a, **kw: None)
    monkeypatch.setattr("brindle.providers.signed_out", lambda *a, **kw: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    return ws


def configure(repo, names, pinned="gpt-6.1-sol"):
    settings = repo / ".brindle"
    profiles = settings / "agents"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "pinned.md").write_text(f"---\nextends: dev.gpt\nmodel: {pinned}\n---\n")
    (settings / "config.json").write_text(json.dumps({
        "routing": {"heavy": names}, "routing_locked": ["heavy"],
    }))


@pytest.mark.variant("an explicitly locked order skips a stale first entry without opening another tier")
def test_routing_silently_skips_missing_profile(db, repo, routing):
    configure(repo, ["retired-worker", "developer"])
    why = []
    assert autopilot.choose_profile(db, "", str(repo), weight="heavy", why=why)[0] == "developer"
    assert all("retired-worker" not in line for line in why)


@pytest.mark.parametrize("pinned,listed,allowed,expected", [
    ("gpt-6.1-sol", {"gpt-6.2-sol"}, ("gpt-6.2-sol",), "gpt-6.2-sol"),
    pytest.param("gpt-6.1-sol", {"gpt-6.1-sol"}, None, "gpt-6.1-sol",
                 marks=pytest.mark.variant("the newest model must be listed by the provider")),
    ("gpt-6.1-sol", {"gpt-6.2-sol"}, ("gpt-6.1-sol",), "gpt-6.1-sol"),
    pytest.param("gpt-6.1-terra", {"gpt-6.2-sol"}, None, "gpt-6.1-terra",
                 marks=pytest.mark.variant("a newer version in another model line cannot replace the pin")),
])
def test_routing_launches_newest_allowed_version_in_same_line(
        db, repo, routing, launches, detached, monkeypatch, pinned, listed, allowed, expected):
    configure(repo, ["pinned", "dev.gpt"], pinned)
    policy = team_policy.OrgPolicy("example", 1, allowed_models=allowed)
    monkeypatch.setattr(team_policy.ProPolicy, "policy", lambda self: policy)
    monkeypatch.setattr("brindle.policy.plugin", lambda cfg, root: team_policy.ProPolicy(root))
    checker = model_availability.ModelAvailabilityChecker(
        discover=lambda request, limit: model_availability.Catalog(frozenset(listed), "codex"))
    monkeypatch.setattr(agents, "_model_checker", checker)

    why = []
    profile, _ = autopilot.choose_profile(db, "", str(repo), weight="heavy", why=why)
    assert profile == "pinned"
    agents.spawn(db, routing, profile, prompt="task", mode="assign", background_setup=True)
    assert flag(launches[-1], "--model") == expected
    assert expected in " ".join(why).lower().replace("‑", "-").replace(" ", "-")
    assert load_profile(profile, str(repo)).model == pinned
