import functools
import os
import shutil
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import sh

from brindle import agents, bus, tmux, workspaces
from brindle.db import DB, Agent
from brindle.providers import ClaudeCode, LaunchContext
from brindle.profiles import load_profile


def fake_agent(db, ws, status="processing", mode="interactive", parent=None, agent_id="a1"):
    a = Agent(agent_id, ws.id, "developer", "claude", parent, mode, status, "@0", None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture(autouse=True)
def back_to_back_screen_reads(monkeypatch):
    """send_message reconciles its target's status from two screen reads
    0.7s apart; here they're taken back to back (still two, still compared)."""
    monkeypatch.setattr(agents, "reconcile", functools.partial(agents.reconcile, gap=0))


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


def _no_turn_work(monkeypatch):
    """Each stop's turn snapshot and conflict forecast are git work. The first
    stop of test_no_unreported_alert_while_a_long_command_runs (no changes yet)
    and the first stops of test_unreported_alert_still_fires_once_when_nothing_is_running
    (after a commit) run them for real; the many stops after those skip them."""
    from brindle import tasks
    monkeypatch.setattr(agents, "snapshot_turn", lambda db_, a: None)
    monkeypatch.setattr(tasks, "warn_predicted_conflicts", lambda db_, a: None)


def test_no_unreported_alert_while_a_long_command_runs(db, ws, monkeypatch):
    sent = _unreported_setup(db, ws, monkeypatch)
    running = {"on": True}
    monkeypatch.setattr(agents, "command_running", lambda db_, a: running["on"])
    agents.handle_hook(db, "a1", "stop", {})                              # reminded
    _no_turn_work(monkeypatch)   # its first stop ran it on a branch with no changes yet
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
    _no_turn_work(monkeypatch)
    for _ in range(3):   # agy polling a command: brief resumes between stops give no new alert
        agents.handle_hook(db, "a1", "pre-tool", {})
        agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert len(sent) == 1
    agents.handle_hook(db, "a1", "prompt-submit", {})   # its supervisor's message resumes it
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert len(sent) == 2


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
    _no_turn_work(monkeypatch)
    db.update_agent("a1", provider="antigravity")
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert sent == []   # the test run is going: not stopped
    del table[12]
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    assert len(sent) == 1   # just the CLI left: stopped


@pytest.mark.variant("permission notification hook drives the waiting transition")
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
    pytest.param(CLAUDE_QUOTED_BUSY_NO_PADDING, "idle",
                 marks=pytest.mark.variant("busy text quoted with no padding above the box: anchored regex")),
    (CLAUDE_BUSY_REAL_CAPTURE, "busy"),
    pytest.param(CLAUDE_PROMPT, "waiting",
                 marks=pytest.mark.variant("approval prompt: the only screen-based waiting state")),
])
def test_claude_screen_state(screen, want):
    assert ClaudeCode().screen_state(screen) == want
    if want == "busy":
        assert ClaudeCode().busy_in_footer(screen)


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
    pytest.param(CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES, True, None,
                 marks=pytest.mark.variant("transcript quoting the footer phrases is not the real footer")),
    (CLAUDE_PLACEHOLDER_GREY_256, True, None),  # grey 256-colour placeholder
    (CLAUDE_PLACEHOLDER_TRUECOLOR_GREY, True, None),  # grey truecolor placeholder
    pytest.param(CLAUDE_TYPING_WITH_INVERSE_CURSOR, True, "typing",
                 marks=pytest.mark.variant("inverse-video cursor cell must be ignored")),
    pytest.param("❯\n\n" + "continued draft\n" * 40 + "────\n? for shortcuts", True, "typing",
                 marks=pytest.mark.variant("long draft after a blank row")),
])
def test_claude_paste_blocked(screen, interactive, want):
    assert ClaudeCode().paste_blocked(screen, interactive) == want


@pytest.mark.parametrize("options", [
    pytest.param(["1. Yes", "2. Don't show again", "3. Not now"],
                 marks=pytest.mark.variant("decline label moves: never choose by menu position")),
    pytest.param(["1. Yes", "2. Not now"],
                 marks=pytest.mark.variant("no 'Don't show again': falls back to the second option")),
    ["1. Yes", "2. More information"],
])
def test_teach_auto_mode_menu_is_declined_never_typed_into(db, ws, monkeypatch, options):
    from brindle import cull, prompt_answer
    from test_prompt_answer import Pane

    question = ("Teach auto mode about your environment?\n\n"
                "Auto mode works better when it knows your environment. Takes about a minute.")
    for mode in ("assign", "interactive"):
        aid = mode
        fake_agent(db, ws, status="idle", mode=mode, parent="boss", agent_id=aid)
        pane = Pane(question, options)
        render = pane.render
        monkeypatch.setattr(pane, "render", lambda: CLAUDE_IDLE if pane.chosen else
                            render() + "\nEnter to confirm · Esc to cancel\n⏵⏵ auto mode on\n")
        monkeypatch.setattr(tmux, "capture", lambda *a, **k: pane.render())
        monkeypatch.setattr(tmux, "send_keys", pane.press)
        monkeypatch.setattr(ClaudeCode, "hooks_disabled", lambda *a: None)
        pasted, told = [], []
        monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
        monkeypatch.setattr(agents, "send_message", lambda *a, **k: told.append(a))
        monkeypatch.setattr(agents, "pane_owners", lambda *a: {})
        monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
        monkeypatch.setattr(agents, "is_alive", lambda *a: True)
        db.enqueue(aid, "queued message", None)
        screen = pane.render()
        assert ClaudeCode().paste_blocked(screen, mode == "interactive") is not None
        assert not ClaudeCode().input_ready(screen)
        assert not agents.flush(db, aid) and not pasted
        monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen)
        for _ in range(2):  # a redraw has not arrived: only one key may go in
            agents.screen_status(db, db.get_agent(aid), samples=2, gap=0)
        declines = any("Not now" in o or "Don't show again" in o for o in options)
        assert pane.keys == (["Down"] if declines else [])
        assert not prompt_answer.at_turn_end(db, db.get_agent(aid))
        cull.decline_environment_setups(db, {})
        assert not cull.note_stuck(db, time.time(), {})
        assert len(told) == (0 if declines else 1)
        monkeypatch.setattr(tmux, "capture", lambda *a, **k: pane.render())
        for _ in range(4):
            cull.decline_environment_setups(db, {})
        if declines:
            expected = next((o for o in options if "Don't show again" in o), options[1])
            assert pane.chosen == expected
            assert pane.keys == ["Down"] * options.index(expected) + ["Enter"]
            agents.flush(db, aid)
            assert pasted == ["queued message"] and db.pending_count(aid) == 0
            keys = []
            monkeypatch.setattr(tmux, "send_keys", lambda *a, **k: keys.append(a))
            for draft in ("", "Don't show again", "another draft", "\x1b[2mTry a task\x1b[0m", "first line\ncontinued draft"):
                quoted_options = "\n".join(options) if draft == "another draft" else ""
                quoted = question + "\n" + quoted_options + "\n" + CLAUDE_IDLE.replace("❯ ", "❯ " + draft)
                monkeypatch.setattr(tmux, "capture", lambda *a, **k: quoted)
                handled = prompt_answer.decline_teach_auto_mode(db, db.get_agent(aid), quoted)
                assert keys == []
                assert not handled
                assert ClaudeCode().input_ready(quoted)
                assert ClaudeCode().paste_blocked(quoted, False) is None
        else:
            assert pane.keys == [] and pane.chosen is None
            assert not pasted and db.pending_count(aid) == 1
            assert len(told) == 1
            quoted_option = "9. Don't show again\n" + screen
            monkeypatch.setattr(tmux, "capture", lambda *a, **k: quoted_option)
            assert prompt_answer.decline_teach_auto_mode(db, db.get_agent(aid), quoted_option)
            assert pane.keys == []
        db.delete_agent(aid)


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


_REAL_PANE_INPUT = getattr(tmux, "pane_input", None)   # before conftest's nobody_typing


@pytest.mark.parametrize("provider,fault", [
    pytest.param("claude", "unreadable", marks=pytest.mark.variant("pane gone: capture raises")),
    ("claude", "draft"), ("codex", "draft"),
    ("claude", "typed")])
def test_paste_blocked_or_draft_holds_message_then_delivers_it_once(db, ws, monkeypatch, provider, fault):
    fake_agent(db, ws, status="processing" if provider == "codex" else "idle", mode="interactive")
    db.update_agent("a1", provider=provider)
    db.enqueue("a1", "queued message", None)
    clean, draft = {"claude": (CLAUDE_IDLE, CLAUDE_TYPING_WITH_INVERSE_CURSOR), "codex": ("› \n? for shortcuts\n", "› hello\n? for shortcuts\n")}[provider]
    s, pasted = {"screen": None if fault == "unreadable" else clean, "arm": fault == "draft"}, []
    # "typed": the person is typing a reply into the chat, but the loaded CLI
    # hasn't drawn their keys yet; tmux saw them (the person's own client).
    s["typed"] = time.time() if fault == "typed" else None
    if fault == "typed":
        from types import SimpleNamespace
        real_tmux = tmux._tmux

        def tmux_call(*args, **k):
            if args[0] != "list-clients":
                return real_tmux(*args, **k)
            rows = f"%9\t{int(time.time())}\n@0\t{int(s['typed'])}\n"   # another pane's keys don't count
            return SimpleNamespace(returncode=0, stdout=rows, stderr="")
        monkeypatch.setattr(tmux, "pane_input", _REAL_PANE_INPUT, raising=False)
        monkeypatch.setattr(tmux, "_tmux", tmux_call)

    def capture(*a, **k):
        if s["screen"] is None:
            raise tmux.TmuxError("gone")
        return s["screen"]

    def prepare_lead(*a, **k):   # a draft appears during preparation, after the claim
        s["screen"] = draft if s.pop("arm", False) else s["screen"]
        return ""
    monkeypatch.setattr(tmux, "capture", capture)
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    monkeypatch.setattr(agents, "message_lead", prepare_lead)
    assert agents.flush(db, "a1") is False and not pasted and db.pending_count("a1") == 1
    s["screen"] = clean   # the draft is sent or cleared
    s["typed"] = s["typed"] and time.time() - 60
    assert agents.flush(db, "a1") is True and pasted == ["queued message"]


@pytest.mark.parametrize("typed_after,then", [
    ("load-buffer", "cleared"),
    pytest.param("body", "sent", marks=pytest.mark.variant("person sends the stopped report after the body is written")),
    ("body", "codex turn"),   # Codex reports what went in only when the turn ends
    # cleared, then something else sent before the next flush
    pytest.param("body", "replaced", marks=pytest.mark.variant("replacement sent after a clear")),
    # agy reports no prompt text: once it starts a turn, sent and replaced look alike
    pytest.param("body", "agy sent", marks=pytest.mark.variant("agy reports no prompt text")),
    pytest.param("body", "agy cleared", marks=pytest.mark.variant("agy: cleared after the body")),
    # keys pressed after the lead, but the next write comes over TYPED_WITHIN later
    ("lead-late", "sent"),
    pytest.param("lead-switch", "cleared", marks=pytest.mark.variant("person's client moves to another pane")),
    # they send it (or a replacement) and its turn's hook runs before brindle's Enter is authorized
    pytest.param("enter", "sent", marks=pytest.mark.variant("send lands before brindle's Enter is authorized")),
    pytest.param("enter", "agy sent", marks=pytest.mark.variant("agy: send lands before brindle's Enter")),
    # ... before the body's write is even recorded
    pytest.param("paste", "sent", marks=pytest.mark.variant("send lands before the body's write is recorded")),
    pytest.param("paste", "agy sent", marks=pytest.mark.variant("agy: send lands before the body's write")),
    # stopped before its body: the turn they start can't carry it
    ("lead", "agy sent"),
    # ... they delete only their reply, the body is resumed onto the lead, and they send mid-paste
    ("lead", "agy resumed"),
])
def test_person_typing_mid_paste_stops_it_and_message_goes_in_once_later(db, ws, monkeypatch, typed_after, then):
    # Real flush and tmux.paste over a fake terminal: the person starts a reply
    # after brindle's last look, while the paste is under way, then sends or
    # clears whatever the composer holds.
    from types import SimpleNamespace
    from brindle import antigravity, autopilot

    agy = then.startswith("agy")
    fake_agent(db, ws, status="idle", mode="interactive")
    db.update_agent("a1", provider="antigravity" if agy else "claude")
    monkeypatch.setattr(antigravity, "_flush_soon", lambda *a, **k: None)
    spoke = []
    monkeypatch.setattr(autopilot, "user_spoke", lambda db_, a: spoke.append(a.id))
    db.enqueue("a1", "WORKER REPORT", None)
    s = {"composer": "", "typed": time.time() - 60, "at": typed_after}   # their client, idle a minute
    submitted = []

    def person_types(point):
        if s["at"] in (point, point + "-late", point + "-switch"):
            s["typed"] = None if s["at"].endswith("-switch") else time.time() - (6 if s["at"].endswith("-late") else 0)
            s["at"] = None
            s["composer"] += "MY UNSENT REPLY"

    def start_turn(prompt):   # the CLI's real hook for a turn the person started
        if agy:
            antigravity.handle_hook(db, "a1", "agy-pre-invocation", {"invocationNum": 0})
        else:
            agents.handle_hook(db, "a1", "prompt-submit", {"prompt": prompt})

    def person_sends(point):   # they type, send it (or a replacement), and its turn's hook runs
        if s["at"] == point:
            s["at"], s["typed"] = None, time.time()
            s["composer"] += "MY UNSENT REPLY"
            start_turn("SOMETHING ELSE" if then.endswith("replaced") else s["composer"])
            s["composer"] = ""

    def tmux_call(*args, input=None, authorize=None, **k):
        if args[0] == "send-keys" and args[-1] == "Enter":
            person_sends("enter")
        if authorize is not None:
            authorize()
        out = ""
        if args[0] == "capture-pane":
            out = (f"────\n> {s['composer']}\n────\n? for shortcuts\n" if agy
                   else CLAUDE_IDLE.replace("❯ ", "❯ " + s["composer"]))
        elif args[0] == "list-clients" and s["typed"]:
            out = f"@0\t{int(s['typed'])}\n"
        elif args[0] == "load-buffer":
            s["buffer"] = input
            person_types("load-buffer")
        elif args[0] == "send-keys" and "-l" in args:
            s["composer"] += args[-1]
            person_types("lead")
        elif args[0] == "paste-buffer":
            s["composer"] += s.pop("buffer")
            person_types("body")
            person_sends("paste")   # the body is in, its write not yet recorded
        elif args[0] == "send-keys" and args[-1] == "Enter":
            submitted.append(s["composer"])
            s["composer"] = ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(tmux, "pane_input", _REAL_PANE_INPUT, raising=False)
    monkeypatch.setattr(tmux, "_tmux", tmux_call)
    lead = agents.message_lead(db, db.get_agent("a1"), None)

    assert agents.flush(db, "a1") is False and submitted == [] and db.pending_count("a1") == 1
    assert ("WORKER REPORT" in s["composer"]) == (typed_after == "body")   # the body never lands on their text
    if then == "agy resumed":
        s["composer"], s["typed"], s["at"] = f"{lead} ", time.time() - 60, "paste"
        assert agents.flush(db, "a1") is False and submitted == []
        antigravity.handle_hook(db, "a1", "agy-stop", {})
    sent, s["composer"], s["typed"] = s["composer"], "", time.time() - 60   # a minute ago
    if then.endswith("replaced"):
        sent = "SOMETHING ELSE"
    in_flight = typed_after in ("enter", "paste") or then == "agy resumed"   # their send and its hook came mid-paste
    if then in ("sent", "replaced", "agy sent", "agy replaced") and not in_flight:
        start_turn(sent)   # the report went in with their reply, or it didn't
    if then in ("sent", "replaced"):
        db.set_status("a1", "idle")
    elif then == "codex turn":
        agents.handle_hook(db, "a1", "codex-notify", {"type": "agent-turn-complete", "input-messages": [sent]})
    elif then in ("agy sent", "agy replaced"):
        antigravity.handle_hook(db, "a1", "agy-stop", {})
    assert len(spoke) == (then in ("sent", "replaced", "agy sent", "agy replaced", "agy resumed"))   # theirs
    again = not (typed_after == "body" or in_flight) or then in ("cleared", "replaced", "agy cleared")
    unresolved = then in ("agy sent", "agy replaced", "agy resumed") and (typed_after == "body" or in_flight)
    # this flush counts it (Codex's turn-end notify already flushed it)
    acknowledged = then == "sent" and (typed_after == "body" or in_flight)
    assert agents.flush(db, "a1") is (again or acknowledged) and db.pending_count("a1") == 0
    assert submitted == ([f"{lead} WORKER REPORT"] if again else [])
    # by no other way either: read_messages hands nothing over again
    assert "WORKER REPORT" not in agents.render_unread(db, db.read_held("a1"))
    kept = db.get_message("a1", 1)   # agy's unknowable case: kept, never counted delivered
    assert (kept.held == 2 and kept.delivered_at is None) == unresolved
    db.set_status("a1", "idle")   # that turn ended; the queue moves on
    db.enqueue("a1", "NEXT", None)
    assert agents.flush(db, "a1") is True and submitted[-1] == f"{lead} NEXT" and len(submitted) == 1 + again


def test_reconcile_keeps_hook_status_when_screen_is_unclear(db, ws, monkeypatch):
    fake_agent(db, ws, status="processing")
    screens = iter([CLAUDE_IDLE, CLAUDE_BUSY])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: next(screens))
    assert agents.reconcile(db, db.get_agent("a1"), gap=0).status == "processing"


@pytest.mark.variant("profile mismatch and dead agent are rejected")
def test_find_running_reuses_a_live_interactive_agent(db, ws, monkeypatch):
    alive = {"a1": True}
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: alive.get(a.id, False))
    assert agents.find_running(db, ws, "developer") is None
    fake_agent(db, ws, status="idle")
    db.update_agent("a1", profile="developer")
    assert agents.find_running(db, ws, "developer").id == "a1"
    assert agents.find_running(db, ws, "reviewer") is None      # another profile's agent is not reused
    alive["a1"] = False
    assert agents.find_running(db, ws, "developer") is None      # dead agents don't count


def test_handoff_wait_is_bounded_and_detaches(db, ws, monkeypatch):
    """A detached handoff's reports survive interruptions, then collection drains them."""
    (Path(ws.repo_root) / ".brindle").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".brindle" / "config.json").write_text('{"pipeline": true}')
    from brindle import mcp_server

    fake_agent(db, ws, status="processing", agent_id="boss")
    fake_agent(db, ws, mode="handoff", parent="boss", agent_id="w1")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(agents, "kill", lambda db, aid: db.delete_agent(aid))

    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "still running" in out and "wait_for_worker" in out
    assert db.get_agent("w1").mode == "handoff_detached"

    # Finishing later forwards each accepted report exactly once, even when
    # interrupted before delivery or interleaved with a concurrent report.
    broken = {"git": False, "docs": False, "pre": False, "notify": False}
    at = {"head": "h1"}
    inner = []
    during_docs = []   # (what the interleaved call reports, what the outer call's check then says)
    during_head = []
    read_docs, warm_checks, notify_parent = agents.stale_analysis_docs, agents.warm_checks, agents._notify_supervisor

    def git_head(ws_):
        if broken["git"]:
            raise RuntimeError("interrupted at the first git call")
        if during_head:   # a duplicate call that starts while this one reads the head
            during_head.pop()()
        return at["head"]

    def docs(db_, agent_id):
        if broken["docs"]:
            raise RuntimeError("interrupted after receipt")
        if during_docs:   # a second call runs to the end while this one checks documents
            text, verdict = during_docs.pop()
            agents.report_result(db, "w1", text)
            return verdict
        return read_docs(db_, agent_id)

    def warm(ws_):
        if broken["pre"]:
            raise RuntimeError("interrupted after accept")
        if inner:   # a concurrent duplicate call, mid-flight
            inner.pop()()
        warm_checks(ws_)

    def notify(db_, parent):
        if broken["notify"]:
            raise RuntimeError("interrupted after dispatch")
        notify_parent(db_, parent)

    def states():
        return [r["state"] for r in db.conn.execute("SELECT state FROM report_outbox WHERE canonical_id IS NULL ORDER BY id")]

    def messages():
        return db.conn.execute("SELECT count(*) FROM inbox WHERE agent_id='boss' AND COALESCE(held, 0)=0").fetchone()[0]

    monkeypatch.setattr(agents, "warm_checks", warm)
    monkeypatch.setattr(agents, "stale_analysis_docs", docs)
    monkeypatch.setattr(agents, "_branch_head", git_head)
    monkeypatch.setattr(agents, "_notify_supervisor", notify)

    broken["git"] = True                                   # the first git call hangs
    with pytest.raises(RuntimeError):
        agents.report_result(db, "w1", "done")
    assert states() == ["received"] and db.get_agent("w1").result is None
    broken.update(git=False, docs=True)                    # after receipt
    with pytest.raises(RuntimeError):
        agents.report_result(db, "w1", "done")
    assert states() == ["received"] and db.get_agent("w1").result is None   # nothing reads it yet
    broken.update(docs=False, pre=True)                    # after accept
    with pytest.raises(RuntimeError):
        agents.report_result(db, "w1", "done")
    assert states() == ["accepted"] and db.get_agent("w1").result == "done" and messages() == 0
    broken["pre"] = False
    inner.append(lambda: agents.report_result(db, "w1", "done"))   # a duplicate call, concurrently
    agents.report_result(db, "w1", "done")
    assert states() == ["delivered"] and messages() == 1
    assert [(e["kind"], e["agent_id"], e["payload"].get("sender_id"))
            for e in bus.tail(db, kinds=("agent.result", "message.queued"))] == [
        ("agent.result", "w1", None), ("message.queued", "boss", "w1")]
    agents.report_result(db, "w1", "done")
    at["head"] = "h2"                                      # the same summary at a new head
    broken["notify"] = True                                # after dispatch
    with pytest.raises(RuntimeError):
        agents.report_result(db, "w1", "done")
    assert states() == ["delivered", "delivered"] and messages() == 2
    agents.report_result(db, "w1", "done")
    assert messages() == 2
    # A duplicate that starts while the head is read, for a head already reported: its
    # receipt is folded into the delivered row (kept, never deleted) and both calls follow it.
    during_head.append(lambda: agents.report_result(db, "w1", "done"))
    agents.report_result(db, "w1", "done")
    assert states() == ["delivered", "delivered"] and messages() == 2
    h2 = db.conn.execute("SELECT id FROM report_outbox WHERE head='h2' AND canonical_id IS NULL").fetchone()[0]
    folded = db.conn.execute("SELECT state FROM report_outbox WHERE canonical_id=?", (h2,)).fetchall()
    assert folded and all(r["state"] == "superseded" for r in folded)
    broken["notify"] = False
    # Two calls interleaved at the document check: the one that accepts first writes the
    # result and delivers; the other, whether its check passed or found stale documents,
    # follows the row's state and delivers nothing.
    for n, verdict in enumerate((None, "read the new analysis docs first"), 1):
        at["head"] = f"x{n}"
        db.update_agent("w1", result=None)   # the document check applies to a first report
        during_docs.append((f"v{n}", verdict))
        agents.report_result(db, "w1", f"v{n}")
        assert db.get_agent("w1").result == f"v{n}" and messages() == 2 + n
    # An older report stuck undelivered is superseded by a newer one that is delivered.
    old = db.outbox_receive("w1", "old", "stale")
    db.outbox_bind(old["id"], "h0")
    at["head"] = "h3"
    agents.report_result(db, "w1", "done, more")
    assert db.conn.execute("SELECT state FROM report_outbox WHERE id=?", (old["id"],)).fetchone()[0] == "superseded"
    assert states()[-1] == "delivered"
    # Collecting the latest result directly drops the queued handoff messages.
    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "done, more" in out
    assert db.pending_count("boss") == 0
    assert db.get_agent("w1") is None


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
    brief = agents.worker_brief(ws)   # in the worker's prompt (brief_in_profile)
    assert "run only the tests that cover your change" in brief
    assert "Run the full suite once, just before you commit." in brief
    (Path(ws.repo_root) / ".brindle").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".brindle" / "config.json").write_text('{"checks": ["uv run pytest -q"]}')
    brief = agents.worker_brief(ws)
    assert "Don't run the full suite yourself" in brief and "`uv run pytest -q`" in brief
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


def test_spawn_chooses_unclaimed_milestone(db, ws, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(agents, "_launch", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: bool(a.tmux_window) and a.status not in ("paused", "done"))
    fake_agent(db, ws, agent_id="boss")
    fake_agent(db, ws, agent_id="other")
    db.add_autopilot("boss")
    db.set_milestones("boss", [(t, "true", None) for t in ("Done", "Build", "Ship")])
    db.record_check(db.milestones("boss")[0].id, True, "")
    outsider = fake_agent(db, ws, mode="assign", parent="other", agent_id="outsider")
    db.set_agent_milestone(outsider.id, "Build")
    contested = threading.Event()

    def overlapping():
        other_db = DB(db.path)
        try:
            try:
                with agents.milestone_claim_lock("boss", wait=False):
                    worker = agents.spawn(other_db, ws, "developer", parent_id="boss", mode="handoff")
            except BlockingIOError:
                contested.set()  # The first launch owns the claim; this launch must wait for it.
                worker = agents.spawn(other_db, ws, "developer", parent_id="boss", mode="handoff")
            return worker
        finally:
            contested.set()
            other_db.conn.close()

    link = db.set_agent_milestone
    with ThreadPoolExecutor(max_workers=1) as pool:
        def interleave(agent_id, title):
            nonlocal second
            second = pool.submit(overlapping)
            contested.wait()
            link(agent_id, title)
        second = None
        with monkeypatch.context() as patch:
            patch.setattr(db, "set_agent_milestone", interleave)
            first = agents.spawn(db, ws, "developer", parent_id="boss", mode="assign")
        second = second.result()
    assert [db.agent_milestone(a.id) for a in (first, second)] == ["Build", "Ship"]
    fallback = agents.spawn(db, ws, "developer", parent_id="boss", mode="assign")
    assert db.agent_milestone(fallback.id) == "Build"
    explicit = agents.spawn(db, ws, "developer", parent_id="boss", mode="assign", milestone="Ship")
    assert db.agent_milestone(explicit.id) == "Ship"
    for a in (first, fallback):
        db.set_status(a.id, "done")
    replacement = agents.spawn(db, ws, "developer", parent_id="boss", mode="assign")
    assert db.agent_milestone(replacement.id) == "Build"


def test_no_goal_command_when_hooks_are_disabled(db, ws):
    from brindle.providers import get_provider

    (Path(ws.path) / ".claude").mkdir(exist_ok=True)
    (Path(ws.path) / ".claude" / "settings.local.json").write_text('{"disableAllHooks": true}')
    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, "tests pass", get_provider("claude"), headless=False)
    assert not prompt.startswith("/goal")
    assert "Finish line: tests pass" in prompt


@pytest.mark.parametrize("provider", [
    "codex",
    pytest.param("antigravity", marks=pytest.mark.variant("agy resume: warmup skipped only without resume (#280)")),
])
def test_supervisor_resume_keeps_prompt(db, ws, monkeypatch, provider):
    agent = Agent("boss", ws.id, "supervisor", provider, None, "interactive", "idle", "@0", None, time.time())
    db.add_agent(agent)
    monkeypatch.setenv("BRINDLE_CODEX_BIN", "/bin/codex")
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    monkeypatch.setattr(agents, "launch_blockers", lambda *a, **k: [])
    window = Mock(side_effect=RuntimeError("launch captured"))
    monkeypatch.setattr(agents, "_open_window", window)
    with pytest.raises(RuntimeError, match="launch captured"):
        agents._launch(db, agent, ws, prompt="continue the task", resume="saved-session",
                       watch_pane=False, model_checked=True)
    queued = [m.body for m in iter(lambda: db.pop_pending(agent.id), None)]
    launched = window.call_args.args[4]
    assert "saved-session" in launched
    delivered = "\n".join([*launched, *queued])
    assert load_profile("supervisor", ws.repo_root).prompt.strip() in delivered
    assert "continue the task" in delivered


def test_repo_read_dirs_become_read_only_allow_rules(ws):
    """worker_extra_read_dirs reach Claude Code as Read allow rules, never --add-dir (which also writes)."""
    (Path(ws.repo_root) / ".brindle").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".brindle" / "config.json").write_text('{"worker_extra_read_dirs": ["results", "/srv/data/"]}')
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer", ws.repo_root), "do the thing"))
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert f"Read(/{Path(ws.repo_root).resolve()}/results/**)" in allowed and "Read(//srv/data/**)" in allowed
    assert "--add-dir" not in argv
    # --add-dir is variadic: a flag must follow the last one, or Claude Code reads the prompt as a directory.
    from dataclasses import replace

    profile = replace(load_profile("developer"), add_dirs=["/srv/cache", "/srv/refs"])
    argv = ClaudeCode().command(LaunchContext("abc", profile, "do the thing"))
    last = max(i for i, a in enumerate(argv) if a == "--add-dir")
    assert argv[last + 2].startswith("--"), argv[last:]


def test_spawn_reports_a_missing_add_dir_once(db, ws, monkeypatch, capsys):
    """Claude Code ignores an --add-dir that does not exist, so launch says so,
    once, however many times the profile is loaded on the way."""
    config = Path(ws.repo_root) / ".brindle" / "config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text('{"add_dirs": ["/no/such/cache"]}')
    # Stopped at the window, so every profile load on the way (spawn's own and
    # _profile_for's) has happened.
    monkeypatch.setattr(agents, "_open_window", Mock(side_effect=RuntimeError("launch captured")))
    monkeypatch.setattr("brindle.providers.trust_folder", lambda path: None)

    with pytest.raises(RuntimeError, match="launch captured"):
        agents.spawn(db, ws, "developer", prompt="hi", mode="assign")
    assert capsys.readouterr().err.count("/no/such/cache") == 1


@pytest.mark.parametrize("tool, reports, ends", [
    ("assign", "later", "load falls"),
    pytest.param("handoff", "while it starts", "load falls",
                 marks=pytest.mark.variant("report arrives before the start returns")),
    pytest.param("assign", "later", "start now",
                 marks=pytest.mark.variant("forced start: check run waits for room under load")),
    ("assign", "later", "forced in flight"),
    ("assign", "later", "wait off")])
def test_a_new_worker_waits_for_room_visibly_and_starts_when_load_falls(db, repo, monkeypatch, tool, reports, ends):
    """Plan 3.5: on a machine with no room an assign (or handoff) queues the
    task with the reason, list_tasks shows it, autopilot doesn't count it as
    a stall, a paused session starts nothing even once the load falls, and
    resumed, it starts, with a message to the supervisor, and its result
    reaches the supervisor exactly once, even when it reports before its
    start has returned."""
    import asyncio

    from conftest import full_text
    from brindle import autopilot, events, mcp_server, spawn, tasks

    # The real spawn, with a launch that only marks the agent running.
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws, **kw: db_.set_status(a.id, "processing"))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status not in ("paused", "done"))
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"pipeline": false}')
    root = workspaces.adopt_root(db, str(repo))
    boss = Agent("boss", root.id, "supervisor", "claude", None, "interactive", "processing", "@0",
                 None, time.time())
    db.add_agent(boss)
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Ship it", [("Feature", "false", None)])
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    load = [14.0]
    monkeypatch.setattr(spawn, "_load", lambda: load[0])
    monkeypatch.setattr(spawn, "_cores", lambda: 10)

    if ends == "wait off":   # configured from the outset: the real dispatch starts a worker at once
        (repo / ".brindle" / "config.json").write_text('{"pipeline": false, "wait_for_room": false}')
        out = asyncio.run(getattr(mcp_server, tool)("developer", "do A", branch="feat-a"))
        assert not out.startswith("Queued") and not db.list_tasks(str(repo), state="pending")
        assert len(db.list_agents()) == 2
        return
    out = asyncio.run(getattr(mcp_server, tool)("developer", "do A", branch="feat-a"))
    busy = "the machine is busy (load 14.0 on 10 cores"
    assert out.startswith("Queued task") and busy in out, out
    [t] = db.list_tasks(str(repo), state="pending")
    assert f"waiting for room: {busy}" in tasks.list_text(db, str(repo))
    assert [a.id for a in db.list_agents()] == ["boss"]                  # nothing started
    # Waiting is not a stall (no nudge), but the supervisor is reminded which task waits.
    reminder = autopilot.on_stop(db, boss, {})
    assert t.id in reminder["reason"]
    assert db.get_autopilot("boss").nudges == 0
    # Before the room wait limit, the task stays queued while the machine is full.
    db.update_task(t.id, waiting_since=time.time() - 60)
    tasks.sweep(db)
    assert db.get_task(t.id).state == "pending" and len(db.list_agents()) == 1

    if ends == "forced in flight":   # a competing force while the first is really starting is refused
        asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b"))
        [b] = [x for x in db.list_tasks(str(repo), state="pending") if x.id != t.id]
        from dataclasses import replace

        from brindle.db import DB

        other = DB()   # a second process: its own connection to the same database
        stale = db.get_task(t.id)   # what its sweep and helper read before the claim
        real_start, seen, helpers = tasks.start_queued, {}, []
        monkeypatch.setattr(tasks, "_wait_in_background", helpers.append)

        def competing(db_, task):   # at the claim boundary: the first is claimed, not yet launched
            try:
                tasks.start_now(other, boss, b.id)
            except ValueError:
                seen["refused"] = other.get_task(b.id).state
            seen["swept"] = tasks._start_if_ready(other, stale, "room on the machine", check_room=False)
            tasks.wait_for_room(other, replace(stale, waiting_since=None), "busy again")   # stale sweep
            other.set_task_waiting(t.id, "busy again", time.time())                       # stale helper
            seen["in_flight"] = other.get_task(t.id)
            return real_start(db_, task)

        monkeypatch.setattr(tasks, "start_queued", competing)
        tasks.start_now(db, boss, t.id)
        mid = seen["in_flight"]
        assert seen["refused"] == "pending" and not seen["swept"] and not helpers
        assert mid.state == "starting" and mid.forced == 1 and mid.waiting != "busy again"
        assert db.get_task(t.id).state == "started" and db.get_task(t.id).forced == 1
        assert db.get_task(b.id).state == "pending" and len(db.list_agents()) == 2   # one launch
        assert spawn.stats()["forced"] == 1
        return
    if ends == "start now":
        from brindle import gates

        assert tasks.load_counts(db) == (0, 0)          # the processing supervisor is no worker
        forced = spawn.stats()["forced"]
        tasks.start_now(db, boss, t.id)
        assert db.get_task(t.id).state == "started" and db.get_task(t.id).forced == 1
        assert spawn.stats()["forced"] == forced + 1
        worker = db.get_agent(db.get_task(t.id).agent_id)
        wws = db.get_workspace(worker.workspace_id)
        assert tasks.load_counts(db) == (1, 0)
        # A check run on its branch still waits for room under the same load, then runs at normal priority.
        ran, seen, wrapped = Path(wws.path) / "ran.txt", [], []

        def cancel():   # polled while the run waits for a slot or room, and again while it runs
            if gates.marked("checks-room", wws.id):
                seen.append(ran.exists())
                load[0] = 2.0   # admitted after a look at the full machine
            return False

        load[0] = 25.0   # a check waits only above twice the cores (a worker start, above ~1x)
        real_start = spawn.start   # the admission gate is off here, as in a plain MCP process
        monkeypatch.setattr(spawn, "start", lambda argv, **kw: wrapped.append(argv) or real_start(argv, **kw))
        monkeypatch.setattr(gates, "SLOT_POLL", 0)
        ok, _ = gates.run_checked(db, wws, "touch ran.txt", {}, 60, cancel=cancel, full=True)
        assert ok and seen and not any(seen) and ran.exists()
        assert wrapped == ["touch ran.txt"]
        db.set_status(worker.id, "idle")                # reported, but its check run is still going
        with gates.marker("checks-running", wws.id):
            assert tasks.load_counts(db) == (0, 1)
        return

    db.set_status("boss", "paused")
    load[0] = 2.0
    assert not tasks.start_when_room(db, t.id, poll=0) and tasks.start_with_room(db) == 0
    assert db.get_task(t.id).state == "pending" and len(db.list_agents()) == 1

    if reports == "while it starts":
        emit = events.emit

        def finished_at_once(cfg, kind, ws, agent, **kw):   # delegate's last step before it returns
            emit(cfg, kind, ws, agent, **kw)
            agents.report_result(db, agent.id, "did A", docs_checked=True)

        monkeypatch.setattr(events, "emit", finished_at_once)
    agents.resume(db, "boss")
    assert db.get_task(t.id).state == "started"
    worker = db.get_agent(db.get_task(t.id).agent_id)
    assert worker.parent_id == "boss" and worker.mode == {"assign": "assign", "handoff": "handoff_detached"}[tool]
    if reports == "later":
        agents.report_result(db, worker.id, "did A", docs_checked=True)
    told = [full_text(db, "boss", db.pop_pending("boss").body) for _ in range(db.pending_count("boss"))]
    assert sum(f"Started {worker.id} for task {t.id} (was waiting for room: {busy}" in m for m in told) == 1
    assert sum("did A" in m for m in told) == 1, told


@pytest.mark.parametrize("merged, edit, shared, want_edited, want_both, want_diff", [
    ("main", None, None, [], [], "c.txt"),                                # a clean merge of the base, then a fix
    ("main", "a.txt", None, ["a.txt"], [], "a.txt c.txt"),                # the merge commit was edited by hand
    ("other", None, None, [], [], "b.txt c.txt"),                         # a branch that isn't in the base
    ("main", "shared.txt", ("mine", "theirs"), ["shared.txt"], ["shared.txt"], "c.txt shared.txt"),   # conflict resolved by hand
    ("main", None, ("same", "same"), [], ["shared.txt"], "c.txt"),        # identical edit on both sides
])
def test_review_base_after_merge(tmp_path, merged, edit, shared, want_edited, want_both, want_diff):
    from types import SimpleNamespace

    sh("git init -q -b main && git config user.name t && git config user.email t@t", tmp_path)
    for n in ("a", "b", "c", "shared"):
        (tmp_path / f"{n}.txt").write_text("1\n")
    sh("git add -A && git commit -qm init && git checkout -qb feat", tmp_path)
    (tmp_path / "a.txt").write_text("worker\n")
    sh("git commit -qam w1", tmp_path)
    prev = sh("git rev-parse HEAD", tmp_path)
    if shared:
        (tmp_path / "shared.txt").write_text(shared[0] + "\n")
        sh("git commit -qam w2", tmp_path)
    sh("git checkout -q main && git checkout -qb other", tmp_path)
    (tmp_path / "b.txt").write_text("other\n")
    if shared:
        (tmp_path / "shared.txt").write_text(shared[1] + "\n")
    sh("git commit -qam o1", tmp_path)
    if merged == "main":
        sh("git checkout -q main && git merge -q --ff-only other", tmp_path)
    sh("git checkout -q feat", tmp_path)
    sh(f"git merge -q --no-commit --no-ff {merged} || true", tmp_path)
    if edit:
        (tmp_path / edit).write_text("hand\n")
    sh("git add -A && git commit -qm merge", tmp_path)
    (tmp_path / "c.txt").write_text("fix\n")
    sh("git commit -qam fix", tmp_path)

    ws = SimpleNamespace(path=str(tmp_path), base_branch="main", id="w1")
    rb = agents.review_base(ws, prev)
    assert rb.edited == want_edited and rb.both == want_both
    assert sh(f"git diff --name-only {rb.sha}..HEAD", tmp_path).split() == want_diff.split()

