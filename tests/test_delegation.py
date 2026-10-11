"""The repo's "delegation" setting: how readily a supervisor hands work to
workers (conservative, balanced by default, or fast)."""

import json
import time
import pytest

from typer.testing import CliRunner

from brindle import agents, workspaces
from brindle.cli import app
from brindle.config import set_user, user_config_path
from brindle.db import Agent


def _prompt(db, ws, aid="s1", profile="supervisor", parent=None, mode="interactive"):
    db.add_agent(Agent(aid, ws.id, profile, "claude", parent, mode, "idle", "@0", None, time.time()))
    return agents._profile_for(db, db.get_agent(aid), ws).prompt


def test_supervisor_defaults_to_balanced(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    prompt = _prompt(db, ws)
    assert "Delegation rule: balanced" in prompt


def test_repo_can_choose_fast_or_conservative(db, repo):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"delegation": "fast"}))
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule: fast" in _prompt(db, ws)
    (repo / ".brindle" / "config.local.json").write_text(json.dumps({"delegation": "conservative"}))
    assert "Delegation rule: conservative" in _prompt(db, ws, aid="s2")  # local wins


def test_unknown_level_falls_back_to_balanced(db, repo):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"delegation": "turbo"}))
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule: balanced" in _prompt(db, ws)


def test_workers_get_no_delegation_rule(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule" not in _prompt(db, ws, aid="w1", profile="developer", parent="s1", mode="assign")


def test_cli_saves_it_for_every_repo_and_tells_a_running_supervisor(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("s1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0", None, time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "s1")
    told = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: told.append((to, body)))
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["delegation", "fast"])
    assert result.exit_code == 0, result.output
    assert json.loads(user_config_path().read_text())["delegation"] == "fast"
    assert not (repo / ".brindle" / "config.local.json").exists()
    assert told and told[0][0] == "s1" and "Delegation rule: fast" in told[0][1]
    assert "delegation: fast" in CliRunner().invoke(app, ["delegation"]).output
    assert CliRunner().invoke(app, ["delegation", "turbo"]).exit_code != 0


def test_user_setting_applies_everywhere_but_a_repo_can_override(db, repo, monkeypatch):
    set_user("delegation", "fast")
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule: fast" in _prompt(db, ws)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["delegation", "conservative", "--repo"])
    assert result.exit_code == 0, result.output
    assert "Delegation rule: conservative" in _prompt(db, ws, aid="s2")
    assert json.loads(user_config_path().read_text())["delegation"] == "fast"  # untouched


def test_known_unavailable_model_rejected_before_workspace_or_agent_side_effects(db, repo, monkeypatch):
    from brindle import model_availability as ma
    from brindle.profiles import Profile
    ws = workspaces.adopt_root(db, str(repo))
    p = Profile("missing", "", "native", "", model="absent", base_url="https://api.openai.com/v1")
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    monkeypatch.setattr(agents, "_model_checker", ma.ModelAvailabilityChecker(
        discover=lambda *a: ma.Catalog(frozenset({"other"}), "openai", authoritative=True)))
    monkeypatch.setattr(workspaces, "create", lambda *a, **k: pytest.fail("created worktree"))
    monkeypatch.setattr(db, "add_agent", lambda *a: pytest.fail("created agent"))
    monkeypatch.setattr(agents, "_open_window", lambda *a, **k: pytest.fail("launched process"))
    with pytest.raises(agents.AgentError, match="absent from"):
        agents.delegate(db, None, ws, "missing", "real task", "assign")
    with pytest.raises(agents.AgentError, match="absent from"):
        agents.spawn(db, ws, "missing", prompt="real task")


def test_routing_skips_catalog_unavailable_with_same_preflight(db, repo, monkeypatch):
    from brindle import autopilot, model_availability as ma
    from brindle.config import RepoConfig
    from brindle.profiles import Profile
    p = Profile("missing", "", "native", "", model="absent", base_url="https://api.openai.com/v1")
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    monkeypatch.setattr(autopilot, "load_profile", lambda *a: p)
    monkeypatch.setattr(agents, "_model_checker", ma.ModelAvailabilityChecker(
        discover=lambda *a: ma.Catalog(frozenset(), "openai", authoritative=True)))
    assert "absent from" in autopilot._unavailable("missing", RepoConfig(), str(repo))


def test_resume_rechecks_model_before_launch_side_effects(db, repo, monkeypatch):
    from brindle import model_availability as ma, signin_pause
    from brindle.profiles import Profile
    ws = workspaces.adopt_root(db, str(repo))
    a = Agent("worker", ws.id, "missing", "native", None, "assign", "paused", "", None, time.time())
    db.add_agent(a)
    p = Profile("missing", "", "native", "", model="absent", base_url="https://api.openai.com/v1")
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    monkeypatch.setattr(agents, "_model_checker", ma.ModelAvailabilityChecker(
        discover=lambda *a: ma.Catalog(frozenset(), "openai", authoritative=True)))
    monkeypatch.setattr(signin_pause, "record_launch", lambda *a: pytest.fail("recorded launch"))
    monkeypatch.setattr(agents, "_open_window", lambda *a, **k: pytest.fail("opened pane"))
    with pytest.raises(agents.AgentError, match="absent from"):
        agents._launch(db, a, ws, prompt="real task", resume=None, watch_pane=False)


@pytest.mark.parametrize("status", ["available", "unverified"])
def test_catalog_available_and_unverified_do_not_reject_provisioning(db, repo, monkeypatch, status):
    from unittest.mock import Mock
    from brindle.model_availability import Availability
    ws = workspaces.adopt_root(db, str(repo))
    monkeypatch.setattr(agents, "_model_checker", Mock(check=Mock(return_value=Availability(status, "advisory"))))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(a))
    a = agents.spawn(db, ws, "developer", prompt="real task")
    assert launched == [a] and db.get_agent(a.id) is not None


@pytest.mark.parametrize("provider", ["native", "codex", "antigravity"])
@pytest.mark.parametrize("times_out", [False, True])
def test_slow_catalog_is_reused_across_routing_delegate_and_isolated_spawn(db, repo, monkeypatch, provider, times_out):
    from brindle import autopilot, model_availability as ma, tmux
    from brindle.config import RepoConfig
    from brindle.profiles import Profile
    ws = workspaces.adopt_root(db, str(repo))
    p = Profile("test", "", provider, "", model="wanted", base_url="https://api.openai.com/v1")
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    monkeypatch.setattr(autopilot, "load_profile", lambda *a: p)
    monkeypatch.setattr(tmux, "inherited_env", lambda *a: {"HOME": str(repo), "PATH": "/usr/bin"})
    from brindle import providers, quota
    monkeypatch.setattr(providers, "signed_out", lambda *a, **k: None)
    monkeypatch.setattr(providers, "unusable", lambda *a: None)
    monkeypatch.setattr(quota, "headroom", lambda *a, **k: 100)
    now, requests = [0.0], []

    def slow_discovery(request, timeout):
        requests.append(request)
        now[0] += timeout  # a whole catalog timeout, without sleeping
        if times_out:
            raise TimeoutError
        return ma.Catalog(frozenset({"wanted"}), "openai" if provider == "native" else provider)

    monkeypatch.setattr(agents, "_model_checker", ma.ModelAvailabilityChecker(discover=slow_discovery, clock=lambda: now[0]))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append((a, ws)))
    assert autopilot._unavailable("test", RepoConfig(), str(repo)) is None
    worker, worker_ws = agents.delegate(db, None, ws, "test", "real task", "assign")
    assert worker_ws.path != ws.path and launched == [(worker, worker_ws)]
    assert len(requests) == 1 and now[0] == ma.PROVISIONING_TIMEOUT and now[0] < 3.0
    assert requests[0].env["BRINDLE_AGENT_ID"] == "preflight"  # discovery still gets exact worker env
