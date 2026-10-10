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
