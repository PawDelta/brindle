import json
import time

import pytest

from brindle import agents, codex_hook, providers, tmux, workspaces
from brindle.config import POLICY_DEFAULT_KEY, set_user
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


def test_codex_on_a_default_policy_is_not_refused_and_told_once(db, ws, monkeypatch):
    """Codex would ignore brindle's sandbox profile (its config sets sandbox_mode).
    The policy only on by default: it starts, on Codex's own approvals, without
    the hook, with one notice however many launch. Set by someone: still refused."""
    set_user(POLICY_DEFAULT_KEY, "on")
    monkeypatch.setattr(providers, "_codex_layers",
                        lambda cwd: [("/codex/config.toml", {"sandbox_mode": "workspace-write"})])
    monkeypatch.setattr(codex_hook, "ensure", lambda **_: codex_hook.TRUSTED)   # the hook isn't what's missing
    agent = codex_agent(db, ws)

    def launch():
        argv = Codex().command(LaunchContext("c1", load_profile("developer"), None, cwd=ws.path))
        assert not [a for a in argv if a.startswith(("default_permissions=", "permissions.", "hooks."))]
        return codex_hook.tell_skipped(db, agent, ws.path)

    assert launch() and not launch()
    [message] = db.conn.execute("SELECT body FROM inbox WHERE agent_id='c1'").fetchall()
    assert "/codex/config.toml" in message["body"] and "won't start" not in message["body"]
    assert codex_hook.launch_warning("codex", ws.path) is None

    # A clean config, but Codex won't trust the hook: this supervisor starts on
    # Codex's own prompts, so it's told that once too, as a worker's supervisor is.
    monkeypatch.setattr(providers, "_codex_layers", lambda cwd: [])
    monkeypatch.setattr(codex_hook, "ensure", lambda **_: codex_hook.NEW)
    argv = Codex().command(LaunchContext("c1", load_profile("developer"), None, cwd=ws.path))
    assert not [a for a in argv if a.startswith("hooks.")]
    assert codex_hook.tell_untrusted(db, agent, ws.path) and not codex_hook.tell_untrusted(db, agent, ws.path)
    assert db.conn.execute("SELECT count(*) FROM inbox WHERE agent_id='c1' AND instr(body, ?) > 0",
                           (codex_hook.UNTRUSTED,)).fetchone()[0] == 1

    monkeypatch.setattr(providers, "_codex_layers",
                        lambda cwd: [("/codex/config.toml", {"sandbox_mode": "workspace-write"})])
    set_user("permission_policy", "on")
    with pytest.raises(providers.CodexSandboxRefused):
        Codex().command(LaunchContext("c1", load_profile("developer"), None, cwd=ws.path))


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


def test_codex_turn_complete_unreported_tells_parent(db, ws, monkeypatch):
    """No Stop hook can remind a Codex worker to report: a turn it ends
    without a result is one its supervisor hears about."""
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@1", None, time.time()))
    db.add_agent(Agent("c1", ws.id, "developer", "codex", "boss", "assign", "unknown", "@0",
                       None, time.time()))
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: sent.append(to))
    agents.hook_main(db, "c1", "codex-notify", json.dumps({"type": "agent-turn-complete"}))
    assert db.get_agent("c1").status == "idle"
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
    # Repeats are suppressed while the backoff runs: the supervisor is only waiting.
    for _ in range(3):
        agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == 1
    # Once the backoff has passed, a still-idle supervisor is nudged again.
    clock = [time.time() + autopilot.NUDGE_BACKOFF + 1]
    monkeypatch.setattr(autopilot.time, "time", lambda: clock[0])
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == 2
    # Capped like any nudge: no endless loop on a supervisor making no progress.
    for _ in range(autopilot.MAX_NUDGES + 2):
        clock[0] += autopilot.NUDGE_BACKOFF + 1
        agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == autopilot.MAX_NUDGES


def test_supervisor_refuses_a_provider_that_cant_supervise(repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["supervisor", "--provider", "native"])
    assert res.exit_code != 0 and "can't run the supervisor" in res.output
