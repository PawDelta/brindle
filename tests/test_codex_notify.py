import json
import time

import pytest

from brindle import agents, tmux, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import Codex, LaunchContext


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def codex_agent(db, ws, status="processing"):
    a = Agent("c1", ws.id, "developer", "codex", None, "interactive", status, "@0", None, time.time())
    db.add_agent(a)
    return a


def test_command_passes_notify_hook_with_agent_id(monkeypatch):
    monkeypatch.setenv("BRINDLE_CODEX_BIN", "/opt/codex")
    argv = Codex().command(LaunchContext("abc", load_profile("developer"), "do it"))
    setting = next(a for a in argv if a.startswith("notify="))
    notify = json.loads(setting[len("notify="):])
    assert notify[-4:] == ["_hook", "codex-notify", "--agent", "abc"]
    assert argv[argv.index(setting) - 1] == "-c"
    assert argv[-1].endswith("do it")


def test_codex_trusts_hook_status():
    assert Codex.uses_hooks is True
    assert Codex.announces_start is False


@pytest.mark.parametrize("input_text", [
    "half a message",
    "\x1b[1;32mhalf a message\x1b[0m",
    "\n  second line only",
    "a long first line\n  wrapped continuation\n\n  another paragraph",
    "\x1b[2mplaceholder\x1b[0m real input",
    "\x1b[2mplaceholder\x1b[22m real input",
    "\x1b[38;5;244mplaceholder\x1b[38;5;196m real input",
])
def test_notify_keeps_typed_input_queued_until_cleared(db, ws, monkeypatch, input_text):
    codex_agent(db, ws)
    db.enqueue("c1", "next step please", None)
    screen = f"Done.\n\n› {input_text}\n\n  ? for shortcuts    90% context left"
    captures, pasted = [], []

    def capture(*a, **k):
        captures.append(k)
        return screen

    monkeypatch.setattr(tmux, "capture", capture)
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert pasted == []
    assert db.pending_count("c1") == 1
    assert db.get_agent("c1").status == "idle"
    assert captures[-1]["escapes"] is True
    assert agents.flush(db, "c1") is False
    screen = "Done.\n\n› \n\n  ? for shortcuts    90% context left"
    assert agents.flush(db, "c1") is True
    assert pasted == ["next step please"]
    assert db.pending_count("c1") == 0
    assert agents.flush(db, "c1") is False


@pytest.mark.parametrize("placeholder", [
    "", "\x1b[2mFind and fix a bug\x1b[0m",
    "\x1b[90mExplain this codebase\x1b[39m",
    "\x1b[38;5;244mWrite tests\x1b[0m",
    "\x1b[38;2;128;128;128mWrite tests\x1b[0m",
    "\x1b[2mFind and fix\n  a bug\x1b[0m",
])
def test_notify_delivers_over_empty_placeholder(db, ws, monkeypatch, placeholder):
    codex_agent(db, ws)
    db.enqueue("c1", "next step please", None)
    # A previous submitted prompt in the transcript is not the active input.
    screen = f"› old request\nDone.\n\n› {placeholder}\n\n  90% context left"
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert pasted == ["next step please"]
    assert db.pending_count("c1") == 0


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("question", [
    "Would you like to run the following command?",
    "Would you like to run the following\n  command?",
    "Would you like to make the following edits?",
    "Would you like to grant these permissions?",
    "Would you like to send input to the existing terminal?",
    "Trust this folder?",
])
def test_codex_approval_is_not_an_input_prompt(question, interactive):
    screen = f"\x1b[1m{question}\x1b[0m\n› 1. Yes\n  2. No"
    assert Codex().paste_blocked(screen, interactive) == "approval"


def test_codex_clipped_input_is_conservatively_blocked():
    assert Codex().paste_blocked("  wrapped input without its marker", True) == "input"


def test_codex_placeholder_style_before_marker():
    screen = "\x1b[2m› Find and fix a bug\x1b[0m\n\n  ? for shortcuts"
    assert Codex().paste_blocked(screen, True) is None


@pytest.mark.parametrize("mode", ["interactive", "assign"])
@pytest.mark.parametrize("question", [
    "Would you like to run the following command?",
    "Would you like to run the following\n  command?",
])
def test_notify_queues_over_approval_until_chat_returns(db, ws, monkeypatch, mode, question):
    agent = codex_agent(db, ws)
    with db.tx() as c:
        c.execute("UPDATE agents SET mode=? WHERE id=?", (mode, agent.id))
    db.enqueue("c1", "next step please", None)
    screen = f"{question}\n› 1. Yes\n  2. No"
    pasted, keys = [], []
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen)
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    monkeypatch.setattr(tmux, "send_keys", lambda *a, **k: keys.append(a))
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert pasted == keys == []
    assert db.pending_count("c1") == 1
    screen = "Done.\n\n› \n\n  ? for shortcuts"
    assert agents.flush(db, "c1") is True
    assert pasted == ["next step please"]
    assert db.pending_count("c1") == 0


def test_notify_turn_complete_sets_idle_and_delivers_queued_message(db, ws, monkeypatch):
    codex_agent(db, ws)
    db.enqueue("c1", "next step please", None)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "")
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"}) is None
    assert pasted == ["next step please"]
    assert db.pending_count("c1") == 0


def test_notify_turn_complete_without_queue_is_idle(db, ws):
    codex_agent(db, ws)
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert db.get_agent("c1").status == "idle"


def test_notify_other_event_leaves_status(db, ws):
    codex_agent(db, ws)
    agents.handle_hook(db, "c1", "codex-notify", {"type": "something-else"})
    assert db.get_agent("c1").status == "processing"


def test_hook_main_parses_notify_payload(db, ws):
    codex_agent(db, ws)
    agents.hook_main(db, "c1", "codex-notify", json.dumps({"type": "agent-turn-complete"}))
    assert db.get_agent("c1").status == "idle"


def test_autopilot_codex_supervisor_is_told_to_keep_going(db, ws, monkeypatch):
    """No Stop hook on Codex: a finished turn is where autopilot nudges it."""
    from brindle import autopilot

    codex_agent(db, ws)
    db.add_autopilot("c1")
    autopilot.set_goal(db, "c1", "Ship it", [("Done", "false", None)])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "")
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == 1 and "Keep going" in pasted[0]
    # Capped like any nudge: no endless loop on a supervisor making no progress.
    for _ in range(autopilot.MAX_NUDGES + 2):
        agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == autopilot.MAX_NUDGES


def test_supervisor_refuses_a_provider_that_cant_supervise(repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["--provider", "native"])
    assert res.exit_code != 0 and "can't run the supervisor" in res.output


def test_command_passes_the_profiles_model_and_effort(monkeypatch):
    import dataclasses
    monkeypatch.setenv("BRINDLE_CODEX_BIN", "/opt/codex")
    profile = dataclasses.replace(load_profile("developer"), model="gpt-6.1-sol", effort="high")
    argv = Codex().command(LaunchContext("abc", profile, "do it"))
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in argv
    assert argv[argv.index('model_reasoning_effort="high"') - 1] == "-c"
