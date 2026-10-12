"""Profiles that build on another (``extends``) and name rule packs (``rules``)."""
import time
from pathlib import Path

import pytest

from brindle import agents, workspaces
from brindle.db import Agent
from brindle.profiles import (list_rule_packs, load_profile,
                              load_rule_pack)


def write_profile(repo: Path, name: str, text: str, home: bool = False) -> Path:
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    f.write_text(text)
    return f


@pytest.fixture
def proj(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".brindle").mkdir(parents=True)
    return repo


def test_a_missing_parent_names_both_profiles(proj):
    write_profile(proj, "orphan", "---\nname: orphan\nextends: nobody\n---\n")
    with pytest.raises(KeyError, match="'orphan' extends 'nobody'"):
        load_profile("orphan", str(proj))


def test_packs_come_from_repo_then_home_then_builtin(proj, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "rules" / "security").mkdir(parents=True)
    monkeypatch.setenv("BRINDLE_HOME", str(home))
    (home / "rules" / "security" / "backend.md").write_text("---\ndescription: home\n---\nHome pack.\n")
    (home / "rules" / "team.md").write_text("---\ndescription: team\ndeny_deps: requests\n---\nTeam.\n")
    assert load_rule_pack("security/backend", str(proj)).description == "home"
    assert load_rule_pack("team", str(proj)).deny_deps == ["requests"]
    assert load_rule_pack("tests/only", str(proj)).source == "built-in rule pack tests/only"

    (proj / ".brindle" / "rules" / "security").mkdir(parents=True)
    (proj / ".brindle" / "rules" / "security" / "backend.md").write_text(
        "---\ndescription: repo\ndeny_patterns:\n  - foo, bar\n  - TODO\n---\nRepo pack.\n")
    pack = load_rule_pack("security/backend", str(proj))
    assert pack.description == "repo" and pack.prompt == "Repo pack."
    assert pack.deny_patterns == ["foo, bar", "TODO"]   # block items keep their commas
    assert "team" in list_rule_packs(str(proj))


def test_profile_for_injects_the_packs_text(db, repo):
    write_profile(repo, "sec", "---\nname: sec\nextends: developer\nrules: security/backend\n---\nSec.\n")
    ws = workspaces.create(db, str(repo), "feat").workspace
    agent = Agent("w1", ws.id, "sec", "claude", None, "assign", "idle", "@0", None, time.time())
    db.add_agent(agent)
    p = agents._profile_for(db, agent, ws)
    assert p.permission_mode == "auto"
    assert "## Rules: security/backend" in p.prompt and "Sec." in p.prompt
    assert p.prompt.index("Sec.") < p.prompt.index("## Rules: security/backend")
    plain = Agent("w2", ws.id, "developer", "claude", None, "assign", "idle", "@0", None, time.time())
    db.add_agent(plain)
    assert "## Rules" not in agents._profile_for(db, plain, ws).prompt


def test_profile_cli(db, repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(repo)
    runner = CliRunner()
    res = runner.invoke(app, ["profile", "new", "auditor", "--extends", "backend-auditor",
                              "--rules", "style/minimal-diff", "-d", "Audits"])
    assert res.exit_code == 0, res.output
    written = (repo / ".brindle" / "agents" / "auditor.md").read_text()
    assert "extends: backend-auditor" in written and "rules: style/minimal-diff" in written
    p = load_profile("auditor", str(repo))
    assert p.rules == ["security/backend", "style/minimal-diff"] and p.description == "Audits"
    assert runner.invoke(app, ["profile", "new", "auditor"]).exit_code == 1   # no overwrite
    assert runner.invoke(app, ["profile", "new", "bad", "--extends", "nobody"]).exit_code == 1
    assert runner.invoke(app, ["profile", "new", "bad", "--rules", "no/pack"]).exit_code == 1
    assert not (repo / ".brindle" / "agents" / "bad.md").exists()

    res = runner.invoke(app, ["profile", "show", "auditor"])
    assert res.exit_code == 0, res.output
    assert "extends   backend-auditor -> reviewer" in res.output
    assert "security/backend" in res.output and "style/minimal-diff" in res.output
    assert "strict_mcp True" in res.output
    assert "## Rules: security/backend" in res.output
    assert runner.invoke(app, ["profile", "show", "--prompt", "auditor"]).output.startswith(
        load_profile("reviewer").prompt[:40])
    assert runner.invoke(app, ["profile", "show", "nobody"]).exit_code == 1

    assert runner.invoke(app, ["profile", "lint"]).exit_code == 0
    write_profile(repo, "loop", "---\nname: loop\nextends: loop\n---\n")
    write_profile(repo, "badpack", "---\nname: badpack\nrules: no/pack\n---\nx\n")
    (repo / ".brindle" / "rules").mkdir()
    (repo / ".brindle" / "rules" / "broken.md").write_text("---\ndeny_patterns: [\n---\nx\n")
    write_profile(repo, "badre", "---\nname: badre\nrules: broken\n---\nx\n")
    res = runner.invoke(app, ["profile", "lint"])
    assert res.exit_code == 1
    assert "✗ loop" in res.output and "extends itself" in res.output
    assert "✗ badpack" in res.output and "no rule pack named 'no/pack'" in res.output
    assert "✗ badre" in res.output and "doesn't compile" in res.output
    assert "✓ auditor" in res.output and "✓ developer" in res.output
    assert runner.invoke(app, ["profile", "lint", "auditor"]).exit_code == 0
