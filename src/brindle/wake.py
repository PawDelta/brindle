"""Waking a Claude Code supervisor with brindle's messages, through its own hook.

Claude Code's inbox socket (brindle.inbox) wakes an idle session, but shows
every message as "Another Claude session sent a message:" plus a paragraph
of peer-safety text: two or three worker reports fill the person's chat.
For an interactive Claude Code agent (a supervisor, the person's own chat)
brindle uses a hook instead:

- At SessionStart and at every Stop, Claude Code also runs
  ``brindle _hook await-mail`` as an ``asyncRewake`` hook: in the background,
  with no wait on it. That waiter watches the agent's queue in brindle's DB.
- When a message is queued for the agent, the waiter takes everything queued,
  writes it to stderr and exits with code 2. Claude Code then wakes the
  model with that text (a turn starts if it was idle; a running turn gets it
  between steps), and the chat shows a single "Stop hook feedback" line.
- The text keeps brindle's typed label (agents.message_lead), so the model
  can tell it came from brindle, and from which agent, never from the person.

One waiter per agent: each records itself in ``<brindle home>/wake/<agent>``
(pid, a token, and a heartbeat as the file's mtime), and an older waiter
that finds a newer one there leaves quietly. With no live waiter (one
already handed over mail and its turn hasn't ended, or a session started by
an older brindle), messages stay queued for the next Stop's waiter while
the agent is busy, or are typed into its pane if it's idle (agents.flush).
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

from brindle.config import brindle_home
from brindle.db import DB, Agent

EVENT = "await-mail"
# Claude Code kills a hook at its timeout (seconds), asyncRewake ones
# included; a waiter gives up a minute before, and a message after that is
# typed in instead.
TIMEOUT = 86400
POLL = 0.5
HEARTBEAT = 10.0       # a waiter whose file is older than this is gone
ALIVE_EVERY = 15.0     # how often a waiter checks the agent's window is still there
ENDED = ("paused", "done", "failed")


def applies(provider: str, mode: str | None, headless: bool = False) -> bool:
    """Whether an agent is launched with the waiter: an interactive Claude
    Code chat. Workers keep their Stop hook (a blocked stop nobody watches)."""
    return provider == "claude" and mode == "interactive" and not headless


def hook_entry(command: str) -> dict:
    """The hook settings Claude Code runs the waiter with."""
    return {"type": "command", "command": command, "asyncRewake": True, "timeout": TIMEOUT}


def _path(agent_id: str) -> Path:
    return brindle_home() / "wake" / agent_id


def enabled(agent: Agent) -> bool:
    """Whether ``agent``'s session runs the waiter (one has registered)."""
    return applies(agent.provider, agent.mode, bool(agent.headless)) and _path(agent.id).exists()


def _read(agent_id: str) -> tuple[int, str, float] | None:
    p = _path(agent_id)
    try:
        pid, token = p.read_text().split()[:2]
        return int(pid), token, p.stat().st_mtime
    except (OSError, ValueError):
        return None


def live(agent: Agent) -> bool:
    """Whether a waiter is watching ``agent``'s queue right now."""
    if not enabled(agent):
        return False
    found = _read(agent.id)
    if found is None:
        return False
    pid, _, beat = found
    if pid <= 0 or time.time() - beat > HEARTBEAT:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _register(agent_id: str) -> str:
    token = uuid.uuid4().hex
    p = _path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{agent_id}.{token}")
    tmp.write_text(f"{os.getpid()} {token}\n")
    os.replace(tmp, p)
    return token


def _current(agent_id: str, token: str) -> bool:
    found = _read(agent_id)
    return found is not None and found[1] == token


def _stand_down(agent_id: str, token: str) -> None:
    """Mark the waiter gone at once (pid 0), if it's still the current one,
    so a sender doesn't count on it until its heartbeat runs out."""
    if _current(agent_id, token):
        try:
            _path(agent_id).write_text(f"0 {token}\n")
        except OSError:
            pass


def _label(db: DB, agent: Agent, body: str, sender_id: str | None) -> str:
    from brindle import agents

    if sender_id is None and body.lstrip().startswith(("brindle", "[brindle")):
        return body  # brindle's own notices and reminders already say so
    return f"{agents.message_lead(db, agent, sender_id)}\n{body}"


def take(db: DB, agent: Agent) -> str | None:
    """Everything queued for ``agent``, labeled, as one text; None if nothing."""
    parts = []
    while (msg := db.pop_pending(agent.id)) is not None:
        parts.append(_label(db, agent, msg.body, msg.sender_id))
    return "\n\n".join(parts) if parts else None


def wait(db: DB, agent_id: str, poll: float = POLL, limit: float = TIMEOUT - 60,
         alive=None) -> str | None:
    """Watch ``agent_id``'s queue until something arrives (returned), a newer
    waiter takes over, the agent ends, or ``limit`` seconds pass (None)."""
    from brindle import agents

    alive = alive or agents.is_alive
    token = _register(agent_id)
    end = time.time() + limit
    checked = time.time()
    try:
        while time.time() < end:
            if not _current(agent_id, token):
                return None  # a newer waiter took over
            agent = db.get_agent(agent_id)
            if agent is None or agent.status in ENDED:
                return None
            if time.time() - checked >= ALIVE_EVERY:
                checked = time.time()
                if not alive(agent):
                    return None
            # A read first: taking is a write transaction, too much to hold every poll.
            text = take(db, agent) if db.pending_count(agent_id) else None
            if text:
                db.set_status(agent_id, "processing", only_if="idle")  # it wakes with this
                return text
            os.utime(_path(agent_id))  # heartbeat
            time.sleep(poll)
        return None
    finally:
        _stand_down(agent_id, token)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return time.time()


def prune(known: set[str]) -> None:
    """Forget the waiter records of agents brindle no longer has."""
    try:
        entries = list((brindle_home() / "wake").iterdir())
    except OSError:
        return
    for p in entries:
        # An old dotfile is a record a waiter died writing (see _register).
        stray = p.name.startswith(".") and time.time() - _mtime(p) > 60
        if stray or (not p.name.startswith(".") and p.name not in known):
            try:
                p.unlink()
            except OSError:
                pass


def main(db: DB, agent_id: str) -> int:
    """``brindle _hook await-mail``: the exit code Claude Code reads (2: wake
    the model with stderr)."""
    text = wait(db, agent_id)
    if not text:
        return 0
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    return 2
