"""The chat panel's routes: an agent's recent conversation (user and assistant
text only, no tool traffic) and a way to send it a message as the person."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from brindle import agents
from brindle.db import Agent
from brindle.web.server import Request, Response, json_response, route

# The most messages one transcript response carries (the newest, when more).
MAX_MESSAGES = 200


def _blocks_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content
                     if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str))


def _claude_message(entry: object) -> dict | None:
    """The chat line of one Claude Code JSONL entry; None for tool results,
    tool calls, thinking, sub-agent traffic and bookkeeping entries."""
    if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant") \
            or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    text = _blocks_text(msg.get("content")).strip()
    return {"role": entry["type"], "text": text} if text else None


def _native_message(msg: object) -> dict | None:
    if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
        return None
    text = _blocks_text(msg.get("content")).strip()
    return {"role": msg["role"], "text": text} if text else None


def _read_items(agent: Agent) -> tuple[list, object]:
    """The raw entries of ``agent``'s conversation, and the parser for them."""
    if agent.provider == "native":
        from brindle.native.runner import conversation_path
        try:
            data = json.loads(conversation_path(agent.id).read_text(encoding="utf-8"))
            return list(data["messages"]), _native_message
        except (OSError, ValueError, KeyError, TypeError):
            return [], _native_message
    path = agent.transcript_path
    if agent.provider != "claude" or not path or not Path(path).is_file():
        return [], _claude_message
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = raw.split("\n")[:-1]  # only complete lines; a partial tail waits for next poll
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except ValueError:
            items.append(None)
    return items, _claude_message


def _generation(items: list) -> str:
    """Hash of the first kept message after the task/summary header, so a
    context fold (which replaces the middle with a summary) changes it."""
    start = 3 if len(items) > 2 and isinstance(items[1], dict) and items[1].get("summary") else 1
    head = items[1:start + 1]
    if not head:
        return "0"
    raw = json.dumps(head, sort_keys=True, default=str).encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def transcript(agent: Agent, after: int | str = 0) -> dict:
    """Messages after cursor ``after`` and the cursor to pass next time.
    When ``after``'s generation doesn't match (e.g. a native conversation
    folded), returns ``reset=True`` and a recent snapshot."""
    items, parse = _read_items(agent)
    gen = _generation(items)
    given_gen, sep, count_s = str(after or 0).rpartition(":")
    count = int(count_s or 0)
    folded = len(items) > 2 and isinstance(items[1], dict) and bool(items[1].get("summary"))
    mismatch = count < 0 or count > len(items) or (
        bool(sep) and given_gen != gen and not (given_gen == "0" and count <= 1 and not folded)
    )
    start = 0 if mismatch else count
    messages = [m for m in map(parse, items[start:]) if m]
    return {"messages": messages[-MAX_MESSAGES:], "cursor": f"{gen}:{len(items)}", "reset": mismatch}


@route("GET", "/api/agents/{id}/transcript")
def api_transcript(req: Request) -> Response:
    agent = req.db.get_agent(req.params["id"])
    if agent is None:
        return json_response({"error": "no such agent"}, 404)
    try:
        return json_response(transcript(agent, req.query.get("after") or 0))
    except ValueError:
        return json_response({"error": "bad cursor"}, 400)


@route("POST", "/api/agents/{id}/message")
def api_message(req: Request) -> Response:
    try:
        body = req.json()
    except ValueError:
        body = None
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        return json_response({"error": "text is required"}, 400)
    if req.db.get_agent(req.params["id"]) is None:
        return json_response({"error": "no such agent"}, 404)
    try:
        outcome = agents.send_message(req.db, req.params["id"], text, person=True)
    except agents.AgentError as e:
        return json_response({"error": str(e)}, 409)
    return json_response({"status": outcome})
