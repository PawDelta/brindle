"""Executable handoff contracts for the chat message box.

Send: -k send; queue: -k test_queue_; attachments: -k attach.
No real CLI, network, or git processes.
"""

import base64
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle import agents, attachments, chat, identity_lock, tmux, view
from brindle.daemon import routes_actions
from brindle.db import Agent, Workspace
from brindle.service.dto import ChatAttachmentDTO, ChatMessageDTO
from brindle.service.errors import ServiceError
from brindle.view_reads import ViewReads

real_paste = tmux.paste


@dataclass
class Pane:
    """CLI boundary fake: records submitted turns and interrupt ordering."""

    events: list = field(default_factory=list)
    turns: list[str] = field(default_factory=list)

    def paste(self, target, text, submit=True, **kwargs):
        assert target == "%1"
        if authorize := kwargs.get("authorize"):
            authorize()
        if submit:
            self.turns.append((kwargs.get("lead") or "") + text)
            self.events.append("submitted")

    def keys(self, target, *keys, **kwargs):
        assert target == "%1"
        self.events.extend(keys)


@pytest.fixture
def live(db, tmp_path, monkeypatch):
    db.add_workspace(Workspace("chat", str(tmp_path), "chat", "root", "main", None,
                               str(tmp_path), None, "chat", 1))
    db.add_agent(Agent("a", "chat", "supervisor", "claude", None, "interactive", "idle", "%1", None, 1))
    db.add_agent(Agent("b", "chat", "supervisor", "claude", None, "interactive", "idle", "%2", None, 1))
    pane = Pane()
    monkeypatch.setattr(tmux, "paste", pane.paste)
    monkeypatch.setattr(tmux, "send_keys", pane.keys)
    monkeypatch.setattr(tmux, "pane_input", lambda *args, **kwargs: None)
    monkeypatch.setattr(attachments.git, "exclude", lambda *args: None)
    return pane


# -- send owner ---------------------------------------------------------------
@pytest.mark.variant("Multi-line and large pasted text must reach the CLI whole as one submitted turn")
def test_send_text_reaches_live_cli_as_one_submitted_turn(db, live):
    result = routes_actions.chat_send(db, {"id": "a"}, {"text": "Look at this\nthen explain it"})
    assert result["status"] == "delivered"
    assert live.turns == ["Look at this\nthen explain it"]
    # A big multi-line paste with Unicode reaches the CLI whole.
    text = "Please inspect this log\n" + "a long log line with Unicode: ✓\n" * 412
    routes_actions.chat_send(db, {"id": "a"}, {"text": text})
    assert live.turns[-1] == text


@pytest.mark.parametrize("condition", [
    "empty", "missing", "stopped",
    pytest.param("headless", marks=pytest.mark.variant("A headless agent may retain a pane but cannot receive live CLI input")),
    pytest.param("locked", marks=pytest.mark.variant("Already locked chat sends must be rejected before preparing a paste")),
])
def test_send_rejects_unavailable_or_empty_input(db, live, monkeypatch, condition):
    agent_id, text = "a", "My message"
    error = ServiceError
    if condition == "empty":
        text = " \n"
    elif condition == "missing":
        agent_id = "absent"
    elif condition == "stopped":
        db.update_agent("a", status="done")
    elif condition == "headless":
        db.update_agent("a", headless=1)
    else:
        monkeypatch.setattr(identity_lock, "locked_by", lambda _: "another identity")
        error = agents.AgentError
    with pytest.raises(error):
        routes_actions.chat_send(db, {"id": agent_id}, {"text": text})
    assert live.turns == []


@pytest.mark.parametrize("change", [
    pytest.param("identity", marks=pytest.mark.variant("A sign-in change during transport preparation must prevent submission")),
    "pane",
])
def test_send_reauthorizes_after_transport_preparation(db, live, monkeypatch, change):
    def prepare(target, text, **kwargs):
        if change == "identity":
            monkeypatch.setattr(identity_lock, "locked_by", lambda _: "another identity")
        else:
            db.update_agent("a", tmux_window="%2")
        live.paste(target, text, **kwargs)

    monkeypatch.setattr(tmux, "paste", prepare)
    with pytest.raises((agents.AgentError, ServiceError)):
        chat.send(db, "a", ChatMessageDTO(text="My message"))
    assert live.turns == []


@pytest.fixture
def transport(live, monkeypatch):
    buffers = {}
    composer = []
    writes = []
    boundary = []
    clock = [100.0]
    input_time = [10.0]

    def command(*args, input=None, authorize=None, **kwargs):
        if authorize:
            authorize()
        writes.append(args[0])
        if args[0] == "load-buffer":
            buffers[args[2]] = input
        elif args[0] == "paste-buffer":
            composer.append(buffers[args[4]])
        elif args[0] == "send-keys":
            live.turns.append("".join(composer))
            composer.clear()
        if boundary:
            boundary[0](args[0])

    monkeypatch.setattr(tmux, "_tmux", command)
    monkeypatch.setattr(tmux, "capture", lambda *args, **kwargs: "❯ " + "".join(composer) + "\n────────")
    monkeypatch.setattr(tmux, "pane_input", lambda *args, **kwargs: input_time[0])
    monkeypatch.setattr(agents, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(chat, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(tmux, "paste", lambda *args, **kwargs: real_paste(
        *args, **kwargs, debounce=lambda: None))
    return writes, boundary, composer, clock, input_time


@pytest.mark.parametrize(("stage", "change"), [
    pytest.param("load-buffer", "permission", marks=pytest.mark.variant(
        "A permission arriving before the buffer load must withhold every write")),
    pytest.param("paste-buffer", "permission", marks=pytest.mark.variant(
        "A permission arriving after paste must still withhold Enter")),
    pytest.param("load-buffer", "processing", marks=pytest.mark.variant(
        "A busy CLI without a permission request must also block the prepared paste")),
])
def test_send_withholds_writes_when_readiness_changes(db, live, transport, monkeypatch, stage, change):
    writes, boundary, composer, clock, input_time = transport

    def permission(command):
        if command == stage:
            if change == "permission":
                db.add_permission_request("a", "request", "{}", "approval")
            else:
                db.update_agent("a", status="processing")

    boundary.append(permission)
    message = ChatMessageDTO(text="My draft")
    with pytest.raises(ServiceError):
        chat.send(db, "a", message)
    assert live.turns == []
    assert "send-keys" not in writes
    if stage == "load-buffer":
        assert "paste-buffer" not in writes
    assert message.text == "My draft"
    boundary.clear()
    db.clear_permission_requests("a")
    db.update_agent("a", status="idle")
    if stage == "paste-buffer":
        # Both the retained draft and an edited Send must leave the old body
        # alone until the person resolves it in the CLI.
        for text in ("My draft", "Edited draft"):
            with pytest.raises(ServiceError):
                chat.send(db, "a", ChatMessageDTO(text=text))
        assert live.turns == []
        assert composer == ["My draft"]
        # The CLI can still render its old empty prompt while the paste is
        # pending. That empty screen is no evidence the person resolved it.
        clock[0] = 180.0
        with monkeypatch.context() as delayed:
            delayed.setattr(tmux, "capture", lambda *args, **kwargs: "❯ \n────────")
            with pytest.raises(ServiceError):
                chat.send(db, "a", ChatMessageDTO(text="Edited draft"))
        assert live.turns == []
        assert composer == ["My draft"]
        composer.clear()  # The person clears the partial text in the CLI.
        with pytest.raises(ServiceError):
            chat.send(db, "a", message)
        # An empty composer in a replacement pane cannot resolve the old one.
        db.update_agent("a", tmux_window="%2")
        with pytest.raises(ServiceError):
            chat.send(db, "a", message)
        db.update_agent("a", tmux_window="%1")
    # Positive CLI input after the interrupted write, followed by a settled
    # empty composer, authorizes another explicit Send.
    input_time[0] = 190.0
    clock[0] = 200.0
    message = ChatMessageDTO(text="Edited draft")
    assert chat.send(db, "a", message).status == "delivered"
    assert live.turns == ["Edited draft"]


def test_send_overlapping_dispatch_preserves_competing_draft(db, live, transport):
    writes, boundary, _, _, _ = transport
    competing = ChatMessageDTO(text="Second message")

    def overlap(command):
        if command == "paste-buffer":
            boundary.clear()
            with pytest.raises(ServiceError):
                chat.send(db, "a", competing)

    boundary.append(overlap)
    assert chat.send(db, "a", ChatMessageDTO(text="First message")).status == "delivered"
    assert live.turns == ["First message"]
    assert writes.count("load-buffer") == 1
    assert competing.text == "Second message"
    # A completed delivery releases ownership for a subsequent explicit Send.
    chat.send(db, "a", competing)
    assert live.turns == ["First message", "Second message"]


# -- queue owner --------------------------------------------------------------
def test_queue_busy_send_waits_in_brindle_and_appears_in_snapshot(db, live):
    db.update_agent("a", status="processing")
    result = routes_actions.chat_send(db, {"id": "a"}, {"text": "Next task"})
    assert result["status"] == "queued"
    assert live.turns == []
    reads = ViewReads(db)  # the snapshot's batched reads
    entry = view.agent_entry(reads, reads.get_agent("a"), alive=True, read_screen=False)
    assert [(item["id"], item["text"]) for item in entry["chat_queue"]] == [(result["message_id"], "Next task")]
    assert chat.queue_snapshot(db, "b") == []


@pytest.mark.variant("A removed queued message must never be typed at the next turn end")
def test_queue_remove_never_types_the_removed_message(db, live):
    queued = routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Discard me"})
    routes_actions.chat_remove(db, {"id": "a", "message_id": queued["message_id"]}, {})
    chat.on_turn_end(db, "a", "turn-1")
    assert chat.queue_snapshot(db, "a") == []
    assert live.turns == []


@pytest.mark.variant("Turn end must release one message in FIFO order and ignore a repeated turn id")
def test_queue_turn_end_releases_one_fifo_message_and_deduplicates_turn(db, live):
    for text in ("First", "Second"):
        routes_actions.chat_enqueue(db, {"id": "a"}, {"text": text})
    chat.on_turn_end(db, "a", "turn-1")
    chat.on_turn_end(db, "a", "turn-1")
    assert live.turns == ["First"]
    assert [item.text for item in chat.queue_snapshot(db, "a")] == ["Second"]
    chat.on_turn_end(db, "a", "turn-2")
    assert live.turns == ["First", "Second"]


@pytest.mark.variant("A permission prompt must not release a queued message into the prompt")
def test_queue_permission_prompt_does_not_release_a_message(db, live):
    routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "After the turn"})
    chat.on_turn_end(db, "a", "turn-1", permission_prompt=True)
    assert live.turns == []
    assert len(chat.queue_snapshot(db, "a")) == 1
    chat.on_turn_end(db, "a", "turn-1")
    assert live.turns == ["After the turn"]


@pytest.mark.variant("Send now must interrupt and submit only the selected item, leaving the rest queued")
def test_queue_send_now_interrupts_then_submits_only_selected_item(db, live):
    routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Keep waiting"})
    selected = routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Do now"})
    routes_actions.chat_send_now(db, {"id": "a", "message_id": selected["message_id"]}, {})
    assert live.events == ["Escape", "submitted"]
    assert live.turns == ["Do now"]
    assert [item.text for item in chat.queue_snapshot(db, "a")] == ["Keep waiting"]


def test_queue_send_now_twice_types_the_message_once(db, live, monkeypatch):
    selected = routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Do now"})
    route = {"id": "a", "message_id": selected["message_id"]}
    refused = []

    def paste(target, text, **kwargs):
        # A second Send now while the first is typing respects it: no Esc, no paste.
        with pytest.raises(ServiceError) as second:
            routes_actions.chat_send_now(db, route, {})
        refused.append(second.value.code)
        live.paste(target, text, **kwargs)

    monkeypatch.setattr(tmux, "paste", paste)
    routes_actions.chat_send_now(db, route, {})
    # And once it is delivered, a late repeat finds nothing left to send.
    with pytest.raises(ServiceError):
        routes_actions.chat_send_now(db, route, {})
    assert refused == ["conflict"]
    assert live.events == ["Escape", "submitted"]
    assert live.turns == ["Do now"]


def test_queue_send_now_leaves_a_hook_held_permission_unanswered(db, live):
    selected = routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Do now"})
    db.update_agent("a", status="waiting")
    db.add_permission_request("a", "tool-1", "{}", "approval", channel="hook", hold_until=9e9)
    with pytest.raises(ServiceError) as refused:
        routes_actions.chat_send_now(db, {"id": "a", "message_id": selected["message_id"]}, {})
    # Its own code, which the app's chip turns into "answer the request first".
    assert (refused.value.code, refused.value.http_status) == ("permission_pending", 409)
    assert live.events == []
    assert [item.text for item in chat.queue_snapshot(db, "a")] == ["Do now"]


@pytest.mark.parametrize("trigger", [
    pytest.param("send_now", marks=pytest.mark.variant(
        "Remove racing a Send now paste must be refused, not drop a message already being typed")),
    pytest.param("turn_end", marks=pytest.mark.variant(
        "Remove racing a turn-end release must be refused, not drop a message already being typed")),
])
def test_queue_remove_refuses_a_message_being_typed(db, live, monkeypatch, trigger):
    queued = routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "In flight"})
    route = {"id": "a", "message_id": queued["message_id"]}
    refused = []

    def paste(target, text, **kwargs):
        # Remove before the external write: the chip must not vanish and the text still go in.
        with pytest.raises(ServiceError) as removing:
            routes_actions.chat_remove(db, route, {})
        refused.append(removing.value.code)
        live.paste(target, text, **kwargs)

    monkeypatch.setattr(tmux, "paste", paste)
    if trigger == "send_now":
        routes_actions.chat_send_now(db, route, {})
    else:
        chat.on_turn_end(db, "a", "turn-1")
    assert refused == ["conflict"]
    assert live.turns == ["In flight"]
    assert chat.queue_snapshot(db, "a") == []


@pytest.mark.parametrize(("provider", "event", "payload"), [
    ("claude", "stop", {"session_id": "s"}),
    ("codex", "codex-notify", {"type": "agent-turn-complete", "turn-id": "t1", "thread-id": "s"}),
])
def test_queue_stop_hook_releases_one_item_per_turn(db, live, monkeypatch, provider, event, payload):
    helpers = []
    monkeypatch.setattr(agents.processes, "detach", lambda argv, **kwargs: helpers.append(argv))
    for text in ("First", "Second"):
        routes_actions.chat_enqueue(db, {"id": "a"}, {"text": text})
    db.update_agent("a", status="processing", provider=provider)
    for _ in range(2):  # the turn's end, then a repeated observation of it
        agents.handle_hook(db, "a", event, payload)
    for argv in helpers:  # what each started `_chat-turn-end` helper runs
        chat.on_turn_end(db, "a", argv[-1])
    assert live.turns == ["First"]
    assert [item.text for item in chat.queue_snapshot(db, "a")] == ["Second"]


# -- attach owner -------------------------------------------------------------
@pytest.mark.variant("Clipboard screenshot bytes without a source path must be saved under the workspace and sent by reference")
def test_attach_screenshot_bytes_materialize_without_a_source_path(db, live):
    # A tiny valid PNG, such as a screenshot thumbnail delivered by the clipboard.
    data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6KAAAAABJRU5ErkJggg=="
    paths = chat.materialize_attachments(db, "a", [ChatAttachmentDTO(name="Screenshot.png", data_base64=data)])
    assert len(paths) == 1
    assert paths[0].read_bytes() == base64.b64decode(data)
    assert paths[0].is_relative_to(db.get_workspace("chat").path)
    assert live.turns == []
    routes_actions.chat_send(db, {"id": "a"}, {"text": "", "attachments": [
        {"name": "Screenshot.png", "data_base64": data},
    ]})
    assert len(live.turns) == 1
    assert live.turns[0].startswith("@") and "Screenshot.png" in live.turns[0]
    assert data not in live.turns[0]


@pytest.mark.variant("Text attachments must be sent as a file reference, never inlined as base64")
def test_attach_send_delivers_text_and_materialized_file_reference(db, live):
    message = ChatMessageDTO(text="Read this", attachments=[
        ChatAttachmentDTO(name="notes.txt", data_base64=base64.b64encode(b"full contents").decode())])
    routes_actions.chat_send(db, {"id": "a"}, message.to_json())
    assert len(live.turns) == 1
    assert live.turns[0].startswith("Read this")
    assert "@" in live.turns[0] and "notes.txt" in live.turns[0]
    assert "ZnVsbCBjb250ZW50cw==" not in live.turns[0]
    saved = list((Path(db.get_workspace("chat").path) / ".brindle" / "attachments").iterdir())
    assert [path.read_bytes() for path in saved] == [b"full contents"]


@pytest.mark.parametrize("condition", [
    pytest.param("base64", marks=pytest.mark.variant("Malformed second payload must not save the valid first attachment")),
    pytest.param("size", marks=pytest.mark.variant("Oversized second payload must not save the valid first attachment")),
    "provider",
])
def test_attach_validates_entire_batch_before_saving(db, live, monkeypatch, condition):
    data = "YQ=="
    if condition == "base64":
        data = "invalid!"
    elif condition == "size":
        monkeypatch.setattr(attachments, "MAX_BYTES", 1)
        data = "YWE="
    else:
        db.update_agent("a", provider="subagent")
    with pytest.raises(ServiceError):
        chat.send(db, "a", ChatMessageDTO(text="", attachments=[
            ChatAttachmentDTO(name="valid.txt", data_base64="YQ=="),
            ChatAttachmentDTO(name="second.txt", data_base64=data),
        ]))
    assert not (Path(db.get_workspace("chat").path) / ".brindle" / "attachments").exists()
    assert live.turns == []


@pytest.mark.variant("A sign-in change during attachment preparation must prevent the byte write")
def test_attach_rechecks_identity_before_writing_bytes(db, live, monkeypatch):
    def change_identity(*args):
        monkeypatch.setattr(identity_lock, "locked_by", lambda _: "another identity")

    monkeypatch.setattr(attachments.git, "exclude", change_identity)
    with pytest.raises(agents.AgentError):
        chat.send(db, "a", ChatMessageDTO(text="", attachments=[
            ChatAttachmentDTO(name="notes.txt", data_base64="YQ=="),
        ]))
    directory = Path(db.get_workspace("chat").path) / ".brindle" / "attachments"
    assert list(directory.iterdir()) == []
    assert live.turns == []
