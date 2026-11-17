"""Executable handoff contracts; unimplemented queue cases remain xfailed.

Send: -k send; queue: -k queue; attachments: -k attach. Remove the relevant
xfail as its implementation lands. Only the explicit stub exception is expected;
other exceptions remain failures. No real CLI, network, or git processes.
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

queue_stub = pytest.mark.xfail(raises=chat.ChatNotImplemented, strict=True, reason="chat queue contract stub")
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
def test_send_text_reaches_live_cli_as_one_submitted_turn(db, live):
    result = routes_actions.chat_send(db, {"id": "a"}, {"text": "Look at this\nthen explain it"})
    assert result["status"] == "delivered"
    assert live.turns == ["Look at this\nthen explain it"]


def test_send_big_pasted_text_is_not_truncated(db, live):
    text = "Please inspect this log\n" + "a long log line with Unicode: ✓\n" * 412
    routes_actions.chat_send(db, {"id": "a"}, {"text": text})
    assert live.turns == [text]


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
    ("load-buffer", "permission"),
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
@queue_stub
def test_queue_busy_send_waits_in_brindle_and_appears_in_snapshot(db, live):
    db.update_agent("a", status="processing")
    result = routes_actions.chat_send(db, {"id": "a"}, {"text": "Next task"})
    assert result["status"] == "queued"
    assert live.turns == []
    entry = view.agent_entry(db, db.get_agent("a"), alive=True, read_screen=False)
    assert [(item["id"], item["text"]) for item in entry["chat_queue"]] == [(result["message_id"], "Next task")]
    assert chat.queue_snapshot(db, "b") == []


@queue_stub
def test_queue_remove_never_types_the_removed_message(db, live):
    queued = routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Discard me"})
    routes_actions.chat_remove(db, {"id": "a", "message_id": queued["message_id"]}, {})
    chat.on_turn_end(db, "a", "turn-1")
    assert chat.queue_snapshot(db, "a") == []
    assert live.turns == []


@queue_stub
def test_queue_turn_end_releases_one_fifo_message_and_deduplicates_turn(db, live):
    for text in ("First", "Second"):
        routes_actions.chat_enqueue(db, {"id": "a"}, {"text": text})
    chat.on_turn_end(db, "a", "turn-1")
    chat.on_turn_end(db, "a", "turn-1")
    assert live.turns == ["First"]
    assert [item.text for item in chat.queue_snapshot(db, "a")] == ["Second"]
    chat.on_turn_end(db, "a", "turn-2")
    assert live.turns == ["First", "Second"]


@queue_stub
def test_queue_permission_prompt_does_not_release_a_message(db, live):
    routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "After the turn"})
    chat.on_turn_end(db, "a", "turn-1", permission_prompt=True)
    assert live.turns == []
    assert len(chat.queue_snapshot(db, "a")) == 1
    chat.on_turn_end(db, "a", "turn-1")
    assert live.turns == ["After the turn"]


@queue_stub
def test_queue_send_now_interrupts_then_submits_only_selected_item(db, live):
    routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Keep waiting"})
    selected = routes_actions.chat_enqueue(db, {"id": "a"}, {"text": "Do now"})
    routes_actions.chat_send_now(db, {"id": "a", "message_id": selected["message_id"]}, {})
    assert live.events == ["Escape", "submitted"]
    assert live.turns == ["Do now"]
    assert [item.text for item in chat.queue_snapshot(db, "a")] == ["Keep waiting"]


# -- attach owner -------------------------------------------------------------
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
