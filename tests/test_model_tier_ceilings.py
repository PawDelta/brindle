import pytest

from brindle import autopilot, pipeline, mcp_server
from brindle.config import RepoConfig
from brindle.db import Workspace, Task
from brindle import agents


def test_max_weight_ceiling_caps_heavy_to_medium(db, repo, monkeypatch):
    cfg = RepoConfig(max_weight="medium")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(db, "caller-1", str(repo), weight="heavy", why=why)
    assert any("capped to medium" in w for w in why)
    # The resolved profile must be in medium routing, not developer-heavy
    assert profile in cfg.routing["medium"]


def test_retry_on_heavy_refuses_when_max_weight_medium(db, repo):
    ws = Workspace("ws-m", str(repo), "feat/m", "main", "main", "worktree", str(repo), 20000, "tmux-m", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")
    # Record task with heavy weight
    t = Task("t-1", str(repo), worker.id, None, ws.id, "developer", "task", "assign", 1, "feat/m", None, None, None, "started", 0.0, weight="heavy")
    db.add_task(t)

    cfg = RepoConfig(max_weight="medium")
    # Must refuse retry on heavy
    assert not pipeline._retry_on_heavy(db, worker, ws, cfg, "failed review", "worker report")


def test_docs_only_files_demotes_tier(db, repo, monkeypatch):
    cfg = RepoConfig()
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(
        db, "caller-2", str(repo), weight="heavy", files=["docs/guide.md", "docs/index.md"], why=why
    )
    assert any("demoted" in w for w in why)
    assert profile in cfg.routing["medium"]


def test_routing_explanation_includes_model_and_cost_signal(db, repo, monkeypatch):
    cfg = RepoConfig()
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(db, "caller-3", str(repo), weight="heavy", why=why)
    joined = " ".join(why)
    # Must show tier, profile, and cost indication
    assert "weight heavy ->" in joined
    assert "most expensive tier" in joined or "effort" in joined


def test_list_agents_includes_token_usage_when_available(db, repo, monkeypatch):
    ws = Workspace("ws-list", str(repo), "feat/list", "main", "main", "main", str(repo), 20000, "tmux-l", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")

    from unittest.mock import MagicMock
    fake_usage = MagicMock()
    fake_usage.total = 5000
    from brindle import usage
    monkeypatch.setattr(usage, "agent_usage", lambda db_arg, a_arg: fake_usage)
    monkeypatch.setattr(usage, "short_summary", lambda u: "5k tokens")
    monkeypatch.setattr(mcp_server, "DB", lambda: db)
    monkeypatch.setattr(mcp_server, "_caller", lambda db_arg: (None, ws))

    out = mcp_server.list_agents(repo=None)
    assert f"{worker.id}" in out
    assert "tokens=5k tokens" in out


def test_explicit_heavy_profile_refused_under_light_ceiling(db, repo, monkeypatch):
    cfg = RepoConfig(max_weight="light")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    with pytest.raises(autopilot.AutopilotError) as exc_info:
        autopilot.choose_profile(db, "caller-exp", str(repo), requested="developer-heavy")
    assert "exceeds configured max_weight 'light'" in str(exc_info.value)


def test_pending_milestone_heavy_profile_capped_under_light_ceiling(db, repo, monkeypatch):
    ws = Workspace("ws-mile", str(repo), "feat/mile", "main", "main", "main", str(repo), 20000, "tmux-m", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive")
    db.add_autopilot(sup.id)
    autopilot.set_goal(db, sup.id, "Goal", [("Step 1", "true", "", "developer-heavy")])

    cfg = RepoConfig(max_weight="light")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(db, sup.id, str(repo), why=why)
    assert any("capped to" in w and "by max_weight" in w for w in why)
    assert profile in cfg.routing["light"]


def test_fallback_heavy_capped_under_light_ceiling(db, repo, monkeypatch):
    cfg = RepoConfig(max_weight="light", default_agent="developer-heavy")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(db, "caller-fallback", str(repo), why=why)
    assert any("capped to" in w for w in why)
    assert profile in cfg.routing["light"]


def test_max_weight_medium_filters_heavy_candidate_from_medium_routing(db, repo, monkeypatch):
    from brindle import learning
    cfg = RepoConfig(max_weight="medium")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    candidates_seen = []
    def fake_choose_why(db_arg, cfg_arg, repo_arg, task_arg, files_arg, candidates, weight, default):
        candidates_seen.extend(candidates)
        return candidates[0], "first"

    monkeypatch.setattr(learning, "choose_why", fake_choose_why)
    profile, learned = autopilot.choose_profile(db, "caller-med", str(repo), weight="medium")
    assert "developer-heavy" not in candidates_seen
    assert profile != "developer-heavy"


def test_no_permissible_candidates_under_ceiling_raises(db, repo, monkeypatch):
    cfg = RepoConfig(max_weight="light", default_agent="developer-heavy", routing={"light": ["developer-heavy"], "medium": ["developer-heavy"], "heavy": ["developer-heavy"]})
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    with pytest.raises(autopilot.AutopilotError) as exc_info:
        autopilot.choose_profile(db, "caller-none", str(repo), weight="light")
    assert "no permissible agent profile under max_weight" in str(exc_info.value)
