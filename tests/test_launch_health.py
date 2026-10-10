"""Launching never leaves an agent hung at startup, and one that is stuck on
a prompt is visible.

- A profile's permission_mode (and allowed_tools) reaches the CLI on every
  launch path: a fresh spawn, a handoff/assign worker, a resume via `brindle
  continue` (with and without a saved conversation), and a reviewer.
- The worktree a Claude worker starts in is pre-trusted, so Claude Code's
  first-run folder-trust dialog never waits on nobody.
- The foreground launch paths (`brindle start`, handoff/assign) never wait on
  slow work: retention, culling, pool refills and startup dialogs happen in
  detached helpers.
- A pane showing a permission or trust prompt reads as 'waiting', and a
  worker stuck like that is reported to its supervisor, once.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time

import pytest

from brindle import agents, cli, cull, pool, procs, sessions, tmux, workspaces
from brindle.db import Agent
from brindle.providers import ClaudeCode, trust_folder

from test_agents import CLAUDE_PROMPT
from test_reliability import CLAUDE_BUSY, CLAUDE_IDLE

TRUST_SCREEN = (
    " Do you trust the files in this folder?\n\n"
    " /Users/me/.brindle/worktrees/proj/feature\n\n"
    " ❯ 1. Yes, proceed\n   2. No, exit\n\n Enter to confirm · Esc to exit\n"
)


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


@pytest.fixture
def launches(monkeypatch):
    """Record each CLI command line brindle would run, without running it."""
    seen: list[list[str]] = []

    def fake_open(db, agent, ws, name, argv, watch_pane):
        seen.append(argv)
        target = f"%fake-{agent.id}"
        db.update_agent(agent.id, tmux_window=target)
        agent.tmux_window = target
        return target

    monkeypatch.setattr(agents, "_open_window", fake_open)
    monkeypatch.setattr(ClaudeCode, "after_launch", lambda self, target: None)
    return seen


@pytest.fixture
def detached(monkeypatch):
    """Record brindle's detached helpers (`brindle _after-launch`, `_cull`,
    `_pool-fill`) instead of starting them; every other process runs."""
    started: list[list[str]] = []
    real = subprocess.Popen

    def popen(args, *a, **k):
        if isinstance(args, list) and args[1:3] == ["-m", "brindle"] and str(args[3]).startswith("_"):
            started.append([str(x) for x in args])
            return real(["true"])
        return real(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


@pytest.fixture
def root(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive",
                 "processing", "%boss", None, time.time())
    db.add_agent(boss)
    return boss, ws


@pytest.fixture
def claude_config(tmp_path, monkeypatch):
    """A Claude Code config dir with an existing global state file."""
    config = tmp_path / "claude-config"
    config.mkdir(exist_ok=True)
    (config / ".claude.json").write_text(json.dumps(
        {"numStartups": 3, "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


# -- permission mode survives every launch path ----------------------------------


def test_fresh_worker_spawn_gets_its_permission_mode(db, root, launches, detached):
    boss, ws = root
    worker, _ = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    argv = launches[-1]
    assert flag(argv, "--permission-mode") == "auto"
    assert "Bash(git commit:*)" in flag(argv, "--allowedTools")
    assert worker.profile == "developer"


def test_resume_keeps_permission_mode_with_and_without_a_saved_conversation(
        db, root, launches, detached, claude_config):
    boss, ws = root
    worker, _ = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    for a in (boss, worker):
        db.set_status(a.id, "paused")
    # The worker has a saved conversation (resumed with --resume); the
    # supervisor never said anything (starts fresh on its task).
    db.update_agent(worker.id, session_ref="sess-1")
    saved = claude_config / "projects" / "p"
    saved.mkdir(parents=True)
    (saved / "sess-1.jsonl").write_text("{}\n")
    launches.clear()

    agents.resume(db, boss.id, watch_pane=False)

    by_resume = {flag(argv, "--resume"): argv for argv in launches}
    assert flag(by_resume["sess-1"], "--permission-mode") == "auto"
    assert "Bash(git commit:*)" in flag(by_resume["sess-1"], "--allowedTools")
    assert len(launches) == 2


def test_profile_named_differently_inside_still_resumes_with_its_own_permissions(
        db, root, launches, detached):
    """A copied profile whose frontmatter `name:` wasn't changed: the agent
    must record the file it was started from, or a resume loads another
    profile (here the built-in developer) with other permissions."""
    boss, ws = root
    agents_dir = os.path.join(ws.repo_root, ".brindle", "agents")
    os.makedirs(agents_dir)
    with open(os.path.join(agents_dir, "careful.md"), "w") as f:
        f.write("---\nname: developer\nprovider: claude\npermission_mode: auto\n---\nBe careful.\n")
    worker, _ = agents.delegate(db, boss, ws, "careful", "add a flag", "assign")
    assert db.get_agent(worker.id).profile == "careful"
    db.set_status(boss.id, "paused")
    db.set_status(worker.id, "paused")
    launches.clear()

    agents.resume(db, boss.id, watch_pane=False)

    assert [flag(argv, "--permission-mode") for argv in launches].count("auto") == 1


def test_reviewer_never_waits_on_a_permission_prompt(db, root, launches, detached):
    boss, ws = root
    agents.spawn(db, ws, "reviewer", prompt="review this", parent_id=boss.id, mode="review")
    argv = launches[-1]
    # dontAsk: whatever allowed_tools doesn't cover is refused, not asked about.
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert "Bash(git diff:*)" in flag(argv, "--allowedTools")


# -- worktree pre-trust ------------------------------------------------------------


def test_worker_worktree_is_pre_trusted(db, root, launches, detached, claude_config):
    boss, ws = root
    _, wws = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    data = json.loads((claude_config / ".claude.json").read_text())
    assert data["projects"][os.path.realpath(wws.path)]["hasTrustDialogAccepted"] is True
    # Everything else in Claude Code's state is left as it was.
    assert data["numStartups"] == 3
    assert data["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": True}


def test_trust_leaves_a_missing_or_broken_config_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert trust_folder(str(tmp_path)) is False
    assert not (tmp_path / ".claude.json").exists()
    (tmp_path / ".claude.json").write_text("{not json")
    assert trust_folder(str(tmp_path)) is False
    assert (tmp_path / ".claude.json").read_text() == "{not json"


def test_trust_keeps_the_config_private(tmp_path, claude_config):
    config = claude_config / ".claude.json"
    config.chmod(0o600)
    assert trust_folder(str(tmp_path)) is True
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_trust_cleans_up_its_temp_file_when_the_write_fails(tmp_path, claude_config, monkeypatch):
    config = claude_config / ".claude.json"
    before = config.read_text()

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    assert trust_folder(str(tmp_path)) is False
    assert config.read_text() == before
    assert sorted(p.name for p in claude_config.iterdir()) == [".claude.json"]


def test_concurrent_trusts_all_land(tmp_path, claude_config):
    folders = [tmp_path / f"wt{i}" for i in range(8)]
    for f in folders:
        f.mkdir()
    threads = [threading.Thread(target=trust_folder, args=(str(f),)) for f in folders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    projects = json.loads((claude_config / ".claude.json").read_text())["projects"]
    assert all(projects[os.path.realpath(f)]["hasTrustDialogAccepted"] is True for f in folders)
    assert sorted(p.name for p in claude_config.iterdir()) == [".claude.json"]


def test_trust_is_idempotent(tmp_path, claude_config):
    assert trust_folder(str(tmp_path)) is True
    before = (claude_config / ".claude.json").stat().st_mtime_ns
    assert trust_folder(str(tmp_path)) is True
    assert (claude_config / ".claude.json").stat().st_mtime_ns == before


# -- the foreground launch path never waits on slow work -------------------------

BUDGET = 3.0  # seconds; each stubbed slow step below takes longer than this alone


@pytest.fixture
def slow_background(monkeypatch):
    def slow(*a, **k):
        time.sleep(BUDGET + 1)

    monkeypatch.setattr(sessions, "enforce", slow)
    monkeypatch.setattr(cull, "sweep", slow)
    monkeypatch.setattr(pool, "fill_in_background", slow)
    monkeypatch.setattr(procs, "stop", slow)
    monkeypatch.setattr(ClaudeCode, "after_launch", lambda self, target: slow())


def test_brindle_start_returns_within_budget(db, repo, monkeypatch, detached, slow_background):
    monkeypatch.chdir(repo)
    ws = workspaces.adopt_root(db, str(repo))
    # A session still running here, which start must pause first.
    old = agents.spawn(db, ws, "supervisor", provider_name="shell")
    try:
        t0 = time.monotonic()
        cli.start(agent="supervisor", prompt=None, provider="shell", attach=False,
                  watch=False, autopilot=False, branch=None, worktree=None)
        elapsed = time.monotonic() - t0
    finally:
        tmux.kill_session(ws.tmux_session)
    assert elapsed < BUDGET
    assert db.get_agent(old.id).status == "paused"
    # Retention and culling went to the detached helper instead.
    assert ["_cull", "--repo", ws.repo_root] in [argv[3:] for argv in detached]


def test_assign_returns_within_budget(db, root, launches, detached, slow_background):
    boss, ws = root
    t0 = time.monotonic()
    worker, _ = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    assert time.monotonic() - t0 < BUDGET
    # The startup dialog is handled by the detached helper, not waited on here.
    assert ["_after-launch", worker.id] in [argv[3:5] for argv in detached]


# -- a prompt is visible: 'waiting', and its supervisor is told once -----------------


def test_trust_dialog_reads_as_waiting():
    assert ClaudeCode().screen_state(TRUST_SCREEN) == "waiting"
    assert ClaudeCode().screen_state(CLAUDE_PROMPT) == "waiting"


def worker_on(db, ws, status, since, parent="boss"):
    a = Agent("w1", ws.id, "developer", "claude", parent, "assign", status, "%w1", None,
              since, status_since=since)
    db.add_agent(a)
    return a


@pytest.fixture
def screens(monkeypatch):
    """What each pane shows; the supervisor's is busy, so messages queue."""
    shown = {"%boss": CLAUDE_BUSY}
    monkeypatch.setattr(tmux, "capture", lambda target, **k: shown.get(target, CLAUDE_IDLE))
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return shown


def test_starting_worker_on_the_trust_dialog_shows_waiting(db, root, screens):
    _, ws = root
    screens["%w1"] = TRUST_SCREEN
    a = worker_on(db, ws, "starting", time.time())
    agents.reconcile(db, a, samples=1)  # what the sidebar does every redraw
    assert db.get_agent("w1").status == "waiting"
    # Once the dialog is answered, Claude Code starts and says so.
    agents.handle_hook(db, "w1", "session-start", {})
    assert db.get_agent("w1").status == "idle"


def test_starting_worker_is_not_marked_ready_by_the_screen(db, root, screens):
    _, ws = root
    a = worker_on(db, ws, "starting", time.time())
    agents.reconcile(db, a, samples=1)
    assert db.get_agent("w1").status == "starting"  # only its hooks say it's ready


def test_stuck_worker_is_reported_to_its_supervisor_once(db, root, screens):
    _, ws = root
    screens["%w1"] = CLAUDE_PROMPT
    since = time.time() - cull.STUCK_AFTER - 5
    worker_on(db, ws, "waiting", since)
    now = time.time()

    assert cull.note_stuck(db, now, {}) != []
    assert cull.note_stuck(db, now + 60, {}) == []  # still the same spell: not again
    assert db.pending_count("boss") == 1
    body = db.pop_pending("boss").body
    assert "w1" in body and "waiting on a prompt" in body and "Do you want to proceed?" in body

    # Answered, then stuck again later: that's a new spell, reported again.
    db.set_status("w1", "processing")
    db.set_status("w1", "waiting")
    db.update_agent("w1", status_since=now - cull.STUCK_AFTER - 1)
    assert cull.note_stuck(db, now, {}) != []
    assert db.pending_count("boss") == 1


def test_trust_dialog_nobody_answered_is_reported_by_the_sweep(db, root, screens):
    """No hook runs before the trust dialog is answered, so the sweep reads
    the screen itself, then reports it once it's been waiting long enough."""
    _, ws = root
    screens["%w1"] = TRUST_SCREEN
    worker_on(db, ws, "starting", time.time() - 600)
    now = time.time()
    assert cull.note_stuck(db, now, {}) == []  # just noticed: waiting from now on
    assert db.get_agent("w1").status == "waiting"
    assert cull.note_stuck(db, now + cull.STUCK_AFTER + 1, {}) != []
    assert db.pending_count("boss") == 1


def test_briefly_waiting_worker_is_not_reported(db, root, screens):
    _, ws = root
    screens["%w1"] = CLAUDE_PROMPT
    worker_on(db, ws, "waiting", time.time() - 5)
    assert cull.note_stuck(db, time.time(), {}) == []
    assert db.pending_count("boss") == 0


def test_trust_writes_through_a_symlinked_config(tmp_path, claude_config):
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    link = claude_config / ".claude.json"
    real.write_text(link.read_text())
    link.unlink()
    link.symlink_to(real)
    folder = tmp_path / "wt"
    folder.mkdir()
    assert trust_folder(str(folder)) is True
    assert link.is_symlink()
    assert json.loads(real.read_text())["projects"][os.path.realpath(folder)]["hasTrustDialogAccepted"] is True


def test_silent_codex_worker_is_reported_once(db, root, screens, monkeypatch):
    """Issue #42: a Codex worker (no hooks) stuck on a startup error."""
    _, ws = root
    screens["%w1"] = "■ unexpected status 403 Forbidden: your plan doesn't include Codex\n"
    now = time.time()
    a = worker_on(db, ws, "unknown", now - 3600)
    db.update_agent(a.id, provider="codex")
    last = now - cull.SILENT_AFTER - 5
    monkeypatch.setattr(tmux, "window_activity", lambda target, **k: last)

    assert cull.note_silent(db, now, {}) != []
    assert cull.note_silent(db, now + 60, {}) == []  # same silence: not again
    body = db.pop_pending("boss").body
    assert "w1" in body and "403" in body and "another profile" in body


def test_recently_active_worker_is_not_silent(db, root, screens, monkeypatch):
    _, ws = root
    worker_on(db, ws, "unknown", time.time() - 3600)
    monkeypatch.setattr(tmux, "window_activity", lambda target, **k: time.time() - 30)
    assert cull.note_silent(db, time.time(), {}) == []


@pytest.fixture
def health(db, root, monkeypatch):
    from brindle import launch_health as lh
    _, ws = root
    a = worker_on(db, ws, "processing", 100)
    db.update_agent(a.id, task="implement a feature", provider="codex")
    a = db.get_agent(a.id)
    now = [100.0]
    sleeps, started, reports = [], [], []
    monkeypatch.setattr(lh.time, "time", lambda: now[0])

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(lh.time, "sleep", sleep)
    monkeypatch.setattr(agents, "_detach", started.append)
    monkeypatch.setattr(agents, "same_server", lambda a: True)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(lh, "capture", lambda a: "working")
    monkeypatch.setattr(agents, "send_message", lambda db, to, body: reports.append((to, body)))
    lh.prepare(db, a, "effective-account")
    return lh, a, now, sleeps, started, reports


def test_followup_is_detached_once_and_five_seconds_after_delivery(db, health):
    lh, a, now, sleeps, started, reports = health
    # Readiness can take arbitrarily long. It is not task delivery.
    now[0] += 30
    assert lh.inspect(db, a.id) is None
    assert sleeps == [] and reports == []
    lh.delivered(db, a)
    lh.delivered(db, a)
    assert len(started) == 1 and started[0][3:5] == ["_launch-health", a.id]
    assert sleeps == []  # foreground never sleeps
    now[0] += 2
    assert lh.inspect(db, a.id) == "pending"
    assert sleeps == [3.0] and now[0] == 135
    assert "pending" in reports[0][1] and "not been verified" in reports[0][1]
    assert lh.inspect(db, a.id) is None and len(reports) == 1


@pytest.mark.parametrize("screen,cause", [
    ("■ unexpected status 403 Forbidden: secret-token https://private", "launch_auth"),
    ("Error: invalid API key secret-token", "launch_auth"),
    ("Error: model not found secret-token", "launch_model"),
    ("ERROR: 429 capacity exhausted secret-token", "launch_capacity"),
    ("● API Error: model use case details have not been submitted secret-token", "bedrock_use_case"),
    ("── brindle: the model endpoint failed: 401 secret-token; this worker has stopped ──", "launch_auth"),
])
def test_current_failures_report_safe_actions_and_effective_cooldown(db, health, monkeypatch, screen, cause):
    from brindle import model_access
    lh, a, _, _, _, reports = health
    monkeypatch.setattr(lh, "capture", lambda a: screen)
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) == "failed"
    body = reports[0][1]
    assert "Fix:" in body and "secret-token" not in body and "https://private" not in body
    denied = model_access.refused(a.profile, fp="effective-account")
    assert denied.key == cause
    assert model_access.refused(a.profile, fp="another-account") is None


@pytest.mark.parametrize("delivered", [False, True])
def test_early_exit_reports_without_claiming_access(db, health, monkeypatch, delivered):
    lh, a, _, _, _, reports = health
    monkeypatch.setattr(agents, "is_alive", lambda a: False)
    if delivered:
        lh.delivered(db, a)
    assert lh.inspect(db, a.id) == "exited"
    assert "exited" in reports[0][1] and "retry" in reports[0][1]


def test_readiness_failure_is_reported_without_sending_a_probe(db, health, monkeypatch):
    lh, a, _, sleeps, started, reports = health
    monkeypatch.setattr(lh, "capture", lambda a: "Error: authentication failed")
    assert lh.inspect(db, a.id, wait=False) == "failed"
    assert sleeps == [] and started == [] and "credentials" in reports[0][1]


def test_after_launch_does_not_move_followup_ahead_of_five_seconds(db, health):
    lh, a, _, sleeps, _, reports = health
    lh.delivered(db, a)
    assert lh.inspect(db, a.id, wait=False) is None
    assert reports == [] and sleeps == []


@pytest.mark.parametrize("screen,baseline,task", [
    ("Error: model not found", "Error: model not found", ""),
    ("  Error: model not found", "Error: model not found", ""),
    ("Error: model not found", "", "test Error: model not found handling"),
    ("❯ test these failures\n  Error: invalid API key\n  Error: 429 capacity", "", ""),
    ("The user asked us to handle 403 and model not found", "", ""),
    ("tool output: Error: invalid API key", "", ""),
    ("❯ test these failures\nError: invalid API key", "", ""),
    ("● API Error: 403 Forbidden\n❯ retry\n● fixed it", "", ""),
    ("● API Error: 403 Forbidden\nesc to interrupt", "", ""),
    ("```\nError: invalid API key\n```", "", ""),
])
def test_prompt_history_and_prose_are_not_launch_evidence(screen, baseline, task):
    from brindle import launch_health
    assert launch_health.failure(screen, baseline=baseline, task=task) is None


def test_paused_finished_and_replaced_launches_are_not_reported(db, health):
    lh, a, _, _, _, reports = health
    lh.delivered(db, a)
    db.set_status(a.id, "paused")
    assert lh.inspect(db, a.id) is None
    db.set_status(a.id, "processing")
    db.update_agent(a.id, tmux_window="%new-launch")
    assert lh.inspect(db, a.id) is None
    db.update_agent(a.id, tmux_window=a.tmux_window, result="finished")
    assert lh.inspect(db, a.id) is None and reports == []


def test_queued_actual_task_schedules_only_after_readiness_and_paste(db, health, monkeypatch):
    lh, a, now, sleeps, started, _ = health
    pastes = []
    monkeypatch.setattr(agents, "_paste_blocked", lambda a: False)
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pastes.append(body))
    db.set_status(a.id, "starting")
    db.enqueue(a.id, a.task, None)
    now[0] = 140
    assert not agents.flush(db, a.id) and started == []
    db.set_status(a.id, "idle")
    assert agents.flush(db, a.id)
    assert not agents.flush(db, a.id)
    assert pastes == [a.task] and len(started) == 1 and sleeps == []
    assert lh.inspect(db, a.id) == "pending" and sleeps == [5.0]


def test_initial_command_task_delivered_once_and_no_generation_probe(db, root, launches, detached, monkeypatch):
    from brindle import model_access
    monkeypatch.setattr(model_access, "probe", lambda *a, **k: pytest.fail("synthetic generation"))
    monkeypatch.setattr(ClaudeCode, "warmup", lambda *a: pytest.fail("warm-up generation"))
    boss, ws = root
    a = agents.spawn(db, ws, "developer", prompt="real task", parent_id=boss.id, mode="assign",
                     background_setup=True)
    assert sum("real task" in arg for arg in launches[-1]) == 1
    assert db.pending_count(a.id) == 0
    assert ["_launch-health", a.id] in [argv[3:5] for argv in detached]
    assert ["_after-launch", a.id] in [argv[3:5] for argv in detached]


def test_shell_followup_starts_after_actual_paste(db, root, launches, monkeypatch):
    from brindle import launch_health
    boss, ws = root
    steps = []
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: steps.append("task"))
    monkeypatch.setattr(agents, "_detach", lambda *a: steps.append("followup"))
    monkeypatch.setattr(agents.get_provider("shell"), "after_launch", lambda *a: None)
    agents.spawn(db, ws, "developer", provider_name="shell", prompt="real task",
                 parent_id=boss.id, mode="assign")
    assert steps == ["task", "followup"]


def test_headless_real_turn_schedules_followup_once(db, root, launches, monkeypatch):
    from brindle import launch_health
    from dataclasses import replace
    boss, ws = root
    original = agents.load_profile
    monkeypatch.setattr(agents, "load_profile", lambda *a: replace(original(*a), headless=True))
    started, turns = [], []
    monkeypatch.setattr(agents, "_detach", started.append)
    monkeypatch.setattr(launch_health, "capture", lambda a: "")
    monkeypatch.setattr(subprocess, "call", lambda argv, **k: turns.append(argv) or 0)
    a = agents.spawn(db, ws, "developer", prompt="real task", parent_id=boss.id, mode="assign")
    assert len(started) == 1 and started[0][-1] == "--startup"
    assert db.conn.execute("SELECT delivered_at FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] is None
    assert agents.run_headless(db, a.id, exit_when_idle=True) == 0
    assert len(turns) == 1 and len(started) == 2
    assert started[1][3:5] == ["_launch-health", a.id]
    assert db.pending_count(a.id) == 0


def test_native_real_task_schedules_followup_without_extra_model_turn(db, health, monkeypatch):
    from unittest.mock import Mock
    from brindle.native import runner
    from brindle.profiles import Profile
    lh, a, _, sleeps, started, _ = health
    db.update_agent(a.id, provider="native", headless=1, mode="interactive")
    p = Profile("developer", "", "native", "", model="wanted", base_url="http://localhost/v1")
    monkeypatch.setattr(agents, "load_profile", lambda *a: p)
    loop = Mock()
    loop.run.return_value = "done"
    monkeypatch.setattr(runner, "NativeAgent", lambda *a, **k: loop)
    db.enqueue(a.id, "real task", None)
    assert runner.run_native(db, a.id, exit_when_idle=True) == 0
    loop.run.assert_called_once_with("real task")
    assert len(started) == 1 and sleeps == []


def test_headless_startup_helper_reports_exit_before_delivery(db, health, monkeypatch):
    lh, a, _, sleeps, _, reports = health
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    monkeypatch.setattr(agents, "is_alive", lambda a: False)
    cli.launch_health_cmd(a.id, startup=True, token=a._launch_health_token)
    assert sleeps == [5.0] and "exited" in reports[0][1]


def test_headless_startup_helper_leaves_delivered_task_to_its_followup(db, health, monkeypatch):
    lh, a, _, sleeps, _, reports = health
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    lh.delivered(db, a)
    cli.launch_health_cmd(a.id, startup=True, token=a._launch_health_token)
    assert sleeps == [5.0] and reports == []
    assert lh.inspect(db, a.id) == "pending" and len(reports) == 1


def test_delegate_prepares_an_empty_pane_before_binding_and_delivers_once(db, root, detached, monkeypatch):
    boss, ws = root
    launched = []

    def open_window(db, agent, ws, name, argv, watch_pane):
        # Exercise delegate -> spawn -> _launch -> prepare on the real unbound
        # Agent, rather than preparing an already-bound health fixture.
        assert agent.tmux_window == ""
        row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
        assert row["pane"] == "" and row["delivered_at"] is None and row["token"]
        assert sum("real initial task" in arg for arg in argv) == 1
        launched.append(agent.id)
        agent.tmux_window = "%bound"
        db.update_agent(agent.id, tmux_window=agent.tmux_window)
        return agent.tmux_window

    monkeypatch.setattr(agents, "_open_window", open_window)
    a, _ = agents.delegate(db, boss, ws, "developer", "real initial task", "assign")
    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
    assert launched == [a.id] and row["pane"] == "%bound" and row["delivered_at"] is not None
    followups = [argv for argv in detached if argv[3] == "_launch-health"]
    assert len(followups) == 1 and followups[0][-2:] == ["--token", row["token"]]
    assert db.pending_count(a.id) == 0


def test_prepare_normalizes_none_pane(db, health):
    lh, a, _, _, _, _ = health
    a.tmux_window = None
    lh.prepare(db, a, "new-account")
    assert db.conn.execute("SELECT pane FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] == ""


@pytest.mark.parametrize("replacement_point", ["wait", "capture"])
def test_concurrent_resume_cannot_report_or_mutate_replacement_launch(db, health, monkeypatch, replacement_point):
    from brindle import model_access
    from brindle.db import DB
    lh, a, now, sleeps, _, reports = health
    original_token = a._launch_health_token
    lh.delivered(db, a)
    other = DB(db.path)

    def replace_launch():
        resumed = other.get_agent(a.id)
        # Deliberately reuse the pane ID: a pane-only outcome CAS is insufficient.
        lh.prepare(other, resumed, "resumed-account")
        lh.delivered(other, resumed)
        assert resumed._launch_health_token != original_token

    if replacement_point == "wait":
        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay
            replace_launch()
        monkeypatch.setattr(lh.time, "sleep", sleep)
    else:
        def capture(agent):
            replace_launch()
            return "■ unexpected status 403 Forbidden"
        monkeypatch.setattr(lh, "capture", capture)

    try:
        assert lh.inspect(db, a.id, token=original_token) is None
        row = other.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
        assert row["fingerprint"] == "resumed-account" and row["outcome"] is None
        assert sleeps == [5.0] and reports == []
        assert model_access.refused(a.profile, fp="effective-account") is None
    finally:
        other.conn.close()


def test_old_helper_started_after_resume_does_not_observe_new_launch(db, health, monkeypatch):
    lh, a, _, sleeps, _, reports = health
    old_token = a._launch_health_token
    resumed = db.get_agent(a.id)
    lh.prepare(db, resumed, "resumed-account")
    lh.delivered(db, resumed)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    assert lh.inspect(db, a.id, token=old_token) is None
    cli.launch_health_cmd(a.id, startup=True, token=old_token)
    cli.after_launch_cmd(a.id, token=old_token)
    assert sleeps == [] and reports == []
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] is None


def test_old_prepared_agent_cannot_deliver_into_replacement_launch(db, health):
    lh, a, _, _, started, _ = health
    resumed = db.get_agent(a.id)
    lh.prepare(db, resumed, "resumed-account")
    lh.delivered(db, a)
    assert started == []
    assert db.conn.execute("SELECT delivered_at FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] is None


def test_launch_health_schema_migrates_existing_rows(db, health):
    from brindle.db import DB
    _, a, _, _, _, _ = health
    db.conn.execute("DROP TABLE launch_health")
    db.conn.execute("CREATE TABLE launch_health (agent_id TEXT PRIMARY KEY, pane TEXT NOT NULL, "
                    "fingerprint TEXT NOT NULL, delivered_at REAL, baseline TEXT NOT NULL DEFAULT '', outcome TEXT)")
    db.conn.execute("INSERT INTO launch_health (agent_id,pane,fingerprint) VALUES (?,?,?)",
                    (a.id, a.tmux_window, "legacy"))
    migrated = DB(db.path)
    try:
        row = migrated.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
        assert row["fingerprint"] == "legacy" and row["token"] == ""
    finally:
        migrated.conn.close()
