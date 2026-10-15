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
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from brindle import agents, cli, cull, pool, procs, sessions, tmux, workspaces
from brindle.db import Agent
from brindle.providers import ClaudeCode, trust_folder

from test_reliability import CLAUDE_BUSY, CLAUDE_IDLE
from test_codex_notify import CODEX_EMPTY_PANE

TRUST_SCREEN = (
    " Do you trust the files in this folder?\n\n"
    " /Users/me/.brindle/worktrees/proj/feature\n\n"
    " ❯ 1. Yes, proceed\n   2. No, exit\n\n Enter to confirm · Esc to exit\n"
)


@pytest.fixture
def isolated_health_clock(monkeypatch):
    # Virtual health deadlines must not advance during subprocess.wait's
    # Linux polling sleeps (git and trust setup use real subprocesses).
    from brindle import launch_health, providers

    clock = SimpleNamespace(**{name: getattr(time, name) for name in dir(time)})
    monkeypatch.setattr(launch_health, "time", clock)
    monkeypatch.setattr(cli, "time", clock)
    monkeypatch.setattr(providers, "time", clock)


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
        if isinstance(args, list) and args[1:4] == ["-P", "-m", "brindle"] and str(args[4]).startswith("_"):
            started.append([str(x) for x in args])
            from unittest.mock import MagicMock
            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0
            mock_proc.wait.return_value = 0
            mock_proc.returncode = 0
            mock_proc.pid = 99999
            return mock_proc
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


# -- the foreground launch path never waits on slow work -------------------------

BUDGET = 5.0  # seconds; each stubbed slow step below takes longer than this alone


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
        cli.start(prompt=None, provider="shell", attach=False,
                  watch=False, autopilot=False, branch=None, worktree=None)
        elapsed = time.monotonic() - t0
    finally:
        tmux.kill_session(ws.tmux_session)
    assert elapsed < BUDGET
    assert db.get_agent(old.id).status == "paused"
    # Retention and culling went to the detached helper instead.
    assert ["_cull", "--repo", ws.repo_root] in [argv[4:] for argv in detached]


# -- a prompt is visible: 'waiting', and its supervisor is told once -----------------


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


@pytest.mark.parametrize("status,provider", [
    ("unknown", "codex"),       # issue #42: no hooks, stuck on a startup error
    ("processing", "claude"),   # a turn whose pane stopped redrawing
])
def test_silent_codex_worker_is_reported_once(db, root, screens, monkeypatch, status, provider):
    _, ws = root
    screens["%w1"] = "■ unexpected status 403 Forbidden: your plan doesn't include Codex\n"
    now = time.time()
    a = worker_on(db, ws, status, now - 3600)
    db.update_agent(a.id, provider=provider)
    last = now - cull.SILENT_AFTER - 5
    monkeypatch.setattr(tmux, "window_activity", lambda target, **k: last)

    assert cull.note_silent(db, now, {}) != []
    assert cull.note_silent(db, now + 60, {}) == []  # same silence: not again
    from conftest import full_text

    body = full_text(db, "boss", db.pop_pending("boss").body)
    assert "w1" in body and "403" in body and "another profile" in body


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
    # time.sleep is patched process-wide, so other threads and Popen.wait's
    # timeout polling (on this thread too) get the real sleep and never show
    # up in the recorded delays.
    creator = threading.current_thread()
    real_sleep = time.sleep

    def sleep(delay):
        if (threading.current_thread() is not creator
                or sys._getframe(1).f_globals.get("__name__") == "subprocess"):
            real_sleep(delay)
            return
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(lh.time, "sleep", sleep)
    monkeypatch.setattr(agents, "_detach", started.append)
    monkeypatch.setattr(agents, "same_server", lambda *a: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(lh, "capture", lambda a: "working")
    monkeypatch.setattr(agents, "send_message", lambda db, to, body: reports.append((to, body)))
    lh.prepare(db, a, "effective-account")
    lh.bind(db, a, a.tmux_window)
    return lh, a, now, sleeps, started, reports


@pytest.mark.parametrize("screen,cause,refused", [
    ("■ unexpected status 403 Forbidden: secret-token https://private", "launch_auth", "provider_403"),
    ("Error: model not found secret-token", "launch_model", None),
    ("● API Error: model use case details have not been submitted secret-token", "bedrock_use_case", None),
    ("⏺ The model claude-fable-5-1 is not available on your vertex deployment.", "launch_model", None),
])
def test_current_failures_report_safe_actions_and_effective_cooldown(db, health, monkeypatch, screen, cause, refused):
    from brindle import model_access
    from brindle.pro import route_health
    lh, a, _, _, _, reports = health
    # Only the actual provider's response marker can attest a current failure.
    provider = "claude" if screen.startswith(("●", "⏺")) else "native" if screen.startswith("──") else "codex"
    db.update_agent(a.id, provider=provider)
    monkeypatch.setattr(lh, "capture", lambda a: screen)
    monkeypatch.setattr(agents, "route_context", lambda db_, agent, ws: ("bedrock", "claude-opus-5-5"))
    monkeypatch.setattr(route_health, "reporting", lambda: True)
    route_health.reset()
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) == "failed"
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] == "failed"
    assert len(reports) == 1
    body = reports[0][1]
    assert "Fix:" in body and "secret-token" not in body and "https://private" not in body
    denied = model_access.refused(a.profile, fp="effective-account")
    assert denied.key == cause
    assert (db.get_agent(a.id).status == "failed") == (cause == "launch_model")
    ws = db.get_workspace(a.workspace_id)
    assert route_health.pending() == ({(ws.repo_root, provider, "bedrock", "claude-opus-5-5", refused): 1}
                                      if refused else {})
    route_health.reset()
    assert model_access.refused(a.profile, fp="another-account") is None
    assert lh.inspect(db, a.id) is None and len(reports) == 1


@pytest.mark.parametrize("provider,banner,marker", [
    ("claude", " ▐▛███▜▌   Claude Code v2.1.0\n▝▜█████▛▘  Sonnet · Claude Pro\n  ▘▘ ▝▝    /workspace", "⏺ API Error: 401 invalid API key"),
    ("claude", "╭─── Claude Code v2.1.0 ───╮\n│ Welcome back!             │\n│ Sonnet · Claude Pro       │\n│ /workspace                │\n╰──────────────────────────╯", "● API Error: 401 invalid API key"),
])
def test_startup_chrome_and_native_turn_accept_current_auth_failure(db, health, monkeypatch, provider, banner, marker):
    from brindle import model_access
    lh, a, _, sleeps, _, reports = health
    db.update_agent(a.id, provider=provider)
    turn = f"❯ {a.task}" if provider == "claude" else f"── brindle: turn 1 ──\n{a.task}"
    baseline = f"{banner}\n{turn}\n"
    monkeypatch.setattr(lh, "capture", lambda a: baseline + marker)
    lh.delivered(db, a, baseline)
    assert lh.inspect(db, a.id) == "failed"
    assert sleeps == [5.0]
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] == "failed"
    assert len(reports) == 1 and "Fix:" in reports[0][1]
    assert "secret-token" not in reports[0][1] and "https://private" not in reports[0][1]
    assert model_access.refused(a.profile, fp="effective-account").key == "launch_auth"


@pytest.mark.parametrize("provider,screen", [
    ("native", "brindle: native worker w1 on chosen at https://private\n── brindle: turn 1 ──\nimplement a feature\nHere is an example:\n── brindle: turn 2 ──\n── brindle: the model endpoint failed: 401; this worker has stopped ──"),
])
def test_quoted_startup_and_turn_chrome_remain_silent_pending(db, health, monkeypatch, provider, screen):
    from brindle import model_access
    lh, a, _, _, _, reports = health
    db.update_agent(a.id, provider=provider)
    monkeypatch.setattr(lh, "capture", lambda a: screen)
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) == "pending"
    assert reports == [] and db.pending_count("boss") == 0
    assert model_access.refused(a.profile, fp="effective-account") is None


def test_actual_native_task_preview_does_not_hide_current_failure(db, health, monkeypatch):
    from brindle import model_access
    from brindle.native.runner import _preview
    lh, a, _, _, _, reports = health
    task = "\n".join(f"instruction {i}" for i in range(8))
    db.update_agent(a.id, provider="native", task=task)
    # runner prints this preview, captures it, then calls loop.run on the task.
    baseline = f"brindle: native worker {a.id} on chosen at https://private\n\n── brindle: turn 1 ──\n{_preview(task)}\n"
    assert baseline.endswith("...\n")
    monkeypatch.setattr(lh, "capture", lambda a: baseline + "── brindle: the model endpoint failed: 401; this worker has stopped ──")
    lh.delivered(db, a, baseline)
    assert lh.inspect(db, a.id) == "failed"
    assert len(reports) == 1 and "Fix:" in reports[0][1]
    assert model_access.refused(a.profile, fp="effective-account").key == "launch_auth"


def test_full_codex_ui_assistant_and_tool_output_cannot_report_or_cool_down(db, health, monkeypatch):
    from brindle import model_access
    lh, a, _, sleeps, _, reports = health
    reply = "• Example output:\n```\n■ unexpected status 403 Forbidden\n```"
    screen = f"OpenAI Codex\n› {a.task}\n{reply}\n\n› \n100% context left"
    monkeypatch.setattr(lh, "capture", lambda a: screen)
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) == "pending"
    assert sleeps == [5.0] and reports == [] and db.pending_count("boss") == 0
    assert model_access.refused(a.profile, fp="effective-account") is None


@pytest.mark.parametrize("provider,screen", [
    ("codex", "OpenAI Codex\n• Example output:\n› implement a feature\n■ unexpected status 403 Forbidden\n\n› \n100% context left"),
])
def test_quoted_prompt_and_foreign_error_markers_remain_silent_pending(db, health, monkeypatch, provider, screen):
    from brindle import model_access
    lh, a, _, sleeps, _, reports = health
    db.update_agent(a.id, provider=provider)
    monkeypatch.setattr(lh, "capture", lambda a: screen)
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) == "pending"
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] == "pending"
    assert sleeps == [5.0] and reports == [] and db.pending_count("boss") == 0
    assert model_access.refused(a.profile, fp="effective-account") is None


def test_early_exit_reports_without_claiming_access(db, health, monkeypatch):
    lh, a, _, _, _, reports = health
    monkeypatch.setattr(agents, "is_alive", lambda a: False)
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) == "exited"
    assert len(reports) == 1 and "exited" in reports[0][1] and "retry" in reports[0][1]


def test_shell_followup_starts_after_actual_paste(db, root, launches, monkeypatch):
    boss, ws = root
    steps = []
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: steps.append("task"))
    monkeypatch.setattr(agents, "_detach", lambda *a: steps.append("followup"))
    monkeypatch.setattr(type(agents.get_provider("shell")), "after_launch", lambda *a: None)
    agents.spawn(db, ws, "developer", provider_name="shell", prompt="real task",
                 parent_id=boss.id, mode="assign")
    assert steps == ["task", "followup"]


@pytest.mark.parametrize("provider", ["claude"])
def test_argv_health_waits_for_delayed_readiness_then_observes_current_error(
        db, root, launches, detached, monkeypatch, provider):
    import time
    from brindle import launch_health as lh
    boss, ws = root
    now, sleeps, reports = [100.0], [], []
    monkeypatch.setattr(time, "time", lambda: now[0])
    # time.sleep is patched process-wide: other threads and subprocess's
    # timeout polling get the real sleep, and only this thread's health waits
    # move the fake clock (same guard as the `health` fixture, #123).
    creator, real_sleep = threading.current_thread(), time.sleep

    def sleep(delay):
        if (threading.current_thread() is not creator
                or sys._getframe(1).f_globals.get("__name__") == "subprocess"):
            real_sleep(delay)
        elif abs(delay - 5.0) < 0.01 or abs(delay - 3.0) < 0.01:
            sleeps.append(delay)
            now[0] += delay
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    monkeypatch.setattr(agents, "same_server", lambda *a: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    shown = ["Trust this folder?"]
    monkeypatch.setattr(lh, "capture", lambda a: shown[0])
    monkeypatch.setattr(agents, "send_message", lambda db, to, body: reports.append(body))
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    monkeypatch.setattr(agents, "signed_out", lambda *a, **k: None)
    a = agents.spawn(db, ws, "developer", provider_name=provider, prompt="real task", parent_id=boss.id,
                     mode="assign", background_setup=True)
    token = a._launch_health_token
    assert sum("real task" in arg for arg in launches[-1]) == 1
    assert sleeps == [] and db.pending_count(a.id) == 0
    cli.launch_health_cmd(a.id, startup=True, token=token)
    assert now[0] == 105 and reports == []
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] is None

    def delayed_ready(self, target):
        assert target == a.tmux_window
        now[0] = 112.0  # trust/readiness takes longer than the initial five seconds
        shown[0] = "■ unexpected status 403 Forbidden" if provider == "codex" else "● API Error: 401 invalid API key"
        return True

    monkeypatch.setattr(type(agents.get_provider(provider)), "after_launch", delayed_ready)
    cli.after_launch_cmd(a.id, token=token)
    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
    assert row["delivered_at"] == 112 and row["outcome"] is None and reports == []
    followups = [cmd for cmd in detached if cmd[4] == "_launch-health" and "--startup" not in cmd]
    assert len(followups) == 1
    now[0] = 114.0
    cli.launch_health_cmd(a.id, startup=False, token=token)
    assert sleeps == [5.0, 3.0] and now[0] == 117
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] == "failed"
    assert len(reports) == 1 and "Fix:" in reports[0]
    assert db.pending_count(a.id) == 0 and sum("real task" in arg for arg in launches[-1]) == 1


@pytest.mark.parametrize("provider,ready_screen", [("claude", "❯"), ("codex", CODEX_EMPTY_PANE),
                                                   ("antigravity", "? for shortcuts")])
def test_provider_readiness_returns_acceptance_only_after_current_prompt(monkeypatch, provider, ready_screen):
    now = [100.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "loading" if now[0] < 106 else ready_screen)
    assert agents.get_provider(provider).after_launch("%test") is True
    assert now[0] == 106
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "loading")
    assert agents.get_provider(provider).after_launch("%test") is False
    assert now[0] == 136


@pytest.mark.parametrize("provider,trust_screen,ready_screen", [
    ("codex", "Trust this folder?\n› 1. Trust and continue", "› 100% context left"),
    # agy's newer wording, which brindle missed: the worker sat on it unanswered
    ("antigravity", "Antigravity CLI requires permission to read, edit, and execute files here.\n\n"
                    "> Yes, I trust this folder\n  No, exit", "? for shortcuts"),
])
def test_trust_confirmation_is_not_yet_task_acceptance(monkeypatch, provider, trust_screen, ready_screen):
    now, keys = [100.0], []
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    screens = iter([trust_screen, "loading", ready_screen])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: next(screens))
    monkeypatch.setattr(tmux, "send_keys", lambda *a: keys.append(a))
    assert agents.get_provider(provider).after_launch("%test") is True
    assert now[0] == 101.5 and keys == [("%test", "Enter")]


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


def test_headless_startup_helper_leaves_delivered_task_to_its_followup(db, health, monkeypatch):
    lh, a, _, sleeps, _, reports = health
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    lh.delivered(db, a)
    cli.launch_health_cmd(a.id, startup=True, token=a._launch_health_token)
    assert sleeps == [5.0] and reports == []
    assert lh.inspect(db, a.id) == "pending" and reports == []


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
        lh.bind(other, resumed, resumed.tmux_window)
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
    lh.bind(db, resumed, resumed.tmux_window)
    lh.delivered(db, resumed)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    assert lh.inspect(db, a.id, token=old_token) is None
    cli.launch_health_cmd(a.id, startup=True, token=old_token)
    cli.after_launch_cmd(a.id, token=old_token)
    assert sleeps == [] and reports == []
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] is None


@pytest.mark.parametrize("replacement_point", ["readiness", "ready"])
def test_readiness_helper_cannot_arm_concurrently_replaced_argv_launch(db, health, monkeypatch, replacement_point):
    lh, a, _, _, started, reports = health
    lh.prepare(db, a, "old-account", argv_task=True)
    lh.bind(db, a, a.tmux_window)
    old_token = a._launch_health_token

    def replace():
        resumed = db.get_agent(a.id)
        lh.prepare(db, resumed, "new-account", argv_task=True)

    def readiness(self, target):
        if replacement_point == "readiness":
            replace()
        return True

    monkeypatch.setattr(type(agents.get_provider(a.provider)), "after_launch", readiness)
    monkeypatch.setattr(agents, "ready", lambda *args: replace() if replacement_point == "ready" else None)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    cli.after_launch_cmd(a.id, token=old_token)
    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
    assert row["token"] != old_token and row["delivered_at"] is None and row["outcome"] is None
    assert started == [] and reports == []


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
        assert row["argv_task"] == 0
        assert row["submitted_prompt"] == ""
    finally:
        migrated.conn.close()


@pytest.mark.parametrize("reply,cause", [
    ("API Error: 529 Overloaded", "launch_capacity"),
    ("Here is the fixture:\nAPI Error: 401 invalid x-api-key", None),
])
def test_headless_runner_context_preserves_errors_and_quoted_output(db, health, monkeypatch, reply, cause):
    from brindle import model_access
    lh, a, _, _, _, reports = health
    db.update_agent(a.id, provider="claude", headless=1)
    baseline = f"── brindle: turn 1 ──\n{a.task}\n"
    monkeypatch.setattr(lh, "capture", lambda a: baseline + reply)
    lh.delivered(db, a, baseline)
    assert lh.inspect(db, a.id) == ("failed" if cause else "pending")
    refusal = model_access.refused(a.profile, fp="effective-account")
    assert (refusal.key if refusal else None) == cause
    assert len(reports) == bool(cause)


@pytest.mark.parametrize("reply,cause", [
    ("• Working (2s • esc to interrupt)\n■ unexpected status 403 Forbidden", "launch_auth"),
    ("■ unexpected status 403 Forbidden\nThe retry succeeded.", None),
])
def test_codex_progress_and_response_context(db, health, monkeypatch, reply, cause):
    from brindle import model_access
    lh, a, _, _, _, reports = health
    screen = f"OpenAI Codex\n› {a.task}\n{reply}\n› \n100% context left"
    monkeypatch.setattr(lh, "capture", lambda a: screen)
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) == ("failed" if cause else "pending")
    refusal = model_access.refused(a.profile, fp="effective-account")
    assert (refusal.key if refusal else None) == cause
    assert len(reports) == bool(cause)


@pytest.mark.parametrize("mutation", ["result", "owner"])
def test_health_capture_cannot_report_after_agent_changes(db, health, monkeypatch, mutation):
    from brindle import model_access
    lh, a, _, _, _, reports = health
    owns = [True]
    monkeypatch.setattr(agents, "owns_pane", lambda *a: owns[0])

    def capture(agent):
        if mutation == "result":
            db.update_agent(a.id, result="done")
        else:
            owns[0] = False
        return "■ unexpected status 403 Forbidden"

    monkeypatch.setattr(lh, "capture", capture)
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) is None
    assert reports == []
    assert model_access.refused(a.profile, fp="effective-account") is None
    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] is None


def test_health_never_captures_a_reassigned_pane(db, health, monkeypatch):
    lh, a, _, _, _, reports = health
    monkeypatch.setattr(agents, "owns_pane", lambda *a: False)
    monkeypatch.setattr(lh, "capture", lambda *a: pytest.fail("read someone else's pane"))
    lh.delivered(db, a)
    assert lh.inspect(db, a.id) is None and reports == []


def test_delivery_capture_binds_launch_before_concurrent_resume(db, health, monkeypatch):
    lh, a, _, _, started, _ = health
    fresh = db.get_agent(a.id)  # flush/runner loads an Agent without an in-memory token

    def capture(agent):
        lh.prepare(db, db.get_agent(a.id), "replacement")
        return "old screen"

    monkeypatch.setattr(lh, "capture", capture)
    lh.delivered(db, fresh, lh.before_delivery(db, fresh))
    assert started == []
    assert db.conn.execute("SELECT delivered_at FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()[0] is None


@pytest.mark.parametrize("provider,screen,cause", [
    ("claude", "Claude Code v2.1.0\n● API Error: 400 The selected model\n  is not available for this account", "bedrock_quota"),
    ("claude", "Claude Code v2.1.0\n● Example:\n  API Error: 400 The selected model\n  is not available for this account", None),
    ("claude", "Claude Code v2.1.0\n⏺ There's an issue with the selected model (us.anthropic.claude-haiku-4-5-v1:0).\n"
               "  It may not exist or you may not have access to it.", "launch_model"),
])
def test_wrapped_provider_blocks_require_provider_context(provider, screen, cause):
    from brindle import launch_health
    found = launch_health.failure(screen, provider=provider)
    assert (found.key if found else None) == cause


def test_concurrent_refusals_and_notices_preserve_every_update(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from brindle import model_access
    save = model_access._save
    barrier = threading.Barrier(8)

    def slow_save(data):
        time.sleep(0.01)  # force overlapping writers without the process lock
        save(data)

    monkeypatch.setattr(model_access, "_save", slow_save)

    def record(i):
        barrier.wait(timeout=5)
        model_access.refuse(f"worker-{i}", model_access.LAUNCH_AUTH, fp=f"identity-{i}")
        return model_access.first_time("same-notice")

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(record, range(8))) == 1
    assert all(model_access.refused(f"worker-{i}", fp=f"identity-{i}") == model_access.LAUNCH_AUTH
               for i in range(8))


@pytest.mark.parametrize("provider,screen", [
    ("claude", "❯ Test these failure messages:\nesc to interrupt\n● API Error: 401 invalid API key"),
])
def test_provider_names_and_keyboard_help_in_prose_are_not_ui(provider, screen):
    from brindle import launch_health
    assert launch_health.failure(screen, provider=provider) is None


def test_real_codex_footer_arms_delivery_followup_without_notify(db, root, launches, detached, monkeypatch,
                                                               isolated_health_clock):
    from brindle import launch_health as lh, model_access
    boss, ws = root
    now, sleeps, reports = [100.0], [], []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    monkeypatch.setattr(lh.time, "time", lambda: now[0])
    monkeypatch.setattr(lh.time, "sleep", sleep)
    monkeypatch.setattr(agents, "same_server", lambda *a: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    monkeypatch.setattr(agents, "is_alive", lambda *a: True)
    monkeypatch.setattr(agents, "send_message", lambda db, to, body: reports.append(body))
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CODEX_EMPTY_PANE)
    monkeypatch.setattr(cli, "_helper_db", lambda: db)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    a = agents.spawn(db, ws, "developer", provider_name="codex", prompt="real task",
                     parent_id=boss.id, mode="assign", background_setup=True)
    cli.after_launch_cmd(a.id, token=a._launch_health_token)
    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
    assert row["delivered_at"] == 100.5 and row["outcome"] is None
    assert len([cmd for cmd in detached if "_launch-health" in cmd and "--startup" not in cmd]) == 1
    assert sum("real task" in arg for arg in launches[-1]) == 1
    monkeypatch.setattr(lh, "capture", lambda a: "■ unexpected status 403 Forbidden\n" + CODEX_EMPTY_PANE)
    assert lh.inspect(db, a.id, token=a._launch_health_token) == "failed"
    assert sleeps == [0.5, 5.0] and now[0] == 105.5
    assert len(reports) == 1 and model_access.refused(a.profile, fp=row["fingerprint"]) == lh.AUTH


def test_resumed_startup_failure_observes_new_pane_before_delivery(db, health, monkeypatch):
    lh, a, _, _, _, reports = health
    ws = db.get_workspace(a.workspace_id)
    db.update_agent(a.id, tmux_window="%old", headless=1)
    a = db.get_agent(a.id)
    monkeypatch.setattr(type(agents.get_provider(a.provider)), "after_launch", lambda *a: False)
    # A native runner uses the headless launch path without invoking a CLI.
    db.update_agent(a.id, provider="native")
    a.provider = "native"
    def open_window(db, agent, ws, name, argv, watch_pane):
        row = db.conn.execute("SELECT pane FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
        assert row["pane"] == ""  # prepare did not inherit %old
        db.update_agent(a.id, tmux_window="%new")
        agent.tmux_window = "%new"
        return "%new"
    monkeypatch.setattr(agents, "_open_window", open_window)
    monkeypatch.setattr(agents, "is_alive", lambda *a: False)
    seen = []
    monkeypatch.setattr(lh, "capture", lambda a: seen.append(a.tmux_window) or "Error: authentication failed")
    agents._launch(db, a, ws, prompt="real task", resume="saved", watch_pane=False)
    assert lh.inspect(db, a.id, wait=False, token=a._launch_health_token) == "failed"
    assert seen == ["%new"] and len(reports) == 1


def test_new_pane_publication_and_early_delivery_schedule_once(db, health, monkeypatch):
    lh, a, now, sleeps, started, reports = health
    db.update_agent(a.id, tmux_window="%old")
    a = db.get_agent(a.id)
    lh.prepare(db, a, "resumed-account", argv_task=True)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    monkeypatch.setenv("TMUX_PANE", "%new")
    monkeypatch.setattr(tmux, "current_server", lambda: "new-server")
    seen = []
    monkeypatch.setattr(lh, "capture", lambda a: seen.append((a.tmux_window, a.tmux_server)) or "new baseline")
    lh.delivered(db, db.get_agent(a.id), lh.before_delivery(db, a))
    assert started == [] and lh.inspect(db, a.id, wait=False) is None
    now[0] += 2
    db.update_agent(a.id, tmux_window="%new", tmux_server="new-server")
    a.tmux_window = "%new"
    lh.bind(db, a, "%new")
    lh.bind(db, a, "%wrong")
    lh.delivered(db, a)
    assert len(started) == 1 and seen == [("%new", "new-server")]
    assert lh.inspect(db, a.id) == "pending"
    assert sleeps == [3.0] and reports == []
    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
    assert row["pane"] == "%new" and row["baseline"] == "new baseline"


def test_old_launch_cannot_bind_replacement_pane(db, health):
    lh, old, _, _, started, _ = health
    replacement = db.get_agent(old.id)
    lh.prepare(db, replacement, "replacement")
    lh.bind(db, old, "%old")
    row = db.conn.execute("SELECT pane FROM launch_health WHERE agent_id=?", (old.id,)).fetchone()
    assert row["pane"] == "" and started == []


@pytest.mark.parametrize("stamp,sid,outcome", [
    ("1970-01-01T00-01-40-800Z", "sess-1", "mcp-failed"),     # same second as the launch
    ("1970-01-01T00-01-40-800Z", "other-session", None),      # another session's failure
    ("1970-01-01T00-00-10-000Z", "sess-1", None),             # a log from before this launch
])
def test_failed_brindle_mcp_start_is_told_to_the_supervisor(db, health, monkeypatch, tmp_path,
                                                            stamp, sid, outcome):
    lh, a, now, _, _, reports = health
    now[0] = 100.5
    lh.prepare(db, a, "effective-account")      # launched at t=100.5s
    monkeypatch.setenv("HOME", str(tmp_path))
    db.update_agent(a.id, provider="claude", session_ref="sess-1")
    logs = lh.mcp_log_dir(db.get_workspace(a.workspace_id).path)
    logs.mkdir(parents=True)
    (logs / f"{stamp}.jsonl").write_text(
        json.dumps({"debug": "Starting connection", "sessionId": sid}) + "\n"
        + json.dumps({"error": "Connection timeout triggered", "sessionId": sid}) + "\n")

    agents.handle_hook(db, a.id, "tool-done", {})
    agents.handle_hook(db, a.id, "tool-done", {})   # reported once

    assert db.conn.execute("SELECT outcome FROM launch_health WHERE agent_id=?",
                           (a.id,)).fetchone()[0] == outcome
    assert [to for to, _ in reports] == (["boss"] if outcome else [])
