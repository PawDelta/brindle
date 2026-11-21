"""A Codex sandbox escalation at Codex's own prompt, decided like a hook's ask.

A Codex worker whose repo runs without brindle's permission hook (the policy
off, or the hook untrusted) stops on Codex's own "Would you like to run the
following command?" whenever a command needs to leave its sandbox: often just
the repo's own test runner. Nothing read that prompt but the person, who got
the pane pasted into their Inbox.

Now the prompt is read (``parse``) and decided the way the PermissionRequest
hook decides a Codex request (agents.codex_policy_decision):

- what the rules allow is answered "Yes, proceed" in the pane, once;
- what they deny, with the policy on, is answered "No" and the worker told why;
- anything else goes to the worker's supervisor first, as a hook's ask does
  (permission_routing), for ``permission_supervisor_hold_seconds``, then to
  the person, as one plain sentence with Allow and Deny.

The pane is answered only with arrow keys and Enter, and Enter only once a
fresh look shows the same request with the chosen option selected, under the
pane's delivery lock. What was decided is kept in an ``agent_marks`` row per
worker (MARK), keyed by the request and the spell of waiting it showed in.
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

log = logging.getLogger("brindle.pane_escalation")

MARK = "pane-escalation"
PREFIX = "pane-"          # request ids of escalations read off a pane
CHOICES = {"allow": "allow", "allow_once": "allow", "supervisor_allow": "allow",
           "deny": "deny", "supervisor_deny": "deny"}
RUN = re.compile(r"Would you like to run the following command\?")
OPTION = re.compile(r"^\s*(›\s*)?(\d+)[.)]\s+(\S.*?)\s*$")
TARGET = {"allow": re.compile(r"(?i)^yes, proceed\b(?!.*don.t ask)"), "deny": re.compile(r"(?i)^no\b")}
MAX_MOVES = 6
CAPTURE_LINES = 60
DECIDING_EXPIRES = 120.0  # a claim this old whose decision never landed is the person's
BY = {"policy": "by brindle's permission rules", "supervisor": "by your supervisor", "person": "by the person"}


@dataclass(frozen=True)
class Escalation:
    command: str
    reason: str          # Codex's own "Reason:" line: the model's words, never trusted
    exact: bool          # the command is on one line, so it reads exactly as Codex will run it
    options: tuple       # (number, text, selected) per menu line

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.command}\0{self.reason}".encode()).hexdigest()

    @property
    def request_id(self) -> str:
        return PREFIX + self.key[:16]


def parse(screen: str) -> Escalation | None:
    """The command escalation Codex's prompt at the bottom of ``screen`` asks
    for, or None when it shows none (or another of Codex's prompts)."""
    lines = [ln.rstrip() for ln in prompt_answer.plain(screen).splitlines()]
    at = [i for i, ln in enumerate(lines) if RUN.search(ln)]
    if not at:
        return None
    body = lines[at[-1] + 1:]
    first_option = next((i for i, ln in enumerate(body) if OPTION.match(ln)), None)
    if first_option is None:
        return None
    options = []
    for ln in body[first_option:]:
        m = OPTION.match(ln)
        if m:
            options.append((int(m[2]), m[3], bool(m[1])))
        elif ln.strip() and not re.search(r"(?i)press enter|esc to cancel", ln):
            return None     # something after the menu: it isn't the live prompt
    if not any(TARGET["allow"].match(t) for _, t, _ in options) or not any(TARGET["deny"].match(t) for _, t, _ in options):
        return None
    head = body[:first_option]
    command, reason, field = [], [], None
    for ln in head:
        text = ln.strip()
        if not text:
            field = None
        elif text.startswith("$ "):
            command = [text[2:]]
            field = command
        elif text.startswith("Reason:"):
            reason = [text.removeprefix("Reason:").strip()]
            field = reason
        elif field is not None:
            field.append(text)
    if not command:
        return None
    return Escalation("\n".join(command), " ".join(reason), len(command) == 1, tuple(options))


def _mark(db: DB, agent: Agent) -> dict | None:
    raw = db.mark(agent.id, MARK)
    return json.loads(raw) if raw else None


def _ours(mark: dict | None, agent: Agent, esc: Escalation) -> bool:
    return bool(mark) and mark["key"] == esc.key and mark["since"] == _since(agent)


def _since(agent: Agent) -> float:
    return agent.status_since or agent.created_at


def _branch(db: DB, agent: Agent) -> str | None:
    ws = db.get_workspace(agent.workspace_id)
    return ws.branch if ws else None


def sentence(db: DB, agent: Agent, esc: Escalation) -> str:
    """What the person reads: who asks, to run what, and why, in one sentence."""
    branch = _branch(db, agent)
    who = f"The Codex worker on branch {branch}" if branch else "A Codex worker"
    command = " ".join(prompt_answer.redact(esc.command).split())
    why = prompt_answer.redact(esc.reason).strip().rstrip(".?!")
    because = f", saying “{why}”" if why else ""
    return f"{who} wants to run {command} outside its sandbox{because}."


def _request(db: DB, agent: Agent, esc: Escalation):
    from brindle import permissions

    ws = db.get_workspace(agent.workspace_id)
    return permissions.Request("codex", "bash", "Bash", cwd=ws.path, worktree=ws.path, repo_root=ws.repo_root,
                               command=esc.command, agent=agent.id)


def _decide(db: DB, agent: Agent, esc: Escalation):
    """The policy's word on ``esc``, as Codex's hook would have it."""
    from brindle import agents, permissions
    from brindle.config import load_repo_config
    from brindle.profiles import profile_rules_for

    ws = db.get_workspace(agent.workspace_id)
    cfg = load_repo_config(ws.repo_root)
    if not esc.exact:
        return permissions.Decision("ask", "the command runs over several lines, so brindle can't read it exactly"), cfg
    decision = agents.codex_policy_decision(agent, ws, cfg, [_request(db, agent, esc)],
                                            profile_rules_for(agent.profile, ws.repo_root),
                                            agents._session_of(db, agent))
    if decision.decision == "deny" and cfg.permission_policy != "on":
        # The person turned brindle's rules off: only what they allow answers by itself.
        decision = permissions.Decision("ask", decision.reason)
    return decision, cfg


def route(db: DB, agent: Agent, screen: str | None = None) -> str | None:
    """Decide the escalation ``agent``'s pane shows, once per request and spell
    of waiting, and say where it stands: "answered" (by the policy), "supervisor"
    (its supervisor's to answer still) or "person"; None when the pane shows
    no command escalation. Safe from any process: the first to claim it acts."""
    from brindle import agents, permission_routing

    if agent.provider != "codex" or not agent.tmux_window or agent.status != "waiting":
        return None
    if screen is None:
        try:
            screen = tmux.capture(agent.tmux_window, lines=CAPTURE_LINES, server=agents.server_of(agent))
        except tmux.TmuxError:
            return None
    esc = parse(screen)
    if esc is None or db.get_workspace(agent.workspace_id) is None:
        return None
    raw = db.mark(agent.id, MARK)
    mark = json.loads(raw) if raw else None
    if _ours(mark, agent, esc):
        return _stage(db, agent, mark)
    fresh = {"key": esc.key, "since": _since(agent), "stage": "deciding", "at": time.time()}
    if not db.swap_mark(agent.id, MARK, raw, json.dumps(fresh)):
        return _stage(db, agent, _mark(db, agent))   # another process claimed it first
    try:
        decision, cfg = _decide(db, agent, esc)
    except Exception:  # noqa: BLE001 - a broken policy leaves it to the person
        log.exception("brindle: couldn't decide %s's escalation", agent.id)
        from brindle import permissions

        decision, cfg = permissions.Decision("ask", "brindle couldn't check it"), None
    _note(db, agent, esc, decision)
    if decision.decision in ("allow", "deny"):
        if press(db, agent, esc, decision.decision, decision.reason, by="policy"):
            return "answered"
        shown = parse(_capture(agent) or "")
        if shown is None or shown.key != esc.key:
            _set(db, agent, {**fresh, "stage": "gone"})
            return None     # answered or withdrawn meanwhile: nothing is left to route
    supervisor = permission_routing.supervisor_for(db, agent, decision) if decision.decision == "ask" else None
    seconds = cfg.permission_supervisor_hold_seconds if cfg is not None else 0
    if supervisor is not None and seconds > 0 and permission_routing.ask_supervisor(
            db, agent, supervisor, esc.request_id, _request(db, agent, esc),
            f"{decision.reason.removesuffix('; asking the user')}; Codex asks at its own prompt to run it "
            "outside its sandbox"
            + (f" (its reason: {prompt_answer.redact(esc.reason)})" if esc.reason else ""), seconds):
        _set(db, agent, {**fresh, "stage": "supervisor", "until": time.time() + seconds})
        return "supervisor"
    return _to_person(db, agent, esc, fresh)


def _set(db: DB, agent: Agent, mark: dict) -> None:
    db.set_mark(agent.id, MARK, json.dumps(mark))


def _stage(db: DB, agent: Agent, mark: dict | None) -> str | None:
    """Where an escalation already claimed stands; a supervisor's window that
    ran out hands it to the person."""
    if mark is None:
        return None
    if mark["stage"] == "supervisor" and time.time() >= mark.get("until", 0):
        old = json.dumps(mark)
        new = {**mark, "stage": "person"}
        if db.replace_mark(agent.id, MARK, old, json.dumps(new)):
            esc = parse(_capture(agent) or "")
            if esc is not None and esc.key == mark["key"]:
                _tell_person_pending(db, agent, esc)
        return "person"
    if mark["stage"] == "deciding" and time.time() - mark.get("at", 0) >= DECIDING_EXPIRES:
        return "person"     # whoever claimed it died deciding
    return mark["stage"]


def _to_person(db: DB, agent: Agent, esc: Escalation, mark: dict) -> str:
    _set(db, agent, {**mark, "stage": "person"})
    _tell_person_pending(db, agent, esc)
    return "person"


def _tell_person_pending(db: DB, agent: Agent, esc: Escalation) -> None:
    from brindle import permission_routing

    permission_routing.tell_waiting_on_person(db, agent.id, sentence(db, agent, esc))


def _note(db: DB, agent: Agent, esc: Escalation, decision) -> None:
    from brindle import agents

    try:
        ws = db.get_workspace(agent.workspace_id)
        summary = _request(db, agent, esc).summary()
        agents._record_permission(db, agent, ws, summary, f"{decision.decision}: {decision.reason}")
        if decision.decision != "ask":
            agents._note_decision(db, agent, ws, "bash", summary, decision)
    except Exception:  # noqa: BLE001 - bookkeeping never changes the answer
        log.exception("brindle: couldn't record %s's escalation", agent.id)


def with_supervisor(db: DB, agent: Agent) -> bool:
    """Whether the escalation ``agent`` waits on is its supervisor's to answer still."""
    mark = _mark(db, agent)
    return bool(mark) and mark["stage"] == "supervisor" and mark["since"] == _since(agent) \
        and time.time() < mark.get("until", 0)


def pending(db: DB, agent: Agent, screen: str) -> Escalation | None:
    """The escalation on ``screen`` that is the person's to answer, after routing it."""
    esc = parse(screen)
    if esc is None or route(db, agent, screen) != "person":
        return None
    return esc


def _capture(agent: Agent) -> str | None:
    from brindle import agents

    try:
        return tmux.capture(agent.tmux_window, lines=CAPTURE_LINES, server=agents.server_of(agent))
    except tmux.TmuxError:
        return None


def press(db: DB, agent: Agent, esc: Escalation, choice: str, reason: str = "", by: str = "person") -> bool:
    """Answer ``esc`` at Codex's prompt with ``choice`` (allow or deny): arrow
    keys to its option, then Enter once a fresh look shows the same request
    with that option selected. A deny also tells the worker why. False when
    the pane no longer shows that request, or the option can't be reached."""
    from brindle import agents

    target = TARGET[CHOICES[choice]]
    with agents.delivery_lock(agent.id):
        fresh = db.get_agent(agent.id)
        if fresh is None or fresh.status != "waiting" or fresh.tmux_window != agent.tmux_window:
            return False
        server = agents.server_of(fresh)
        for _ in range(MAX_MOVES + 1):
            shown = parse(_capture(fresh) or "")
            if shown is None or shown.key != esc.key:
                return False
            want = next((n for n, text, _ in shown.options if target.match(text)), None)
            at = next((n for n, _, selected in shown.options if selected), None)
            if want is None or at is None:
                return False
            if want == at:
                try:
                    tmux.send_keys(fresh.tmux_window, "Enter", server=server)
                except tmux.TmuxError:
                    return False
                break
            try:
                tmux.send_keys(fresh.tmux_window, "Down" if want > at else "Up", server=server)
            except tmux.TmuxError:
                return False
        else:
            return False
        mark = _mark(db, fresh) or {"key": esc.key, "since": _since(fresh)}
        _set(db, fresh, {**mark, "stage": "answered", "choice": CHOICES[choice], "by": by})
        db.set_status(fresh.id, "processing", only_if="waiting")
    if CHOICES[choice] == "deny":
        why = f": {reason}" if reason else ""
        body = (f"[brindle] Running {esc.command} outside your sandbox was denied {BY.get(by, by)}{why}. "
                "Don't run it again: do without it, or ask your supervisor with the send_message tool.")
        try:
            agents.send_message(db, fresh.id, body)
        except agents.AgentError:
            db.enqueue(fresh.id, body, None)
    return True


def answer(db: DB, agent: Agent, request_id: str, choice: str, reason: str = "", by: str = "person") -> bool:
    """``choice`` to the escalation ``request_id`` ``agent``'s pane shows now,
    for whoever ``by`` is (the person, or its supervisor while it is theirs).
    False when the pane shows another request, or none."""
    esc = parse(_capture(agent) or "")
    if esc is None or esc.request_id != request_id:
        return False
    mark = _mark(db, agent)
    if by == "supervisor" and not with_supervisor(db, agent):
        return False
    if by == "person" and not (_ours(mark, agent, esc) and _stage(db, agent, mark) == "person"):
        return False
    return press(db, agent, esc, choice, reason, by=by)


def escalate(db: DB, agent: Agent, request_id: str) -> bool:
    """The supervisor hands ``request_id`` to the person."""
    mark = _mark(db, agent)
    esc = parse(_capture(agent) or "")
    if not with_supervisor(db, agent) or esc is None or esc.request_id != request_id:
        return False
    if not db.replace_mark(agent.id, MARK, json.dumps(mark), json.dumps({**mark, "stage": "person"})):
        return False
    _tell_person_pending(db, agent, esc)
    return True
