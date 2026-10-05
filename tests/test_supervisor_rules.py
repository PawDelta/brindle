"""The supervisor's rules for acting on findings (#44, #45), and the person's
standing ``rules`` from copse config, in every supervisor's brief."""

import json
import time

from copse import agents, autopilot, workspaces
from copse.config import load_repo_config, user_config_path
from copse.db import Agent


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_rules_add_up_from_user_repo_and_local(repo):
    write(user_config_path(), {"rules": ["Mine", "Shared"]})
    write(repo / ".copse" / "config.json", {"rules": ["Shared", "  Repo  ", 3, ""]})
    write(repo / ".copse" / "config.local.json", {"rules": ["Local"]})
    assert load_repo_config(repo).rules == ["Mine", "Shared", "Repo", "Local"]


def test_no_rules_is_an_empty_list(repo):
    write(repo / ".copse" / "config.json", {"rules": "not a list"})
    assert load_repo_config(repo).rules == []


def test_builtin_rules_say_validate_and_fix_without_asking(repo):
    text = autopilot.supervisor_rules(load_repo_config(repo))
    assert "validate" in text and "Don't ask first" in text
    assert "Standing rules" not in text


def brief(db, ws, profile="supervisor", mode="interactive"):
    a = Agent("s1", ws.id, profile, "claude", None, mode, "processing", "%s1", None, time.time())
    db.add_agent(a)
    return agents._profile_for(db, a, ws).prompt


def test_supervisor_brief_carries_the_rules(db, repo):
    write(repo / ".copse" / "config.json", {"rules": ["Never touch the vendored code"]})
    ws = workspaces.create(db, str(repo), "feature").workspace
    prompt = brief(db, ws)
    assert "verify each finding" in prompt
    assert "- Never touch the vendored code" in prompt
    assert prompt.index("Acting on what you find") < prompt.index("Never touch the vendored code")


def test_workers_dont_get_the_supervisor_rules(db, repo):
    write(repo / ".copse" / "config.json", {"rules": ["Never touch the vendored code"]})
    ws = workspaces.create(db, str(repo), "feature").workspace
    prompt = brief(db, ws, profile="developer", mode="assign")
    assert "Never touch the vendored code" not in prompt
    assert "Acting on what you find" not in prompt
