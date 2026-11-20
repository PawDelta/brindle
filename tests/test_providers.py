"""The colour theme brindle seeds into Claude Code's settings (the
first-run answers it gives on a fresh runner or once on a person's machine),
and the move of a "dark" theme brindle seeded earlier on each launch. Fake
data only; the config folder is the test's own (conftest)."""

import json

import pytest

from brindle import provider_homes, providers


@pytest.mark.variant("seeding and onboarding must keep a theme the person chose (setdefault, never assignment)")
def test_theme_a_person_chose_is_kept_by_the_seed_and_the_onboarding(tmp_path, monkeypatch):
    config, work = _brindle_folder(tmp_path, monkeypatch)
    settings = config.with_name("settings.json")
    settings.write_text(json.dumps({"theme": "dark-daltonized", "env": {"EXAMPLE": "kept"}}))
    providers.seed_ci_config([])
    providers.record_claude_onboarding()
    assert providers.trust_folder(work)
    assert json.loads(settings.read_text()) == {"theme": "dark-daltonized", "env": {"EXAMPLE": "kept"}}
    assert "theme" not in json.loads(config.read_text())


def _brindle_folder(tmp_path, monkeypatch):
    """Claude Code runs from brindle's own signed-in config folder (provider_homes),
    the person's own home beside it. Returns the folder's .claude.json and a repo
    folder for a launch to trust."""
    person, ours, work = tmp_path / "person", tmp_path / "ours", tmp_path / "work"
    for folder in (person, ours, work):
        folder.mkdir()
    monkeypatch.setenv("HOME", str(person))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(ours))
    (ours / provider_homes.SIGNED_IN).write_text("1\n")
    return ours / ".claude.json", str(work)


@pytest.mark.parametrize("entry", ["trust", "seed", "onboarding"])
@pytest.mark.parametrize("theme", ["dark-ansi", None])
@pytest.mark.variant("each launch/first-run entry must migrate settings, including an absent theme")
def test_theme_ansi_brindle_seeded_moves_to_the_builtin_theme_on_an_existing_owned_launch(tmp_path, monkeypatch, entry, theme):
    # An already-onboarded brindle folder (no onboarding again) whose theme brindle seeded as "dark".
    config, work = _brindle_folder(tmp_path, monkeypatch)
    config.write_text(json.dumps({"hasCompletedOnboarding": True, "projects": {work: {"hasTrustDialogAccepted": True}}}))
    settings = config.with_name("settings.json")
    data = {"env": {"EXAMPLE": "kept"}}
    if theme is not None:
        data["theme"] = theme
    settings.write_text(json.dumps(data))
    if entry == "trust":
        assert providers.trust_folder(work)
    elif entry == "seed":
        providers.seed_ci_config([])
    else:
        providers.record_claude_onboarding()
    assert json.loads(settings.read_text()) == {"theme": "dark", "env": {"EXAMPLE": "kept"}}
    assert "theme" not in json.loads(config.read_text())


@pytest.mark.parametrize("app,before,after", [
    pytest.param("light", "dark-ansi", "light", marks=pytest.mark.variant("an ANSI theme brindle seeded moves to the built-in light")),
    pytest.param("dark", "light-ansi", "dark", marks=pytest.mark.variant("flipping back to dark replaces what light wrote")),
    pytest.param("light", "dark", "light", marks=pytest.mark.variant("the first seed value is also brindle's")),
    pytest.param("light", "solarized", "solarized", marks=pytest.mark.variant("a person's own pick is kept")),
])
def test_app_theme_sets_brindles_claude_theme_and_is_remembered(tmp_path, monkeypatch, app, before, after):
    config, work = _brindle_folder(tmp_path, monkeypatch)
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home"))
    settings = config.with_name("settings.json")
    settings.write_text(json.dumps({"theme": before}))
    providers.set_app_theme(app)
    assert json.loads(settings.read_text())["theme"] == after
    assert providers.app_theme() == app
    # A later launch seeds from the remembered theme, not a fixed one.
    settings.write_text(json.dumps({}))
    providers.seed_ci_config([])
    assert json.loads(settings.read_text())["theme"] == providers.CLAUDE_THEMES[app]


@pytest.mark.variant("the migration must never rewrite the person's own ~/.claude.json, only brindle's folder")
def test_theme_dark_in_the_persons_own_config_is_kept_on_a_launch(tmp_path, monkeypatch):
    person = tmp_path / "person"
    work = tmp_path / "work"
    person.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(person))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (person / ".claude").mkdir()
    settings = person / ".claude" / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    (person / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}))
    providers.trust_folder(str(work))
    assert json.loads((person / ".claude.json").read_text())["theme"] == "dark"
    assert json.loads(settings.read_text()) == {"theme": "dark"}
