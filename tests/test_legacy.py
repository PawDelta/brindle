"""brindle detects state left under its previous name (copse), never reads it,
and says what to do."""

import json

import pytest

from brindle import legacy


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home" / ".brindle"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_old_home_with_agy_settings_points_at_mirrored_rules(home):
    (home / ".copse").mkdir()
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"permissions": {"allow": ["command(git status)"]}}))
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


