"""Waking a Claude Code agent with brindle's messages, through its own hook.

Claude Code's inbox socket (brindle.inbox) wakes an idle session, but shows
every message as "Another Claude session sent a message:" plus a paragraph
of peer-safety text: two or three worker reports fill the person's chat.
Typed into a worker's pane, a message reads as if the person had typed it.
For a Claude Code agent in a tmux window (a supervisor, the person's own
chat, or a worker: see applies) brindle uses a hook instead:

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
the agent is busy. An idle worker gets them typed into its pane
(agents.flush). A person's chat (a supervisor) is never typed into: brindle
pokes it (see poke) and its FileChanged hook starts a waiter, or, for a
session launched before pokes, the mail waits for its next Stop or SessionStart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import sys
import time
import uuid
from pathlib import Path

from brindle.config import brindle_home
from brindle.db import DB, WAITER_CLAIM, Agent

EVENT = "await-mail"
# Claude Code kills a hook at its timeout (seconds), asyncRewake ones
# included; a waiter gives up a minute before, and a message after that is
# typed in instead.
TIMEOUT = 86400
POLL = 0.5
HEARTBEAT = 10.0       # a waiter whose file is older than this is gone
ALIVE_EVERY = 15.0     # how often a waiter checks the agent's window is still there
ENDED = ("paused", "done", "failed")
CLAIM_EXPIRES = 120.0  # a hand-over takes milliseconds: mail held longer is abandoned, whatever its pid says


# Modes launched with the waiter: a person's chat and the workers it starts,
# whose chats the app shows too. A worker keeps its Stop hook (the reminder
# to call report_result still blocks its stop); only its mail moves here.
MODES = ("interactive", "handoff", "handoff_detached", "assign")


def applies(provider: str, mode: str | None, headless: bool = False) -> bool:
    """Whether an agent is launched with the waiter: a Claude Code chat in a
    tmux window, a supervisor's or a worker's. A headless agent's runner
    takes its mail itself."""
    return provider == "claude" and mode in MODES and not headless


def applies_poke(provider: str, mode: str | None, headless: bool = False) -> bool:
    """Whether an agent is launched with the FileChanged waiter too (see
    poke): a person's chat, which brindle never types into."""
    return applies(provider, mode, headless) and mode == "interactive"


def hook_entry(command: str) -> dict:
    """The hook settings Claude Code runs the waiter with."""
    return {"type": "command", "command": command, "asyncRewake": True, "timeout": TIMEOUT}


def _path(agent_id: str) -> Path:
    return brindle_home() / "wake" / agent_id


def enabled(agent: Agent) -> bool:
    """Whether ``agent``'s session runs the waiter (one has registered)."""
    return applies(agent.provider, agent.mode, bool(agent.headless)) and _path(agent.id).exists()


def silent(agent: Agent) -> bool:
    """Whether brindle must never type into ``agent``'s pane: a person's
    Claude Code chat (a supervisor), whatever its session runs or has
    registered so far. Its mail reaches it through a waiter, one a poke starts
    (``poke``) or the next hook event's; a session with no waiter hook at all
    (launched by a much older brindle) through Claude Code's inbox."""
    return applies_poke(agent.provider, agent.mode, bool(agent.headless))


# A poke: brindle writes a file that the session watches (its SessionStart
# hook returns it as watchPaths), and Claude Code runs the chat's FileChanged
# hook, the same asyncRewake waiter as at Stop. That waiter finds the queued
# mail at once and wakes the model with it: an idle chat with no live waiter
# (a turn ended by Esc or an API error, a waiter past its limit) gets its mail
# without a key typed into its pane.
def poke_path(agent_id: str) -> Path:
    return brindle_home() / "wake-poke" / agent_id


def arm_poke(agent_id: str) -> None:
    """At launch, for a session started with the FileChanged waiter hook:
    its poke file exists from now on, so its SessionStart hook has it watched."""
    p = poke_path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def pokeable(agent: Agent) -> bool:
    """Whether ``agent``'s session was launched with the FileChanged waiter
    hook (one launched by an older brindle wasn't: its mail waits for its
    next Stop or SessionStart instead). Its launch made the poke file, so
    this holds before its first waiter has registered too."""
    return silent(agent) and poke_path(agent.id).exists()


def poke(agent: Agent) -> bool:
    """Have Claude Code start a waiter for ``agent`` now. Returns whether the
    poke was written (not whether a waiter came: the next sweep pokes again)."""
    try:
        poke_path(agent.id).write_text(f"{time.time()!r}\n")
        return True
    except OSError:
        return False


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


NONCES = "wake-nonces"   # agent_marks kind: {nonce: when} of the agent's recent hand-overs
_DELIVERY_LINE = re.compile(r'brindle delivered \d+ messages? \(nonce "([0-9a-f]{16})"\):')
_ENVELOPE = ("<task-notification>", "</task-notification>")


def _record_nonce(db: DB, agent_id: str, nonce: str) -> None:
    """Remember ``nonce`` as one of ``agent_id``'s hand-overs (see handed_over),
    forgetting any older than a waiter can live."""
    while True:   # another waiter's record landed between the read and the write: read again
        saved = db.mark(agent_id, NONCES)
        try:
            known = json.loads(saved) if saved else {}
        except ValueError:
            known = {}
        now = time.time()
        kept = {n: t for n, t in (known.items() if isinstance(known, dict) else ())
                if isinstance(t, (int, float)) and now - t < TIMEOUT}
        if db.swap_mark(agent_id, NONCES, saved, json.dumps({**kept, nonce: now})):
            return


def handed_over(db: DB, agent_id: str, prompt: str) -> bool:
    """Whether ``prompt`` is one of this agent's waiter hand-overs as Claude
    Code submits it: its ``<task-notification>`` envelope first, then the
    delivery line, naming a nonce brindle drew and recorded for this agent.
    Never the wording alone: anyone can type that, while the nonce is fresh
    for each hand-over and drawn after every body in it was written."""
    text = prompt.lstrip()
    if not text.startswith(_ENVELOPE[0]) or _ENVELOPE[1] not in text:
        return False
    found = _DELIVERY_LINE.search(text, text.index(_ENVELOPE[1]))
    saved = db.mark(agent_id, NONCES) if found else None
    try:
        known = json.loads(saved) if saved else {}
    except ValueError:
        return False
    return isinstance(known, dict) and found is not None and found.group(1) in known


def take(db: DB, agent: Agent) -> tuple[str, list] | None:
    """Everything queued for ``agent``, each message framed with one fresh
    nonce, as one text, with the messages taken (for ``give_back``); None
    if nothing. A failure part way puts back what it had taken. The nonce
    is recorded before the text leaves, so the prompt Claude Code wakes the
    model with is known as brindle's, never the person speaking (handed_over)."""
    nonce = secrets.token_hex(8)
    taken: list = []
    parts = []
    try:
        while (msg := db.pop_pending(agent.id, waiter_pid=os.getpid())) is not None:
            taken.append(msg)
            parts.append(frame(db, msg.body, msg.sender_id, nonce))
        if parts:
            _record_nonce(db, agent.id, nonce)
    except BaseException:
        give_back(db, agent.id, taken)
        raise
    if not parts:
        return None
    return (f"brindle delivered {len(parts)} message{'s' if len(parts) != 1 else ''} "
            f'(nonce "{nonce}"):\n' + "\n".join(parts)), taken


def give_back(db: DB, agent_id: str, taken: list) -> None:
    """Put taken messages back where they were in the queue, as agents.flush
    does when a send fails: the same rows, so ahead of anything newer."""
    for msg in taken:
        db.release_pending(msg.id, agent_id)
    db.set_mark(agent_id, f"{WAITER_CLAIM}{os.getpid()}", None)


def _handed_path(agent_id: str, ids: list[int]) -> Path:
    """Where a claim's completed hand-over is recorded: a file, as it has to
    hold when the database is what just failed. Named for the claim's rows."""
    return brindle_home() / "wake-handed" / hashlib.sha256(f"{agent_id}:{ids}".encode()).hexdigest()[:32]


def _finish_claim(db: DB, agent_id: str, taken: list) -> None:
    """The mail is written: this claim must never be read as abandoned."""
    handed = _handed_path(agent_id, [m.id for m in taken])
    try:
        handed.parent.mkdir(parents=True, exist_ok=True)
        handed.touch()
    except OSError:
        pass   # the database below is still the usual record
    db.set_mark(agent_id, f"{WAITER_CLAIM}{os.getpid()}", None)
    _discard(handed)


def _discard(path: Path) -> None:
    """Remove a receipt no longer needed: one left behind only takes up a name."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _was_handed(path: Path) -> bool | None:
    """Whether a receipt is there; None if it can't be told."""
    try:
        return path.exists()
    except OSError:
        return None


def _gone(state: dict) -> bool:
    if time.time() - state["at"] > CLAIM_EXPIRES:
        return True
    try:
        os.kill(state["pid"], 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return False


def reclaim(db: DB, agent_id: str | None = None) -> None:
    """Put back mail a waiter took and died holding (killed before it could
    hand it over): for the next waiter, or the sweep's flush, to deliver."""
    for row in db.waiter_claims(agent_id):
        state = json.loads(row["value"])
        handed = _handed_path(row["agent_id"], state["ids"])
        known = _was_handed(handed)
        if known is None:
            continue   # it may have reached the agent: the claim waits for a look that works
        if known:   # it reached the agent; only the claim's cleanup failed
            db.set_mark(row["agent_id"], row["kind"], None)
            _discard(handed)
        elif _gone(state):
            db.reclaim_waiter_claim(row["agent_id"], row["kind"], row["value"])


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
        reclaim(db, agent_id)
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
    """Forget the waiter records (and poke files) of agents brindle no longer has."""
    entries = []
    for folder in ("wake", "wake-poke"):
        try:
            entries += list((brindle_home() / folder).iterdir())
        except OSError:
            pass
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
    # Handed over: from here only exit code 2 makes Claude Code read it, so
    # neither a late stop signal nor a busy database may change that.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for step in (lambda: _finish_claim(db, agent_id, taken),
                 lambda: db.set_status(agent_id, "processing", only_if="idle")):  # it wakes with this
        try:
            step()
        except Exception:  # noqa: BLE001 - the mail is written: nothing here may cost it its exit code
            pass   # not logged: stderr is what the model reads, and reclaim reads the claim's record
    return 2
