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

import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from brindle import agents, attachments as files, identity_lock, providers, tmux
from brindle.db import DB, Agent
from brindle.service.dto import ChatAttachmentDTO, ChatMessageDTO, ChatResultDTO, QueuedChatMessageDTO
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)


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
# The engine takes the message box's messages (send and queue): /v1/meta's
# ``chat``, which is what mounts the app's message box (and hides Claude Code's
# own prompt rows). Off for now by the owner's decision (2026-11-18): sessions use
# Claude Code's own input again; the box, queue and clipping code stay in place.
SUPPORTED = False

_QUEUE = "chat-queue"          # mark: JSON list of {"id", "text", "attachments": [{name, data_base64}]}
_TURNS = "chat-queue-turns"    # mark: JSON list of the turn ids that released an item
_TURNS_KEPT = 32
# Screen reads (two samples each, about 1.4s) send-now makes for the prompt to
# come back after its Esc, before it leaves the item queued and says so.
PROMPT_CHECKS = 15


def _queue_lock(agent_id: str):
    """Held around each read-modify-write of an agent's queue, across processes."""
    from brindle import pipeline

    return pipeline._reentrant_lock("chat-queue-" + hashlib.sha1(agent_id.encode()).hexdigest()[:16])


def _items(db: DB, agent_id: str) -> list[dict]:
    saved = db.mark(agent_id, _QUEUE)
    return json.loads(saved) if saved else []


def _store(db: DB, agent_id: str, items: list[dict]) -> None:
    db.set_mark(agent_id, _QUEUE, json.dumps(items) if items else None)


def _changed(db: DB, agent_id: str) -> None:
    from brindle import bus

    bus.publish_agent(db, "agent.chat_queue", agent_id)


def _message(item: dict) -> ChatMessageDTO:
    return ChatMessageDTO(text=item["text"], attachments=[ChatAttachmentDTO(**a) for a in item["attachments"]])


def enqueue(db: DB, agent_id: str, message: ChatMessageDTO) -> ChatResultDTO:
    """Hold text and bytes in brindle, FIFO per agent; no CLI input yet.

    Return status=queued and message_id. Publish snapshot invalidation after
    each mutation; snapshot entries contain attachment names, never base64.
    """
    agent = _live_agent(db, agent_id)
    _validate_message(message)
    if message.attachments:
        # Refuse now what delivery would refuse later, while the draft is still in the box.
        if not providers.PROVIDERS.get(agent.provider, providers.Provider()).accepts_files:
            raise ServiceError("unsupported", "this agent's provider does not accept file attachments")
        for attachment in message.attachments:
            files.decode_clipboard(attachment.name, attachment.data_base64)
    item = {"id": uuid.uuid4().hex[:12], "text": message.text,
            "attachments": [a.to_json() for a in message.attachments]}
    with _queue_lock(agent.id):
        _store(db, agent.id, [*_items(db, agent.id), item])
    _changed(db, agent.id)
    return ChatResultDTO(status="queued", message_id=item["id"])


def queue_snapshot(db: DB, agent_id: str) -> list[QueuedChatMessageDTO]:
    """Populate AgentDTO.chat_queue via view.agent_entry; locked agents reveal none."""
    if identity_lock.locked_by(agent_id):
        return []
    return [QueuedChatMessageDTO(id=item["id"], text=item["text"],
                                 attachments=[a["name"] for a in item["attachments"]])
            for item in _items(db, agent_id)]


def _take(db: DB, agent_id: str, message_id: str) -> bool:
    with _queue_lock(agent_id):
        items = _items(db, agent_id)
        kept = [item for item in items if item["id"] != message_id]
        _store(db, agent_id, kept)
    return len(kept) != len(items)


def _queued(db: DB, agent_id: str, message_id: str) -> dict:
    for item in _items(db, agent_id):
        if item["id"] == message_id:
            return item
    raise ServiceError("not_found", f"no queued chat message {message_id}", 404)


# The queued item each agent's delivery in this process is typing (send-now or
# a turn's release), by agent id. Another process's delivery holds the
# agent's delivery_lock for as long, which remove waits on.
_claimed: dict[str, str] = {}


@contextmanager
def _claim(agent_id: str, message_id: str):
    """Held, inside _delivery, while item ``message_id`` is being typed: remove refuses it."""
    with _sending_lock:
        _claimed[agent_id] = message_id
    try:
        yield
    finally:
        with _sending_lock:
            _claimed.pop(agent_id, None)


def _refuse_in_flight(agent_id: str, message_id: str) -> None:
    with _sending_lock:
        if _claimed.get(agent_id) == message_id:
            raise ServiceError("conflict", "this message is being sent now", 409)


def remove(db: DB, agent_id: str, message_id: str) -> None:
    """Remove only this agent's queued item, without writing to its CLI."""
    agent = db.get_agent(agent_id)
    if agent is None:
        raise ServiceError("not_found", f"no agent {agent_id}", 404)
    identity_lock.ensure(agent)
    # An item already being typed can't be taken back: refused, never reported
    # removed and then sent. A delivery in another process finishes first.
    _refuse_in_flight(agent_id, message_id)
    with agents.delivery_lock(agent_id):
        _refuse_in_flight(agent_id, message_id)
        if not _take(db, agent_id, message_id):
            raise ServiceError("not_found", f"no queued chat message {message_id}", 404)
    _changed(db, agent_id)


def _await_prompt(db: DB, agent: Agent) -> None:
    """After send-now's Esc: wait for the CLI's input box, read from its screen.
    The interrupted turn runs no Stop hook, so the screen is what says it's back."""
    for _ in range(PROMPT_CHECKS):
        fresh = _live_agent(db, agent.id)
        if _ready(db, fresh):
            return
        if db.hook_permission_request(agent.id) is not None:
            break   # a hook began holding a request: the screen is not read meanwhile (see send_now)
        if agents.screen_status(db, fresh, samples=2) == "idle":
            # The Esc answered any permission prompt it was showing.
            db.clear_permission_requests(agent.id, keep_terminal=True)
    raise ServiceError("conflict", "the agent's prompt didn't come back after Esc; the message stays queued", 409)


def send_now(db: DB, agent_id: str, message_id: str) -> ChatResultDTO:
    """Esc, await the live prompt, then deliver this item; keep other items.

    Claim once; repeated triggers respect an in-flight delivery. Remove the
    item only after successful delivery; never replace an active claim.
    """
    # _delivery is the claim: a second trigger in this process is refused while
    # the first is in flight, and one in another process waits for it, then
    # finds the item gone. Neither interrupts or types anything.
    with _delivery(agent_id):
        agent = _live_agent(db, agent_id)
        item = _queued(db, agent.id, message_id)
        if db.hook_permission_request(agent.id) is not None:
            # Its hook holds a request for the person's answer in the app: an
            # Esc would leave it to settle on the hook's own clock, unanswered.
            # The person answers it first; the message stays queued (the app's
            # chip says so by this code).
            raise ServiceError("permission_pending",
                               "answer the agent's permission request first; the message stays queued", 409)
        # An interrupted earlier paste is the person's to resolve before any Esc.
        _reconcile_chat_paste(db, agent)
        with _claim(agent.id, message_id):
            tmux.send_keys(agent.tmux_window, "Escape", server=agent.tmux_server or None)
            _await_prompt(db, agent)
            _deliver(db, agent.id, _message(item))
            _take(db, agent.id, message_id)
    _changed(db, agent_id)
    return ChatResultDTO(status="delivered", message_id=message_id)


def turn_id(agent: Agent, payload: dict) -> str:
    """A completed turn's id, the same for every observation of that turn:
    Codex's own turn id, else (Claude's Stop, which a hook can send back, keeping
    its status_since) its session and when it began."""
    if payload.get("turn-id"):
        return f"codex:{payload['turn-id']}"
    session = payload.get("session_id") or payload.get("thread-id") or ""
    return f"{session}:{agent.status_since}"


def on_turn_end(db: DB, agent_id: str, turn_id: str, *, permission_prompt: bool = False) -> None:
    """Release one FIFO item per distinct completed turn, never at permission.

    The hook/status adapter supplies a stable turn id (duplicate observations
    must not drain the queue). Wire the real turn-end dispatch here; a waiting
    status or a permission notification alone cannot authorize release.
    """
    if permission_prompt or not turn_id or not _items(db, agent_id):
        return
    try:
        with _delivery(agent_id):
            agent = db.get_agent(agent_id)
            if agents._delivery_held(db, agent, waiter=False):
                return   # gone, headless, paused on a login or held for a restart
            seen = json.loads(db.mark(agent_id, _TURNS) or "[]")
            items = _items(db, agent_id)
            if turn_id in seen or not items:
                return
            with _claim(agent_id, items[0]["id"]):
                _deliver(db, agent_id, _message(items[0]))
                with _queue_lock(agent_id):
                    _store(db, agent_id, [item for item in _items(db, agent_id) if item["id"] != items[0]["id"]])
                    db.set_mark(agent_id, _TURNS, json.dumps([*seen, turn_id][-_TURNS_KEPT:]))
    except (ServiceError, agents.AgentError, tmux.TmuxError) as e:
        # Not ready after all (a send in flight, a new turn, an unresolved
        # paste): the item stays queued for the next turn's end.
        log.info("brindle: chat queue for %s kept at turn end: %s", agent_id, e)
        return
    _changed(db, agent_id)


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
