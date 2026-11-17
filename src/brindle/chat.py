"""Contracts for the approved chat-ui5 composer and chat-ui4 queue option A.

This is user-authored input to a live CLI, never a headless chat transport.
Four implementation owners:
* send: send/deliver and routes_actions' chat-send region; ChatBox send region.
* queue: enqueue through on_turn_end, queue routes, AgentDTO.chat_queue and
  view.agent_entry/hook integration; ChatBox queue region.
* attach: materialize_attachments; ChatBox paste/drop region.
* pane: ChatPane, its frame, terminal theme and conditional prompt clipping.

Shared DTOs/client signatures are fixed here; coordinate changes across owners.
The design's terms assumptions still require verification before activation:
deferred input is authorized by Send; clipping must reveal permission/choice
rows; Claude's ANSI-only theme and orange slot need live verification.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from brindle import agents, attachments as files, identity_lock, providers, tmux
from brindle.db import DB, Agent
from brindle.service.dto import ChatAttachmentDTO, ChatMessageDTO, ChatResultDTO, QueuedChatMessageDTO
from brindle.service.errors import ServiceError


class ChatNotImplemented(ServiceError):
    def __init__(self) -> None:
        super().__init__("not_implemented", "Chat input is not implemented", 501)


# -- send owner ---------------------------------------------------------------
_sending: set[str] = set()
_sending_lock = Lock()


@contextmanager
def _delivery(agent_id: str):
    # The shared pane lock is re-entrant. Refuse a second chat dispatch even
    # within the same thread, and retain its draft via a conflict response.
    with _sending_lock:
        if agent_id in _sending:
            raise ServiceError("conflict", "a chat send is already in progress", 409)
        _sending.add(agent_id)
    try:
        with agents.delivery_lock(agent_id):
            yield
    finally:
        with _sending_lock:
            _sending.remove(agent_id)


def _ready(db: DB, agent: Agent) -> bool:
    return agent.status == "idle" and db.latest_permission_request(agent.id) is None


def _destination(agent: Agent) -> list:
    return [agent.tmux_window, agent.tmux_server, agent.workspace_id, agent.provider, agent.created_at]


def _reconcile_chat_paste(db: DB, agent: Agent) -> None:
    saved = db.mark(agent.id, "chat-paste")
    if saved is None:
        return
    # Never append another draft to an interrupted paste, or press Enter for
    # it. The person must resolve that text in the CLI first. Unknown layouts,
    # changed destinations and input arriving during the read all fail closed.
    state = json.loads(saved)
    typed = agents._pane_input(agent)
    # Programmatic pastes can still be pending behind an empty rendered
    # prompt. Require new person input after the interrupted write as well
    # as the settled screen; an old client attaching is not new input.
    if (state["destination"] != _destination(agent)
            or not state.get("input_known")
            or not isinstance(typed, (int, float))
            or typed <= max(state.get("observed_at", float("inf")), state.get("input") or 0)
            or not agents._composer_settled(agent)
            or agents._typed_lately(agent, since=typed)):
        raise ServiceError("conflict", "resolve the previous chat paste in its CLI before sending again", 409)
    db.set_mark(agent.id, "chat-paste", None)


def _live_agent(db: DB, agent_id: str) -> Agent:
    agent = db.get_agent(agent_id)
    if agent is None:
        raise ServiceError("not_found", f"no agent {agent_id}", 404)
    identity_lock.ensure(agent)
    if (agent.status not in ("starting", "idle", "processing", "waiting")
            or not agent.tmux_window or agent.headless):
        raise ServiceError("conflict", "send chat to a running CLI agent", 409)
    return agent


def _validate_message(message: ChatMessageDTO) -> None:
    if not message.text.strip() and not message.attachments:
        raise ServiceError("invalid", "give text or attachments")


def send(db: DB, agent_id: str, message: ChatMessageDTO) -> ChatResultDTO:
    """Idle: deliver; busy or asking permission: enqueue. No generated lead.

    Resolve the exact live agent and enforce its identity lock. Reject an empty
    message without attachments. Preserve all text, including large pastes.
    """
    with _delivery(agent_id):
        agent = _live_agent(db, agent_id)
        _validate_message(message)
        if not _ready(db, agent):
            return enqueue(db, agent.id, message)
        _deliver(db, agent.id, message)
    return ChatResultDTO(status="delivered")


def deliver(db: DB, agent_id: str, message: ChatMessageDTO) -> None:
    """Materialize bytes, then tmux.paste(full text plus file refs, submit=True).

    Use the agent's pane/server and recheck identity at external writes. A
    failed delivery must leave the draft/queue item available to the person.
    """
    with _delivery(agent_id):
        _deliver(db, agent_id, message)


def _deliver(db: DB, agent_id: str, message: ChatMessageDTO) -> None:
    agent = _live_agent(db, agent_id)
    _validate_message(message)
    if not _ready(db, agent):
        raise ServiceError("conflict", "the agent is not ready for chat input", 409)
    _reconcile_chat_paste(db, agent)
    paths = materialize_attachments(db, agent.id, message.attachments)
    provider = providers.PROVIDERS.get(agent.provider, providers.Provider())
    references = " ".join(provider.format_attachment(str(path)) for path in paths)
    text = message.text + ("\n" if message.text and references else "") + references

    def authorize() -> None:
        fresh = _live_agent(db, agent.id)
        if not _ready(db, fresh):
            raise ServiceError("conflict", "the agent is not ready for chat input", 409)
        if ((fresh.tmux_window, fresh.tmux_server, fresh.workspace_id, fresh.provider, fresh.created_at)
                != (agent.tmux_window, agent.tmux_server, agent.workspace_id, agent.provider, agent.created_at)):
            raise ServiceError("conflict", "the agent's CLI changed before delivery", 409)

    authorize()

    state = {}

    def progress(stage: str) -> None:
        typed = agents._pane_input(agent)
        known = typed is not agents._INPUT_UNREAD
        state.update(destination=_destination(agent), stage=stage, observed_at=time.time(),
                     input_known=known, input=typed if known else None)
        db.set_mark(agent.id, "chat-paste", json.dumps(state))

    # Record before transport: even a process exit or an ambiguous write
    # failure between paste-buffer and its progress callback must hold Send.
    progress("prepared")
    try:
        tmux.paste(agent.tmux_window, text, submit=True, server=agent.tmux_server or None,
                   authorize=authorize, progress=progress)
    except BaseException:
        # Input during the interrupted transport is not a later resolution.
        progress(state["stage"])
        raise
    db.set_mark(agent.id, "chat-paste", None)


# -- queue owner --------------------------------------------------------------
def enqueue(db: DB, agent_id: str, message: ChatMessageDTO) -> ChatResultDTO:
    """Hold text and bytes in brindle, FIFO per agent; no CLI input yet.

    Return status=queued and message_id. Publish snapshot invalidation after
    each mutation; snapshot entries contain attachment names, never base64.
    """
    raise ChatNotImplemented()


def queue_snapshot(db: DB, agent_id: str) -> list[QueuedChatMessageDTO]:
    """Populate AgentDTO.chat_queue via view.agent_entry; locked agents reveal none."""
    raise ChatNotImplemented()


def remove(db: DB, agent_id: str, message_id: str) -> None:
    """Remove only this agent's queued item, without writing to its CLI."""
    raise ChatNotImplemented()


def send_now(db: DB, agent_id: str, message_id: str) -> ChatResultDTO:
    """Esc, await the live prompt, then deliver this item; keep other items.

    Claim once; repeated triggers respect an in-flight delivery. Remove the
    item only after successful delivery; never replace an active claim.
    """
    raise ChatNotImplemented()


def on_turn_end(db: DB, agent_id: str, turn_id: str, *, permission_prompt: bool = False) -> None:
    """Release one FIFO item per distinct completed turn, never at permission.

    The hook/status adapter supplies a stable turn id (duplicate observations
    must not drain the queue). Wire the real turn-end dispatch here; a waiting
    status or a permission notification alone cannot authorize release.
    """
    raise ChatNotImplemented()


# -- attach owner -------------------------------------------------------------
def materialize_attachments(db: DB, agent_id: str, attachments: list[ChatAttachmentDTO]) -> list[Path]:
    """Decode {name, data_base64} in the agent's worktree using attachments.py.

    No source path is required, including for dropped screenshot thumbnails.
    Validate names, base64, size and provider support before writing; return
    safe local paths for the CLI's file-reference syntax, preserving bytes.
    """
    agent = _live_agent(db, agent_id)
    if not attachments:
        return []
    workspace = db.get_workspace(agent.workspace_id)
    if workspace is None:
        raise ServiceError("not_found", f"no workspace {agent.workspace_id}", 404)
    provider = providers.PROVIDERS.get(agent.provider, providers.Provider())
    if not provider.accepts_files:
        raise ServiceError("unsupported", "this agent's provider does not accept file attachments")
    # Validate the whole batch before writing even its first file.
    for attachment in attachments:
        files.decode_clipboard(attachment.name, attachment.data_base64)

    def authorize() -> None:
        fresh = _live_agent(db, agent.id)
        current_workspace = db.get_workspace(agent.workspace_id)
        if ((fresh.workspace_id, fresh.provider, fresh.created_at)
                != (agent.workspace_id, agent.provider, agent.created_at)
                or current_workspace is None or current_workspace.path != workspace.path):
            raise ServiceError("conflict", "the agent's workspace changed before attachment", 409)

    paths = []
    for attachment in attachments:
        authorize()
        result = files.attach(workspace.path, provider, attachment.to_json(), authorize=authorize)
        paths.extend(Path(path) for path in result["paths"])
    return paths
