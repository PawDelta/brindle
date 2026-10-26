import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import sh

from brindle import agents, bus, tmux, workspaces
from brindle.db import Agent
from brindle.providers import ClaudeCode, LaunchContext
from brindle.profiles import load_profile


def fake_agent(db, ws, status="processing", mode="interactive", parent=None, agent_id="a1"):
    a = Agent(agent_id, ws.id, "developer", "claude", parent, mode, status, "@0", None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def _unreported_setup(db, ws, monkeypatch):
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0",
                       None, time.time()))
    fake_agent(db, ws, mode="assign", parent="boss")
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: sent.append(a))
    return sent


def test_no_unreported_alert_while_a_long_command_runs(db, ws, monkeypatch):
    sent = _unreported_setup(db, ws, monkeypatch)
    running = {"on": True}
    monkeypatch.setattr(agents, "command_running", lambda db_, a: running["on"])
    agents.handle_hook(db, "a1", "stop", {})                              # reminded
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})      # turn ends, command going
    assert sent == [] and not db.get_agent("a1").unreported_noted
    db.update_agent("a1", result="done")                                  # it later reports
    agents.handle_hook(db, "a1", "stop", {})
    assert sent == []


def test_unreported_alert_still_fires_once_when_nothing_is_running(db, ws, monkeypatch):
    sent = _unreported_setup(db, ws, monkeypatch)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    (Path(ws.path) / "parser.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm wip", ws.path)
    agents.handle_hook(db, "a1", "stop", {})
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert len(sent) == 1 and sent[0][1] == "boss"
    assert "parser.py" in sent[0][2]   # what it did, so the supervisor needn't go and look


def test_unreported_notice_repeats_after_resume(db, ws, monkeypatch):
    sent = _unreported_setup(db, ws, monkeypatch)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    agents.handle_hook(db, "a1", "prompt-submit", {})   # its supervisor's message resumes it
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert len(sent) == 2


def test_unreported_brief_resumes_while_stopped_give_one_alert(db, ws, monkeypatch):
    sent = _unreported_setup(db, ws, monkeypatch)
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    for _ in range(3):   # agy polling a command: stop, then a tool call, over and over
        agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
        agents.handle_hook(db, "a1", "pre-tool", {})
    assert len(sent) == 1


def test_command_running_sees_a_command_under_an_antigravity_worker_so_not_stopped(db, ws, monkeypatch):
    from brindle import procs

    P = procs.Proc
    table = {
        10: P(10, 1, "-zsh BRINDLE_AGENT_ID=a1"),
        11: P(11, 10, "/home/u/.local/bin/agy --model x BRINDLE_AGENT_ID=a1"),
        12: P(12, 11, "python -m pytest -q tests/test_codex.py"),
        # the Stop hook running this check: a child of the CLI, beside the test run
        os.getpid(): P(os.getpid(), 11, "python -m brindle hook stop"),
    }
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(agents, "same_server", lambda a: True)
    monkeypatch.setattr(agents, "owns_pane", lambda d, a: True)
    monkeypatch.setattr(agents.tmux, "window_pids", lambda w: [10])
    sent = _unreported_setup(db, ws, monkeypatch)
    db.update_agent("a1", provider="antigravity")
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert sent == []   # the test run is going: not stopped
    del table[12]
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert len(sent) == 1   # just the CLI left: stopped


def test_running_commands_ignores_the_cli_and_shells_but_sees_a_test_run():
    from brindle import procs

    P = procs.Proc
    table = {
        1: P(1, 0, "tmux: server"),
        10: P(10, 1, "-zsh BRINDLE_AGENT_ID=a1"),
        11: P(11, 10, "claude --model x BRINDLE_AGENT_ID=a1"),
        12: P(12, 11, "/bin/zsh -c uv run pytest"),
        13: P(13, 12, "python -m pytest -q"),
    }
    assert procs.running_commands("a1", [10], table) == [table[13]]
    del table[13]
    assert procs.running_commands("a1", [10], table) == []


def test_prompt_and_notification_hooks(db, ws):
    fake_agent(db, ws, status="idle")
    agents.handle_hook(db, "a1", "prompt-submit", {})
    assert db.get_agent("a1").status == "processing"
    agents.handle_hook(db, "a1", "notification", {"message": "Claude needs your permission to use Bash"})
    assert db.get_agent("a1").status == "waiting"


CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
# Real Claude Code 2.1.283 layouts: a blank line sits between the box's top
# border and the status line above it (and another below the bottom
# border). The status line shows a spinner while a turn runs, in shapes
# ranging from a bare verb to one with token/timing detail, and a "done
# HH:MM" marker once it ends. Older versions instead said "esc to interrupt"
# in the footer below the box.
BOX = "\n────\n❯ \n────\n\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
CLAUDE_BUSY = "✻ Tomfoolering… (7m 22s · ↓ 35.0k tokens · thinking)" + BOX
# An exact capture of a real busy pane (2.1.283), truncated transcript line
# and all: it must not be mistaken for the spinner above the box. Shared
# with test_reliability.py.
CLAUDE_BUSY_REAL_CAPTURE = (
    '     os.environ.setdefault("GIT_COMMITTER_EMAIL", "t@example.c…\n'
    "\n"
    "✽ Hashing… (2m 53s · ↓ 7.6k tokens · thinking)\n"
    "\n"
    "────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────\n"
    "\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
)
# The quote sits with no blank-line padding at all right above the box;
# only the anchored regex (not distance from the box) keeps this idle.
CLAUDE_QUOTED_BUSY_NO_PADDING = (
    "⏺ It shows \"Tomfoolering… (7m 22s · ↓ 35.0k tokens)\" while busy.\n"
    "────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ? for shortcuts\n"
)
CLAUDE_PROMPT = " Bash command\n   pytest\n This command requires approval\n\n Do you want to proceed?\n ❯ 1. Yes\n   4. No\n\n Esc to cancel\n"


@pytest.mark.parametrize("screen,want", [
    (CLAUDE_IDLE, "idle"), (CLAUDE_QUOTED_BUSY_NO_PADDING, "idle"),
    (CLAUDE_BUSY_REAL_CAPTURE, "busy"), (CLAUDE_PROMPT, "waiting"),
])
def test_claude_screen_state(screen, want):
    assert ClaudeCode().screen_state(screen) == want


@pytest.mark.parametrize("screen,want", [
    (CLAUDE_BUSY_REAL_CAPTURE, True),
])
def test_claude_busy_in_footer(screen, want):
    assert ClaudeCode().busy_in_footer(screen) is want


# The real agent-view screen: its footer ("ctrl+x to delete") is the reliable
# anchor, not the loose "moved to the background" phrasing, which can appear
# quoted in an ordinary transcript (see CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES).
CLAUDE_BACKGROUND = (
    "Your conversation moved to the background — enter opens it · esc returns to it\n"
    "────\n❯ describe a task for a new session\n────\n"
    "⏵⏵ auto mode · enter to open · space to reply · ctrl+x to delete · ? for shortcuts\n"
)
# A transcript that happens to quote both telltale phrases, above the last
# border: must NOT be mistaken for the real agent-view footer.
CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES = (
    '⏺ I told them: "Your conversation moved to the background" and to press\n'
    '  "ctrl+x to delete" if they wanted out.\n'
    "────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt\n"
)
CLAUDE_PLACEHOLDER_GREY_256 = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[38;5;244mTry "fix the flaky test in test_agents.py"\x1b[0m\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)
# A dim placeholder styled with a truecolor (38;2;r;g;b) grey instead of the
# 256-color palette.
CLAUDE_PLACEHOLDER_TRUECOLOR_GREY = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[38;2;128;128;128mTry "fix the flaky test in test_agents.py"\x1b[0m\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)
# The terminal cursor sits on the first cell in inverse video (SGR 7); it must
# be ignored so real typed text right after it is still detected.
CLAUDE_TYPING_WITH_INVERSE_CURSOR = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[7mh\x1b[27mello\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)


@pytest.mark.parametrize("screen,interactive,want", [
    (CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES, True, None),  # not the real footer
    (CLAUDE_PLACEHOLDER_GREY_256, True, None),  # grey 256-colour placeholder
    (CLAUDE_PLACEHOLDER_TRUECOLOR_GREY, True, None),  # grey truecolor placeholder
    (CLAUDE_TYPING_WITH_INVERSE_CURSOR, True, "typing"),  # ignore the inverse-video cursor cell
    ("❯\n" + "continued draft\n" * 40 + "────\n? for shortcuts", True, "typing"),
])
def test_claude_paste_blocked(screen, interactive, want):
    assert ClaudeCode().paste_blocked(screen, interactive) == want


def test_flush_keeps_message_queued_over_background_session_launcher(db, ws, monkeypatch):
    # A worker too: a blind paste there would start a NEW background session
    # instead of reaching this one.
    fake_agent(db, ws, status="idle", mode="assign")
    db.enqueue("a1", "queued message", None)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_BACKGROUND)
    sent, pasted = [], []
    monkeypatch.setattr(tmux, "send_keys", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.flush(db, "a1") is False
    assert sent and sent[0][1] == "Escape"  # tried backing out before giving up
    assert not pasted
    assert db.pending_count("a1") == 1


def test_reconcile_keeps_hook_status_when_screen_is_unclear(db, ws, monkeypatch):
    fake_agent(db, ws, status="processing")
    screens = iter([CLAUDE_IDLE, CLAUDE_BUSY])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: next(screens))
    assert agents.reconcile(db, db.get_agent("a1"), gap=0).status == "processing"


def test_handoff_wait_is_bounded_and_detaches(db, ws, monkeypatch):
    from pathlib import Path

    (Path(ws.repo_root) / ".brindle").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".brindle" / "config.json").write_text('{"pipeline": false}')
    from brindle import mcp_server

    fake_agent(db, ws, status="processing", agent_id="boss")
    fake_agent(db, ws, mode="handoff", parent="boss", agent_id="w1")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(agents, "kill", lambda db, aid: db.delete_agent(aid))

    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "still running" in out and "wait_for_worker" in out
    assert db.get_agent("w1").mode == "handoff_detached"

    # Finishing later forwards the result to the supervisor's inbox...
    agents.report_result(db, "w1", "done later")
    assert db.pending_count("boss") == 1
    assert [(e["kind"], e["agent_id"], e["payload"].get("sender_id"))
            for e in bus.tail(db, kinds=("agent.result", "message.queued"))] == [
        ("agent.result", "w1", None), ("message.queued", "boss", "w1")]
    # ...and if the supervisor collects it directly, the duplicate is dropped.
    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "done later" in out
    assert db.pending_count("boss") == 0
    assert db.get_agent("w1") is None  # handoff worker closed after collection


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_watch_pane_shares_the_window_and_messages_reach_the_agent(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell", watch_pane=True)
    try:
        assert a.tmux_window.startswith("%")  # a pane, not a window
        panes = tmux._tmux("list-panes", "-t", a.tmux_window, "-F", "#{pane_id}").stdout.split()
        assert len(panes) == 2 and a.tmux_window in panes
        # Even with the dashboard pane focused, messages go to the agent's pane.
        other = next(p for p in panes if p != a.tmux_window)
        tmux._tmux("select-pane", "-t", other)
        agents.send_message(db, a.id, "echo reached-$BRINDLE_AGENT_ID")
        deadline = time.time() + 5
        while time.time() < deadline and f"reached-{a.id}" not in tmux.capture(a.tmux_window):
            time.sleep(0.2)
        assert f"reached-{a.id}" in tmux.capture(a.tmux_window)
    finally:
        tmux.kill_session(ws.tmux_session)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_find_running_reuses_a_live_interactive_agent(db, ws):
    assert agents.find_running(db, ws, "developer") is None
    a = agents.spawn(db, ws, "developer", provider_name="shell")
    try:
        assert agents.find_running(db, ws, "developer").id == a.id
        assert agents.find_running(db, ws, "reviewer") is None
    finally:
        tmux.kill_session(ws.tmux_session)
    assert agents.find_running(db, ws, "developer") is None  # dead agents don't count


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_chat_exit_closes_its_session_even_with_extra_windows(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell", watch_pane=True)
    tmux._tmux("new-window", "-d", "-t", f"={ws.tmux_session}:", "-n", "extra")  # something the user opened
    tmux.send_keys(a.tmux_window, "exit", "Enter")
    deadline = time.time() + 30
    while time.time() < deadline and tmux.has_session(ws.tmux_session):
        time.sleep(0.2)
    assert not tmux.has_session(ws.tmux_session)
    assert db.get_agent(a.id).status == "paused"


def test_workers_test_their_change_and_leave_the_full_suite_to_checks(db, ws, monkeypatch):
    from brindle.providers import get_provider

    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, None, get_provider("claude"), headless=False)
    assert "run only the tests that cover your change" in prompt
    assert "Run the full suite once, just before you commit." in prompt
    (Path(ws.repo_root) / ".brindle").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".brindle" / "config.json").write_text('{"checks": ["uv run pytest -q"]}')
    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, None, get_provider("claude"), headless=False)
    assert "Don't run the full suite yourself" in prompt and "`uv run pytest -q`" in prompt
    sub = agents.subagent_prompt("You are a subagent.", "add a flag", ws, None)
    assert "Don't run the full suite yourself" in sub
    agents_dir = Path(ws.repo_root) / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "my-auditor.md").write_text("---\nname: my-auditor\nextends: backend-auditor\n---\nAudit.\n")
    for name, review in [("reviewer-gemini", True), ("developer", False),
                         ("my-auditor", True),   # reviewer -> backend-auditor -> my-auditor
                         ("no-such-profile", False)]:
        assert agents.is_review_profile(name, ws.repo_root) is review
    # A review profile gets the report-only footer, whichever way it is launched.
    seen = []

    class Launched(Exception):
        pass

    def decorate(*a, report_only=False, **k):
        seen.append(report_only)
        raise Launched

    monkeypatch.setattr(agents, "decorate_worker_prompt", decorate)
    monkeypatch.setattr("brindle.providers.trust_folder", lambda path: None)
    for name in ("my-auditor", "developer"):
        with pytest.raises(Launched):
            agents.spawn(db, ws, name, prompt="hi", mode="assign")
    assert seen == [True, False]


def test_no_goal_command_when_hooks_are_disabled(db, ws):
    from brindle.providers import get_provider

    (Path(ws.path) / ".claude").mkdir(exist_ok=True)
    (Path(ws.path) / ".claude" / "settings.local.json").write_text('{"disableAllHooks": true}')
    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, "tests pass", get_provider("claude"), headless=False)
    assert not prompt.startswith("/goal")
    assert "Finish line: tests pass" in prompt



def test_add_dir_is_never_the_last_flag():
    """--add-dir is variadic, so a flag must follow the last one.

    If it were last, Claude Code would read the initial prompt as another
    directory and the worker would start with no task and no error.
    """
    from dataclasses import replace

    profile = replace(load_profile("developer"), add_dirs=["/srv/cache", "/srv/refs"])
    argv = ClaudeCode().command(LaunchContext("abc", profile, "do the thing"))
    last = max(i for i, a in enumerate(argv) if a == "--add-dir")
    assert argv[last + 2].startswith("--"), argv[last:]


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_supervisor_resume_keeps_prompt(db, ws, monkeypatch, provider):
    agent = Agent("boss", ws.id, "supervisor", provider, None, "interactive", "idle", "@0", None, time.time())
    db.add_agent(agent)
    monkeypatch.setenv("BRINDLE_CODEX_BIN", "/bin/codex")
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    monkeypatch.setattr(agents, "launch_blockers", lambda *a, **k: [])
    launched = []

    def open_window(db, agent, ws, name, argv, *a, **k):
        launched.extend(argv)
        raise RuntimeError("launch captured")

    monkeypatch.setattr(agents, "_open_window", open_window)
    with pytest.raises(RuntimeError, match="launch captured"):
        agents._launch(db, agent, ws, prompt="continue the task", resume="saved-session",
                       watch_pane=False, model_checked=True)
    queued = [m.body for m in iter(lambda: db.pop_pending(agent.id), None)]
    assert "saved-session" in launched
    delivered = "\n".join([*launched, *queued])
    assert load_profile("supervisor", ws.repo_root).prompt.strip() in delivered
    assert "continue the task" in delivered


def test_repo_read_dirs_become_read_only_allow_rules(ws):
    """worker_extra_read_dirs reach Claude Code as Read allow rules, never --add-dir (which also writes)."""
    from pathlib import Path

    (Path(ws.repo_root) / ".brindle").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".brindle" / "config.json").write_text('{"worker_extra_read_dirs": ["results", "/srv/data/"]}')
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer", ws.repo_root), "do the thing"))
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert f"Read(/{Path(ws.repo_root).resolve()}/results/**)" in allowed and "Read(//srv/data/**)" in allowed
    assert "--add-dir" not in argv


def test_spawn_reports_a_missing_add_dir_once(db, ws, monkeypatch, capsys):
    """Claude Code ignores an --add-dir that does not exist, so launch says so,
    once, however many times the profile is loaded on the way."""
    from pathlib import Path

    class Launched(Exception):
        pass

    def stop(*a, **k):
        raise Launched

    config = Path(ws.repo_root) / ".brindle" / "config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text('{"add_dirs": ["/no/such/cache"]}')
    # Stopped at the window, so every profile load on the way (spawn's own and
    # _profile_for's) has happened.
    monkeypatch.setattr(agents, "_open_window", stop)
    monkeypatch.setattr("brindle.providers.trust_folder", lambda path: None)

    with pytest.raises(Launched):
        agents.spawn(db, ws, "developer", prompt="hi", mode="assign")
    assert capsys.readouterr().err.count("/no/such/cache") == 1


