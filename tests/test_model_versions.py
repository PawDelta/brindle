"""Route and launch pinned profiles against the user's model catalog."""

import json

import pytest

from brindle import agents, autopilot, model_availability, provider_health, tmux, workspaces
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
    monkeypatch.setattr(agents, "_model_checker", model_availability.ModelAvailabilityChecker(
        discover=lambda request, limit: model_availability.Catalog(frozenset(), "codex", complete=False)))
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
        def check_summary(*args, **kwargs):
            nonlocal home
            home = "/review-home"
            assert agents.start_review(db, caller, routing, cfg=cfg) is None
            assert queued == [command]
            assert agents.start_review(db, caller, routing, "same-line", cfg=cfg, second=True) is None
            assert len(queued) == 2
            return ""

        monkeypatch.setattr("brindle.gates.check_summary", check_summary)
        reviewer = agents.review_after_checks(
            db, caller.id, routing, command[command.index("--profile") + 1], cfg=cfg,
            request_id=command[command.index("--request-id") + 1], slot="",
            auto_profile="--auto-profile" in command)
    else:
        reviewer = agents.request_review(db, caller, routing, cfg=cfg)
    assert reviewer.profile == "other-line"
    assert reviewer.config_home == "/review-home"
    assert flag(launches[-1], "--model") == "gpt-6.2-astra"
    if checks:
        monkeypatch.setattr(agents, "alive_or_starting", lambda agent: agent.id == reviewer.id)
        assert agents.start_review(db, caller, routing, cfg=cfg).id == reviewer.id
        assert agents.start_review(db, caller, routing, "same-line", cfg=cfg, second=True) is None
        assert sum("_review-after-checks" in argv for argv in queued) == 2


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


def test_post_start_theme_error_keeps_launched_model(db, repo, routing, detached, monkeypatch):
    configure(repo, ["pinned"], pinned="gpt-6.2-astra")
    runnable = set()
    themed = []

    def open_window(*args, tag, **kwargs):
        assert db.agent_launch_model(tag[1]) == "gpt-6.2-astra"
        return "%new"

    def fail_theme(session):
        themed.append(bool(runnable))
        raise OSError("theme failed")

    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **kw: None)
    monkeypatch.setattr(tmux, "new_window", open_window)
    monkeypatch.setattr(tmux, "open_gate", runnable.add)
    monkeypatch.setattr(tmux, "apply_theme", fail_theme)
    monkeypatch.setattr(tmux, "mark_launched", lambda *a: None)
    monkeypatch.setattr(agents, "_ensure_sidebar", lambda *a: None)
    worker = agents.spawn(db, routing, "pinned", prompt="task", mode="assign",
                          watch_pane=True, background_setup=True)
    assert themed == [True]
    assert db.get_agent(worker.id).tmux_window == "%new"
    assert db.agent_launch_model(worker.id) == "gpt-6.2-astra"


@pytest.mark.parametrize("fault", [
    "registration", "gate",
    pytest.param("unconfirmed-registration", marks=pytest.mark.variant(
        "an unconfirmed teardown after registration failure must keep the model and leave the gate closed")),
])
def test_critical_pane_startup_failure_is_reported(db, repo, routing, detached, monkeypatch, fault):
    configure(repo, ["pinned"], pinned="gpt-6.2-astra")
    opened = []
    gate_signals = []
    discarded = []
    update_agent = db.update_agent

    def new_window(*args, tag, **kwargs):
        opened.append(tag[1])
        return "%new"

    def register(agent_id, **fields):
        if "tmux_window" in fields and fault != "gate":
            raise OSError("registration failed")
        update_agent(agent_id, **fields)

    def signal(gate):
        gate_signals.append(gate)
        if fault == "gate":
            raise OSError("gate failed")

    def discard(target):
        assert not gate_signals
        discarded.append(target)
        return fault == "registration"

    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **kw: None)
    monkeypatch.setattr(tmux, "new_window", new_window)
    monkeypatch.setattr(tmux, "open_gate", signal)
    monkeypatch.setattr(tmux, "discard_gated_launch", discard, raising=False)
    monkeypatch.setattr(tmux, "apply_theme", lambda *a: None)
    monkeypatch.setattr(tmux, "mark_launched", lambda *a: None)
    monkeypatch.setattr(agents, "_ensure_sidebar", lambda *a: None)
    monkeypatch.setattr(db, "update_agent", register)
    with pytest.raises((OSError, agents.AgentError)):
        agents.spawn(db, routing, "pinned", prompt="task", mode="assign",
                     watch_pane=True, background_setup=True)
    [agent_id] = opened
    if fault == "registration":
        assert discarded == ["%new"]
        assert not gate_signals
        assert db.get_agent(agent_id) is None
        assert db.agent_launch_model(agent_id) is None
    elif fault == "gate":
        assert len(gate_signals) == 1
        assert not discarded
        assert db.get_agent(agent_id).tmux_window == "%new"
        assert db.agent_launch_model(agent_id) == "gpt-6.2-astra"
    else:
        assert discarded == ["%new"]
        assert not gate_signals
        assert db.get_agent(agent_id) is not None
        assert db.agent_launch_model(agent_id) == "gpt-6.2-astra"


@pytest.mark.parametrize("during_registration", [
    False,
    pytest.param(True, marks=pytest.mark.variant("competing dispatch must see startup before pane registration completes")),
])
def test_review_dispatch_surfaces_unconfirmed_gate_on_later_requests(
        db, repo, routing, detached, monkeypatch, during_registration):
    from brindle import gates, login_shell_env

    configure(repo, ["pinned"], pinned="gpt-6.2-astra")
    opened = []
    pane_gone = False
    original = tmux.NewPane("%71")
    original.launch = "123456abcdef"

    def new_window(*args, tag, **kwargs):
        opened.append(tag[1])
        return "%72" if pane_gone else original

    def fail_signal(gate):
        if not pane_gone:
            raise OSError("gate signal did not reach tmux")

    def confirm_gone(pane, launch, server):
        assert (pane, launch, server) == (original, original.launch, "review-server")
        return pane_gone

    update_agent = db.update_agent

    def register(agent_id, **fields):
        update_agent(agent_id, **fields)
        if during_registration and fields.get("tmux_window") == original:
            with pytest.raises(agents.AgentError):
                agents.request_review(db, None, routing, "pinned")

    monkeypatch.setattr(login_shell_env, "asks_daemon", lambda: True)
    monkeypatch.setattr(login_shell_env, "shim", lambda agent_id, argv: argv)
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **kw: None)
    monkeypatch.setattr(tmux, "new_window", new_window)
    monkeypatch.setattr(tmux, "open_gate", fail_signal)
    monkeypatch.setattr(tmux, "current_server", lambda: "review-server")
    monkeypatch.setattr(tmux, "launch_pane_gone", confirm_gone)
    monkeypatch.setattr(tmux, "apply_theme", lambda *a: None)
    monkeypatch.setattr(tmux, "mark_launched", lambda *a: None)
    monkeypatch.setattr(agents, "alive_or_starting", lambda agent: agent.tmux_window == original and not pane_gone)
    monkeypatch.setattr(agents, "_hidden_brindle_tools", lambda keep: [])
    monkeypatch.setattr(db, "update_agent", register)

    with pytest.raises(agents.AgentError):
        agents.request_review(db, None, routing, "pinned")

    [agent_id] = opened
    assert db.get_agent(agent_id).tmux_window == original
    assert db.agent_launch_model(agent_id) == "gpt-6.2-astra"
    assert db.review_claim(routing.id, gates.head(routing), "pinned")[0] == agent_id

    with pytest.raises(agents.AgentError):
        agents.request_review(db, None, routing, "pinned")
    with pytest.raises(agents.AgentError):
        agents.start_review(db, None, routing, "pinned", cfg=RepoConfig(checks=["suite"]))
    with pytest.raises(agents.AgentError):
        agents.request_review(db, None, routing, "review.gpt", slot="pinned")
    assert opened == [agent_id] and not detached
    if not during_registration:
        pane_gone = True
        replacement = agents.request_review(db, None, routing, "pinned")
        assert replacement.id != agent_id and replacement.review_sha == gates.head(routing)
        assert db.review_claim(routing.id, replacement.review_sha, "pinned")[0] == replacement.id
