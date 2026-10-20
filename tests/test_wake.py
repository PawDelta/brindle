"""brindle.wake: an interactive Claude Code chat gets brindle's messages from
its own asyncRewake waiter hook, never Claude Code's peer-message inbox (no
real CLIs)."""

import json
import subprocess
import sys
import time

import pytest

from brindle import agents, inbox, wake, workspaces
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
    return ws


@pytest.fixture
def peer_inbox(monkeypatch):
    """A Claude Code inbox is there; anything sent to it is recorded (and must not be)."""
    sent = []
    monkeypatch.setattr(inbox, "usable", lambda a: True)
    monkeypatch.setattr(inbox, "send", lambda a, text, **k: sent.append(text) or True)
    return sent


def _settings(mode):
    argv = ClaudeCode().command(LaunchContext("a1", load_profile("developer"), None, mode=mode))
    return json.loads(argv[argv.index("--settings") + 1])


def _waiters(hooks):
    return [h for group in hooks for h in group["hooks"] if wake.EVENT in h["command"]]


def test_only_an_interactive_chat_runs_the_waiter():
    hooks = _settings("interactive")["hooks"]
    for event in ("SessionStart", "Stop"):
        [w] = _waiters(hooks[event])
        assert w["asyncRewake"] is True and w["timeout"] == wake.TIMEOUT
        assert "--agent a1" in w["command"]
    assert not _waiters(hooks["UserPromptSubmit"])
    worker = _settings("assign")["hooks"]
    assert not _waiters(worker["SessionStart"]) and not _waiters(worker["Stop"])


def test_waiter_hands_over_queued_mail_labeled(db, boss):
    wake._register("boss")
    agents.send_message(db, "boss", "the result", sender_id="w1")  # pull mode: one notice
    db.enqueue("boss", "[developer w1] done (full text: ...)", "w1")
    text = wake.wait(db, "boss", poll=0.01, limit=1)
    notice, report = text.split("\n\n")
    assert notice.startswith("brindle (") and "Call read_messages" in notice
    assert report.startswith("brindle delivered this message from developer agent w1:\n")
    assert db.pending_count("boss") == 0
    assert db.get_agent("boss").status == "processing"  # it wakes with this


def test_waiter_leaves_quietly(db, boss):
    assert wake.wait(db, "boss", poll=0.01, limit=0.05) is None  # nothing came
    assert not wake.live(db.get_agent("boss"))
    db.set_status("boss", "done")
    db.enqueue("boss", "late", None)
    assert wake.wait(db, "boss", poll=0.01, limit=1) is None  # the agent ended
    assert db.pending_count("boss") == 1


def test_messages_go_to_the_waiter_not_the_peer_inbox(db, boss, peer_inbox, monkeypatch):
    pasted = []
    monkeypatch.setattr(agents.tmux, "paste", lambda *a, **k: pasted.append(a))
    wake._register("boss")  # its waiter is watching
    assert agents.send_message(db, "boss", "the result", sender_id="w1") == "delivered"
    assert peer_inbox == [] and pasted == []
    assert db.pending_count("boss") == 1  # for the waiter
    assert agents.flush(db, "boss") is False and pasted == []

    # The waiter has handed mail over and the turn is running: the rest waits
    # for the next Stop's waiter, never the inbox.
    wake._path("boss").write_text("0 gone\n")
    db.set_status("boss", "processing")
    db.read_held("boss")
    assert agents.send_message(db, "boss", "two", sender_id="w1") == "queued"
    assert peer_inbox == [] and pasted == []


def test_stop_hands_reminders_to_the_waiter(db, boss, peer_inbox):
    wake._register("boss")
    db.set_status("boss", "processing")
    db.add_autopilot("boss")
    db.enqueue_held("boss", "the result", "w1")
    assert agents.handle_hook(db, "boss", "stop", {}) is None  # never a blocked stop
    assert peer_inbox == []
    text = wake.take(db, db.get_agent("boss"))
    assert "read_messages" in text and text.startswith(("brindle", "[brindle"))

    # Mail queued during the turn stays queued for the waiter this Stop starts.
    db.enqueue("boss", "a queued message", "w1")
    assert agents.handle_hook(db, "boss", "stop", {}) is None
    assert db.pending_count("boss") == 1 and db.get_agent("boss").status == "processing"


def test_session_without_a_waiter_keeps_the_inbox(db, boss, peer_inbox):
    # Launched by an older brindle (no waiter ever registered): unchanged.
    db.enqueue("boss", "a queued message", "w1")
    assert agents.handle_hook(db, "boss", "stop", {}) is None
    assert peer_inbox == ["a queued message"]


def test_hook_command_exits_2_with_the_mail_on_stderr(db, boss):
    db.enqueue("boss", "brindle (12:00:00): 1 new message (from w1). Call read_messages.", None)
    out = subprocess.run([sys.executable, "-m", "brindle", "_hook", wake.EVENT, "--agent", "boss"],
                         input="{}", capture_output=True, text=True, timeout=60)
    assert out.returncode == 2
    assert out.stderr.strip() == "brindle (12:00:00): 1 new message (from w1). Call read_messages."
    assert db.pending_count("boss") == 0
