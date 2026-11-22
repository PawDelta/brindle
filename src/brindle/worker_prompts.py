"""Every prompt a worker stops on, classified and routed by brindle.

A worker's CLI can stop on its own permission prompt (a command, an edit, a
sandbox escalation: when brindle's hook isn't in the way), a trust dialog,
a menu or confirmation, or a question it ended its turn on. Each used to
reach its supervisor, and through the Inbox the person, as the bottom of the
worker's pane pasted in. Now one path handles them all:

``classify`` reads the pane into a ``Prompt``: its kind, what it asks for,
and its options. Each CLI's prompts have their own parser (PARSERS); a new
kind is one more parser and, if it routes differently, one more branch in
``route``.

``route`` decides it, once per prompt and spell of waiting (an
``agent_marks`` row, MARK), and says where it stands:

- a permission is decided as its CLI's hook would decide it (agents.
  codex_policy_decision): what the rules allow, or deny with the policy on,
  is answered in the pane, where its menu is one brindle can answer
  (``Prompt.keys``); anything else is its supervisor's (answer_permission,
  as a hook's ask), then the person's;
- a trust dialog, menu, confirmation or question is its supervisor's, as a
  structured message: the question and the numbered options, never the pane;
- the person gets only what a rule leaves to them, what the supervisor
  escalates or leaves unanswered, or what has no live supervisor: one
  sentence (``sentence``), with Allow and Deny where brindle can answer it.

The pane is answered only with arrow keys and Enter, and Enter only once a
fresh look shows the same prompt with the chosen option selected, under the
pane's delivery lock. A trust dialog raised mid-task is never answered: it
may be the agent's own attempt to widen its access.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass

from brindle import prompt_answer, tmux
from brindle.db import DB, Agent

log = logging.getLogger("brindle.worker_prompts")

MARK = "worker-prompt"
PREFIX = "pane-"          # request ids of permissions read off a pane
CAPTURE_LINES = 60
MAX_MOVES = 6
DECIDING_EXPIRES = 120.0  # a claim this old whose decision never landed is the person's
KINDS = ("permission", "trust", "menu", "question", "unknown")
CHOICES = {"allow": "allow", "allow_once": "allow", "supervisor_allow": "allow",
           "deny": "deny", "supervisor_deny": "deny"}
# The one option each answer may pick: never "don't ask again", "always" or "for this session".
TARGET = {"allow": re.compile(r"(?i)^(?:yes|allow)(?:,?\s*(?:proceed|once))?\s*(?:\(\w+\))?$"),
          "deny": re.compile(r"(?i)^(?:no|deny|reject)\b")}
BY = {"policy": "by brindle's permission rules", "supervisor": "by your supervisor", "person": "by the person"}
LABEL = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity", "opencode": "OpenCode",
         "native": "brindle"}
CURSOR = "❯›>▸→"
OPTION = re.compile(rf"^\s*([{CURSOR}]\s*)?(\d+)[.)]\s+(\S.*?)\s*$")
FOOTER = re.compile(r"(?i)press enter|esc to cancel|enter to (?:confirm|select)|↑/↓|tab to")
TRAILER = re.compile(rf"^\s*$|^[\s─━╭╮╰╯]+$|^\s*[{CURSOR}]\s*$|⏵⏵|⏸|\? for shortcuts|\d+% context left")


@dataclass(frozen=True)
class Prompt:
    kind: str                 # KINDS
    provider: str
    question: str = ""        # the prompt's own question, one line
    target: str = ""          # a permission's command, file or path, as shown
    action: str = ""          # a permission's: "run", "edit" or "use"
    reason: str = ""          # the agent's stated reason (Codex): the model's words, never trusted
    exact: bool = False       # ``target`` reads exactly as it will run: the rules may decide it
    options: tuple = ()       # (number, text, selected) per menu line
    keys: bool = False        # a vertical menu with a cursor: brindle can answer it
    sandbox: bool = False     # a Codex sandbox escalation

    @property
    def key(self) -> str:
        return hashlib.sha256("\0".join((self.kind, self.question, self.target, self.reason,
                                         *(t for _, t, _ in self.options))).encode()).hexdigest()

    @property
    def request_id(self) -> str:
        return PREFIX + self.key[:16]


# -- classify -------------------------------------------------------------------------------


def _lines(screen: str) -> list[str]:
    """``screen``'s lines without colours or box borders (Claude draws its prompts in a box)."""
    return [re.sub(r"[\s│┃╭╮╰╯]+$", "", re.sub(r"^\s*[│┃]", "", ln)) for ln in prompt_answer.plain(screen).splitlines()]


def _menu(lines: list[str]) -> tuple[int, tuple] | None:
    """The numbered menu ending ``lines``: where it starts and its options.
    Below it only a footer, a rule or an empty input box may follow, so a
    menu answered long ago, with the transcript gone on past it, is none."""
    end = len(lines)
    while end and (TRAILER.search(lines[end - 1]) or FOOTER.search(lines[end - 1])):
        end -= 1
    start, options = end, []
    while start and OPTION.match(lines[start - 1]):
        m = OPTION.match(lines[start - 1])
        options.insert(0, (int(m[2]), m[3], bool(m[1])))
        start -= 1
    numbers = [n for n, _, _ in options]
    if len(options) < 2 or numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return None
    return start, tuple(options)


def _codex(lines: list[str], provider: str) -> Prompt | None:
    at = [i for i, ln in enumerate(lines) if re.search(r"Would you like to (run the following command|make the following edits"
                                                     r"|grant these permissions|send input to)", ln)]
    if not at:
        return None
    menu = _menu(lines[at[-1]:])
    if menu is None:
        return None
    start, options = menu
    head, question = lines[at[-1] + 1:at[-1] + start], lines[at[-1]].strip()
    if "run the following command" not in question:
        target = " ".join(ln.strip() for ln in head if ln.strip() and not ln.strip().startswith("Reason:"))
        return Prompt("permission", provider, question, target, "edit" if "edits" in question else "use",
                      options=options, keys=True)
    starts = [i for i, ln in enumerate(head) if ln.strip().startswith("$ ")]
    if not starts:
        return None
    # The command is everything from its "$ " line to the menu, blank lines
    # included: a later line is still part of what Enter would run.
    block = [ln.strip() for ln in head[starts[0]:]]
    block[0] = block[0][2:]
    while block and not block[-1]:
        block.pop()
    reason, field = [], None
    for ln in head[:starts[0]]:
        text = ln.strip()
        if not text:
            field = None
        elif text.startswith("Reason:"):
            reason = [text.removeprefix("Reason:").strip()]
            field = reason
        elif field is not None:
            field.append(text)
    # Exact only as one line, the only "$ " on screen: anything else may be more than the rules see.
    return Prompt("permission", provider, question, "\n".join(block), "run", " ".join(reason),
                  len(block) == 1 and len(starts) == 1, options, keys=True, sandbox=True)


CLAUDE_ASK = re.compile(r"Do you want to (?:proceed|make this edit[^?]*|create [^?]*|allow[^?]*)\?")
CLAUDE_HEADER = re.compile(r"^(Bash command|Edit file|Create file|Write file|Read file|Fetch|Web fetch|Tool use|MCP)\b",
                           re.I)


def _claude(lines: list[str], provider: str) -> Prompt | None:
    at = [i for i, ln in enumerate(lines) if CLAUDE_ASK.search(ln)]
    if not at:
        return None
    menu = _menu(lines[at[-1]:])
    options = menu[1] if menu else ()    # no menu drawn (yet): read, never answered
    head = lines[:at[-1]]
    header = max((i for i, ln in enumerate(head) if CLAUDE_HEADER.match(ln.strip())), default=None)
    body = [ln.strip() for ln in head[header + 1:]] if header is not None else []
    target = next((t for t in body if t), "")
    kind = head[header].strip().lower() if header is not None else ""
    action = "run" if kind.startswith("bash") else "edit" if kind.split()[0:1] in (["edit"], ["create"], ["write"]) else "use"
    # The command box can carry a description under the command: never exact.
    return Prompt("permission", provider, lines[at[-1]].strip(), target, action, options=options,
                  keys=any(selected for _, _, selected in options))


def _antigravity(lines: list[str], provider: str) -> Prompt | None:
    at = [i for i, ln in enumerate(lines) if "Requesting permission for" in ln]
    if not at:
        return None
    first = lines[at[-1]].split("Requesting permission for", 1)[1].strip().lstrip(":").strip()
    body = []
    for ln in lines[at[-1] + 1:]:
        text = ln.strip()
        if not text or re.search(r"\?$|^yes\s*/\s*no$|esc to cancel", text, re.I):
            break
        body.append(text)
    question = next((ln.strip() for ln in lines[at[-1]:] if ln.strip().endswith("?")), "Allow this?")
    target = "\n".join(body) or first
    action = "run" if "command" in question.lower() else "use"
    return Prompt("permission", provider, question, target, action, exact=len(body) == 1,
                  options=((1, "Yes", False), (2, "No", False)))


def _opencode(lines: list[str], provider: str) -> Prompt | None:
    at = [i for i, ln in enumerate(lines) if re.search(r"Permission required|Allow once\s+Allow always\s+Reject", ln)]
    if not at:
        return None
    start = next((i for i, ln in enumerate(lines) if "Permission required" in ln), at[-1])
    body = [ln.strip() for ln in lines[start + 1:] if ln.strip() and not re.search(r"Allow once|Reject", ln)]
    command = next((t[2:] for t in body if t.startswith("$ ")), "")
    target = command or next(iter(body), "")
    return Prompt("permission", provider, "Permission required", target, "run" if command else "use",
                  options=((1, "Allow once", False), (2, "Allow always", False), (3, "Reject", False)))


TRUST = re.compile(r"(?i)\btrust (?:this|the files in this|the contents of this) (?:folder|project|workspace)"
                   r"|one you trust|Trust this folder\?")


def _trust(lines: list[str], provider: str) -> Prompt | None:
    text = "\n".join(lines)
    if not TRUST.search(text):
        return None
    menu = _menu(lines)
    question = next((ln.strip() for ln in lines if TRUST.search(ln)), "Trust this folder?")
    return Prompt("trust", provider, question, options=menu[1] if menu else ())


def _question(lines: list[str], provider: str) -> Prompt | None:
    """A numbered question (prompt_answer._numbered_question), or another menu
    brindle's sweeps take for a live prompt (prompt_answer.looks_like_prompt)."""
    tail = "\n".join(lines)
    menu = _menu(lines)
    if menu is not None:
        start, options = menu
        question = next((ln.strip() for ln in reversed(lines[:start]) if ln.strip()), "")
        question = re.sub(r"^[⏺•●]\s*", "", question)
        kind = "question" if prompt_answer._numbered_question(tail) else "menu"
        return Prompt(kind, provider, question, options=options, keys=any(s for _, _, s in options))
    if prompt_answer.looks_like_prompt(tail):
        question = next((re.sub(r"^[⏺•●]\s*", "", ln.strip()) for ln in reversed(lines)
                         if ln.strip() and not FOOTER.search(ln) and not re.match(rf"^\s*[{CURSOR}]", ln)), "")
        return Prompt("menu", provider, question)
    return None


# Each CLI's own permission prompts first, then what any CLI can show.
PARSERS = {"codex": (_codex,), "claude": (_claude,), "antigravity": (_antigravity,), "opencode": (_opencode,)}
SHARED = (_trust, _question)


def classify(provider: str, screen: str, brief: str = "") -> Prompt | None:
    """What ``screen``, a worker's pane, is waiting on, or None when it isn't
    waiting on anything brindle can see (busy, ready for input, the brief)."""
    from brindle.providers import get_provider

    if prompt_answer.brief_draft(screen, brief):
        return None
    tail = "\n".join(prompt_answer._tail(prompt_answer._past_brief(screen, brief), 40))
    if prompt_answer.pane_busy(tail):
        return None
    lines = _lines(tail)
    for parse in (*PARSERS.get(provider, ()), *SHARED):
        prompt = parse(lines, provider)
        if prompt is not None:
            return prompt
    try:
        asking = get_provider(provider).tool_permission(tail)
    except KeyError:
        asking = False
    if asking:
        return Prompt("permission", provider, "Permission requested")
    if prompt_answer.blocking_prompt(screen, provider, brief):
        return Prompt("unknown", provider)
    return None


# -- words ----------------------------------------------------------------------------------


def _one_line(text: str, width: int = 300) -> str:
    text = " ".join(prompt_answer.redact(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def who(db: DB, agent: Agent) -> str:
    ws = db.get_workspace(agent.workspace_id)
    label = LABEL.get(agent.provider, agent.provider.capitalize())
    return f"The {label} worker on branch {ws.branch}" if ws else f"A {label} worker"


def sentence(db: DB, agent: Agent, prompt: Prompt) -> str:
    """What the person reads: who asks, for what, and why, in one sentence."""
    return f"{who(db, agent)} {predicate(prompt)}"


def predicate(prompt: Prompt) -> str:
    """``sentence`` after who asks: what the app shows beside the worker's name."""
    why = _one_line(prompt.reason).rstrip(".?!")
    because = f", saying “{why}”" if why else ""
    if prompt.kind == "permission":
        if not prompt.target:
            return "is waiting on a permission prompt brindle can't read; open its terminal to see what it asks."
        verb = {"run": "run", "edit": "edit"}.get(prompt.action, "use")
        where = " outside its sandbox" if prompt.sandbox else ""
        return f"wants to {verb} {_one_line(prompt.target)}{where}{because}."
    if prompt.kind == "trust":
        return "is asking whether to trust a folder, which brindle never answers for a running worker."
    question = _one_line(prompt.question).rstrip(".:")
    if not question:
        return "is waiting on a prompt brindle can't read; open its terminal to see it."
    choices = ", ".join(_one_line(t, 60) for _, t, _ in prompt.options[:6])
    return f"asks “{question}”" + (f" (options: {choices})." if choices else ".")


def _note_head(db: DB, agent: Agent, prompt: Prompt) -> str:
    """A supervisor note's first line, the one its chat shows: which worker, what it waits on."""
    ws = db.get_workspace(agent.workspace_id)
    where = f" on branch `{ws.branch}`" if ws else ""
    what = {"trust": "a trust dialog", "menu": "a menu", "question": "a question",
            "permission": "a permission prompt", "unknown": "a prompt brindle can't read"}[prompt.kind]
    return f"[brindle] Worker {agent.id} ({agent.profile}){where} is waiting on {what}: {sentence(db, agent, prompt)}"


def supervisor_note(db: DB, agent: Agent, prompt: Prompt, screen: str = "") -> str:
    """The structured message its supervisor gets for a prompt that isn't a
    permission it can answer with answer_permission. ``screen``: shown,
    redacted, only for a prompt brindle couldn't read."""
    lines = [_note_head(db, agent, prompt)]
    if prompt.options:
        lines.append("Options:\n" + "\n".join(f"  {n}. {_one_line(t, 120)}" for n, t, _ in prompt.options))
    if prompt.kind == "unknown" and screen.strip():
        shown = [ln for ln in prompt_answer._tail(screen, 12) if ln.strip()]
        lines.append("What its screen shows:\n" + prompt_answer.redact("\n".join(shown)))
    how = {"question": "If it is a task question, answer it with send_message; otherwise requeue its task or "
                       "remove its workspace.",
           "trust": "brindle never trusts a folder for a running worker: requeue its task or remove its "
                    "workspace, or ask the person with need_user if the folder is one it needs.",
           }.get(prompt.kind, "brindle can't pick an option for it: send it guidance with send_message, requeue its "
                              "task, or ask the person with need_user if only they can decide.")
    lines.append(how)
    return "\n\n".join(lines)


# -- route ----------------------------------------------------------------------------------


def _since(agent: Agent) -> float:
    return agent.status_since or agent.created_at


def _mark(db: DB, agent: Agent) -> dict | None:
    raw = db.mark(agent.id, MARK)
    return json.loads(raw) if raw else None


def _ours(mark: dict | None, agent: Agent, prompt: Prompt) -> bool:
    return bool(mark) and mark["key"] == prompt.key and mark["since"] == _since(agent)


def _set(db: DB, agent: Agent, mark: dict) -> None:
    db.set_mark(agent.id, MARK, json.dumps(mark))


def _capture(agent: Agent) -> str | None:
    from brindle import agents

    try:
        return tmux.capture(agent.tmux_window, lines=CAPTURE_LINES, server=agents.server_of(agent))
    except tmux.TmuxError:
        return None


def _brief(db: DB, agent: Agent) -> str:
    row = db.conn.execute("SELECT submitted_prompt FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    return (row["submitted_prompt"] if row else None) or agent.task or ""


def current(db: DB, agent: Agent, screen: str | None = None) -> Prompt | None:
    """What ``agent``'s pane is waiting on now."""
    screen = _capture(agent) if screen is None else screen
    return classify(agent.provider, screen or "", _brief(db, agent))


def _supervisor(db: DB, agent: Agent) -> Agent | None:
    from brindle import agents

    if not agent.parent_id or agent.parent_id == agent.id:
        return None
    sup = db.get_agent(agent.parent_id)
    if sup is None or agents._closed(sup) or not agents.runs_process(sup):
        return None
    return sup


def route(db: DB, agent: Agent, screen: str | None = None, prompt: Prompt | None = None) -> str | None:
    """Decide what waiting worker ``agent``'s pane shows, once per prompt and
    spell of waiting, and say where it stands: "answered" (by the rules),
    "supervisor" (its supervisor's), "person", or "hook" (a permission
    brindle's hook already routed); None when it isn't a waiting worker or
    its pane shows nothing to answer. The first process to claim it acts."""
    from brindle import agents

    if (agent.status != "waiting" or not agent.tmux_window or not agent.parent_id or agent.headless
            or agent.mode not in agents.REPORTING_MODES):
        return None
    if screen is None:
        screen = _capture(agent) or ""
    if prompt is None:
        prompt = current(db, agent, screen)
    if prompt is None or db.get_workspace(agent.workspace_id) is None:
        return None
    if prompt.kind == "permission" and db.permission_requests_of(agent.id):
        return "hook"   # its hook recorded the request and routed it (agents.permission_request_decision)
    raw = db.mark(agent.id, MARK)
    mark = json.loads(raw) if raw else None
    if _ours(mark, agent, prompt):
        return _stage(db, agent, mark)
    fresh = {"key": prompt.key, "since": _since(agent), "kind": prompt.kind, "stage": "deciding", "at": time.time(),
             "sentence": sentence(db, agent, prompt)}
    if not db.swap_mark(agent.id, MARK, raw, json.dumps(fresh)):
        return _stage(db, agent, _mark(db, agent))   # another process claimed it first
    try:
        if prompt.kind == "permission":
            return _route_permission(db, agent, prompt, fresh)
        return _route_other(db, agent, prompt, fresh, screen)
    except BaseException:
        # Unclaimed again, so the next sweep routes it (a send that failed, say).
        db.set_mark(agent.id, MARK, raw)
        raise


def _route_other(db: DB, agent: Agent, prompt: Prompt, fresh: dict, screen: str = "") -> str:
    """A trust dialog, menu, question or unreadable prompt: its supervisor's, as a structured message."""
    from brindle import agents

    won, previous = db.claim_note(agent.id, _since(agent))
    if won:     # queued for a supervisor that isn't running now, as any message to it is
        body = supervisor_note(db, agent, prompt, screen)
        try:
            agents.send_message(db, agent.parent_id, body, sender_id=agent.id)
        except agents.AgentError:
            db.enqueue(agent.parent_id, body, agent.id)
        except Exception:
            db.release_note(agent.id, _since(agent), previous)
            raise
    if _supervisor(db, agent) is None:
        _set(db, agent, {**fresh, "stage": "person"})    # nobody running will answer it
        return "person"
    _set(db, agent, {**fresh, "stage": "supervisor"})
    return "supervisor"


def _decide(db: DB, agent: Agent, prompt: Prompt):
    """The policy's word on a command ``prompt`` asks to run, as Codex's hook would have it."""
    from brindle import agents, permissions
    from brindle.config import load_repo_config
    from brindle.profiles import profile_rules_for

    ws = db.get_workspace(agent.workspace_id)
    cfg = load_repo_config(ws.repo_root)
    if not (prompt.exact and prompt.action == "run" and prompt.keys):
        return permissions.Decision("ask", "brindle can't read this request exactly enough to decide it"), cfg
    # A reviewer with the policy off gets what its allowed_tools cover and nothing else, as its hook does.
    review_only = cfg.permission_policy != "on" and agent.mode == "review"
    decision = agents.codex_policy_decision(agent, ws, cfg, [_request(db, agent, prompt)],
                                            profile_rules_for(agent.profile, ws.repo_root),
                                            agents._session_of(db, agent), review_only=review_only)
    if decision.decision == "deny" and cfg.permission_policy != "on":
        # The person turned brindle's rules off: only what they allow answers by itself.
        decision = permissions.Decision("ask", decision.reason)
    return decision, cfg


def _request(db: DB, agent: Agent, prompt: Prompt):
    from brindle import permissions

    ws = db.get_workspace(agent.workspace_id)
    return permissions.Request(agent.provider, "bash" if prompt.action == "run" else "other",
                               "Bash" if prompt.action == "run" else "Tool", cwd=ws.path, worktree=ws.path,
                               repo_root=ws.repo_root, command=prompt.target or None, agent=agent.id)


def _route_permission(db: DB, agent: Agent, prompt: Prompt, fresh: dict) -> str | None:
    from brindle import permission_routing, permissions
    from brindle.config import load_repo_config

    try:
        decision, cfg = _decide(db, agent, prompt)
    except Exception:  # noqa: BLE001 - a broken policy decides nothing: an ask
        log.exception("brindle: couldn't decide %s's prompt", agent.id)
        decision = permissions.Decision("ask", "brindle couldn't check it against its rules")
        cfg = load_repo_config(db.get_workspace(agent.workspace_id).repo_root)
    _note(db, agent, prompt, decision)
    if decision.decision in ("allow", "deny"):
        if press(db, agent, prompt, decision.decision, decision.reason, by="policy"):
            return "answered"
        shown = current(db, agent)
        if shown is None or shown.key != prompt.key:
            _set(db, agent, {**fresh, "stage": "gone"})
            return None     # answered or withdrawn meanwhile: nothing is left to route
    # Its supervisor's first, as a hook's ask is; the person's once it escalates or its time is up.
    supervisor = permission_routing.supervisor_for(db, agent, decision)
    seconds = cfg.permission_supervisor_hold_seconds
    why = decision.reason.removesuffix("; asking the user")
    if prompt.sandbox:
        why += "; Codex asks at its own prompt to run it outside its sandbox"
    if prompt.reason:
        why += f" (its reason: {_one_line(prompt.reason)})"
    if supervisor is not None and seconds > 0 and (
            permission_routing.ask_supervisor(db, agent, supervisor, prompt.request_id, _request(db, agent, prompt),
                                              why, seconds) if prompt.keys
            else _ask_unpressable(db, agent, supervisor, prompt, why, seconds)):
        _set(db, agent, {**fresh, "stage": "supervisor", "until": time.time() + seconds})
        return "supervisor"
    return _to_person(db, agent, prompt, fresh)


def _ask_unpressable(db: DB, agent: Agent, supervisor: Agent, prompt: Prompt, why: str, seconds: float) -> bool:
    """A permission at a prompt whose keys brindle doesn't know (Antigravity's,
    OpenCode's): its supervisor decides whether the person sees it, structured."""
    from brindle import agents

    body = (f"{_note_head(db, agent, prompt)} ({why}).\n\n"
            "brindle can't press an answer on this CLI's prompt. If the person should decide it, "
            f"answer_permission(agent_id=\"{agent.id}\", request_id=\"{prompt.request_id}\", answer=\"escalate\", "
            "reason=\"...\"); if it shouldn't run, send the worker guidance with send_message or requeue its task. "
            f"After {round(seconds)}s the person is asked.")
    try:
        agents._send_message(db, supervisor, body, None)
    except (agents.AgentError, tmux.TmuxError, OSError):
        log.info("brindle: couldn't ask supervisor %s about %s's prompt", supervisor.id, agent.id)
        return False
    return True


def _to_person(db: DB, agent: Agent, prompt: Prompt, mark: dict) -> str:
    _set(db, agent, {**mark, "stage": "person"})
    _tell_person_pending(db, agent, prompt)
    return "person"


def _tell_person_pending(db: DB, agent: Agent, prompt: Prompt) -> None:
    from brindle import permission_routing

    permission_routing.tell_waiting_on_person(db, agent.id, sentence(db, agent, prompt))


def _stage(db: DB, agent: Agent, mark: dict | None) -> str | None:
    """Where a prompt already claimed stands; a supervisor's window that ran
    out hands a permission to the person."""
    if mark is None:
        return None
    if mark["stage"] == "supervisor" and "until" in mark and time.time() >= mark["until"]:
        if db.replace_mark(agent.id, MARK, json.dumps(mark), json.dumps({**mark, "stage": "person"})):
            prompt = current(db, agent)
            if prompt is not None and prompt.key == mark["key"]:
                _tell_person_pending(db, agent, prompt)
        return "person"
    if mark["stage"] == "supervisor" and _supervisor(db, agent) is None:
        return "person"     # its supervisor is gone: nobody else will answer it
    if mark["stage"] == "deciding" and time.time() - mark.get("at", 0) >= DECIDING_EXPIRES:
        return "person"     # whoever claimed it died deciding
    return mark["stage"]


def _note(db: DB, agent: Agent, prompt: Prompt, decision) -> None:
    from brindle import agents

    try:
        ws = db.get_workspace(agent.workspace_id)
        summary = _request(db, agent, prompt).summary()
        agents._record_permission(db, agent, ws, summary, f"{decision.decision}: {decision.reason}")
        if decision.decision != "ask":
            agents._note_decision(db, agent, ws, "bash", summary, decision)
    except Exception:  # noqa: BLE001 - bookkeeping never changes the answer
        log.exception("brindle: couldn't record %s's prompt decision", agent.id)


def with_supervisor(db: DB, agent: Agent) -> bool:
    """Whether what ``agent`` waits on is its supervisor's to answer still."""
    mark = _mark(db, agent)
    return (bool(mark) and mark["since"] == _since(agent) and mark["stage"] == "supervisor"
            and _stage(db, agent, mark) == "supervisor")


def withheld(db: DB, agent: Agent) -> bool:
    """Whether what ``agent`` waits on isn't the person's yet: a decision is
    under way (another process claimed it), its supervisor has it, or the
    rules answered it and the worker is only now moving on."""
    mark = _mark(db, agent)
    return (bool(mark) and mark["since"] == _since(agent)
            and _stage(db, agent, mark) in ("deciding", "supervisor", "answered", "gone"))


def described(db: DB, agent: Agent) -> str | None:
    """The sentence for what ``agent`` waits on in this spell of waiting, once routed."""
    mark = _mark(db, agent)
    return mark.get("sentence") if mark and mark["since"] == _since(agent) else None


def for_person(db: DB, agent: Agent, screen: str) -> Prompt | None:
    """The prompt on ``screen`` that is the person's to answer, after routing it."""
    prompt = current(db, agent, screen)
    if prompt is None or route(db, agent, screen, prompt) not in ("person", "hook"):
        return None
    return prompt


# -- answer ---------------------------------------------------------------------------------


def press(db: DB, agent: Agent, prompt: Prompt, choice: str, reason: str = "", by: str = "person") -> bool:
    """Answer ``prompt`` in the pane with ``choice`` (allow or deny): arrow
    keys to its option, then Enter once a fresh look shows the same prompt
    with that option selected. A deny also tells the worker why. False when
    the pane no longer shows it, or its option can't be reached."""
    from brindle import agents

    if not prompt.keys or prompt.kind != "permission":
        return False
    target = TARGET[CHOICES[choice]]
    with agents.delivery_lock(agent.id):
        fresh = db.get_agent(agent.id)
        if fresh is None or fresh.status != "waiting" or fresh.tmux_window != agent.tmux_window:
            return False
        server = agents.server_of(fresh)
        for _ in range(MAX_MOVES + 1):
            shown = current(db, fresh)
            if shown is None or shown.key != prompt.key:
                return False
            want = next((n for n, text, _ in shown.options if target.match(text)), None)
            at = next((n for n, _, selected in shown.options if selected), None)
            if want is None or at is None:
                return False
            try:
                if want == at:
                    tmux.send_keys(fresh.tmux_window, "Enter", server=server)
                    break
                tmux.send_keys(fresh.tmux_window, "Down" if want > at else "Up", server=server)
            except tmux.TmuxError:
                return False
        else:
            return False
        mark = _mark(db, fresh) or {"key": prompt.key, "since": _since(fresh), "kind": prompt.kind}
        _set(db, fresh, {**mark, "stage": "answered", "choice": CHOICES[choice], "by": by})
        db.set_status(fresh.id, "processing", only_if="waiting")
    if CHOICES[choice] == "deny":
        why = f": {reason}" if reason else ""
        body = (f"[brindle] {_one_line(prompt.target) or 'That request'} was denied {BY.get(by, by)}{why}. "
                "Don't run it again: do without it, or ask your supervisor with the send_message tool.")
        try:
            agents.send_message(db, fresh.id, body)
        except agents.AgentError:
            db.enqueue(fresh.id, body, None)
    return True


def answer(db: DB, agent: Agent, request_id: str, choice: str, reason: str = "", by: str = "person") -> bool:
    """``choice`` to the permission ``request_id`` ``agent``'s pane shows now,
    for ``by``: the person, or its supervisor while it is theirs."""
    prompt = current(db, agent)
    if prompt is None or prompt.request_id != request_id:
        return False
    mark = _mark(db, agent)
    if by == "supervisor" and not with_supervisor(db, agent):
        return False
    if by == "person" and not (_ours(mark, agent, prompt) and _stage(db, agent, mark) == "person"):
        return False
    return press(db, agent, prompt, choice, reason, by=by)


def escalate(db: DB, agent: Agent, request_id: str) -> bool:
    """The supervisor hands ``request_id`` to the person."""
    mark = _mark(db, agent)
    prompt = current(db, agent)
    if not with_supervisor(db, agent) or prompt is None or prompt.request_id != request_id:
        return False
    if not db.replace_mark(agent.id, MARK, json.dumps(mark), json.dumps({**mark, "stage": "person"})):
        return False
    _tell_person_pending(db, agent, prompt)
    return True
