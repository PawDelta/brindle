"""Answer a CLI's blocking startup prompt by what it asks, not how it's worded.

Each provider's after_launch knows its own dialogs by regex: the fast path.
When a CLI rewords one, a worker would sit on it for good, so a screen that
matches none of them, isn't the CLI's input box, and looks like a menu or a
question waiting for input goes to one small-model call. The model says what
kind of prompt it is and which on-screen option answers it; brindle, not the
model, decides whether it may answer:

- folder trust, for a folder brindle made (a worktree), a notice to
  acknowledge, and a theme or settings picker (its default) are answered;
- anything about signing in, keys, payment, deleting data or access outside
  the worktree is the person's: the worker's supervisor is told once, with
  the prompt, instead of the worker sitting in 'starting'.

An answer is only ever arrow keys and Enter, never typed text, and Enter only
once the chosen option shows selected (as each provider's TRUST_SELECTED).
Decisions are cached by the screen's normalized hash, a launch makes at most
MAX_CALLS model calls, and anything unclear fails safe to needs_user. Tool
permission prompts are not startup prompts: the permission policy owns them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from brindle import tmux
from brindle.config import brindle_home

log = logging.getLogger(__name__)

KINDS = ("trust", "acknowledge", "setting_choice", "needs_user", "not_a_prompt")
ANSWERABLE = ("trust", "acknowledge", "setting_choice")
MODEL = "haiku"
MAX_CALLS = 3     # model calls per launch, across after_launch and sweeps
MAX_MOVES = 12    # arrow keys per launch before giving up on finding the option
CALL_TIMEOUT = 60.0
BRIEF_LINE = 12   # shortest screen line taken as part of the pasted brief

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CURSOR = "❯›>▸→"
# A menu line with the cursor on it: "❯ 1. Yes, proceed", "> Yes, I trust this folder".
SELECTED = re.compile(rf"^(\s*)[{CURSOR}]\s*(?:\d+[.)]\s+)?(\S.*?)\s*$")
OPTION = re.compile(rf"^(\s*)(?:[{CURSOR}]\s*)?(?:\d+[.)]\s+)?(\S.*?)\s*$")
# Tool-permission prompts, the permission policy's (see Provider.tool_permission).
TOOL_PERMISSION = re.compile(r"(?i)requesting permission for|run this command\?|would you like to run"
                             r"|do you want to (?:proceed|make this edit|create|allow)")
NUMBERED = re.compile(rf"^\s*(?:[{CURSOR}]\s*)?\d+[.)]\s+\S")
HINT = re.compile(r"(?i)\benter to (?:confirm|continue|select|proceed)\b|\bpress enter\b")
# A CLI's own input box and footer: the CLI is ready (or busy), not asking.
INPUT_BOX = re.compile(r"\? for shortcuts|\d+% context left|esc to interrupt|⏵⏵|⏸"
                       r"|\S…\s*\((?:\d+m\s*)?\d+s\b", re.I)   # the last: a busy spinner, "✳ Clauding… (1m 37s)"
# A turn under way: a spinner before its timer shows ("✻ Pondering…"), or a
# tool call or reply ("⏺ Read(…)", "⎿ Running…"). No startup prompt has these.
BUSY_SPINNER = re.compile(r"(?m)^\s*[·✢✳✶✻✽*]\s*[^\W\d_][\w-]*…")
WORKING = re.compile(BUSY_SPINNER.pattern + r"|^\s*[⏺⎿]")
BRIEF_LETTERS = 10   # fewest letters in a screen line taken as part of the brief
# Only the person answers these, whatever the model makes of them: their
# accounts and data, anything that widens what the agent may run (a
# project's MCP servers or hooks, skipping permission checks, installing).
PERSONAL = re.compile(r"(?i)sign.?in|log.?in\b|password|api.?key|payment|billing|credit card|subscri"
                      r"|delet|erase|outside (?:this|the) (?:folder|project|worktree|workspace)"
                      r"|\bmcp\b|\bhooks?\b|bypass|dangerous|skip\w*.{0,20}permission|install|updat")
# Nor these: terms, a licence, an agreement or a first-run notice a CLI wants
# accepted (brindle never accepts one for anyone; see brindle.preflight).
# Looked for in the whole capture, not just the tail the model sees: an
# agreement's terms can sit far above its "Continue".
CONSENT = re.compile(r"(?i)\bterms\b|licen[cs]e|\bagree|consent|privacy|security notes")
# The model only points at an option; brindle answers with it only if it is
# one of these (the screen can carry text from the repo, which can steer the
# model). A settings picker is only ever answered with its default.
SAFE_OPTION = {
    "trust": re.compile(r"(?i)^(?:yes|trust|allow|continue|proceed|open)\b"),
    "acknowledge": re.compile(r"(?i)^(?:ok(?:ay)?|continue|got it|dismiss|close|next|(?:press )?enter to continue)\b"),
}
# Every word of an option brindle picks for trust or acknowledge comes from
# here, so "Yes, and enable this project's servers" never passes for "Yes".
SAFE_WORDS = frozenset("yes i trust this the folder project workspace directory files contents allow "
                       "continue proceed open and ok okay got it next dismiss close press enter to "
                       "work in here".split())
# Never picked, whatever the kind: options that grant more than the one
# folder or the one notice, including a settings picker's default.
BROAD = re.compile(r"(?i)\b(?:all|always|future|every\w*|don.t ask|enabl\w*|accept\w*|auto\w*|skip\w*"
                   r"|bypass\w*|permissions?|run|execute|yolo)\b")
# Config files a path on screen can name (a project's MCP servers, hooks,
# settings), which normalize() hides behind <path>.
CONFIG_PATH = re.compile(r"(?i)\.(?:mcp|claude|codex|gemini|agy)\b|settings(?:\.local)?\.json|hooks?\b")
# Text that may be a credential: a screen showing one never leaves this machine.
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{20,}"
                    r"|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN"
                    r"|\b(?=[\w+=-]*[A-Za-z])(?=[\w+=-]*\d)[\w+=-]{40,}")
PATH = re.compile(r"(?<![\w.~/])(?:~|/)[\w.@+-]*(?:/[\w.@+-]+)+")

PROMPT = """\
A command-line coding agent has just started and stopped on the screen below \
before accepting any task. Say what it is waiting for. Reply with only a JSON \
object: {"kind": KIND, "option": OPTION}

KIND is one of:
- "trust": it asks whether to trust, or allow working in, this folder or project.
- "acknowledge": a notice to read and continue past.
- "setting_choice": a theme, style or similar settings picker.
- "needs_user": signing in, accounts, API keys, payment, deleting data, access \
outside this folder, installing or updating software, or anything else only \
its owner should decide.
- "not_a_prompt": it is not waiting for an answer (still loading, or ready for input).

OPTION is the exact text of the on-screen option that trusts the folder, \
continues, or keeps the default setting, copied from the screen without its \
number or cursor; "" for needs_user and not_a_prompt.

Screen (folder paths are shown as <path>):
<screen>
{screen}
</screen>
"""


@dataclass
class Decision:
    kind: str
    option: str = ""


def plain(screen: str) -> str:
    return ANSI.sub("", screen)


def _letters(text: str) -> str:
    """``text``'s letters and digits, casefolded: what survives a CLI's
    markdown rendering, wrapping and indenting of it."""
    return re.sub(r"[\W_]+", "", plain(text)).casefold()


def _tail(screen: str, n: int = 25) -> list[str]:
    return [ln.rstrip() for ln in plain(screen).rstrip().splitlines()][-n:]


def brief_draft(screen: str, brief: str) -> bool:
    """Recognize a known brief in the composer, including a scrolled tail.

    A returned empty composer supersedes transcript text. Without a prompt
    marker, require both a matching last line and substantial matching text;
    a generic 'Worker restrictions:' title alone is not enough evidence.
    """
    brief = " ".join((brief or "").split())
    if not brief:
        return False
    lines = plain(screen).splitlines()
    cursors = [i for i, line in enumerate(lines) if re.match(rf"^\s*[{CURSOR}]", line)]
    if cursors:
        lines = lines[cursors[-1]:]
        lines[0] = re.sub(rf"^\s*[{CURSOR}]\s*", "", lines[0])
        lines[0] = re.sub(r"^brindle delivered this message(?: from [^:]+)?:\s*", "", lines[0])
    end = next((i for i, line in enumerate(lines) if INPUT_BOX.search(line)), len(lines))
    parts = [" ".join(line.split()) for line in lines[:end]
             if line.strip() and not re.fullmatch(r"[\s─━╭╮╰╯│]+", line)]
    if not parts:
        return False
    if cursors and len(parts[0]) >= 20 and brief.startswith(parts[0]):
        return True
    return (len(parts[-1]) >= BRIEF_LINE and parts[-1] in brief
            and sum(len(part) for part in parts if part in brief) >= 60)


def looks_like_prompt(screen: str) -> bool:
    """Whether the bottom of ``screen`` is a menu or question waiting for an
    answer: a selected menu line next to another option, or an "Enter to
    continue" hint. Never the CLI's own input box, nor a turn under way."""
    lines = _tail(screen)
    text = "\n".join(lines)
    if INPUT_BOX.search(text) or WORKING.search(text):
        return False
    if HINT.search(text):
        return True
    for i, line in enumerate(lines):
        m = SELECTED.match(line)
        if not m:
            continue
        for other in lines[max(0, i - 4):i] + lines[i + 1:i + 5]:
            o = OPTION.match(other)
            if o and (NUMBERED.match(other) and NUMBERED.match(line) or o.start(2) == m.start(2)):
                return True
    return False


def blocking_prompt(screen: str, provider: str, brief: str = "") -> bool:
    """A live approval or menu, shared by delivery health and pane sweeps."""
    from brindle.providers import get_provider

    if brief_draft(screen, brief):
        return False
    screen = "\n".join(_tail(_past_brief(screen, brief)))
    return (get_provider(provider).tool_permission(screen)
            or _numbered_question(screen)
            or (looks_like_prompt(screen) and cached_kind(screen) != "not_a_prompt"))


def _numbered_question(screen: str) -> bool:
    """An unanswered task question, with consecutive choices at the end
    of the transcript. An empty composer is allowed; later replies aren't.
    This only forwards questions, never grants startup-menu approval."""
    # Claude's ⏺ responses and ⎿ tool results remain in completed turns;
    # only a live spinner disqualifies a question in that transcript.
    if BUSY_SPINNER.search(screen):
        return False
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    # Claude's bordered empty composer has a status line beneath it. Match
    # the entire suffix so a filled composer or later reply stays visible.
    if (len(lines) >= 4 and re.fullmatch(r"─+", lines[-4]) and lines[-3] == "❯"
            and re.fullmatch(r"─+", lines[-2])
            and re.match(r"⏵⏵|⏸|\? for shortcuts", lines[-1])):
        del lines[-4:]
    if lines and re.fullmatch(r"\d+% context left|\? for shortcuts", lines[-1], re.I):
        lines.pop()
    if lines and re.fullmatch(rf"[{CURSOR}]", lines[-1]):
        lines.pop()
    if INPUT_BOX.search("\n".join(lines)):
        return False
    numbers = []
    while lines:
        option = re.match(r"(\d+)[.)]\s+\S", lines[-1])
        if not option:
            break
        numbers.append(int(option[1]))
        lines.pop()
    return bool(len(numbers) >= 2 and numbers[::-1] == list(range(1, len(numbers) + 1))
                and lines and lines[-1].endswith("?"))


def _past_brief(screen: str, brief: str) -> str:
    """Remove the echoed brief, including markdown wrapping and scrolled
    numbered instructions, before interpreting the remaining pane as UI."""
    brief = _letters(brief)
    if not brief:
        return screen
    keep, rest = [], ""
    for line in screen.splitlines():
        text = _letters(line)
        if rest and rest.startswith(text):
            rest = rest[len(text):]
        elif len(text) >= BRIEF_LETTERS and text in brief:
            rest = brief[brief.index(text) + len(text):]
        else:
            rest = ""
            keep.append(line)
    return "\n".join(keep)


def normalize(screen: str) -> str:
    """The prompt as text to send and hash: no colours, no cursor (it moves
    while answering), no folder paths (they differ per worktree)."""
    lines = []
    for line in _tail(screen):
        line = re.sub(rf"^(\s*)[{CURSOR}]", r"\1 ", line)
        line = PATH.sub("<path>", line)
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _key(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def _cache_file():
    return brindle_home() / "startup_prompts.json"


def _cached(key: str) -> Decision | None:
    try:
        entry = json.loads(_cache_file().read_text()).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(entry, dict) and entry.get("kind") in KINDS:
        return Decision(entry["kind"], str(entry.get("option") or ""))
    return None


def _remember(key: str, decision: Decision) -> None:
    path = _cache_file()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[key] = {"kind": decision.kind, "option": decision.option}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass


def _ask(prompt: str) -> str | None:
    from brindle import model_access
    from brindle.providers import claude_binary

    return model_access.ask(claude_binary(), MODEL, prompt, str(brindle_home()), os.environ,
                            timeout=CALL_TIMEOUT)


def parse(reply: str | None) -> Decision | None:
    """The model's reply as a Decision, or None when it isn't one."""
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        return None
    option = data.get("option")
    return Decision(data["kind"], option.strip() if isinstance(option, str) else "")


def _option_text(line: str) -> str | None:
    m = OPTION.match(line)
    return " ".join(m.group(2).split()).casefold() if m else None


def next_key(screen: str, decision: Decision) -> str | None:
    """The key that moves toward ``decision.option`` on ``screen``: "Enter"
    once it shows selected, else "Up" or "Down". None when the option isn't
    on screen (once), or no selection can be seen to verify it."""
    lines = _tail(screen)
    want = _option_text(decision.option)
    if not want:
        return None
    # Only the exact line: what brindle checked is what it selects.
    matches = [i for i, ln in enumerate(lines) if _option_text(ln) == want]
    if len(matches) != 1:
        return None
    target = matches[0]
    selected = [i for i, ln in enumerate(lines) if SELECTED.match(ln)]
    if not selected:
        # A notice with no menu, only "Press Enter to continue".
        ok = decision.kind == "acknowledge" and HINT.search(lines[target])
        return "Enter" if ok else None
    if target in selected:
        return "Enter"
    cursor = min(selected, key=lambda i: abs(i - target))
    return "Down" if cursor < target else "Up"


def _brindle_folder(screen: str) -> bool:
    """Every folder ``screen`` names is one of brindle's worktrees (and it
    names at least one): the only folders brindle trusts for the person."""
    from brindle.providers import _in_worktree

    paths = PATH.findall(plain(screen))
    return bool(paths) and all(_in_worktree(os.path.expanduser(p)) for p in paths)


def _may_answer(screen: str, decision: Decision) -> bool:
    """brindle's own check of the model's pick (cached ones too): an
    answerable kind, an option it allows for that kind and nothing broader
    ("all future", "always"), and trust only for a brindle worktree."""
    option = _option_text(decision.option) or ""
    if decision.kind not in ANSWERABLE or not option or BROAD.search(option):
        return False
    safe = SAFE_OPTION.get(decision.kind)
    if safe and not (safe.search(option) and set(re.findall(r"[^\W_]+", option)) <= SAFE_WORDS):
        return False
    return decision.kind != "trust" or _brindle_folder(screen)


def redact(text: str) -> str:
    return SECRET.sub("[redacted]", text)


# Prompts nobody but the person may answer, by pane, for the launcher to
# report (after_launch only returns whether the CLI accepted its task).
_BLOCKED: dict[str, str] = {}


def take_blocked(target: str) -> str | None:
    """The prompt after_launch left ``target`` stopped on for the person, once."""
    return _BLOCKED.pop(target, None)


def cached_kind(screen: str) -> str | None:
    """What a model call already said ``screen`` is, without making one."""
    cached = _cached(_key(normalize(screen)))
    return cached.kind if cached else None


# -- launches: which ones are still starting, and their call budget ----------
#
# A launch is pending from its window opening until after_launch sees the
# CLI's input box, whatever the agent's status says, so a sweep can come back
# to a startup prompt after_launch's 30 seconds missed (cull.settle_startups).
# Its model calls are counted in the same row, across after_launch and every
# sweep. A launcher binds its pane before after_launch so the Startup there
# finds the row (after_launch itself only gets the pane).

_BOUND: dict[str, tuple] = {}


def launched(db, agent) -> None:
    """``agent``'s CLI just opened in ``agent.tmux_window``."""
    db.conn.execute("INSERT OR REPLACE INTO startup_prompts (agent_id, pane, launched_at, calls, pending) "
                    "VALUES (?,?,?,0,1)", (agent.id, agent.tmux_window, time.time()))


def pending(db) -> list[tuple[str, str, float]]:
    """(agent id, pane, launched at) of each launch still starting."""
    return [tuple(r) for r in db.conn.execute(
        "SELECT agent_id, pane, launched_at FROM startup_prompts WHERE pending=1")]


def bind(db, agent) -> None:
    if agent.tmux_window:
        _BOUND[agent.tmux_window] = (db, agent.id)


def finish_launch(db, agent) -> bool:
    """Run ``agent``'s after_launch (bound to its launch) and record what it
    found: ready (the launch is over), or a prompt left for the person (its
    supervisor is told). Returns whether the CLI was ready."""
    from brindle import agents, launch_health

    bind(db, agent)
    if not agents.get_provider(agent.provider).after_launch(agent.tmux_window):
        report_blocked(db, agent)
        return False
    settled(db, agent)
    agents.ready(db, agent.id)
    launch_health.verify_delivery(db, agent)
    return True


def settled(db, agent) -> None:
    """after_launch saw ``agent``'s input box: its launch is no longer pending."""
    db.conn.execute("UPDATE startup_prompts SET pending=0 WHERE agent_id=? AND pane=?",
                    (agent.id, agent.tmux_window))
    if _BOUND.get(agent.tmux_window, (None, None))[1] == agent.id:
        del _BOUND[agent.tmux_window]   # a later agent may get this pane id


class Startup:
    """One look, by after_launch, at unrecognized startup prompts on pane
    ``target``. ``ask`` sends a prompt to the model and returns its reply (or
    None); ``permission`` says whether a screen is a tool-permission prompt,
    which is never answered here (the permission policy owns those)."""

    def __init__(self, target: str, *, ask=None, permission=None):
        self.target = target
        self.ask = ask or _ask
        self.permission = permission or (lambda screen: False)
        self.calls = 0         # for a launch no launcher bound (see bind)
        self.moves = 0
        self.acted_on = None   # the screen the last key answered, until the CLI redraws
        db, agent_id = _BOUND.get(target, (None, None))
        row = db.conn.execute("SELECT submitted_prompt FROM launch_health WHERE agent_id=?",
                              (agent_id,)).fetchone() if db is not None else None
        self.brief = " ".join(((row["submitted_prompt"] if row else "") or "").split())

    def _past_brief(self, screen: str) -> str:
        return _past_brief(screen, self.brief)

    def draft(self, screen: str) -> bool:
        return brief_draft(screen, self.brief)

    def _spend_call(self) -> bool:
        """Count one model call against the launch's budget; False when it's spent."""
        db, agent_id = _BOUND.get(self.target, (None, None))
        if db is not None:
            row = db.conn.execute("SELECT 1 FROM startup_prompts WHERE agent_id=? AND pane=?",
                                  (agent_id, self.target)).fetchone()
            if row:
                return db.conn.execute("UPDATE startup_prompts SET calls=calls+1 "
                                       "WHERE agent_id=? AND pane=? AND calls<?",
                                       (agent_id, self.target, MAX_CALLS)).rowcount == 1
        self.calls += 1
        return self.calls <= MAX_CALLS

    def decide(self, screen: str) -> Decision:
        text = normalize(screen)
        if (PERSONAL.search(text) or CONSENT.search(plain(screen)) or SECRET.search(text)
                or any(CONFIG_PATH.search(p) for p in PATH.findall("\n".join(_tail(screen))))):
            return Decision("needs_user")
        key = _key(text)
        cached = _cached(key)
        if cached:
            return cached
        if not self._spend_call():
            return Decision("needs_user")
        decision = parse(self.ask(PROMPT.replace("{screen}", text)))
        if decision is None:
            return Decision("needs_user")
        _remember(key, decision)
        return decision

    def step(self, screen: str) -> str:
        """Act on one capture of the pane: "answering" (a key went in),
        "blocked" (only the person can answer it; see take_blocked), or ""
        (nothing to answer)."""
        # A half-rendered first brief has no footer yet. Its remaining
        # instructions can look like a menu even after _past_brief removes
        # the recognized prefix. It is still a draft, not a startup dialog.
        if self.draft(screen):
            return ""
        screen = self._past_brief(screen)
        if self.permission(screen) or not looks_like_prompt(screen):
            return ""
        if _tail(screen) == self.acted_on:
            return "answering"   # the last key hasn't shown yet: don't press another
        decision = self.decide(screen)
        if decision.kind == "not_a_prompt":
            return ""
        if decision.kind in ANSWERABLE:
            # The model call can take a while: act only on the pane as it is
            # now, and only if it still shows the same prompt.
            asked = screen
            try:
                screen = self._past_brief(tmux.capture(self.target, lines=60))
            except tmux.TmuxError:
                return ""
            if _key(normalize(screen)) != _key(normalize(asked)):
                return ""
        key = next_key(screen, decision) if _may_answer(screen, decision) else None
        if decision.kind == "setting_choice" and key != "Enter":
            key = None   # only the default, already selected; never one the model steers to
        if key in ("Up", "Down"):
            self.moves += 1
            if self.moves > MAX_MOVES:
                key = None
        if key is None:
            _BLOCKED[self.target] = redact("\n".join(normalize(screen).splitlines()[-12:]))
            return "blocked"
        tmux.send_keys(self.target, key)
        self.acted_on = _tail(screen)
        return "answering"


def tell(db, agent, prompt: str) -> bool:
    """Forward a blocking prompt once per spell of waiting, sharing the
    mark with cull.note_stuck so the sweep doesn't tell again."""
    from brindle import agents

    a = db.get_agent(agent.id)
    if not a or not a.parent_id or (a.status == "waiting" and a.stuck_noted == a.status_since):
        return False
    db.set_status(a.id, "waiting")
    a = db.get_agent(a.id)
    ws = db.get_workspace(a.workspace_id)
    attach = f"Attach with `brindle attach {ws.name}` to answer it" if ws else "Answer it in its pane"
    pending = agents.pending_permission(db, a.id)
    request = f"\n\n{pending} Only the user can answer it." if pending else ""
    prompt = redact(prompt)
    summary = " | ".join(prompt.splitlines())
    body = (f"Worker {a.id} ({a.profile}) is waiting on a prompt: {summary}\n\n"
            f"{attach}, or send it guidance if it is asking a task question.{request}\n\nThe prompt:\n{prompt}")
    try:
        agents.send_message(db, a.parent_id, body, sender_id=a.id)
    except agents.AgentError:
        db.enqueue(a.parent_id, body, a.id)
    db.update_agent(a.id, stuck_noted=a.status_since)
    return True


def at_turn_end(db, agent) -> bool:
    """A worker's turn just ended: forward the question it ended on, if its
    pane shows one. This is the one look at its pane per turn, from the
    turn-end hook (cull.note_stuck reads only waiting panes)."""
    from brindle import agents

    if not agent.parent_id or agent.headless or not agent.tmux_window:
        return False
    try:
        screen = tmux.capture(agent.tmux_window, lines=40, server=agents.server_of(agent))
        row = db.conn.execute("SELECT submitted_prompt FROM launch_health WHERE agent_id=?",
                              (agent.id,)).fetchone()
        brief = (row["submitted_prompt"] if row else None) or agent.task or ""
        return blocking_prompt(screen, agent.provider, brief) and tell(db, agent, "\n".join(_tail(screen)))
    except Exception:  # noqa: BLE001 - must never break the hook
        log.exception("brindle: couldn't check %s's pane at the end of its turn", agent.id)
        return False


def report_blocked(db, agent) -> bool:
    """After after_launch on ``agent``'s pane: tell its supervisor about a
    prompt it left for the person, if any."""
    prompt = take_blocked(agent.tmux_window) if agent.tmux_window else None
    return bool(prompt) and tell(db, agent, prompt)
