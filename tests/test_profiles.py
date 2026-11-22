import pytest

from brindle import model_names
from brindle.profiles import _frontmatter, _list, load_profile


def test_an_old_built_in_id_loads_its_renamed_profile():
    for old, new in model_names.ALIASES.items():
        p = load_profile(old)
        assert p.name == new
        assert p.provider == load_profile(new).provider


def test_a_custom_agent_that_extends_an_old_id_still_works(tmp_path):
    agents = tmp_path / ".brindle" / "agents"
    agents.mkdir(parents=True)
    (agents / "mine.md").write_text(
        "---\nname: mine\ndescription: a custom Codex agent\nextends: developer-codex\n---\nDo the task.\n")

    p = load_profile("mine", str(tmp_path))
    assert p.name == "mine" and p.provider == "codex"


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


def test_second_review_names_the_same_profile_as_the_primary(tmp_path):
    from brindle.config import load_repo_config

    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    (repo / ".brindle" / "config.json").write_text(
        '{"reviewer": "reviewer-codex", "second_review": {"heavy": "reviewer-codex"}}')

    cfg = load_repo_config(repo)
    assert cfg.reviewer == cfg.second_review["heavy"] == "review.gpt"
