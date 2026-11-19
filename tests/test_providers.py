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
    settings.write_text(json.dumps({"theme": "light", "env": {"EXAMPLE": "kept"}}))
    providers.seed_ci_config([])
    providers.record_claude_onboarding()
    assert providers.trust_folder(work)
    assert json.loads(settings.read_text()) == {"theme": "light", "env": {"EXAMPLE": "kept"}}
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
@pytest.mark.parametrize("theme", ["dark", None])
@pytest.mark.variant("each launch/first-run entry must migrate settings, including an absent theme")
def test_theme_dark_brindle_seeded_moves_to_the_ansi_theme_on_an_existing_owned_launch(tmp_path, monkeypatch, entry, theme):
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
    assert json.loads(settings.read_text()) == {"theme": "dark-ansi", "env": {"EXAMPLE": "kept"}}
    assert "theme" not in json.loads(config.read_text())


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
