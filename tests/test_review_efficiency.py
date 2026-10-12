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
    big = "x" * 3000
    monkeypatch.setattr(gates, "run_checked", lambda db, ws, cmd, env, timeout: (False, big))
    cfg = RepoConfig(checks=["a", "b", "c"])
    summary = gates.check_summary(None, worker_ws, cfg)
    assert "truncated" in summary
    assert "omitted" in summary
    assert len(summary) < 3 * len(big)


# -- review prompt ---------------------------------------------------------------


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
