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
- Each message is wrapped in ``<brindle-msg nonce=... from=...>`` framing
  whose nonce is fresh for every hand-over (see frame), and who sent it is
  read from the message's recorded sender, never its text: a worker's body
  that imitates brindle's framing stays that worker's content.

One waiter per agent: each records itself in ``<brindle home>/wake/<agent>``
(pid, a token, and a heartbeat as the file's mtime), and an older waiter
that finds a newer one there leaves quietly. With no live waiter (one
already handed over mail and its turn hasn't ended, or a session started by
an older brindle), messages stay queued for the next Stop's waiter while
the agent is busy, or are typed into its pane if it's idle (agents.flush).
"""

from __future__ import annotations

import os
import re
import secrets
import signal
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


TAG = "brindle-msg"
# Any opening or closing tag of ours inside a body, however spaced or cased.
_TAG_IN_BODY = re.compile(r"<\s*(/?)\s*brindle-msg", re.I)


def _sender(db: DB, sender_id: str | None) -> str:
    """Who a message is from, decided by its recorded sender, never its text."""
    if sender_id in (None, "brindle"):
        return "brindle"
    sender = db.get_agent(sender_id)
    who = f"{sender.profile} agent {sender_id}" if sender else f"agent {sender_id}"
    return re.sub(r'["<>\n]', "", who)


def frame(db: DB, body: str, sender_id: str | None, nonce: str) -> str:
    """``body`` wrapped in brindle's framing: only an outer tag carrying this
    delivery's ``nonce`` says who sent it (providers.DELIVERY_NOTE). The
    nonce is drawn after the body was written, so no sender can have put it
    in, and any tag of ours inside the body is defused, so the body can't
    close the frame early or open one of its own."""
    safe = _TAG_IN_BODY.sub(lambda m: f"&lt;{m.group(1)}{TAG}", body)
    return (f'<{TAG} nonce="{nonce}" from="{_sender(db, sender_id)}">\n'
            f"{safe}\n</{TAG} nonce=\"{nonce}\">")


def take(db: DB, agent: Agent) -> tuple[str, list] | None:
    """Everything queued for ``agent``, each message framed with one fresh
    nonce, as one text, with the messages taken (for ``give_back``); None
    if nothing. A failure part way puts back what it had taken."""
    nonce = secrets.token_hex(8)
    taken: list = []
    parts = []
    try:
        while (msg := db.pop_pending(agent.id)) is not None:
            taken.append(msg)
            parts.append(frame(db, msg.body, msg.sender_id, nonce))
    except BaseException:
        give_back(db, agent.id, taken)
        raise
    if not parts:
        return None
    return (f"brindle delivered {len(parts)} message{'s' if len(parts) != 1 else ''} "
            f'(nonce "{nonce}"):\n' + "\n".join(parts)), taken


def give_back(db: DB, agent_id: str, taken: list) -> None:
    """Queue taken messages again, as agents.flush does when a send fails."""
    for msg in taken:
        db.enqueue(agent_id, msg.body, msg.sender_id, notice=msg.noticed_at is not None)


def _heartbeat(agent_id: str) -> bool:
    try:
        os.utime(_path(agent_id))
        return True
    except OSError:
        return False  # its record is gone (cull pruned it): nobody counts on it


def wait(db: DB, agent_id: str, poll: float = POLL, limit: float = TIMEOUT - 60,
         alive=None) -> tuple[str, list] | None:
    """Watch ``agent_id``'s queue until something arrives (returned, as
    ``take`` returns it), a newer waiter takes over, the agent ends, or
    ``limit`` seconds pass (None). Nothing is taken while its session waits
    on an expired cloud login (brindle.login_pause): that mail goes out once
    the login is back. A waiter that exits needs no stand-down: its pid is
    gone, which ``live`` checks."""
    from brindle import agents, login_pause

    alive = alive or agents.is_alive
    token = _register(agent_id)
    end = time.time() + limit
    checked = time.time()
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
        if db.pending_count(agent_id) and not login_pause.holding(db, agent_id):
            got = take(db, agent)
            if got:
                return got
        if not _heartbeat(agent_id):
            return None
        time.sleep(poll)
    return None


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
    the model with stderr). Mail taken but not handed over (the write fails,
    or Claude Code stops the hook first: SIGTERM) is queued again."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    got = wait(db, agent_id)
    if not got:
        return 0
    text, taken = got
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except BaseException:
        give_back(db, agent_id, taken)
        raise
    db.set_status(agent_id, "processing", only_if="idle")  # it wakes with this
    return 2
