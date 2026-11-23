"""Route and launch pinned profiles against the user's model catalog."""

import json

import pytest

from brindle import agents, autopilot, model_availability, provider_health, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent
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


def test_launch_refuses_a_pin_disallowed_after_the_catalog_changes(db, repo, routing, launches, detached, monkeypatch):
    configure(repo, ["pinned"])
    org = team_policy.OrgPolicy("example", 1, allowed_models=("gpt-6.2-sol",))
    monkeypatch.setattr(team_policy.ProPolicy, "policy", lambda self: org)
    monkeypatch.setattr("brindle.policy.plugin", lambda cfg, root: team_policy.ProPolicy(root))

    def discover(request, limit):
        model = "gpt-6.2-sol" if request.cwd == str(repo) else "gpt-6.1-sol"
        return model_availability.Catalog(frozenset({model}), "codex")

    monkeypatch.setattr(agents, "_model_checker", model_availability.ModelAvailabilityChecker(discover=discover, ttl=0))
    profile, _ = autopilot.choose_profile(db, "", str(repo), weight="heavy")
    assert profile == "pinned"
    ws = workspaces.create(db, str(repo), "worker-branch").workspace
    with pytest.raises(agents.AgentError, match="gpt-6.1-sol"):
        agents.spawn(db, ws, profile, prompt="task", mode="assign", background_setup=True)
    assert not launches


@pytest.mark.parametrize("checks", [
    [],
    pytest.param(["suite"], marks=pytest.mark.variant("a queued review reroutes after checks in the current account")),
])
def test_reviewer_order_demotes_the_writers_launched_model(db, repo, routing, launches, detached, monkeypatch, checks):
    configure(repo, ["pinned"])
    profiles = repo / ".brindle" / "agents"
    (profiles / "same-line.md").write_text("---\nextends: dev.gpt\nmodel: gpt-6.1-sol\n---\n")
    (profiles / "other-line.md").write_text("---\nextends: dev.gpt\nmodel: gpt-6-astra\n---\n")
    caller = Agent("review-caller", routing.id, "supervisor", "codex", None,
                   "interactive", "idle", "", None, 0)
    db.add_agent(caller)
    home = "/preview-home" if checks else "/review-home"
    monkeypatch.setattr("brindle.provider_homes.decide", lambda provider: home)
    monkeypatch.setattr(agents, "_hidden_brindle_tools", lambda keep: [])

    def effective(agent, ws, managed=None):
        account = "writer" if agent.mode == "assign" and agent.id != "preflight" else "preview"
        if agent.parent_id == caller.id and agent.config_home == "/review-home" and agent.mode == "review":
            account = "review"
        return {"TEST_CATALOG_ACCOUNT": account}

    def discover(request, limit):
        account = request.env["TEST_CATALOG_ACCOUNT"]
        sol = "gpt-6.2-sol" if account in ("writer", "review") else "gpt-6.1-sol"
        astra = "gpt-6.2-astra" if account == "review" else "gpt-6-astra"
        return model_availability.Catalog(frozenset({sol, astra}), "codex")

    monkeypatch.setattr(agents, "_effective_window_env", effective)
    checker = model_availability.ModelAvailabilityChecker(discover=discover)
    monkeypatch.setattr(agents, "_model_checker", checker)
    agents.spawn(db, routing, "pinned", prompt="task", mode="assign", weight="heavy", background_setup=True)
    assert flag(launches[-1], "--model") == "gpt-6.2-sol"
    configure(repo, ["pinned"], pinned="gpt-6.1-terra")
    cfg = RepoConfig(agent_models={"reviewer": {"heavy": ["same-line", "other-line"]}},
                     review_sizing=False, checks=checks)
    claim_review = agents._claim_review

    def change_home_before_launch(*args):
        nonlocal home
        home = "/preview-home"
        return claim_review(*args)

    monkeypatch.setattr(agents, "_claim_review", change_home_before_launch)
    if checks:
        queued = []
        monkeypatch.setattr(agents, "_detach", queued.append)
        assert agents.start_review(db, caller, routing, cfg=cfg) is None
        [command] = queued
        home = "/review-home"
        monkeypatch.setattr("brindle.gates.check_summary", lambda *a, **kw: "")
        reviewer = agents.review_after_checks(
            db, caller.id, routing, command[command.index("--profile") + 1], cfg=cfg,
            request_id=command[command.index("--request-id") + 1], slot="",
            auto_profile="--auto-profile" in command)
    else:
        reviewer = agents.request_review(db, caller, routing, cfg=cfg)
    assert reviewer.profile == "other-line"
    assert reviewer.config_home == "/review-home"
    assert flag(launches[-1], "--model") == "gpt-6.2-astra"


def test_launch_model_visible_before_pane_starts_and_restored_if_open_fails(
        db, repo, routing, launches, detached, monkeypatch):
    configure(repo, ["pinned"], pinned="gpt-6.2-sol")
    worker = agents.spawn(db, routing, "pinned", prompt="task", mode="assign", background_setup=True)
    configure(repo, ["pinned"], pinned="gpt-6.2-astra")

    def fail_open(database, agent, *args, **kwargs):
        assert database.agent_launch_model(agent.id) == "gpt-6.2-astra"
        raise OSError("window creation failed")

    monkeypatch.setattr(agents, "_open_window", fail_open)
    with pytest.raises(OSError):
        agents._launch(db, worker, routing, prompt=None, resume="previous", watch_pane=False, model_checked=True)
    assert db.agent_launch_model(worker.id) == "gpt-6.2-sol"
