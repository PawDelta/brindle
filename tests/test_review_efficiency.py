import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, autopilot, gates, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import get_provider


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


@pytest.fixture
def boss(db, repo, monkeypatch):
    """A caller for the MCP tools: an interactive agent in the repo's root
    workspace, addressed via BRINDLE_AGENT_ID like a real one would be."""
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


def add_worker(db, ws, agent_id, task=None, done_when=None, mode="handoff",
              status="done", result="did stuff"):
    a = Agent(agent_id, ws.id, "developer", "claude", None, mode, status, "@0", result,
              time.time(), task=task, done_when=done_when)
    db.add_agent(a)
    return a


def count_run_check(monkeypatch):
    """Count real invocations of autopilot.run_check while still running it."""
    calls = []
    orig = autopilot.run_check

    def counting(cmd, cwd, env, timeout):
        calls.append(cmd)
        return orig(cmd, cwd, env, timeout)

    monkeypatch.setattr(autopilot, "run_check", counting)
    return calls


def capture_spawn(monkeypatch):
    captured = {}

    def fake_spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review", **kw):
        captured["prompt"] = prompt
        return Agent("rev1", ws_.id, profile, "claude", parent_id, mode, "starting", "@0",
                     None, time.time())

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    return captured


# -- check cache ---------------------------------------------------------------


def test_check_cache_hit_on_clean_tree(db, worker_ws, monkeypatch):
    calls = count_run_check(monkeypatch)
    env = workspaces.workspace_env(worker_ws)
    ok1, _ = gates.run_checked(db, worker_ws, "true", env, 30)
    ok2, _ = gates.run_checked(db, worker_ws, "true", env, 30)
    assert ok1 and ok2
    assert len(calls) == 1
    assert db.get_check(worker_ws.id, gates.head(worker_ws), "true") is not None


def test_check_cache_miss_on_dirty_tree(db, worker_ws, monkeypatch):
    (Path(worker_ws.path) / "scratch.txt").write_text("uncommitted\n")
    calls = count_run_check(monkeypatch)
    env = workspaces.workspace_env(worker_ws)
    gates.run_checked(db, worker_ws, "true", env, 30)
    gates.run_checked(db, worker_ws, "true", env, 30)
    assert len(calls) == 2
    assert db.get_check(worker_ws.id, gates.head(worker_ws), "true") is None


def test_check_cache_only_caches_success(db, worker_ws, tmp_path, monkeypatch):
    """A failure must never be cached: retrying re-runs it, even at the same
    sha with a clean tree throughout."""
    marker = tmp_path / "marker"  # outside the worktree: doesn't dirty it
    cmd = f"test -f {marker}"
    calls = count_run_check(monkeypatch)
    env = workspaces.workspace_env(worker_ws)

    ok1, _ = gates.run_checked(db, worker_ws, cmd, env, 30)
    assert not ok1
    assert db.get_check(worker_ws.id, gates.head(worker_ws), cmd) is None

    marker.write_text("now it exists")
    ok2, _ = gates.run_checked(db, worker_ws, cmd, env, 30)
    assert ok2
    assert len(calls) == 2  # the failure was not served from a cache
    cached = db.get_check(worker_ws.id, gates.head(worker_ws), cmd)
    assert cached is not None and cached.ok

    ok3, _ = gates.run_checked(db, worker_ws, cmd, env, 30)
    assert ok3 and len(calls) == 2  # now it's cached


def test_check_cache_skips_when_the_check_itself_leaves_the_tree_dirty(db, worker_ws, monkeypatch):
    def leaves_a_mess(cmd, cwd, env, timeout):
        (Path(cwd) / "artifact.txt").write_text("build output\n")
        return True, "ok"

    monkeypatch.setattr(autopilot, "run_check", leaves_a_mess)
    env = workspaces.workspace_env(worker_ws)
    ok, _ = gates.run_checked(db, worker_ws, "make build", env, 30)
    assert ok
    assert db.get_check(worker_ws.id, gates.head(worker_ws), "make build") is None


def test_gates_run_does_not_rerun_checks_at_same_sha(db, worker_ws, monkeypatch):
    calls = count_run_check(monkeypatch)
    cfg = RepoConfig(checks=["true"])
    r1 = gates.run(db, worker_ws, cfg, review_required=False)
    r2 = gates.run(db, worker_ws, cfg, review_required=False)
    assert r1.ok and r2.ok
    assert len(calls) == 1


# -- check summary ---------------------------------------------------------------


def test_check_summary_caps_total_failure_output(worker_ws, monkeypatch):
    big = "x" * 3000
    monkeypatch.setattr(gates, "run_checked", lambda db, ws, cmd, env, timeout: (False, big))
    cfg = RepoConfig(checks=["a", "b", "c"])
    summary = gates.check_summary(None, worker_ws, cfg)
    assert "truncated" in summary
    assert "omitted" in summary
    assert len(summary) < 3 * len(big)


def test_check_summary_truncation_keeps_the_tail_not_the_head(worker_ws, monkeypatch):
    # run_check's own exit-code marker is the last line; a reviewer needs
    # that more than the first line of a long failure.
    out = "noise\n" * 1000 + "(exit 1)"
    monkeypatch.setattr(gates, "run_checked", lambda db, ws, cmd, env, timeout: (False, out))
    cfg = RepoConfig(checks=["a"])
    summary = gates.check_summary(None, worker_ws, cfg)
    assert "(exit" in summary
    assert summary.startswith("FAIL `a`\n... (truncated)\n") or "... (truncated)" in summary


# -- review prompt ---------------------------------------------------------------


def test_review_prompt_has_task_and_done_when_and_no_checks_without_a_summary(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1", task="Add a login page with OAuth support.",
               done_when="tests/test_login.py passes")
    captured = capture_spawn(monkeypatch)
    cfg = RepoConfig(checks=["true", "false"])
    agents.request_review(db, None, worker_ws, "reviewer", cfg=cfg)
    prompt = captured["prompt"]
    assert "Add a login page with OAuth support." in prompt
    assert "tests/test_login.py passes" in prompt
    assert "checks" not in prompt.lower()
    assert "PASS" not in prompt and "FAIL" not in prompt


def test_incremental_review_focuses_on_diff_since_previous_sha(db, worker_ws, monkeypatch):
    sha1 = gates.head(worker_ws)
    db.add_review(worker_ws.id, sha1, "revA", False, "missing input validation on line 10")
    (Path(worker_ws.path) / "more.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm more", Path(worker_ws.path))

    captured = capture_spawn(monkeypatch)
    agents.request_review(db, None, worker_ws, "reviewer")
    prompt = captured["prompt"]
    assert sha1[:8] in prompt
    assert "missing input validation on line 10" in prompt
    assert f"git diff {sha1}..HEAD" in prompt


def test_incremental_review_falls_back_to_full_review_after_a_rebase(db, worker_ws, monkeypatch):
    sha1 = gates.head(worker_ws)
    db.add_review(worker_ws.id, sha1, "revA", False, "off-by-one in the loop bound")
    sh("git commit --amend -qm 'work (reworded)'", Path(worker_ws.path))  # rewrites sha1 away

    captured = capture_spawn(monkeypatch)
    agents.request_review(db, None, worker_ws, "reviewer")
    prompt = captured["prompt"]
    assert "off-by-one in the loop bound" in prompt
    assert "diverged" in prompt
    assert f"git diff {sha1}..HEAD" not in prompt


def test_incremental_review_falls_back_to_full_review_after_a_merge_commit(db, worker_ws, monkeypatch):
    sha1 = gates.head(worker_ws)
    db.add_review(worker_ws.id, sha1, "revA", False, "missing null check")
    branch = worker_ws.branch
    sh("git checkout -qb side", Path(worker_ws.path))
    (Path(worker_ws.path) / "side.py").write_text("s = 1\n")
    sh("git add -A && git commit -qm side", Path(worker_ws.path))
    sh(f"git checkout -q {branch}", Path(worker_ws.path))
    sh("git merge -q --no-ff side -m merge", Path(worker_ws.path))  # sha1 is still an ancestor

    captured = capture_spawn(monkeypatch)
    agents.request_review(db, None, worker_ws, "reviewer")
    prompt = captured["prompt"]
    assert "missing null check" in prompt
    assert "diverged" in prompt


# -- raw task storage ---------------------------------------------------------------


@pytest.fixture
def pasted(monkeypatch):
    """Record what a real spawn would type into the agent's pane instead of
    typing it: these tests are about the stored task, and a shell pane on a CI
    runner can be gone before the paste's Enter arrives."""
    texts = []
    monkeypatch.setattr(agents.tmux, "paste", lambda target, text, submit=True, **k: texts.append(text))
    return texts


def test_worker_task_is_stored_raw_not_decorated(db, repo, pasted):
    ws = workspaces.create(db, str(repo), "feat-raw").workspace
    worker = agents.spawn(db, ws, "developer", prompt="Add input validation to the login form.",
                          provider_name="shell", mode="handoff", done_when="tests/test_login.py passes")
    stored = db.get_agent(worker.id)
    assert stored.task == "Add input validation to the login form."
    assert stored.done_when == "tests/test_login.py passes"
    assert "Finish line" not in stored.task
    assert "report_result" not in stored.task  # WORKER_FOOTER marker
    assert "/goal" not in stored.task
    assert pasted and "Add input validation to the login form." in pasted[0]


def test_decorate_worker_prompt_adds_the_goal_wrapper_for_claude_workers(worker_ws):
    decorated = agents.decorate_worker_prompt(
        "Add input validation.", "w1", worker_ws, "tests/test_x.py passes",
        get_provider("claude"), headless=False,
    )
    assert decorated.startswith("/goal Finish line: tests/test_x.py passes")
    assert "Add input validation." in decorated
    assert "report_result" in decorated  # the footer is added back for the real launch


def test_review_prompt_uses_the_raw_worker_task_via_real_spawn(db, repo, monkeypatch, pasted):
    ws = workspaces.create(db, str(repo), "feat-review").workspace
    agents.spawn(db, ws, "developer", prompt="Add input validation to the login form.",
                provider_name="shell", mode="handoff", done_when="tests/test_login.py passes")
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))

    captured = capture_spawn(monkeypatch)
    agents.request_review(db, None, ws, "reviewer")
    prompt = captured["prompt"]
    assert "Add input validation to the login form." in prompt
    assert "tests/test_login.py passes" in prompt
    assert "report_result" not in prompt
    assert "/goal" not in prompt


# -- reviewer profile ---------------------------------------------------------------


def test_reviewer_profile_is_cheap():
    p = load_profile("reviewer")
    assert p.model == "sonnet"
    assert p.effort == "medium"
    assert p.strict_mcp is True
    assert p.setting_sources == ["project", "local"]
    assert not p.headless
    assert p.allowed_tools and any("git diff" in t for t in p.allowed_tools)
