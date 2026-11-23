import pytest

from brindle import learning, model_names, profiles
from brindle.pro import learning as pro_learning
from test_launch_health import detached, flag, launches  # noqa: F401


def test_learner_history_under_an_old_id_is_recorded_under_the_renamed_profile():
    key = pro_learning.OrgKey("org_1", "k1", b"0" * 32)
    task = learning.TaskInfo(repo_root="/repo", task="fix it", profile="developer-codex")
    body = pro_learning.record_payload(key, "id", task, learning.Outcome("merged"))
    assert body["profile"] == "dev.gpt"


@pytest.mark.variant("a built-in profile added without a model entry would show its raw id in the app")
def test_every_builtin_profile_has_a_display_name():
    for name in profiles._builtin_names("brindle.builtin_agents"):
        p = profiles.load_profile(name)
        shown = model_names.describe(p)
        assert set(shown) == {"display", "maker", "tool", "effort"}, name
        assert shown["display"] and shown["maker"] and shown["tool"], name
        assert "default" not in shown["display"].lower(), name
        if p.model:
            assert p.model not in shown["display"], name   # a raw id like gpt-6.1-sol or qwen3-coder:30b
        assert shown["effort"] == p.effort, name


@pytest.mark.variant("a model added to the catalog must reach pricing, display and worker tiers from that one edit")
def test_catalog_models_reach_pricing_display_and_worker_tiers():
    from brindle import model_catalog, pricing, worker_models

    for model in model_catalog.CATALOG.keys() | model_names.MODELS.keys():
        assert pricing.price_for(model) is not None, model
        shown = model_names.model_name(model)
        assert shown.display and shown.display != model, model
        assert shown.family and shown.maker, model
    for model in model_names.MODELS:
        assert worker_models.model_tiers(model), model
    for pick in worker_models.CATALOG:
        found = model_catalog.lookup(pick.model)
        if found:
            assert (pick.label, pick.tiers) == (found.name.display, found.tiers)


@pytest.mark.variant("a GPT version bump must preserve tier priority and saved worker picks")
def test_gpt_worker_routes_upgrade_in_place():
    from brindle.config import DEFAULT_ROUTING
    from brindle.worker_models import find, runs

    assert DEFAULT_ROUTING["medium"] == [
        "dev.gpt", "dev.claude-sonnet", "developer", "dev.claude-opus", "dev.gpt-terra", "dev.gpt-sol",
    ]
    assert DEFAULT_ROUTING["heavy"] == [
        "dev.claude-opus", "dev.gemini", "developer", "dev.gpt", "dev.gpt-sol", "dev.gpt-astra",
    ]
    for profile, old_id, model in (
        ("dev.gpt-sol", "gpt-6.1-sol", "gpt-6.2-sol"),
        ("dev.gpt-astra", "gpt-6-astra", "gpt-6.2-astra"),
    ):
        assert profiles.load_profile(profile).model == model
        pick = find(model)
        assert pick is not None and pick.profile == profile
        assert find(old_id) == pick
        assert runs(pick) == ("codex", model)


@pytest.mark.parametrize("model, provider, family, display", [
    ("claude-opus-5-7", "claude", "Claude", "Claude Opus 5.7"),
    pytest.param("gpt-6.2-unlisted", "codex", "GPT", "GPT‑6.2 Unlisted", marks=pytest.mark.variant(
        "a new GPT-6.2 suffix must retain the dotted version and never inherit another model's price")),
])
@pytest.mark.variant("an uncatalogued future version must keep its family and version with unknown cost")
def test_uncatalogued_model_keeps_its_family_version_and_unknown_cost(repo, model, provider, family, display):
    from brindle import autopilot, pricing
    from test_pricing import profile

    profile(repo, "future", provider=provider, model=model)
    shown = model_names.describe(profiles.load_profile("future", str(repo)))
    assert shown["display"] == display
    assert model_names.model_name(model).family == family
    assert pricing.price_for(model) is None
    assert autopilot.cost_tier("future", repo_root=str(repo)) == "unknown"


def test_gemini_worker_launch_selects_the_catalog_model(monkeypatch):
    from brindle.providers import Antigravity, LaunchContext

    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    p = profiles.load_profile("dev.gemini")
    argv = Antigravity().command(LaunchContext("worker", p, None))
    assert argv[argv.index("--model") + 1] == "gemini-4-argon"
    assert argv[argv.index("--effort") + 1] == "high"


@pytest.mark.parametrize("profile, provider, model, display", [
    ("review.gpt", "codex", "gpt-6.2-sol", "GPT‑6.2 Sol"),
    pytest.param("review.opencode", "opencode", "opencode/beam-free", "Beam Free", marks=pytest.mark.variant(
        "OpenCode must prefer its merged configured model to the catalog fallback")),
    pytest.param("developer", "claude", "claude-opus-5-5", "Claude Opus 5.5", marks=pytest.mark.variant(
        "Claude must prefer a configured model alias to its catalog default")),
])
@pytest.mark.variant("each row is a provider whose unpinned profile must show the concrete model it launches, never the bare family")
def test_unpinned_profile_shows_the_launched_model(db, repo, monkeypatch, codex_default_model, launches, detached,
                                                  profile, provider, model, display):
    from types import SimpleNamespace

    from brindle import agents, codex_hook, model_availability, policy, providers, spawn, view, workspaces
    from brindle.service.routing import RoutingPolicy

    class AppServer:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, params):
            if method == "config/read":
                return {"config": {}}
            assert method == "model/list"
            return {"data": [{"model": "gpt-6.1-terra", "isDefault": True}]}

        def close(self):
            pass

    monkeypatch.setattr(codex_hook, "_AppServer", AppServer)
    monkeypatch.setattr(providers, "codex_default_model", codex_default_model)
    monkeypatch.setattr(agents, "_model_checker", model_availability.ModelAvailabilityChecker(
        discover=lambda request, limit: model_availability.Catalog(frozenset(), "codex", complete=False)))
    monkeypatch.setattr(agents, "_effective_window_env", lambda *args: {})
    monkeypatch.setattr(codex_hook, "launch_flags", lambda *args, **kwargs: [])
    monkeypatch.setattr(providers, "trust_codex_project", lambda *args: None)
    if provider == "opencode":
        read = spawn.read

        def configured(argv, *args, **kwargs):
            if argv[1:] == ["debug", "config", "--pure"]:
                return SimpleNamespace(stdout='{"model":"opencode/beam-free"}', returncode=0)
            return read(argv, *args, **kwargs)

        monkeypatch.setattr(spawn, "read", configured)
    if provider == "claude":
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        settings = repo / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"model":"opus"}')
    ws = workspaces.adopt_root(db, str(repo))
    assert policy._profile_fields(profile, ws.repo_root) == (provider, model)
    assert RoutingPolicy.load(ws.repo_root).runs(profile) == (provider, model)
    agent = agents.spawn(db, ws, profile, mode="assign" if provider == "claude" else "review", background_setup=True)
    [argv] = launches
    assert flag(argv, "--model") == model
    entry = view.agent_entry(db, agent, alive=False)
    assert (entry["model"], entry["model_display"]) == (model, display)


@pytest.mark.variant("managed and suffixed Claude ids must name the right model, not the raw route id")
def test_full_claude_ids_resolve_to_their_model():
    # An env override or managed route gives a full id, not the alias.
    managed = model_names.model_name("eu.anthropic.claude-opus-x-v1:0", "claude")
    assert managed.family == "Claude" and "Opus" in managed.display
    assert "eu.anthropic" not in managed.display
    assert model_names.model_name("claude-sonnet-5-5[1m]", "claude") == model_names.MODELS["sonnet"]


@pytest.mark.variant("an effort-suffixed id must price as its base model and report the effort")
def test_discovered_effort_variant_uses_canonical_price_and_reports_effort():
    from brindle import pricing, worker_models
    from brindle.usage import Usage, usage_cost

    model = "gemini-4-argon-high"
    p = profiles.Profile(name="discovered", description="", provider="antigravity", prompt="", model=model)
    shown = model_names.describe(p)
    assert shown["display"] == "Gemini 4 Argon" and shown["effort"] == "high"
    price = pricing.price_for("gemini-4-argon")
    assert usage_cost(Usage(input_tokens=1_000_000, model=model)) == price.input
    assert worker_models.model_tiers(model) == worker_models.model_tiers("gemini-4-argon")


@pytest.mark.parametrize("name, reviewer", [
    ("review.gpt", True),
    ("reviewer-codex", True),
    ("dev.gpt", False),
])
@pytest.mark.variant("role detection must accept old and new reviewer ids and reject developer ids")
def test_reviewer_ids_are_recognised_in_both_spellings(name, reviewer):
    assert model_names.is_reviewer_id(name) is reviewer


@pytest.mark.variant("an org policy written before the rename must keep allowing the same profiles")
def test_an_org_allowlist_written_with_old_ids_still_allows_the_renamed_profiles():
    from brindle.pro.team_policy import Access

    assert Access(allowed_profiles=("developer-codex",)).allows_profile("dev.gpt", "codex", None)
    assert Access(allowed_profiles=("dev.gpt",)).allows_profile("developer-codex", "codex", None)
    assert not Access(allowed_profiles=("dev.claude-sonnet",)).allows_profile("dev.gpt", "codex", None)


@pytest.mark.parametrize("claim, shadowed, blocks", [
    pytest.param(True, False, True, marks=pytest.mark.variant(
        "a live claim saved under the old reviewer id holds the renamed reviewer's slot, so no second reviewer starts")),
    pytest.param(False, False, False, marks=pytest.mark.variant("with no legacy claim, the renamed slot is claimed as usual")),
    pytest.param(True, True, False, marks=pytest.mark.variant(
        "a custom profile under the old id is its own identity: its claim must not block the built-in")),
])
def test_a_legacy_claim_holds_the_renamed_review_slot(db, repo, claim, shadowed, blocks):
    from brindle import agents, workspaces

    ws = workspaces.adopt_root(db, str(repo))
    if shadowed:
        (repo / ".brindle" / "agents").mkdir(parents=True)
        (repo / ".brindle" / "agents" / "reviewer-codex.md").write_text(
            "---\nname: reviewer-codex\nprovider: claude\n---\nDo it.\n")
    if claim:
        db.claim_review(ws.id, "abc123", "reviewer-codex")   # claimed, its reviewer not yet started
    token, holder = agents._claim_review(db, ws, "abc123", "review.gpt")
    assert (token is None) is blocks


def test_handover_from_an_old_predecessor_id_is_found():
    from brindle import cascade
    from test_cascade import _table

    raw = _table(50)
    raw["handover"] = {"developer-heavy>developer-codex": {"p": 0.99, "runs": 10}}
    assert cascade.parse(raw).handover_p("developer-heavy", "dev.gpt") == 0.99


@pytest.mark.variant("an alias must never let a custom profile borrow a built-in's org permission")
def test_a_custom_profile_under_an_old_id_is_not_allowed_by_the_renamed_entry(tmp_path):
    from brindle.pro.team_policy import Access

    repo = tmp_path / "proj"
    (repo / ".brindle" / "agents").mkdir(parents=True)
    (repo / ".brindle" / "agents" / "developer-codex.md").write_text(
        "---\nname: developer-codex\nprovider: codex\n---\nDo it.\n")
    acc = Access(allowed_profiles=("developer-codex",), repo_root=str(repo))
    assert acc.allows_profile("developer-codex", "codex", None)
    assert not acc.allows_profile("dev.gpt", "codex", None)

