"""What needs a person across a repo's sessions: the "needs you" rules the
sidebar uses, plus plans, proposed checks, failed merges, cloud logins,
permission requests and org notices as ``InboxItemDTO`` rows.

A permission request is answered (``answer_permission``) through brindle's
hook while it holds the request (agents._await_app_answer), else by typing
the answer into the CLI's own prompt: the same keys a person at the pane
would press, only ever on the person's request from the app."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from brindle import agents, autopilot, bus, permissions, prompt_answer, tmux, view
from brindle.config import load_repo_config
from brindle.db import DB, Agent
from brindle.providers import get_provider
from brindle.service.dto import InboxActionDTO, InboxItemDTO, PermissionChoiceDTO, PermissionDTO
from brindle.service.errors import ServiceError

WORKERS = ("assign", "handoff")
CHOICES = ("allow_once", "allow_always", "deny")
# What a prompt's option grants, by its wording; the first match wins.
OPTION_CHOICE = (
    ("deny", re.compile(r"(?i)^(?:no|reject|deny|don.t allow|cancel)\b")),
    ("allow_always", re.compile(r"(?i)don.t ask again|\balways\b|allow all|for (?:this|the) session")),
    ("allow_once", re.compile(r"(?i)^(?:yes|allow|approve|proceed|run)\b")),
)
BOX = re.compile(r"^[\s│┃╭╮╰╯─━]+|[\s│┃╭╮╰╯─━]+$")
STEP_SECONDS = 0.2       # between keys, and between looks at the pane or the hook
CONFIRM_SECONDS = 5.0    # for the prompt to clear (or the hook to take the answer)


def awaiting_review(agent: dict, ws: dict) -> str | None:
    """How a worker that reported and hasn't been approved yet reads, or None."""
    if (agent.get("mode") in WORKERS and agent.get("reported")
            and agent["status"] not in ("processing", "starting")):
        state = ws.get("review_state")   # where it is in review (pipeline.review_state)
        if state and not state.startswith("approved"):
            return state
        return {"approved": None, "changes": "changes requested"}.get(ws.get("review"), "to review")
    return None


def needs_you(agent: dict, ws: dict) -> str | None:
    """Why a person must act on ``agent``'s row, or None: it is waiting on a
    prompt or dialog, it is a supervisor with an open need_user question, or
    it is a worker awaiting review in a session without autopilot (with
    autopilot on, the supervisor reviews it)."""
    if agent["status"] == "waiting":
        return "needs you"
    if agent["status"] == "failed":
        return "credentials need attention"
    if ws.get("asking") == agent["id"]:
        return "has a question"
    return None if ws.get("autopilot") else awaiting_review(agent, ws)


def summary(snap: list[dict]) -> str:
    agents_ = [a for ws in snap for a in ws["agents"]]
    waiting = sum(bool(needs_you(a, ws)) for ws in snap for a in ws["agents"])
    busy = sum(a["status"] in ("processing", "starting") for a in agents_)
    parts = []
    if waiting:
        parts.append(f"{waiting} need{'s' * (waiting == 1)} you")
    if busy:
        parts.append(f"{busy} working")
    if not parts:
        parts.append("all quiet")
    return " · ".join(parts)


def _kind(agent: dict, ws: dict, reason: str) -> str:
    if agent["status"] == "waiting":
        return "waiting"
    if agent["status"] == "failed":
        return "credentials"
    if ws.get("asking") == agent["id"]:
        return "question"
    return "merge_failed" if reason.startswith("merge failed") else "review"


def _org_notices() -> list[InboxItemDTO]:
    from brindle.pro import status

    try:
        notices = status.unread(status.load())
    except Exception:  # noqa: BLE001 - notices are an extra: never break the inbox
        return []
    return [InboxItemDTO(kind="org_notice", reason=n["text"], detail=n["from_role"], notice_id=n["id"])
            for n in notices]


def _sign_in(provider: str) -> InboxActionDTO | None:
    """The provider CLI's own sign-in the app can start (routes_actions.provider_login)."""
    from brindle.pro.team_policy import CLI_NAMES

    return InboxActionDTO(method="POST", path=f"/v1/providers/{provider}/login") if provider in CLI_NAMES else None


def items(db: DB, repo_root: str | None, panes: dict[str, bool] | None = None) -> list[InboxItemDTO]:
    """Everything waiting on a person in ``repo_root``'s sessions (every
    session when None), then the org's unread notices. Each agent's item
    names its session (``root_id``), so the app can link into it."""
    out: list[InboxItemDTO] = []
    for ws in view.snapshot(db, repo_root, panes=panes):
        for a in ws["agents"]:
            if a.get("locked"):
                continue   # another identity's session: structure only, nothing read from it
            row = db.get_agent(a["id"])
            if row is None:
                continue
            link = {"agent_id": a["id"], "workspace_id": ws["id"], "root_id": autopilot.root_of(db, a["id"])}
            # A failed merge waits on the person even when autopilot reviews the rest.
            reason = needs_you(a, ws) or next(
                (r for r in [awaiting_review(a, ws)] if r and r.startswith("merge failed")), None)
            asking = pending_permission(db, row) if a["status"] == "waiting" else None
            if asking:
                out.append(InboxItemDTO(kind="permission", reason="asks permission",
                                        detail=asking.request.summary() if asking.request else None,
                                        permission=asking.dto, **link))
            elif reason:
                kind = _kind(a, ws, reason)
                out.append(InboxItemDTO(kind=kind, reason=reason, **link,
                                        action=_sign_in(row.provider) if kind == "credentials" else None))
            if row.plan_state == "proposed":
                out.append(InboxItemDTO(kind="plan", reason="plan awaiting approval", **link,
                                        detail=row.plan_text))
            if row.parent_id is not None:
                continue
            ap = db.get_autopilot(row.id)
            # The workspace holds one asking id; each root's own state is the truth.
            if ap and ap.state == "blocked" and not any(
                    i.kind == "question" and i.agent_id == a["id"] for i in out):
                out.append(InboxItemDTO(kind="question", reason="has a question", **link))
            if ap and autopilot.proposing(ap):
                checks = json.loads(ap.proposal).get("checks") or []
                out.append(InboxItemDTO(kind="proposed_checks", reason="checks proposed", **link,
                                        detail="\n".join(checks)))
            login = db.get_login_state(row.id)
            if login and login.state == "auth_paused":
                out.append(InboxItemDTO(kind="login", reason="cloud login expired",
                                        **{**link, "agent_id": login.agent_id or a["id"]},
                                        detail=login.refresh_cmd))
    return out + _org_notices()


# -- permission requests ---------------------------------------------------------------------


@dataclass
class Pending:
    """A permission request ``agent`` waits on, and what answering it needs."""
    via: str                                 # "hook" or "prompt", as PermissionDTO.via
    row: dict | None                         # its permission_requests row, if brindle's hook saw it
    request: permissions.Request | None
    suggestions: list | None                 # Claude Code's own "always allow" updates
    dto: PermissionDTO


def _always_rule(req: permissions.Request | None, suggestions: list | None, repo_root: str | None) -> str | None:
    """What allow-always writes when brindle's hook takes the answer, or
    None when no rule could cover the request: Claude Code's suggested
    updates (it applies them itself), else brindle's own exact allow rule."""
    if suggestions:
        return f"Claude Code's own rule: {permissions.describe_updates(suggestions)}"
    pattern = permissions.learnable_pattern(req) if req else None
    if pattern is None:
        return None
    off = repo_root is not None and load_repo_config(repo_root).permission_policy != "on"
    return (f"brindle's permission rule: allow {pattern[0]} exact {pattern[1]!r}"
            + (" (applies once permission_policy is on)" if off else ""))


def _menu(lines: list[str]) -> tuple[int, list[str]]:
    """The prompt's own menu in ``lines``: the last run of numbered lines
    holding the cursor (a numbered list in the agent's output above it is
    no option), as (its first line's index, the options' texts)."""
    cursor = next((i for i in range(len(lines) - 1, -1, -1)
                   if prompt_answer.NUMBERED.match(lines[i]) and prompt_answer.SELECTED.match(lines[i])), None)
    if cursor is None:
        return len(lines), []
    start = end = cursor
    while start > 0 and prompt_answer.NUMBERED.match(lines[start - 1]):
        start -= 1
    while end + 1 < len(lines) and prompt_answer.NUMBERED.match(lines[end + 1]):
        end += 1
    return start, [prompt_answer.OPTION.match(ln).group(2) for ln in lines[start:end + 1]]


def _read_prompt(screen: str) -> tuple[str, list[PermissionChoiceDTO]]:
    """A permission prompt's wording and the choices among its menu's
    options, each labelled with the option's own text (which is what
    _press selects)."""
    lines = prompt_answer._tail(screen)
    start, options = _menu(lines)
    choices: dict[str, PermissionChoiceDTO] = {}
    for label in options:
        choice = next((c for c, pattern in OPTION_CHOICE if pattern.search(label)), None)
        if choice and choice not in choices:
            choices[choice] = PermissionChoiceDTO(
                choice=choice, label=label,
                rule=f"the CLI's own rule, as its option says: {label}" if choice == "allow_always" else None)
    wording = [BOX.sub("", ln) for ln in lines[:start]]
    wording = prompt_answer.redact("\n".join([ln for ln in wording if ln][-8:]))
    return wording, sorted(choices.values(), key=lambda c: CHOICES.index(c.choice))


def pending_permission(db: DB, agent: Agent) -> Pending | None:
    """The permission request ``agent`` waits on: one brindle's hook holds
    for the app's answer, else its CLI's prompt on screen (with what the
    hook recorded of it, when it saw it)."""
    row = db.latest_permission_request(agent.id)
    row = dict(row) if row is not None else None
    req = permissions.Request.from_dict(json.loads(row["request"])) if row else None
    suggestions = json.loads(row["suggestions"]) if row and row["suggestions"] else None
    fields = {"tool": req.tool, "kind": req.kind, "target": req.target()} if req else {}
    if row and row["channel"] == "hook":
        ws = db.get_workspace(agent.workspace_id)
        rule = _always_rule(req, suggestions, ws.repo_root if ws else None)
        choices = [PermissionChoiceDTO(choice="allow_once", label="Allow once"),
                   *([PermissionChoiceDTO(choice="allow_always", label="Allow always", rule=rule)] if rule else []),
                   PermissionChoiceDTO(choice="deny", label="Deny")]
        return Pending("hook", row, req, suggestions, PermissionDTO(via="hook", choices=choices, **fields))
    if not agent.tmux_window:
        return None
    try:
        screen = tmux.capture(agent.tmux_window, lines=60, server=agents.server_of(agent))
    except tmux.TmuxError:
        return None
    if not get_provider(agent.provider).tool_permission(screen):
        return None
    wording, choices = _read_prompt(screen)
    if req is None or not _on_screen(req, wording):
        # What the hook recorded isn't this prompt (an older request): the screen alone says what it asks.
        row, req, fields = None, None, {}
    return Pending("prompt", row, req, None,
                   PermissionDTO(via="prompt", wording=wording, choices=choices, **fields))


def _on_screen(req: permissions.Request, wording: str) -> bool:
    """Whether the prompt's ``wording`` names what ``req`` asks for: the
    start of its command or URL, or its file's name (CLIs shorten paths)."""
    text = " ".join(wording.split())
    lines = req.target().strip().splitlines()
    head = " ".join(lines[0].split())[:80] if lines else ""
    return bool(head) and head in text or bool(req.path) and os.path.basename(req.path) in text


def _until(done: Callable[[], bool], seconds: float = CONFIRM_SECONDS) -> bool:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() >= deadline:
            return False
        time.sleep(STEP_SECONDS)
    return True


def _press(agent: Agent, label: str) -> bool:
    """Select the option ``label`` in the CLI's permission prompt with its
    arrow keys, Enter once it shows selected, then whether the prompt
    cleared. Refused when the prompt on screen no longer offers it."""
    provider, server = get_provider(agent.provider), agents.server_of(agent)
    want = prompt_answer.Decision("permission", label)

    def capture() -> str:
        return tmux.capture(agent.tmux_window, lines=60, server=server)

    seen = None
    for _ in range(prompt_answer.MAX_MOVES * 2):
        screen = capture()
        if prompt_answer._tail(screen) == seen:
            time.sleep(STEP_SECONDS)   # the last key hasn't shown yet: don't press another
            continue
        key = prompt_answer.next_key(screen, want) if provider.tool_permission(screen) else None
        if key is None:
            raise ServiceError("conflict", f"{agent.id}'s prompt no longer offers {label!r}: "
                                           "fetch the inbox again", 409)
        tmux.send_keys(agent.tmux_window, key, server=server)
        if key == "Enter":
            return _until(lambda: not provider.tool_permission(capture()))
        seen = prompt_answer._tail(screen)
    raise ServiceError("conflict", f"couldn't reach {label!r} in {agent.id}'s prompt: answer it in its terminal", 409)


def answer_permission(db: DB, agent_id: str, choice: str) -> dict:
    """Answer the permission request ``agent_id`` waits on with ``choice``
    (allow_once, allow_always or deny), the person's own from the app.
    Through brindle's hook when it holds the request; else by the CLI
    prompt's own keys. Says how, what rule allow-always wrote, and whether
    the request then cleared."""
    if choice not in CHOICES:
        raise ServiceError("invalid", f"choice must be one of {', '.join(CHOICES)}")
    agent = db.get_agent(agent_id)
    if agent is None:
        raise ServiceError("not_found", f"no agent {agent_id}", 404)
    pending = pending_permission(db, agent)
    if pending is None:
        raise ServiceError("conflict", f"{agent_id} isn't waiting on a permission request", 409)
    offered = next((c for c in pending.dto.choices if c.choice == choice), None)
    if offered is None:
        raise ServiceError("conflict", f"{agent_id}'s request offers no {choice}: answer it in its terminal", 409)
    if pending.via == "hook":
        tool_use_id = pending.row["tool_use_id"]
        if choice == "allow_always" and not pending.suggestions:
            from brindle import antigravity

            permissions.add_rule(*permissions.learnable_pattern(pending.request), "allow")
            antigravity.resync_permissions()
        mac = permissions.answer_mac(agent.id, tool_use_id, choice)
        if mac is None:
            raise ServiceError("conflict", "no running daemon to sign the answer with", 409)
        if not db.answer_permission_request(agent.id, tool_use_id, choice, mac):
            raise ServiceError("conflict", f"{agent_id}'s request just went to its own prompt: "
                                           "fetch the inbox and answer it again", 409)
        cleared = _until(lambda: (db.conn.execute(
            "SELECT channel FROM permission_requests WHERE agent_id=? AND tool_use_id=?",
            (agent.id, tool_use_id)).fetchone() or {"channel": "answered"})["channel"] == "answered")
    else:
        cleared = _press(agent, offered.label)
        if pending.row:
            db.answer_permission_request(agent.id, pending.row["tool_use_id"], choice)
    bus.publish_agent(db, "permission.answered", agent.id, choice=choice, via=pending.via, cleared=cleared)
    return {"choice": choice, "via": pending.via, "cleared": cleared,
            "rule": offered.rule if choice == "allow_always" else None}
