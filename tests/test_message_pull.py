"""message_delivery "pull": agents' messages to an interactive supervisor wait
unread behind one notice until read_messages (no real CLIs)."""

import json
import time

import pytest

from brindle import agents, launch_health, tmux, workspaces
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


@pytest.mark.variant("Claude drops identical peer notices, so later deliveries must produce distinct notices")
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

    # A notice still queued when the supervisor reads its mail announces
    # nothing: it is dropped, not pasted later to wake it for an empty read.
    _age_notices(db, db.NOTICE_TTL + 1)
    screen = "Done.\n\n› half a request\n\n  ? for shortcuts"
    assert agents.send_message(db, "boss", "another", sender_id="w1") == "queued"
    assert db.pending_count("boss") == 1
    db.read_held("boss")
    db.set_status("boss", "idle")
    screen = "Done.\n\n› \n\n  ? for shortcuts"
    assert agents.flush(db, "boss") is False
    assert len(pasted) == 1


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


@pytest.mark.variant("autopilot cannot end a turn before consuming unread worker results")
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


def test_a_person_message_goes_once_beside_read_messages(db, boss, monkeypatch):
    # read_messages takes queued person messages too, so the inbox send claims
    # its row first: a read during the send, or just before it, never doubles it.
    from brindle import inbox

    import json

    sent, read, when = [], [], ["send"]

    def reading(at):
        if when[0] == at:
            read.extend(m.body for m in db.read_held("boss"))
        if when[0] == "pane" and at == "usable":
            # A concurrent flush took the row first and its pane fallback
            # stopped after the lead: that delivery owns the row now.
            [(mid, body)] = db.conn.execute("SELECT id, body FROM inbox WHERE agent_id='boss' "
                                            "ORDER BY id DESC LIMIT 1").fetchall()
            db.record_pane_delivery(mid, "boss", json.dumps(
                {"message_id": mid, "pane": "@0", "server": agents.server_of(db.get_agent("boss")),
                 "provider": "claude", "body": body, "lead": "brindle delivered this message:",
                 "stage": "lead", "stopped": True}))

    monkeypatch.setattr(inbox, "usable", lambda a: reading("usable") or True)
    monkeypatch.setattr(inbox, "send", lambda a, text, **k: reading("send") or sent.append(text) or True)
    assert agents.send_message(db, "boss", "first answer", person=True) == "delivered"
    assert sent == ["first answer"] and read == []
    when[0] = "usable"
    assert agents.send_message(db, "boss", "second answer", person=True) == "delivered"
    assert sent == ["first answer"] and read == ["second answer"]
    assert db.pending_count("boss") == 0

    def unreadable(*a, **k):
        raise tmux.TmuxError("no pane")

    monkeypatch.setattr(tmux, "capture", unreadable)
    when[0] = "pane"
    assert agents.send_message(db, "boss", "third answer", person=True) == "queued"
    assert sent == ["first answer"]  # never the socket too: the pane delivery resumes it
    assert db.pending_count("boss") == 1 and db.read_held("boss") == []


@pytest.mark.variant("read_messages racing a restarted pane delivery's paste")
def test_a_restarted_pane_delivery_keeps_its_message_from_read_messages(db, boss, monkeypatch):
    # A delivery stopped after its lead is typed in again whole once the
    # composer is clear; meanwhile read_messages must not take the same row.
    import json

    db.set_status("boss", "idle")
    answer = "The person answered visual v_1 (r1): option A."
    mid = db.enqueue("boss", answer, None)
    db.record_pane_delivery(mid, "boss", json.dumps(
        {"message_id": mid, "pane": "@0", "server": agents.server_of(db.get_agent("boss")),
         "provider": "claude", "body": answer, "lead": "brindle delivered this message:",
         "stage": "lead", "stopped": True}))
    monkeypatch.setattr(agents, "_composer_settled", lambda a: True)
    monkeypatch.setattr(agents, "_partial_composer", lambda a, state, stage: False)
    monkeypatch.setattr(agents, "_pane_input", lambda a: None)
    pasted, read = [], []

    def paste(target, body, **k):
        read.extend(m.body for m in db.read_held("boss"))  # a read_messages mid-paste
        pasted.append(body)

    monkeypatch.setattr(tmux, "paste", paste)
    assert agents.flush(db, "boss") is True
    assert pasted == [answer] and read == []
    assert db.message_delivered(mid) and db.mark("boss", "pane-delivery") is None


@pytest.mark.variant("a background-task turn without UserPromptSubmit must clear the previous idle status")
def test_tool_hooks_mark_an_idle_agent_busy(db, boss):
    # A turn begun by a finished background task runs no UserPromptSubmit,
    # so the 'idle' its previous Stop left would otherwise stick.
    db.set_status("boss", "idle")
    agents.handle_hook(db, "boss", "pre-tool", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert db.get_agent("boss").status == "processing"


@pytest.mark.variant("a late tool hook cannot reactivate a paused agent")
def test_tool_hooks_leave_a_paused_agent_alone(db, boss):
    db.set_status("boss", "paused")
    agents.handle_hook(db, "boss", "tool-done", {})
    assert db.get_agent("boss").status == "paused"


@pytest.fixture
def pane(db, boss, monkeypatch):
    """An idle supervisor whose pane takes anything typed; what went in."""
    db.set_status("boss", "idle")
    monkeypatch.setattr(agents, "_paste_blocked", lambda a: False)
    monkeypatch.setattr(agents, "_composer_taken", lambda a: False)
    monkeypatch.setattr(agents, "_pane_input", lambda a: None)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    return pasted


def _mark(db, mid, pane, stage, **fields):
    return json.dumps({"message_id": mid, "pane": pane, "server": agents.server_of(db.get_agent("boss")),
                       "provider": "claude", "body": "m", "lead": "L", "stage": stage, **fields})


def _reopen(db, ws, monkeypatch):
    """The session comes back in window @new."""
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "new_window", lambda *a, **k: "@new")
    monkeypatch.setattr(agents, "_window_environment", lambda *a: ({}, [], None, [], {}))
    monkeypatch.setattr(agents, "_agent_env_policy", lambda ws: ({}, False))
    monkeypatch.setattr(agents, "_record_env_drops", lambda *a: None)
    agents._open_window(db, db.get_agent("boss"), ws, "chat", ["claude"], False)


def test_a_delivery_into_the_new_window_survives_its_opening(db, pane, boss, monkeypatch):
    # A flush reaching the new pane between its publication and the cleanup.
    mid = db.enqueue("boss", "m", None)
    real = db.update_agent

    def publish(agent_id, **fields):
        real(agent_id, **fields)
        db.record_pane_delivery(mid, "boss", _mark(db, mid, fields["tmux_window"], "body"))

    monkeypatch.setattr(db, "update_agent", publish)
    _reopen(db, boss, monkeypatch)
    assert json.loads(db.mark("boss", "pane-delivery"))["pane"] == "@new" and db.pop_pending("boss") is None


@pytest.mark.parametrize("stage, fields, typed, delivered", [
    ("lead", {}, ["m", "n"], True),   # never sent: back in the queue, the same row
    ("submitted", {}, ["n"], True),
    ("body", {"turn_started": True}, ["n"], False),   # may have gone in: kept unresolved
])
def test_a_delivery_into_a_pane_since_replaced_doesnt_hold_the_queue(db, pane, boss, monkeypatch, stage, fields,
                                                                     typed, delivered):
    mid = db.enqueue("boss", "m", None)
    db.record_pane_delivery(mid, "boss", _mark(db, mid, "@0", stage, **fields))
    db.enqueue("boss", "n", None)
    _reopen(db, boss, monkeypatch)
    for _ in typed:
        db.set_status("boss", "idle")
        assert agents.flush(db, "boss") is True
    assert pane == typed and db.message_delivered(mid) is delivered and db.pending_count("boss") == 0


def _raises(exc):
    def fail(*a, **k):
        raise exc
    return fail


@pytest.mark.variant("a process dying between its claim and its first write")
def test_a_process_dying_right_after_the_claim_loses_nothing(db, pane, monkeypatch):
    real = launch_health.before_delivery
    mid = db.enqueue("boss", "m", None)
    monkeypatch.setattr(launch_health, "before_delivery", _raises(KeyboardInterrupt))
    monkeypatch.setattr(db, "requeue_claimed", lambda *a: None)   # a dead process cleans nothing up
    with pytest.raises(KeyboardInterrupt):
        agents.flush(db, "boss")
    assert not db.message_delivered(mid)

    monkeypatch.setattr(launch_health, "before_delivery", real)
    monkeypatch.setattr(agents, "_composer_settled", lambda a: True)
    monkeypatch.setattr(agents, "_partial_composer", lambda a, state, stage: False)
    db.set_status("boss", "idle")
    assert agents.flush(db, "boss") is True and pane == ["m"] and db.message_delivered(mid)


@pytest.mark.parametrize("how", [
    pytest.param("blocked", marks=pytest.mark.variant("A pre-paste block preserves status_since so stop notices do not repeat")),
    pytest.param("paste", marks=pytest.mark.variant("A block raised during paste restores the original stop timestamp")),
])
def test_a_failed_delivery_leaves_the_agent_on_the_same_stop(db, pane, monkeypatch, how):
    # Notices about a stop go by status_since: a new one would repeat them.
    db.update_agent("boss", status_since=1000.0)
    mid = db.enqueue("boss", "m", None)
    if how == "blocked":
        monkeypatch.setattr(agents, "_paste_blocked", lambda a: True)
    else:
        monkeypatch.setattr(tmux, "paste", _raises(agents._PasteBlocked))
    assert agents.flush(db, "boss") is False
    a = db.get_agent("boss")
    assert (a.status, a.status_since) == ("idle", 1000.0)
    assert not db.message_delivered(mid) and db.mark("boss", "pane-delivery") is None


@pytest.mark.parametrize("failure", [
    pytest.param(tmux.TmuxError("pane gone"),
                 marks=pytest.mark.variant("A vanished pane after enqueue must return queued so callers do not duplicate the message")),
    pytest.param(agents.AgentError("signed in as someone else"), marks=pytest.mark.variant("identity lock raised mid-flush")),
])
def test_a_message_already_queued_is_never_reported_as_failed(db, pane, monkeypatch, failure):
    # A caller told it failed sends it again, or queues a second copy.
    monkeypatch.setattr(agents, "flush", _raises(failure))
    assert agents.send_message(db, "boss", "m", person=True) == "queued"
    assert db.pending_count("boss") == 1
