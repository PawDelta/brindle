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
    # Even if developer-heavy is in medium candidates, docs-only must NOT select a heavy profile
    cfg = RepoConfig()
    cfg.routing["medium"] = ["developer-heavy", "developer"]
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    why = []
    profile, learned = autopilot.choose_profile(
        db, "caller-2", str(repo), weight="heavy", files=["docs/guide.md", "docs/index.md"], why=why
    )
    assert any("demoted" in w for w in why)
    assert profile != "developer-heavy"
    assert profile == "developer"


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


def test_queued_task_rerouted_when_ceiling_lowered_before_launch(db, repo, monkeypatch):
    import json
    from brindle import tasks, workspaces, agents
    ws = workspaces.create(db, str(repo), "feat/base", reuse_registered=True).workspace
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")

    # 1. Queue a heavy task when max_weight was unlimited or heavy
    task = tasks.enqueue(
        db, supervisor, ws, profile="developer-heavy", task_text="Heavy refactoring",
        mode="assign", isolate=True, branch="feat/heavy-work", done_when=None,
        files=None, depends_on=["dep-branch"], weight="heavy"
    )
    assert task.profile == "developer-heavy"
    assert task.weight == "heavy"

    # 2. Lower max_weight to medium in config
    cfg_path = repo / ".brindle" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg_data["max_weight"] = "medium"
    cfg_path.write_text(json.dumps(cfg_data))

    delegated = []
    def fake_delegate(db_arg, caller_arg, ws_arg, profile_arg, *args, **kwargs):
        delegated.append(profile_arg)
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = "agent-delegated-1"
        return m, ws_arg

    monkeypatch.setattr(agents, "delegate", fake_delegate)

    # 3. Now start_queued launches the task
    tasks.start_queued(db, task)
    assert len(delegated) == 1
    # Profile must be rerouted to a medium profile, NOT developer-heavy!
    assert delegated[0] != "developer-heavy"
    assert delegated[0] in ["developer", "developer-codex"]
    # Task metadata in DB should be updated
    updated_t = db.get_task(task.id)
    assert updated_t.profile == delegated[0]
    assert updated_t.weight == "medium"


def test_custom_expensive_profiles_capped_under_max_weight(db, repo, monkeypatch):
    from unittest.mock import MagicMock
    from brindle import profiles

    # 1. Custom 'research' profile using claude-fable-5-1, effort='medium'
    p_research = MagicMock()
    p_research.model = "claude-fable-5-1"
    p_research.effort = "medium"

    # 2. Custom 'research-light' profile using claude-fable-5-1, effort='low'
    p_light = MagicMock()
    p_light.model = "claude-fable-5-1"
    p_light.effort = "low"

    # 3. Custom 'research-env' profile with model=None but env.ANTHROPIC_MODEL='claude-fable-5-1'
    p_env = MagicMock()
    p_env.model = None
    p_env.effort = None
    p_env.env = {"ANTHROPIC_MODEL": "claude-fable-5-1"}

    def fake_load_profile(name, *args, **kwargs):
        if name == "research":
            return p_research
        if name == "research-light":
            return p_light
        if name == "research-env":
            return p_env
        return profiles.load_profile(name, *args, **kwargs)

    monkeypatch.setattr(autopilot, "load_profile", fake_load_profile)

    # All must classify as heavy regardless of 'medium' or 'low' effort, '-light' name, or model in env
    assert autopilot.profile_weight("research") == "heavy"
    assert autopilot.profile_weight("research-light") == "heavy"
    assert autopilot.profile_weight("research-env") == "heavy"

    # Under max_weight="medium", requesting either must be refused
    cfg = RepoConfig(max_weight="medium")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    with pytest.raises(autopilot.AutopilotError) as exc_info:
        autopilot.choose_profile(db, "caller-res", str(repo), requested="research")
    assert "exceeds configured max_weight 'medium'" in str(exc_info.value)

    with pytest.raises(autopilot.AutopilotError) as exc_info2:
        autopilot.choose_profile(db, "caller-res-light", str(repo), requested="research-light")
    assert "exceeds configured max_weight 'medium'" in str(exc_info2.value)

    with pytest.raises(autopilot.AutopilotError) as exc_info3:
        autopilot.choose_profile(db, "caller-res-env", str(repo), requested="research-env")
    assert "exceeds configured max_weight 'medium'" in str(exc_info3.value)


def test_assignment_and_queue_replies_include_cost_feedback_for_all_selections(db, repo, monkeypatch):
    import asyncio
    from brindle import mcp_server

    ws = Workspace("ws-mcp-cost", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-mcp", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive", autopilot=True)

    from brindle.db import DB
    monkeypatch.setattr(mcp_server, "DB", lambda: DB(db.path))
    monkeypatch.setattr(mcp_server, "_caller", lambda db_arg: (sup, ws))
    monkeypatch.setattr(agents, "_launch", lambda *args, **kwargs: None)

    cfg = RepoConfig()
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)

    # 1. Explicit selection (developer-heavy)
    reply_exp = asyncio.run(mcp_server.assign(agent_profile="developer-heavy", task="explicit task"))
    assert "developer-heavy" in reply_exp
    assert "most expensive tier" in reply_exp

    # 2. Pending milestone selection
    autopilot.set_goal(db, sup.id, "Goal", [("Step 1", "true", "", "developer-heavy")])
    reply_ms = asyncio.run(mcp_server.assign(task="milestone task"))
    assert "developer-heavy" in reply_ms
    assert "most expensive tier" in reply_ms

    # 3. Default selection (no milestone, no explicit, no weight)
    db.conn.execute("DELETE FROM milestones WHERE root_id=?", (sup.id,))
    reply_def = asyncio.run(mcp_server.assign(task="default task"))
    assert "developer" in reply_def
    assert "medium cost tier" in reply_def

    # 4. Queued task reply with explicit selection
    reply_q = asyncio.run(mcp_server.assign(agent_profile="developer-heavy", task="queued task", depends_on=["unmet-dep"]))
    assert "Queued task" in reply_q
    assert "developer-heavy" in reply_q
    assert "most expensive tier" in reply_q


def test_cross_provider_model_env_isolation(db, repo, monkeypatch):
    # Setting an expensive ANTHROPIC_MODEL must NOT bleed into Codex profiles
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-6")
    assert autopilot.effective_profile_model("developer-codex") != "claude-opus-4-6"
    assert autopilot.profile_weight("developer-codex") != "heavy"

    cfg = RepoConfig(max_weight="medium")
    monkeypatch.setattr(autopilot, "load_repo_config", lambda r: cfg)
    p, _ = autopilot.choose_profile(db, "caller-codex", str(repo), requested="developer-codex")
    assert p == "developer-codex"

    # And setting CODEX_MODEL must NOT bleed into Claude profiles
    monkeypatch.setenv("CODEX_MODEL", "o3")
    assert autopilot.effective_profile_model("developer") == "claude-opus-4-6"
    assert autopilot.effective_profile_model("developer-light") != "o3"


