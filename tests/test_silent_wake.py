"""A person's chat (an interactive Claude Code supervisor) is never typed
into: with no live waiter, brindle pokes it and its FileChanged hook starts
one (brindle.wake.poke). No real CLIs."""

import json
import time

import pytest

from brindle import agents, wake, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import ClaudeCode, LaunchContext

REAL_PULLS = agents.pulls_messages  # conftest patches it off for the other tests
REAL_SILENT = wake.silent            # likewise


@pytest.fixture
def boss(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing",
                       "@1", None, time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, *_: True)
    monkeypatch.setattr(agents, "pulls_messages", REAL_PULLS)
    monkeypatch.setattr(wake, "silent", REAL_SILENT)
    monkeypatch.setattr(agents, "reconcile", lambda db, a, *k: a)   # no screen to read
    monkeypatch.setattr(agents, "_paste_blocked", lambda a: False)
    monkeypatch.setattr(agents, "_pane_input", lambda a: None)
    monkeypatch.setattr(agents, "_partial_composer", lambda a, state, stage: True)
    pasted = []
    monkeypatch.setattr(agents.tmux, "paste", lambda target, body, **k: pasted.append(body))
    return pasted


def _waiter_gone():
    """Its waiter registered once and has since gone: a turn ended by Esc, or one past its limit."""
    wake._register("boss")
    wake._path("boss").write_text("0 gone\n")


@pytest.mark.parametrize("registered,armed", [
    pytest.param(True, True, marks=pytest.mark.variant(
        "an armed supervisor whose waiter ended must be poked without typing into the person's chat")),
    pytest.param(True, False, marks=pytest.mark.variant(
        "an older launch with a departed waiter and no poke hook must leave mail queued without typing")),
    (False, False)],
                         ids=["pokeable", "older-launch", "no-waiter-hook"])
def test_idle_supervisor_without_a_waiter_is_never_typed_into(db, boss, registered, armed):
    if registered:
        _waiter_gone()
    if armed:
        wake.arm_poke("boss")
        before = wake.poke_path("boss").read_text()
    assert agents.send_message(db, "boss", "the result", sender_id="w1") == "queued"
    assert agents.flush(db, "boss") is False
    assert boss == []   # nothing typed into the person's chat
    if armed:
        assert wake.poke_path("boss").read_text() != before   # Claude Code starts a waiter
    else:
        assert not wake.poke_path("boss").exists()   # can't be poked: waits for its next Stop
    # The mail stays queued for the waiter, which hands it over whole.
    text, _ = wake.take(db, db.get_agent("boss"))
    assert "new message" in text and db.pending_count("boss") == 0


def test_a_supervisors_session_watches_its_poke_file(db, boss):
    argv = ClaudeCode().command(LaunchContext("a1", load_profile("developer"), None, mode="interactive"))
    [group] = json.loads(argv[argv.index("--settings") + 1])["hooks"]["FileChanged"]
    [hook] = group["hooks"]
    assert hook["asyncRewake"] is True and wake.EVENT in hook["command"] and "--agent a1" in hook["command"]
    argv = ClaudeCode().command(LaunchContext("a1", load_profile("developer"), None, mode="assign"))
    assert "FileChanged" not in json.loads(argv[argv.index("--settings") + 1])["hooks"]

    # A fresh session: its SessionStart hook can run before its first waiter registers.
    wake.arm_poke("boss")
    out = agents.handle_hook(db, "boss", "session-start", {})
    assert out["hookSpecificOutput"]["watchPaths"] == [str(wake.poke_path("boss"))]


@pytest.mark.parametrize("settled", [
    pytest.param(True, marks=pytest.mark.variant(
        "an old delivery cleared from the composer must be requeued whole without resuming pane input")),
    False], ids=["composer-empty", "composer-holds-it"])
def test_a_delivery_an_older_brindle_began_typing_is_not_finished_by_typing(db, boss, monkeypatch, settled):
    monkeypatch.setattr(agents, "_composer_settled", lambda a: settled)
    _waiter_gone()
    wake.arm_poke("boss")
    before = wake.poke_path("boss").read_text()
    mid = db.enqueue("boss", "typed", None)
    db.record_pane_delivery(mid, "boss", json.dumps(
        {"message_id": mid, "pane": "@0", "server": agents.server_of(db.get_agent("boss")), "provider": "claude",
         "body": "typed", "lead": "brindle delivered this message:", "stage": "lead", "stopped": True}))
    db.enqueue("boss", "the answer", None)

    assert agents.flush(db, "boss") is False and boss == []
    if settled:
        # Nothing of it is left in the composer: back in the queue, and the chat poked.
        assert db.mark("boss", "pane-delivery") is None
        assert wake.poke_path("boss").read_text() != before
        text, _ = wake.take(db, db.get_agent("boss"))
        assert "typed" in text and "the answer" in text
    else:
        # The person's to send (its prompt acknowledges it) or clear: kept, not lost.
        assert db.mark("boss", "pane-delivery") is not None
        assert not db.message_delivered(mid) and db.pending_count("boss") == 2
