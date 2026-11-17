import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, gates, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


def capture_spawn(monkeypatch):
    captured = {}

    def fake_spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review", **kw):
        captured["prompt"] = prompt
        return Agent("rev1", ws_.id, profile, "claude", parent_id, mode, "starting", "@0",
                     None, time.time())

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    return captured


# -- check summary ---------------------------------------------------------------


def test_check_summary_caps_total_failure_output(worker_ws, monkeypatch):
    big = "x" * (gates.MAX_FAILURE_CHARS + 1)
    monkeypatch.setattr(gates, "run_checked", lambda db, ws, cmd, env, timeout: (False, big))
    cfg = RepoConfig(checks=["a"])
    summary = gates.check_summary(None, worker_ws, cfg)
    assert "truncated" in summary
    assert len(summary) < 3 * len(big)


# -- review prompt ---------------------------------------------------------------


def test_incremental_review_falls_back_to_full_review_when_the_reviewed_commit_is_gone(db, worker_ws, monkeypatch):
    gone = "deadbeef" * 5   # pruned from the repo: there is no tree to diff from
    db.add_review(worker_ws.id, gone, "revA", False, "off-by-one in the loop bound")

    captured = capture_spawn(monkeypatch)
    agents.request_review(db, None, worker_ws, "reviewer")
    prompt = captured["prompt"]
    assert "off-by-one in the loop bound" in prompt
    assert f"git diff {gone}..HEAD" not in prompt
    assert agents.review_base_ref(worker_ws.id) not in prompt


# -- raw task storage ---------------------------------------------------------------


@pytest.fixture
def pasted(monkeypatch):
    """Record what a real spawn would type into the agent's pane instead of
    typing it: these tests are about the stored task, and a shell pane on a CI
    runner can be gone before the paste's Enter arrives."""
    texts = []
    monkeypatch.setattr(agents.tmux, "paste", lambda target, text, submit=True, **k: texts.append(text))
    return texts


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
