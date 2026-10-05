"""brindle detects state left under its previous name (copse), never reads it,
and says what to do."""

import json

import pytest

from brindle import doctor, legacy


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home" / ".brindle"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_nothing_left_behind(home, tmp_path):
    assert legacy.findings(str(tmp_path)) == []
    assert legacy.warn_once(str(tmp_path)) == []


def test_old_home_is_reported_not_read(home):
    old = home / ".copse"
    old.mkdir()
    (old / "permissions.json").write_text(json.dumps({"deny": ["Bash(rm -rf:*)"]}))
    lines = legacy.findings(None)
    assert len(lines) == 1
    assert str(old) in lines[0] and "don't apply" in lines[0] and ".brindle" in lines[0]


def test_old_home_with_agy_settings_points_at_mirrored_rules(home):
    (home / ".copse").mkdir()
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    lines = legacy.findings(None)
    assert any(str(settings) in line and "allow" in line for line in lines)


def test_old_repo_dir_and_agent_entries(home, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".copse").mkdir(parents=True)
    agents = repo / ".agents"
    agents.mkdir()
    (agents / "hooks.json").write_text(json.dumps({"copse": {}, "brindle": {}}))
    (agents / "mcp_config.json").write_text(json.dumps({"mcpServers": {"copse": {}, "brindle": {}}}))
    lines = legacy.findings(str(repo))
    assert any(str(repo / ".copse") in line and ".brindle" in line for line in lines)
    assert any("hooks.json" in line and "'copse'" in line for line in lines)
    assert any("mcp_config.json" in line and "'mcpServers.copse'" in line for line in lines)


def test_unreadable_agent_files_are_ignored(home, tmp_path):
    agents = tmp_path / ".agents"
    agents.mkdir()
    (agents / "hooks.json").write_text("not json")
    (agents / "mcp_config.json").write_text("[]")
    assert legacy.findings(str(tmp_path)) == []


def test_warns_once_per_leftover_on_stderr(home, capsys):
    (home / ".copse").mkdir()
    assert len(legacy.warn_once(None)) == 1
    out = capsys.readouterr()
    assert out.out == "" and "renamed from copse" in out.err
    assert legacy.warn_once(None) == []
    assert capsys.readouterr().err == ""


def test_doctor_reports_every_time(home, tmp_path):
    (home / ".copse").mkdir()
    legacy.warn_once(None)
    checks = [c for c in doctor.checks(None) if c.name == "previous name"]
    assert len(checks) == 1 and checks[0].level == doctor.WARN
