"""The colour theme brindle seeds into Claude Code's global config (the
first-run answers it gives on a fresh runner or once on a person's machine),
and the move of a "dark" theme brindle seeded earlier on each launch. Fake
data only; the config folder is the test's own (conftest)."""

import json

import pytest

from brindle import provider_homes, providers


def _config(tmp_path):
    return tmp_path / "claude-config" / ".claude.json"


def _write(tmp_path, data):
    _config(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _config(tmp_path).write_text(json.dumps(data))
    _config(tmp_path).chmod(0o600)


@pytest.mark.variant("seeding and onboarding must keep a theme the person chose (setdefault, never assignment)")
def test_theme_a_person_chose_is_kept_by_the_seed_and_the_onboarding(tmp_path):
    _write(tmp_path, {"theme": "light"})
    providers.seed_ci_config([])
    providers.record_claude_onboarding()
    assert json.loads(_config(tmp_path).read_text())["theme"] == "light"


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


def test_theme_dark_brindle_seeded_moves_to_the_ansi_theme_on_an_existing_owned_launch(tmp_path, monkeypatch):
    # An already-onboarded brindle folder (no onboarding again) whose theme brindle seeded as "dark".
    config, work = _brindle_folder(tmp_path, monkeypatch)
    config.write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}))
    assert providers.trust_folder(work)
    data = json.loads(config.read_text())
    assert data["theme"] == "dark-ansi"
    assert data["hasCompletedOnboarding"] is True
    assert "customApiKeyResponses" not in data   # the launch changes nothing else


@pytest.mark.variant("the migration must never rewrite the person's own ~/.claude.json, only brindle's folder")
def test_theme_dark_in_the_persons_own_config_is_kept_on_a_launch(tmp_path, monkeypatch):
    person = tmp_path / "person"
    work = tmp_path / "work"
    person.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(person))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (person / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}))
    providers.trust_folder(str(work))
    assert json.loads((person / ".claude.json").read_text())["theme"] == "dark"
