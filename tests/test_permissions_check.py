import json
import time

from typer.testing import CliRunner

from frith import permissions
from frith.cli import app
from frith.config import set_local
from frith.db import Agent
from frith.profiles import _parse, profile_rules_for
from frith import agents, antigravity, workspaces


def test_profile_permission_denies_parse_and_only_deny():
    profile = _parse(
        '---\nname: reviewer\npermission_denies: ["read glob ~/.private*", "bash prefix deploy"]\n---\n',
        "reviewer",
    )
    rules = permissions.profile_rules(profile)
    assert [(r.kind, r.match_type, r.match, r.decision) for r in rules] == [
        ("read", "glob", "~/.private*", "deny"),
        ("bash", "prefix", "deploy", "deny"),
    ]


def test_profile_deny_participates_in_policy():
    profile = _parse('---\nname: reviewer\npermission_denies: ["read prefix /home/person/.private"]\n---\n', "reviewer")
    req = permissions.Request("claude", "read", "Read", path="/home/person/.private/token")
    result = permissions.decide(req, rules=[*permissions.DEFAULT_RULES, *permissions.profile_rules(profile)])
    assert result.decision == "deny"
    assert result.rule.source == "profile"


def test_invalid_profile_denies_are_reported_and_ignored():
    profile = _parse(
        '---\nname: reviewer\npermission_denies: ["bogus glob x", "read nope x", "read"]\n---\n',
        "reviewer",
    )
    assert permissions.profile_rules(profile) == []
    assert permissions.invalid_profile_rules(profile) == ["bogus glob x", "read nope x", "read"]
    malformed_json = _parse('---\nname: reviewer\npermission_denies: [invalid\n---\n', "reviewer")
    assert permissions.invalid_profile_rules(malformed_json) == ["[invalid"]


def test_missing_profile_has_no_override(tmp_path):
    assert profile_rules_for("deleted-profile", str(tmp_path)) == []


def test_permissions_check_reports_provider_capability(monkeypatch):
    from frith.profiles import Profile

    profile = Profile("reviewer", "", "claude", "", permission_denies=["fetch glob https://private/*"])
    monkeypatch.setattr("frith.cli._here_repo", lambda: None)
    monkeypatch.setattr("frith.cli.load_profile", lambda name, repo: profile)
    result = CliRunner().invoke(app, ["permissions", "check", "--profile", "reviewer"])
    assert result.exit_code == 0, result.output
    assert "Claude Code (frith hook):" in result.output
    assert "Codex (frith hook):" in result.output
    assert "Antigravity:" in result.output
    assert "deny  fetch" in result.output
    assert "deny  fetch fetch glob 'https://private/*' (profile; frith hook only)" in result.output
    assert "hook allow is ignored" in result.output


def test_permissions_check_unknown_profile_and_default_all_profiles(monkeypatch):
    monkeypatch.setattr("frith.cli._here_repo", lambda: None)
    unknown = CliRunner().invoke(app, ["permissions", "check", "--profile", "no-such-profile"])
    assert unknown.exit_code == 2
    all_profiles = CliRunner().invoke(app, ["permissions", "check"])
    assert all_profiles.exit_code == 0, all_profiles.output
    assert "Profile developer:" in all_profiles.output


def test_permission_hooks_enforce_profile_deny(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    set_local(ws.repo_root, "permission_policy", "on")
    (repo / ".frith" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / ".frith" / "agents" / "reviewer.md").write_text(
        '---\nname: reviewer\npermission_denies: ["read prefix /secret"]\n---\nreview\n'
    )
    agent = Agent("reviewer-worker", ws.id, "reviewer", "claude", "boss", "assign", "processing",
                  "%reviewer", None, time.time())
    db.add_agent(agent)
    claude = agents.permission_request_decision(
        db, agent,
        {"tool_name": "Read", "tool_input": {"file_path": "/secret/key"}, "tool_use_id": "t"},
    )
    assert claude["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    agy = antigravity.pre_tool_decision(
        db, agent.id,
        {"toolCall": {"name": "read_file", "args": {"FilePath": "/secret/key"}}},
    )
    assert agy["decision"] == "deny"


def test_dropped_overrides_show_in_check(monkeypatch):
    from frith.profiles import Profile

    profile = Profile("reviewer", "", "claude", "", permission_denies=["unknown matcher x"])
    monkeypatch.setattr("frith.cli._here_repo", lambda: None)
    monkeypatch.setattr("frith.cli.load_profile", lambda name, repo: profile)
    result = CliRunner().invoke(app, ["permissions", "check", "--profile", "reviewer"])
    assert "dropped invalid permission_denies entry" in result.output


def test_permissions_check_shows_agy_settings_as_they_are(monkeypatch, tmp_path):
    """Antigravity's own settings.json, read as agy reads it: frith's mirror
    entries and the person's own, told apart; a profile's denies are never
    claimed to be in it (that file is shared by every agy agent)."""
    from frith import antigravity, permissions
    from frith.profiles import Profile

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"permissions": {
        "allow": ["command(git status)", "command(make lint)"],
        "deny": ["command(git push)"]}}))
    monkeypatch.setattr(antigravity, "settings_path", lambda: settings)
    with permissions.editing() as store:
        store.agy_managed = {"allow": ["command(git status)"], "deny": ["command(git push)"]}
        store.agy_repos = {"/repo": []}
    profile = Profile("reviewer", "", "claude", "", permission_denies=["bash prefix rm"])
    monkeypatch.setattr("frith.cli._here_repo", lambda: None)
    monkeypatch.setattr("frith.cli.load_profile", lambda name, repo: profile)

    out = CliRunner().invoke(app, ["permissions", "check", "--profile", "reviewer"]).output

    assert f"Antigravity settings ({settings})" in out
    assert "frith mirror: on for /repo" in out
    assert "allow command(git status) (frith mirror)" in out
    assert "allow command(make lint) (yours, not managed by frith)" in out
    assert "deny  command(git push) (frith mirror)" in out
    assert "(profile; frith hook only)" in out
    assert "settings + hook" in out


def test_permissions_check_with_unreadable_agy_settings(monkeypatch, tmp_path):
    from frith import antigravity

    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    monkeypatch.setattr(antigravity, "settings_path", lambda: settings)
    monkeypatch.setattr("frith.cli._here_repo", lambda: None)
    out = CliRunner().invoke(app, ["permissions", "check"]).output
    assert "couldn't read it" in out
    assert "frith mirror: off" in out
