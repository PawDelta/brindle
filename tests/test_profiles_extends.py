"""Profiles that build on another (``extends``) and name rule packs (``rules``)."""
import time
from pathlib import Path

import pytest

from brindle import agents, workspaces
from brindle.db import Agent
from brindle.profiles import (ProfileError, _parse, list_profiles, list_rule_packs, load_profile,
                              load_rule_pack, load_rule_packs, rules_prompt)


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


def test_child_overrides_fields_and_appends_its_prompt(proj):
    write_profile(proj, "base", "---\nname: base\ndescription: base\nprovider: claude\n"
                               "model: sonnet\neffort: low\nenv.A: 1\nenv.B: 2\n---\nParent text.\n")
    write_profile(proj, "kid", "---\nname: kid\ndescription: kid\nextends: base\n"
                              "model: opus\nenv.B: 3\n---\nChild text.\n")
    p = load_profile("kid", str(proj))
    assert p.name == "kid" and p.description == "kid"
    assert p.extends == "base"
    assert p.model == "opus"          # the child's field wins
    assert p.effort == "low"          # a field the child doesn't set comes from the parent
    assert p.provider == "claude"
    assert p.env == {"A": "1", "B": "3"}
    assert p.prompt == "Parent text.\n\nChild text."


def test_extends_resolves_through_the_lookup_order(proj, monkeypatch, tmp_path):
    """A parent is found like any profile: the repo first, then ~/.brindle, then built-ins."""
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("BRINDLE_HOME", str(home))
    (home / "agents" / "mine.md").write_text("---\nname: mine\nprovider: codex\n---\nMine.\n")
    write_profile(proj, "a", "---\nname: a\nextends: mine\n---\nA.\n")
    write_profile(proj, "b", "---\nname: b\nextends: developer\nmodel: haiku\n---\nB.\n")

    a = load_profile("a", str(proj))
    assert a.provider == "codex" and a.prompt == "Mine.\n\nA."
    b = load_profile("b", str(proj))
    assert b.model == "haiku" and b.permission_mode == "auto"   # developer's
    assert b.allowed_tools == load_profile("developer").allowed_tools
    assert b.prompt.startswith(load_profile("developer").prompt) and b.prompt.endswith("B.")


def test_grandparents_chain(proj):
    write_profile(proj, "g", "---\nname: g\nprovider: claude\nmodel: a\neffort: low\n---\nG.\n")
    write_profile(proj, "p", "---\nname: p\nextends: g\nmodel: b\n---\nP.\n")
    write_profile(proj, "c", "---\nname: c\nextends: p\n---\nC.\n")
    c = load_profile("c", str(proj))
    assert (c.model, c.effort, c.extends) == ("b", "low", "p")
    assert c.prompt == "G.\n\nP.\n\nC."


def test_a_cycle_is_an_error_not_a_hang(proj):
    write_profile(proj, "x", "---\nname: x\nextends: y\n---\nX.\n")
    write_profile(proj, "y", "---\nname: y\nextends: x\n---\nY.\n")
    write_profile(proj, "me", "---\nname: me\nextends: me\n---\n")
    with pytest.raises(ProfileError, match="x -> y -> x"):
        load_profile("x", str(proj))
    with pytest.raises(ProfileError, match="extends itself"):
        load_profile("me", str(proj))
    # One broken file doesn't hide the rest of the list.
    names = {p.name for p in list_profiles(str(proj))}
    assert {"x", "y", "me", "developer"} <= names


def test_a_missing_parent_names_both_profiles(proj):
    write_profile(proj, "orphan", "---\nname: orphan\nextends: nobody\n---\n")
    with pytest.raises(KeyError, match="'orphan' extends 'nobody'"):
        load_profile("orphan", str(proj))


def test_rules_accumulate_from_parent_to_child(proj):
    write_profile(proj, "base", "---\nname: base\nrules: style/minimal-diff\n---\nB.\n")
    write_profile(proj, "kid", "---\nname: kid\nextends: base\n"
                              "rules: [security/backend, style/minimal-diff]\n---\nK.\n")
    assert load_profile("base", str(proj)).rules == ["style/minimal-diff"]
    assert load_profile("kid", str(proj)).rules == ["style/minimal-diff", "security/backend"]


def test_block_lists_in_frontmatter():
    p = _parse(
        "---\nname: x\nrules:\n  - security/backend\n  - tests/only  # with a comment\n"
        "model: haiku\nallowed_tools:\n  - Bash(git add:*)\n  - Bash(uv run:*)\n---\nbody\n",
        "x",
    )
    assert p.rules == ["security/backend", "tests/only"]
    assert p.model == "haiku"
    assert p.allowed_tools == ["Bash(git add:*)", "Bash(uv run:*)"]
    assert p.prompt == "body"


def test_builtin_packs_load_and_examples_resolve():
    for name in ("security/backend", "tests/only", "style/minimal-diff"):
        pack = load_rule_pack(name)
        assert pack.name == name and pack.prompt and pack.description
        assert pack.source == f"built-in rule pack {name}"
    assert {"security/backend", "tests/only", "style/minimal-diff"} <= set(list_rule_packs())
    assert load_rule_pack("security/backend").deny_patterns
    assert load_rule_pack("tests/only").require_tests_for == ["*"]
    assert not load_rule_pack("style/minimal-diff").mechanical

    auditor = load_profile("backend-auditor")
    assert auditor.extends == "reviewer" and auditor.rules == ["security/backend"]
    assert auditor.strict_mcp and auditor.permission_mode == "dontAsk"   # reviewer's
    assert auditor.prompt.startswith(load_profile("reviewer").prompt)
    qa = load_profile("qa")
    assert qa.extends == "developer" and qa.rules == ["tests/only"]
    assert qa.permission_mode == "auto"
    listed = {p.name: p for p in list_profiles()}
    assert listed["qa"].extends == "developer" and listed["qa"].permission_mode == "auto"


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


def test_a_missing_pack_fails_loudly(proj):
    write_profile(proj, "p", "---\nname: p\nrules: nope/none\n---\n")
    with pytest.raises(KeyError, match="no rule pack named 'nope/none'"):
        load_rule_packs(load_profile("p", str(proj)), str(proj))
    with pytest.raises(ProfileError):
        load_rule_pack("../etc/passwd", str(proj))


def test_rules_prompt_spells_out_the_mechanical_rules():
    text = rules_prompt([load_rule_pack("security/backend"), load_rule_pack("style/minimal-diff")])
    assert "## Rules: security/backend" in text and "## Rules: style/minimal-diff" in text
    assert "Checked mechanically:" in text and "shell=True" in text
    assert "smallest change" in text
    assert rules_prompt([]) == ""


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


def test_subagent_prompt_carries_the_packs(db, repo, monkeypatch):
    write_profile(repo, "sub-qa", "---\nname: sub-qa\nextends: subagent\nrules: tests/only\n---\n")
    ws = workspaces.create(db, str(repo), "feat").workspace
    a = agents.spawn(db, ws, "sub-qa", prompt="add tests", mode="handoff", parent_id=None)
    assert "## Rules: tests/only" in a.task and "Task:\nadd tests" in a.task


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
