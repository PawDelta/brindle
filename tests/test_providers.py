"""The colour theme brindle seeds into Claude Code's global config (the
first-run answers it gives on a fresh runner or once on a person's machine).
Fake data only; the config folder is the test's own (conftest)."""

import json

from brindle import providers


def _config(tmp_path):
    return tmp_path / "claude-config" / ".claude.json"


def _write(tmp_path, data):
    _config(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _config(tmp_path).write_text(json.dumps(data))
    _config(tmp_path).chmod(0o600)


def test_theme_seeded_on_a_fresh_config_is_the_ansi_theme(tmp_path):
    providers.seed_ci_config([])
    assert json.loads(_config(tmp_path).read_text())["theme"] == "dark-ansi"


def test_theme_recorded_at_onboarding_on_a_fresh_config_is_the_ansi_theme(tmp_path):
    providers.record_claude_onboarding()
    assert json.loads(_config(tmp_path).read_text())["theme"] == "dark-ansi"


def test_theme_a_person_chose_is_kept_by_the_seed_and_the_onboarding(tmp_path):
    _write(tmp_path, {"theme": "light"})
    providers.seed_ci_config([])
    providers.record_claude_onboarding()
    assert json.loads(_config(tmp_path).read_text())["theme"] == "light"


def _brindle_folder(tmp_path, monkeypatch):
    """Claude Code runs from brindle's own signed-in config folder (provider_homes),
    the person's own home beside it."""
    from brindle import provider_homes

    person, ours = tmp_path / "person", tmp_path / "ours"
    person.mkdir()
    ours.mkdir()
    monkeypatch.setenv("HOME", str(person))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("BRINDLE_CLAUDE_CONFIG_HOME", str(ours))
    (ours / provider_homes.SIGNED_IN).write_text("1\n")
    return ours / ".claude.json"


def test_theme_dark_in_brindles_own_config_folder_migrates_to_the_ansi_theme(tmp_path, monkeypatch):
    config = _brindle_folder(tmp_path, monkeypatch)
    config.write_text(json.dumps({"theme": "dark"}))
    providers.seed_ci_config([])
    assert json.loads(config.read_text())["theme"] == "dark-ansi"


def test_theme_light_in_brindles_own_config_folder_is_kept(tmp_path, monkeypatch):
    config = _brindle_folder(tmp_path, monkeypatch)
    config.write_text(json.dumps({"theme": "light"}))
    providers.seed_ci_config([])
    assert json.loads(config.read_text())["theme"] == "light"


def test_theme_dark_in_the_persons_own_config_is_kept(tmp_path, monkeypatch):
    person = tmp_path / "person"
    person.mkdir()
    monkeypatch.setenv("HOME", str(person))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (person / ".claude.json").write_text(json.dumps({"theme": "dark"}))
    providers.record_claude_onboarding()
    assert json.loads((person / ".claude.json").read_text())["theme"] == "dark"
