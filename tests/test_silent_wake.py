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


@pytest.fixture
def boss(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing",
                       "@1", None, time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, *_: True)
    monkeypatch.setattr(agents, "pulls_messages", REAL_PULLS)
    monkeypatch.setattr(agents, "reconcile", lambda db, a, *k: a)   # no screen to read
    monkeypatch.setattr(agents, "_paste_blocked", lambda a: False)
    monkeypatch.setattr(agents, "_pane_input", lambda a: None)
    pasted = []
    monkeypatch.setattr(agents.tmux, "paste", lambda target, body, **k: pasted.append(body))
    # Its waiter registered once (the session runs it) and has since gone:
    # a turn ended by Esc, or one past its limit.
    wake._register("boss")
    wake._path("boss").write_text("0 gone\n")
    return pasted


@pytest.mark.parametrize("armed", [True, False], ids=["pokeable", "older-launch"])
def test_idle_supervisor_without_a_waiter_is_never_typed_into(db, boss, armed):
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

    wake.arm_poke("boss")
    out = agents.handle_hook(db, "boss", "session-start", {})
    assert out["hookSpecificOutput"]["watchPaths"] == [str(wake.poke_path("boss"))]
