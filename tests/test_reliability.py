"""Two autopilot reliability bugs: a worker that goes idle without ever
calling report_result must not silently freeze the nudge loop forever, and a
milestone regressed by a merge must not stay hidden behind a stale checkmark
while another milestone is still in progress."""

from __future__ import annotations

import time

import pytest

from brindle import agents, autopilot, tmux, workspaces
from brindle.config import RepoConfig
from brindle.db import Agent

CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
# Real Claude Code 2.1.283 layout: a blank line sits between the box's top
# border and the spinner line above it (and another below the bottom
# border), e.g. "✻ Tomfoolering… (7m 22s · ↓ 35.0k tokens · thinking)".
BOX = "\n────\n❯ \n────\n\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
CLAUDE_BUSY = "✻ Tomfoolering… (7m 22s · ↓ 35.0k tokens · thinking)" + BOX
LONG_AGO = time.time() - autopilot.IDLE_GRACE_SECONDS - 1


def add_agent(db, ws, agent_id, *, status="processing", status_since=None, parent="boss",
              provider="claude"):
    a = Agent(agent_id, ws.id, "worker", provider, parent, "assign", status, "@0", None,
              time.time(), status_since=status_since if status_since is not None else time.time())
    db.add_agent(a)
    return a


@pytest.fixture(autouse=True)
def idle_screen(monkeypatch):
    """Workers' terminals show Claude Code's idle input box unless a test says otherwise."""
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)


@pytest.fixture
def root(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    supervisor = Agent("boss", ws.id, "supervisor", "claude", None, "interactive",
                       "processing", "@0", None, time.time())
    db.add_agent(supervisor)
    db.add_autopilot("boss")
    return supervisor, ws


def with_goal(db, checks=("test -f api.txt", "test -f ui.txt")):
    autopilot.set_goal(db, "boss", "Settings page",
                       [(f"Milestone {i}", c, None) for i, c in enumerate(checks, start=1)])


# -- Bug 1: an idle, unreported worker must not stall the nudge loop --------


def test_worker_idle_past_grace_period_does_not_block_the_nudge(db, root, monkeypatch):
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="idle",
              status_since=LONG_AGO)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out = autopilot.on_stop(db, agent, {})
    assert out and out["decision"] == "block"
    assert "w1" in out["reason"]
    assert "report_result" in out["reason"]


# -- Bug 2: a milestone regression must not hide behind a stale checkmark ---


# -- Bug 3: nothing woke a supervisor that went idle before its worker ------


def test_worker_stopping_unreported_wakes_an_idle_supervisor(db, root, monkeypatch):
    # The usual order: the supervisor idles (a worker is still busy), then the
    # worker stops twice without reporting. The supervisor has to hear of it.
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert agents.handle_hook(db, "boss", "stop", {}) is None
    assert db.get_agent("boss").status == "idle"

    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text, **k: pasted.append(text))
    out = agents.handle_hook(db, "w1", "stop", {})
    assert out and "report_result" in out["reason"]
    assert pasted == []   # reminded once first; nothing to tell yet
    assert agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True}) is None
    assert db.get_agent("w1").status == "idle"
    assert len(pasted) == 1
    assert "w1" in pasted[0] and "report_result" in pasted[0]
    assert ws.branch in pasted[0] and "workspace_diff" in pasted[0]
    assert db.get_agent("boss").status == "processing"


# -- Bug 5: milestone rechecks at an unchanged HEAD; regressions aren't progress


def commit(repo, msg="change"):
    import subprocess

    subprocess.run(f"git add -A && git commit -qm {msg}", shell=True, cwd=repo, check=True)


@pytest.mark.variant("regression: milestone cache follows tree")
def test_passed_milestone_is_not_rerun_while_head_is_unchanged(db, root, repo, monkeypatch):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    commit(repo)
    autopilot.check_milestones(db, "boss", ws, position=1)
    m1 = db.milestones("boss")[0]
    assert m1.status == "passed" and m1.checked_sha
    ran = []
    real = autopilot.run_check
    monkeypatch.setattr(autopilot, "run_check",
                        lambda cmd, *a: ran.append(cmd) or real(cmd, *a))
    autopilot.check_milestones(db, "boss", ws, position=2)
    assert ran == ["test -f ui.txt"]   # milestone 1 passed at this very commit
    (repo / "ui.txt").write_text("")
    commit(repo)
    ran.clear()
    autopilot.check_milestones(db, "boss", ws, position=2)
    assert ran == ["test -f ui.txt", "test -f api.txt"]   # HEAD moved: recheck
    ran.clear()
    import subprocess
    subprocess.run("git commit -q --allow-empty -m same-tree", shell=True, cwd=repo, check=True)
    autopilot.check_milestones(db, "boss", ws, position=2)
    assert ran == []   # HEAD moved but the tree didn't: the passes are cached by tree


@pytest.mark.variant("regression is not progress")
def test_regression_is_not_progress(db, root, repo):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    commit(repo)
    autopilot.check_milestones(db, "boss", ws, position=1)
    before = db.get_autopilot("boss").progress
    (repo / "api.txt").unlink()
    commit(repo, "break")
    out = autopilot.check_milestones(db, "boss", ws, position=2)
    assert "REGRESSED: milestone 1" in out
    assert db.get_autopilot("boss").progress == before
    # Flaky: it passes again. Newly passing is progress.
    (repo / "api.txt").write_text("")
    commit(repo, "fix")
    autopilot.check_milestones(db, "boss", ws, position=1)
    assert db.get_autopilot("boss").progress == before + 1


# -- Re-review: a cheap dashboard, no false busy, parents always told -------


@pytest.mark.parametrize("cause, status", [
    ("settings", "starting"),   # disableAllHooks at launch
    ("grace", "starting"),      # no settings explain it; no hook ever reported
    pytest.param("settings", "idle", marks=pytest.mark.variant("hooks disabled after startup release mail")),
])
def test_sidebar_leaves_starting_and_says_so_when_claude_runs_no_hooks(db, root, monkeypatch, cause, status):
    # Claude Code with disableAllHooks never runs SessionStart, the only thing
    # that moved a 'starting' agent on: its row read "starting up" forever and
    # a message queued for it never went in.
    import os
    from pathlib import Path

    from brindle import view, watch

    _, ws = root
    settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"   # conftest's temporary one
    settings.parent.mkdir(parents=True, exist_ok=True)
    if cause == "settings":
        settings.write_text('{"disableAllHooks": true}')
    since = time.time() - (3600 if cause == "grace" else 0)   # grace: well past agents.HOOKS_GRACE
    add_agent(db, ws, "w1", status=status, status_since=since)
    db.enqueue("w1", "hello", "boss")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text, **k: pasted.append(text))

    entry = view.agent_entry(db, db.get_agent("w1"))   # the sidebar's one-sample read
    assert entry["status"] == "idle" and not pasted
    rows = " ".join(ln.text for ln in watch.render_agent(entry, {"id": ws.id}, time.time(), 30))
    assert "hooks off" in rows

    agents.reconcile(db, db.get_agent("w1"))           # a two-sample read delivers it
    assert pasted and db.pending_count("w1") == 0

    if cause == "settings":                            # turned back on: the warning goes
        settings.write_text('{"disableAllHooks": false}')
        assert "hooks_off" not in view.agent_entry(db, db.get_agent("w1"))
