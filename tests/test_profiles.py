import pytest

from brindle import model_names
from brindle.profiles import _frontmatter, _list, compose, kind_of, load_profile


def test_custom_agent_composes_at_launch_on_its_recorded_model(tmp_path, db, monkeypatch):
    from brindle import agents, providers
    from brindle.db import Agent, DB, Workspace, _columns
    from brindle.providers import Codex, LaunchContext

    root = str(tmp_path)
    library = tmp_path / ".brindle" / "agents"
    library.mkdir(parents=True)
    (library / "mine.md").write_text(
        "---\nextends: qa\nrules: [security/backend]\n---\nCheck database invariants.")
    (library / "pinned.md").write_text("---\nextends: mine\neffort: high\n---\n")
    (library / "pinned-child.md").write_text("---\nextends: pinned\n---\n")
    (tmp_path / ".brindle" / "config.json").write_text('{"prompt_cache_prefix": false}')
    ws = Workspace("w", root, "test", "worktree", "feat/test", "main", root, None, "test", 1)
    db.add_workspace(ws)
    db.add_agent(Agent("legacy", ws.id, "dev.gpt-sol", "codex", None, "assign", "idle", "", None, 1,
                       tmux_server="test"))
    assert db.get_agent("legacy").agent is None
    assert db.conn.execute("SELECT * FROM agent_roles").fetchall() == []
    # Preserve role names from the development build, including a concurrent opener.
    db.conn.execute("ALTER TABLE agents ADD COLUMN agent TEXT")
    db.conn.execute("UPDATE agents SET agent='qa' WHERE id='legacy'")
    real_tx = DB.tx
    moved = []

    def tx_after_other_process(self):
        if not moved:
            moved.append(True)
            DB(db.path).conn.close()
        return real_tx(self)

    with monkeypatch.context() as patch:
        patch.setattr(DB, "tx", tx_after_other_process)
        db._migrate()
    assert moved and "agent" not in _columns(db.conn)["agents"]
    assert db.get_agent("legacy").agent == "qa"
    db.update_agent("legacy", agent="mine", weight="light")
    assert (db.get_agent("legacy").agent, db.get_agent("legacy").weight) == ("mine", "light")
    db.update_agent("legacy", agent=None)
    assert db.get_agent("legacy").agent is None
    row = Agent("a", ws.id, "dev.gpt-sol", "codex", "legacy", "assign", "idle", "", None, 1,
                agent="mine", tmux_server="test")
    db.add_agent(row)
    db.update_agent(row.id, weight="medium")
    saved = db.get_agent(row.id)
    assert (saved.agent, saved.profile) == ("mine", "dev.gpt-sol")
    assert db.children("legacy")[0].agent == "mine"
    assert {a.id: a.agent for a in db.list_agents(ws.id)} == {"legacy": None, "a": "mine"}
    assert {a.id: a.agent for a in db.list_agents()} == {"legacy": None, "a": "mine"}
    assert kind_of("mine", root) == kind_of("qa", root) == "agent"
    assert kind_of("pinned-child", root) == "pinned"
    assert kind_of("developer-codex-sol", root) == "model"

    base = load_profile("dev.gpt-sol", root)
    joined = compose("mine", "dev.gpt-sol", root)
    assert (joined.provider, joined.model, joined.name) == ("codex", "gpt-6.2-sol", "dev.gpt-sol")
    assert joined.allowed_tools == base.allowed_tools
    assert joined.prompt.startswith(base.prompt + "\n\n")
    assert joined.prompt.endswith("Check database invariants.")
    assert joined.rules == ["tests/only", "security/backend"]

    launched = agents._profile_for(db, saved, ws, m=None)
    assert (launched.provider, launched.model, launched.allowed_tools) == (
        joined.provider, joined.model, joined.allowed_tools)
    assert joined.prompt in launched.prompt
    assert launched.rules == joined.rules
    monkeypatch.setattr(providers, "_codex_layers", lambda cwd: [])
    argv = Codex().command(LaunchContext(row.id, launched, None, cwd=root))
    assert argv[argv.index("--model") + 1] == "gpt-6.2-sol"


@pytest.mark.parametrize("agent, model", [
    ("developer", "dev.claude-opus"),
    pytest.param("reviewer", "review.gpt", marks=pytest.mark.variant(
        "standalone provider reviewer roots must not receive the Claude reviewer prompt or model")),
])
@pytest.mark.variant("composing a role root must preserve every legacy field without appending its prompt twice")
def test_default_agent_composition_preserves_the_existing_profile(agent, model):
    assert kind_of(agent) == "agent"
    assert kind_of(model) == "model"
    assert compose(agent, model) == load_profile(model)


def test_custom_agent_cannot_widen_the_provider_tools(tmp_path, monkeypatch):
    library = tmp_path / ".brindle" / "agents"
    library.mkdir(parents=True)
    (library / "mine.md").write_text(
        "---\nextends: developer\nallowed_tools: Bash(git status:*), Bash(*)\n---\nInspect.")
    root = str(tmp_path)
    joined = compose("mine", "dev.qwen3-coder", root)
    assert joined.allowed_tools == ["Bash(git status:*)"]
    assert joined.permission_mode == load_profile("dev.qwen3-coder", root).permission_mode
    # A disjoint request must remain an empty allowlist, never inherit the root's tools.
    (library / "mine.md").write_text(
        "---\nextends: developer\nallowed_tools: Bash(*)\n---\nInspect.")
    assert compose("mine", "dev.qwen3-coder", root).allowed_tools == []
    (library / "mine.md").write_text("---\nextends: developer\n---\nInspect.")
    assert compose("mine", "dev.qwen3-coder", root).allowed_tools == load_profile(
        "dev.qwen3-coder", root).allowed_tools
    # A reviewer on a developer model keeps the provider's reviewer permissions.
    reviewer = compose("reviewer", "dev.gpt-sol", root)
    assert (reviewer.provider, reviewer.model) == ("codex", "gpt-6.2-sol")
    assert reviewer.allowed_tools == load_profile("review.gpt", root).allowed_tools
    assert reviewer.permission_mode == load_profile("review.gpt", root).permission_mode
    # The native role base supplies permissions, not Ollama runtime defaults.
    from brindle.native import catalog

    table = {"example": {
        "api": "https://example.test/v1", "env": ["EXAMPLE_API_KEY"], "npm": "@ai-sdk/anthropic",
        "models": {"cloud-coder": {"limit": {"context": 128000, "output": 8000}}}}}
    monkeypatch.setattr(catalog, "refresh_async", lambda: None)
    monkeypatch.setattr(catalog, "_read_cache", lambda: table)
    (library / "hosted.md").write_text(
        "---\nextends: developer\nprovider: native\nmodel: example/cloud-coder\n---\nCloud task.")
    selected = load_profile("hosted", root)
    hosted = compose("developer", "hosted", root)
    runtime = ("base_url", "model", "api_key_env", "api", "context_tokens", "fallback")
    assert tuple(getattr(hosted, key) for key in runtime) == tuple(
        getattr(selected, key) for key in runtime) == (
            "https://example.test/v1", "cloud-coder", "EXAMPLE_API_KEY", "anthropic", 120000, [])
    native_base = load_profile("dev.qwen3-coder", root)
    assert hosted.allowed_tools == native_base.allowed_tools
    assert hosted.prompt == native_base.prompt + "\n\nCloud task."


def test_agent_model_orders_merge_by_agent_and_size_across_config_scopes(tmp_path, brindle_home):
    import json

    from brindle.config import load_repo_config

    brindle_home.mkdir(parents=True, exist_ok=True)
    (brindle_home / "config.json").write_text(json.dumps({"agent_models": {
        "qa": {"light": ["developer-codex"], "heavy": ["dev.claude-opus"]},
        "reviewer": {"light": ["reviewer-codex"]}}}))
    settings = tmp_path / ".brindle"
    settings.mkdir()
    (settings / "config.json").write_text(json.dumps({"agent_models": {
        "qa": {"light": ["dev.claude-haiku"], "medium": ["dev.gpt-sol"]}}}))
    (settings / "config.local.json").write_text(json.dumps({"agent_models": {
        "qa": {"light": ["developer-local", 7], "heavy": [], "invalid": ["ignored"]},
        "broken": "ignored"}}))

    assert load_repo_config(tmp_path).agent_models == {
        "qa": {"light": ["dev.qwen3-coder"], "medium": ["dev.gpt-sol"], "heavy": []},
        "reviewer": {"light": ["review.gpt"]}}
    assert load_repo_config(tmp_path, user_only=True).agent_models == {
        "qa": {"light": ["dev.gpt"], "heavy": ["dev.claude-opus"]},
        "reviewer": {"light": ["review.gpt"]}}


@pytest.mark.variant("configs naming an old built-in id must keep loading after the rename")
def test_an_old_built_in_id_loads_its_renamed_profile():
    for old, new in model_names.ALIASES.items():
        p = load_profile(old)
        assert p.name == new
        assert p.provider == load_profile(new).provider


@pytest.mark.variant("custom agents extending an old id must still inherit the renamed built-in")
def test_a_custom_agent_that_extends_an_old_id_still_works(tmp_path):
    agents = tmp_path / ".brindle" / "agents"
    agents.mkdir(parents=True)
    (agents / "mine.md").write_text(
        "---\nname: mine\ndescription: a custom Codex agent\nextends: developer-codex\n---\nDo the task.\n")

    p = load_profile("mine", str(tmp_path))
    assert p.name == "mine" and p.provider == "codex"
    assert kind_of("mine", str(tmp_path)) == "pinned"


@pytest.mark.parametrize("name, text, role", [
    ("dev.claude-opus", None, "developer"),            # inherits developer's role through extends
    pytest.param("review.gpt", None, "reviewer", marks=pytest.mark.variant(
        "a standalone built-in reviewer extends nothing, so only its own role: line makes it a reviewer")),
    ("qa", None, "qa"),                                # a role of its own, though built on developer
    ("mine", "extends: reviewer-codex", "reviewer"),   # a custom agent on an old id
    ("auditor", "provider: codex", "auditor"),         # built on nothing: a custom role
    ("developer-mine", "provider: codex", "developer"),  # a standalone copy under the old naming
])
@pytest.mark.variant("each row is a distinct way a profile gets its role: own line, built-in, or inherited")
def test_base_role_follows_the_extends_chain(tmp_path, name, text, role):
    from brindle.profiles import base_role

    if text:
        agents = tmp_path / ".brindle" / "agents"
        agents.mkdir(parents=True)
        (agents / f"{name}.md").write_text(f"---\nname: {name}\n{text}\n---\nDo the task.\n")
    assert base_role(name, str(tmp_path)) == role


@pytest.mark.parametrize("name, model", [
    ("developer-codex-luna", "gpt-6-luna"),
    pytest.param("developer-codex", None, marks=pytest.mark.variant(
        "the unpinned Codex profile launches the CLI's own default model, with no --model flag")),
])
def test_codex_profile_launches_with_its_pinned_model(name, model, tmp_path, monkeypatch):
    from brindle import providers
    from brindle.providers import Codex, LaunchContext

    monkeypatch.setattr(providers, "_codex_layers", lambda cwd: [])
    argv = Codex().command(LaunchContext("w1", load_profile(name), None, cwd=str(tmp_path)))
    assert (argv[argv.index("--model") + 1] if "--model" in argv else None) == model


@pytest.mark.parametrize("raw, expected", [
    (r'''['it\'s', "say \"q\" # x", "\\", "\s", a#b] # note''',
     ["it's", 'say "q" # x', "\\", r"\s", "a#b"]),
    ("'a,b' # note", ["a,b"]),
    ('\n  - "a,b" # note\n', ["a,b"]),
    ("[] # note", None),
])
@pytest.mark.variant("the reader's inline-list grammar read directly, from the learned.md comment-stripping incident; the accept test reaches these lines only through the writer")
def test_frontmatter_list_grammar(raw, expected):
    meta, _ = _frontmatter(f"---\nrules: {raw}\n---\n")
    assert _list(meta["rules"]) == expected


@pytest.mark.variant("path regression: repository add_dirs must expand home paths before resolving relative to the repository")
def test_repo_add_dirs_expand_a_leading_tilde(tmp_path, monkeypatch):
    """``~/cache`` means the home directory's cache, not ``<repo>/~/cache``."""

    home = tmp_path / "home"
    (home / "cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    (repo / ".brindle" / "config.json").write_text('{"add_dirs": ["~/cache"]}')

    assert load_profile("developer", str(repo)).add_dirs == [str(home / "cache")]


def test_an_unknown_user_in_add_dirs_is_reported_not_raised(tmp_path):
    """``~typo/cache`` makes expanduser raise. Kept as written instead, so
    loading the profile (and every launch with it) still works and launch
    reports the entry as missing."""
    from brindle.profiles import missing_add_dirs

    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    (repo / ".brindle" / "config.json").write_text('{"add_dirs": ["~no-such-user-brindle/cache"]}')

    p = load_profile("developer", str(repo))
    assert p.add_dirs == ["~no-such-user-brindle/cache"]
    assert missing_add_dirs(p) == ["~no-such-user-brindle/cache"]


def test_doctor_offers_to_rewrite_old_ids_in_config_and_writes_only_when_told(tmp_path):
    import json

    from brindle import doctor

    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    config = repo / ".brindle" / "config.json"
    config.write_text(json.dumps({"reviewer": "reviewer-codex", "routing": {"light": ["developer-codex"]}}))

    [check] = doctor._profile_id_checks(str(repo))
    assert check.level == doctor.WARN and "reviewer-codex -> review.gpt" in check.detail

    lines: list[str] = []
    doctor._rewrite_profile_ids(config, {"reviewer-codex": "review.gpt"}, lambda prompt: False, lines, str(repo))
    assert json.loads(config.read_text())["reviewer"] == "reviewer-codex" and not lines

    doctor._rewrite_profile_ids(config, {"reviewer-codex": "review.gpt"}, lambda prompt: True, lines, str(repo))
    rewritten = json.loads(config.read_text())
    assert rewritten["reviewer"] == "review.gpt" and rewritten["routing"] == {"light": ["dev.gpt"]}


@pytest.mark.parametrize("custom, routed", [
    pytest.param(True, "developer-codex", marks=pytest.mark.variant(
        "a custom profile under an old id keeps its own name in routing, so its rules and provider still apply")),
    pytest.param(False, "dev.gpt", marks=pytest.mark.variant(
        "with no custom profile under the old id, routing names the renamed built-in")),
])
def test_routing_keeps_a_custom_profile_under_an_old_id(tmp_path, custom, routed):
    from brindle.config import load_repo_config

    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    if custom:
        (repo / ".brindle" / "agents").mkdir()
        (repo / ".brindle" / "agents" / "developer-codex.md").write_text(
            "---\nname: developer-codex\nprovider: claude\n---\nDo it.\n")
    (repo / ".brindle" / "config.json").write_text('{"routing": {"medium": ["developer-codex"]}}')

    assert load_repo_config(repo).routing["medium"][0] == routed


@pytest.mark.variant("primary and second review must normalise old ids the same way")
def test_second_review_names_the_same_profile_as_the_primary(tmp_path):
    from brindle.config import load_repo_config

    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    (repo / ".brindle" / "config.json").write_text(
        '{"reviewer": "reviewer-codex", "second_review": {"heavy": "reviewer-codex"}}')

    cfg = load_repo_config(repo)
    assert cfg.reviewer == cfg.second_review["heavy"] == "review.gpt"
