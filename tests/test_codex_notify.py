import json
import time

import pytest

from brindle import agents, tmux, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import Codex, LaunchContext


# Bottom of a real `tmux capture-pane -p -J -e -t %1234 -S -40` from
# Codex, with trailing padding removed. The model/path is a separate footer
# row ABOVE shortcuts, and is neither faint nor part of the input.
CODEX_EMPTY_PANE = (
    "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n"
    "\n"
    "  \x1b[38;2;246;226;183mGPT-6.1-Sol medium\x1b[39m · "
    "\x1b[38;2;171;223;167m~/.brindle/worktrees/brindle/fix/codex-input-guard\x1b[39m\n"
    "  \x1b[1m?\x1b[0m for shortcuts                  ⚠ "
    "\x1b[38;2;196;167;103m1 warning\x1b[39m · \x1b[1mf2\x1b[0m to view\n"
)


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def codex_agent(db, ws, status="processing"):
    a = Agent("c1", ws.id, "developer", "codex", None, "interactive", status, "@0", None, time.time())
    db.add_agent(a)
    return a


@pytest.mark.parametrize("input_text", [
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


def test_real_codex_footer_allows_placeholder_but_protects_grey_input(db, ws, monkeypatch):
    codex_agent(db, ws)
    placeholder = "\x1b[2mAsk Codex to do anything\x1b[0m"
    screen = CODEX_EMPTY_PANE.replace(placeholder, "\x1b[38;2;205;214;244mhalf a request\x1b[39m")
    pasted = []
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen)
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    db.enqueue("c1", "next step please", None)
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert pasted == []
    assert db.pending_count("c1") == 1
    screen = CODEX_EMPTY_PANE
    assert agents.flush(db, "c1") is True
    assert pasted == ["next step please"]
    assert db.pending_count("c1") == 0


def test_hook_main_parses_notify_payload(db, ws):
    codex_agent(db, ws)
    agents.hook_main(db, "c1", "codex-notify", json.dumps({"type": "agent-turn-complete"}))
    assert db.get_agent("c1").status == "idle"


def test_codex_turn_complete_unreported_tells_parent(db, ws, monkeypatch):
    """No Stop hook can remind a Codex worker to report: a turn it ends
    without a result is one its supervisor hears about."""
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@1", None, time.time()))
    db.add_agent(Agent("c1", ws.id, "developer", "codex", "boss", "assign", "unknown", "@0",
                       None, time.time()))
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append(to))
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert sent == ["boss"]


def test_autopilot_codex_supervisor_is_told_to_keep_going(db, ws, monkeypatch):
    """No Stop hook on Codex: a finished turn is where autopilot nudges it."""
    from brindle import autopilot

    codex_agent(db, ws)
    db.add_autopilot("c1")
    autopilot.set_goal(db, "c1", "Ship it", [("Done", "false", None)])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "")
    monkeypatch.setattr(agents, "is_alive", lambda a, *_: True)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    # One line, like every message brindle sends a supervisor; the rest via read_messages.
    assert len(pasted) == 1 and "\n" not in pasted[0]
    message_id = int(pasted[0].rsplit("message_id=", 1)[1].rstrip(")"))
    assert "Keep going" in agents.full_message(db, "c1", message_id)
    # Capped like any nudge: no endless loop on a supervisor making no progress.
    for _ in range(autopilot.MAX_NUDGES + 2):
        agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == autopilot.MAX_NUDGES


def test_supervisor_refuses_a_provider_that_cant_supervise(repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["supervisor", "--provider", "native"])
    assert res.exit_code != 0 and "can't run the supervisor" in res.output


def test_command_passes_the_profiles_model_and_effort(monkeypatch):
    import dataclasses
    monkeypatch.setenv("BRINDLE_CODEX_BIN", "/opt/codex")
    profile = dataclasses.replace(load_profile("developer"), model="gpt-6.1-sol", effort="high")
    argv = Codex().command(LaunchContext("abc", profile, "do it"))
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in argv
    assert argv[argv.index('model_reasoning_effort="high"') - 1] == "-c"
    assert argv[argv.index("mcp_servers.brindle.startup_timeout_sec=120") - 1] == "-c"
