"""message_delivery "pull": agents' messages to an interactive supervisor wait
unread behind one notice until read_messages (no real CLIs)."""

import time

import pytest

from brindle import agents, tmux, workspaces
from brindle.db import Agent

REAL_PULLS = agents.pulls_messages  # conftest patches it off for the other tests


@pytest.fixture
def boss(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing",
                       "@1", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(agents, "is_alive", lambda a, *_: True)
    monkeypatch.setattr(agents, "pulls_messages", REAL_PULLS)
    return ws


@pytest.fixture
def pushed(monkeypatch):
    """Texts that reached the supervisor's inbox socket."""
    out = []

    def deliver(db, agent, message_id, text, sender_id):
        out.append(text)
        db.mark_delivered(message_id)
        return True

    monkeypatch.setattr(agents, "_deliver_to_inbox", deliver)
    return out


def test_notices_never_read_the_same(db, boss, pushed, monkeypatch):
    # Claude Code drops a peer message identical to the previous one, so a
    # resent notice for the same unread messages must still differ.
    clock = iter(["16:25:03", "16:27:04"])
    monkeypatch.setattr(agents.time, "strftime", lambda fmt, *a: next(clock))
    agents.send_message(db, "boss", "one", sender_id="w1")
    _age_notices(db, db.NOTICE_TTL + 1)
    agents.send_message(db, "boss", "two", sender_id="w1")
    assert pushed == ["brindle (16:25:03): 1 new message (from w1). Call read_messages.",
                      "brindle (16:27:04): 2 new messages (from w1). Call read_messages."]


def test_read_messages_returns_and_marks_read(db, boss, pushed):
    from brindle import mcp_server

    agents.send_message(db, "boss", "the result", sender_id="w1")
    agents.send_message(db, "boss", "queued task started")
    text = mcp_server.read_messages()
    assert "[developer w1] the result" in text and "[brindle] queued task started" in text
    assert db.unread_count("boss") == 0
    assert mcp_server.read_messages() == "No unread messages."


def test_delivered_messages_are_one_line(db, boss, pushed, monkeypatch):
    from brindle import mcp_server

    report = agents.condense("Assigned task finished: done", "line two\nline three\n" * 20)
    agents.send_message(db, "boss", report, sender_id="w1")
    line = mcp_server.read_messages()
    assert "\n" not in line and "Assigned task finished: done" in line and "message_id=" in line
    message_id = int(line.rsplit("message_id=", 1)[1].rstrip(")"))
    full = mcp_server.read_messages(message_id=message_id)
    assert "line three" in full and "to_agent_id=w1" in full

    monkeypatch.setattr(agents, "pulls_messages", lambda *a: False)  # push mode
    pasted = []
    monkeypatch.setattr(agents, "_deliver_to_inbox", lambda db, a, mid, text, s: pasted.append(text) or True)
    agents.send_message(db, "boss", report, sender_id="w1")
    assert len(pasted) == 1 and "\n" not in pasted[0] and "message_id=" in pasted[0]
    pushed_id = int(pasted[0].rsplit("message_id=", 1)[1].rstrip(")"))
    assert "line three" in mcp_server.read_messages(message_id=pushed_id)

    # The milestone check pushed by brindle itself (no sender) is a line too, with
    # the audit still pending, and a long head keeps its action whole.
    from brindle import autopilot

    db.add_autopilot("boss")
    db.set_milestones("boss", [("A", "true", None)])
    db.record_check(db.milestones("boss")[0].id, True, "ok", "abc")
    pending = autopilot.AUDIT_HOLD["start"]
    head = autopilot.check_summary(db, "boss", 1, pending)
    assert "Goal reached" not in head
    action = 'merge_workspace("proj/feat")'
    milestone = agents.condense(f"[brindle] {head}. On `{'x' * 80}` run {action}", pending)
    pasted.clear()
    agents.send_message(db, "boss", milestone)
    assert len(pasted) == 1 and "\n" not in pasted[0] and action in pasted[0]

    # Antigravity supervisors get the same single pasted line, not a wrapped block.
    with db.tx() as c:
        c.execute("UPDATE agents SET provider='antigravity' WHERE id='boss'")
    pasted.clear()
    agents.send_message(db, "boss", report, sender_id="w1")
    assert len(pasted) == 1 and "\n" not in pasted[0]


def test_codex_delivery_waits_for_typed_input_to_clear(db, boss, monkeypatch):
    with db.tx() as c:
        c.execute("UPDATE agents SET provider='codex', status='idle' WHERE id='boss'")
    screen = "Done.\n\n› half a request\n  still typing\n\n  ? for shortcuts"
    pasted = []
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen)
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    monkeypatch.setattr(agents.time, "sleep", lambda *_: None)
    assert agents.send_message(db, "boss", "the result", sender_id="w1") == "queued"
    assert pasted == []
    assert db.pending_count("boss") == 1
    assert db.unread_count("boss") == 1
    assert agents.flush(db, "boss") is False
    screen = "Done.\n\n› \x1b[2mFind and fix a bug\x1b[0m\n\n  ? for shortcuts"
    assert agents.flush(db, "boss") is True
    assert len(pasted) == 1
    assert "Call read_messages." in pasted[0]
    assert db.pending_count("boss") == 0
    assert db.unread_count("boss") == 1


def test_codex_worker_delivery_ignores_input_text(db, boss, monkeypatch):
    with db.tx() as c:
        c.execute("UPDATE agents SET provider='codex', status='idle' WHERE id='w1'")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "› worker input\n  ? for shortcuts")
    monkeypatch.setattr(agents.time, "sleep", lambda *_: None)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.send_message(db, "w1", "do this", sender_id="boss") == "delivered"
    assert len(pasted) == 1 and "do this" in pasted[0]
    assert db.pending_count("w1") == 0
    assert db.unread_count("w1") == 0


def test_stop_hook_blocks_autopilot_with_unread(db, boss, pushed):
    db.add_autopilot("boss")
    agents.send_message(db, "boss", "the result", sender_id="w1")
    out = agents.handle_hook(db, "boss", "stop", {})
    assert out["decision"] == "block"
    assert "read_messages" in out["reason"]
    db.read_held("boss")
    out = agents.handle_hook(db, "boss", "stop", {})
    assert not out or "read_messages" not in out.get("reason", "")


def _age_notices(db, seconds):
    with db.tx() as c:
        c.execute("UPDATE inbox SET noticed_at=noticed_at-?", (seconds,))


def test_supervisor_stop_with_unread_mail_blocks(db, boss, pushed):
    # Its notice went out moments ago, yet it's stopping with the mail unread:
    # that notice may have been lost, and nothing else would wake it.
    agents.send_message(db, "boss", "one", sender_id="w1")
    out = agents.handle_hook(db, "boss", "stop", {})
    assert out["decision"] == "block" and "read_messages" in out["reason"]
    assert agents.handle_hook(db, "boss", "stop", {"stop_hook_active": True}) is None
    assert not agents.handle_hook(db, "boss", "stop", {})  # once per NOTICE_TTL
    db.read_held("boss")
    agents.send_message(db, "boss", "two", sender_id="w1")
    out = agents.handle_hook(db, "boss", "stop", {})  # new mail since it read: reminded again
    assert out["decision"] == "block" and "read_messages" in out["reason"]


@pytest.fixture
def via_inbox(monkeypatch):
    """The supervisor has a Claude Code inbox; texts sent to it are recorded."""
    from brindle import inbox

    sent = []
    monkeypatch.setattr(inbox, "usable", lambda a: True)
    monkeypatch.setattr(inbox, "send", lambda a, text, **k: sent.append(text) or True)
    return sent


def test_watched_supervisor_gets_stop_reminders_through_its_inbox(db, boss, pushed, via_inbox):
    # A blocked stop shows as "Stop hook error" in Claude Code, so a chat with
    # an inbox gets the same text as a message and the stop goes through.
    db.add_autopilot("boss")
    agents.send_message(db, "boss", "the result", sender_id="w1")
    _age_notices(db, db.NOTICE_TTL + 1)
    assert agents.handle_hook(db, "boss", "stop", {}) is None
    assert len(via_inbox) == 1 and "read_messages" in via_inbox[0]
    assert db.get_agent("boss").status == "processing"     # the reminder starts its next turn
    # Ignored, it isn't repeated on every stop: once per notice window.
    assert agents.handle_hook(db, "boss", "stop", {}) is None
    assert len(via_inbox) == 1


def test_queued_message_goes_through_the_inbox_at_stop(db, boss, via_inbox):
    db.enqueue("boss", "a queued message", "w1")
    assert agents.handle_hook(db, "boss", "stop", {}) is None
    assert via_inbox == ["a queued message"]


def test_tool_hooks_mark_an_idle_agent_busy(db, boss):
    # A turn begun by a finished background task runs no UserPromptSubmit,
    # so the 'idle' its previous Stop left would otherwise stick.
    db.set_status("boss", "idle")
    agents.handle_hook(db, "boss", "pre-tool", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert db.get_agent("boss").status == "processing"


def test_tool_hooks_leave_a_paused_agent_alone(db, boss):
    db.set_status("boss", "paused")
    agents.handle_hook(db, "boss", "tool-done", {})
    assert db.get_agent("boss").status == "paused"
