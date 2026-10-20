"""What needs a person across a repo's sessions: the "needs you" rules the
sidebar uses, plus plans, proposed checks, failed merges, cloud logins,
permission requests and org notices as ``InboxItemDTO`` rows.

A permission request is answered from the app (``answer_permission``) only
while brindle's hook holds it (agents._hold_for_app), for its answer window.
A request already at the CLI's own prompt is an item too, to answer in the
agent's terminal: brindle never types an answer into that prompt, since the
screen can't show reliably which request it is."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from brindle import agents, autopilot, bus, identity_lock, permissions, prompt_answer, tmux, view
from brindle.config import load_repo_config
from brindle.daemon.holds import HOLDS, LEASE_SECONDS
from brindle.db import DB, Agent
from brindle.providers import get_provider
from brindle.service.dto import InboxActionDTO, InboxItemDTO, PermissionChoiceDTO, PermissionDTO
from brindle.service.errors import ServiceError

WORKERS = ("assign", "handoff")
CHOICES = ("allow_once", "allow_always", "deny")
RELEASE = "terminal"     # the person's "answer in the terminal instead": the hold ends now
# Whether a CLI draws its own permission prompt while brindle's hook holds
# the request (PermissionDTO.terminal_prompt_visible). Claude Code 2.1.304, in
# a real pane: yes, the prompt shows while its PermissionRequest hook runs, and
# answering there runs the tool at once (its PostToolUse then ends the hold).
# Native workers and agy under agy_approvals "brindle" have no prompt at all.
# Codex: not seen yet (absent: not known).
TERMINAL_PROMPT_VISIBLE: dict[str, bool] = {"claude": True, "native": False, "antigravity": False}
# Providers whose own prompt takes a request once the hold ends; for the rest
# (no prompt of their own) it is refused then.
PROMPT_AFTER_HOLD = ("claude", "codex")
BOX = re.compile(r"^[\s│┃╭╮╰╯─━]+|[\s│┃╭╮╰╯─━]+$")
# Why a request at the CLI's own prompt goes to the agent's terminal (PermissionDTO.terminal).
TERMINAL = "it is at the CLI's own prompt now: answer it in the agent's terminal"
WORDING_LINES = 12       # of a prompt, shown to the person
STEP_SECONDS = 0.2       # between looks at the hook
CONFIRM_SECONDS = 5.0    # for the hook to take the answer


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
            # A request the hook holds is pending whatever the screen shows meanwhile.
            asking = (pending_permissions(db, row)
                      if a["status"] == "waiting" or HOLDS.of_agent(row.id) else [])
            out += [InboxItemDTO(kind="permission", reason="asks permission",
                                 detail=p.request.summary() if p.request else None, permission=p.dto, **link)
                    for p in asking]
            if reason and not asking:
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
    row: dict | None                         # the hook's permission_requests row ("hook" only)
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


def _hold_note(provider: str, hold_until: float | None) -> str | None:
    """How the hold reads for the person: "waiting for you" while more than
    an hour is left, else how long, and what happens then."""
    if hold_until is None:
        return None
    left = hold_until - time.time()
    if left > 3600:
        return "waiting for you"
    then = "it goes to the agent's terminal" if provider in PROMPT_AFTER_HOLD else "it is refused"
    return f"answer within {max(1, round(left / 60))} min, or {then}"


def _prompt_wording(screen: str) -> str:
    """The last lines of the CLI's prompt, redacted, to show the person."""
    lines = [BOX.sub("", ln) for ln in prompt_answer._tail(screen)]
    return prompt_answer.redact("\n".join([ln for ln in lines if ln][-WORDING_LINES:]))


def _closed(agent: Agent) -> bool:
    return agent.status in ("done", "paused") or agent.dismissed_at is not None


def pending_permissions(db: DB, agent: Agent) -> list[Pending]:
    """The permission requests ``agent`` waits on: every one the daemon holds
    for a hook of its now, oldest first (each with its ``request_id``, the
    digest of the exact request and suggested rule it shows, and when the
    hold ends), else the one at its CLI's own prompt on screen,
    terminal-only. A row alone is no request: anything can write one, and
    one whose hold is gone (lapsed, ended, the agent closed) is shown as
    the CLI's own prompt's from then on."""
    if _closed(agent):
        HOLDS.drop_agent(agent.id)      # closed: nothing of its can be answered any more
    live = {h.request_id: h for h in HOLDS.of_agent(agent.id)}
    out = []
    for row in db.permission_requests_of(agent.id):
        row = dict(row)
        hold = live.get(row["tool_use_id"])
        if hold is None:
            if row["channel"] == "hook":
                db.update_permission_request(agent.id, row["tool_use_id"], channel="prompt")
            continue
        req = permissions.Request.from_dict(json.loads(row["request"]))
        suggestions = json.loads(row["suggestions"]) if row["suggestions"] else None
        ws = db.get_workspace(agent.workspace_id)
        # A native worker's loop reads its profile's permissions, not brindle's rules.
        rule = None if agent.provider == "native" else _always_rule(req, suggestions, ws.repo_root if ws else None)
        choices = [PermissionChoiceDTO(choice="allow_once", label="Allow once"),
                   *([PermissionChoiceDTO(choice="allow_always", label="Allow always", rule=rule)] if rule else []),
                   PermissionChoiceDTO(choice="deny", label="Deny")]
        out.append(Pending("hook", row, req, suggestions, PermissionDTO(
            via="hook", request_id=row["tool_use_id"], tool=req.tool, kind=req.kind, target=req.target(),
            digest=permissions.request_digest(row["request"], row["suggestions"]), choices=choices,
            hold_until=hold.hold_until, hold_note=_hold_note(agent.provider, hold.hold_until),
            terminal_prompt_visible=TERMINAL_PROMPT_VISIBLE.get(agent.provider))))
    if out or not agent.tmux_window or _closed(agent):
        return out
    try:
        screen = tmux.capture(agent.tmux_window, lines=60, server=agents.server_of(agent))
    except tmux.TmuxError:
        return []
    if not get_provider(agent.provider).tool_permission(screen):
        return []
    return [Pending("prompt", None, None, None,
                    PermissionDTO(via="prompt", wording=_prompt_wording(screen), terminal=TERMINAL))]


def _until(done: Callable[[], bool], seconds: float | None = None) -> bool:
    deadline = time.monotonic() + (CONFIRM_SECONDS if seconds is None else seconds)
    while not done():
        if time.monotonic() >= deadline:
            return False
        time.sleep(STEP_SECONDS)
    return True


def _write_rule(req: permissions.Request) -> None:
    """Allow-always through the hook with no rule from Claude Code: brindle's
    exact allow rule, written once the hook has consumed the answer (it was
    allowed this time), so an answer that never reaches the hook writes
    nothing, and nothing is ever taken back (another request may rely on
    the same rule)."""
    from brindle import antigravity

    permissions.add_rule(*permissions.learnable_pattern(req), "allow")
    antigravity.resync_permissions()


def answer_permission(db: DB, agent_id: str, request_id: str | None, digest: str | None, choice: str) -> dict:
    """Answer the permission request ``request_id`` brindle's hook holds for
    ``agent_id``, exactly as the app showed it (``digest``: an answer to any
    other request, or to one that differs from what the hook registered with
    the daemon, is refused), with ``choice`` (allow_once, allow_always or
    deny), the person's own from the app. The answer goes to the hook only
    through the daemon's hold (brindle.daemon.holds), never the database.

    The hold is claimed first, so a second answer, a retry, or an answer
    racing the hook's window gets a 409 and changes nothing; it succeeds
    only once the hook consumed the answer, and only then does allow-always
    write brindle's rule (the hook is allowed this time either way). ``choice`` "terminal"
    ends the hold instead, so the CLI's own prompt asks (the app's "answer
    in the terminal instead"). A request at the CLI's own prompt is refused
    (409): no key is ever typed into it. Says what rule allow-always wrote,
    and whether the hook took the answer."""
    if choice not in (*CHOICES, RELEASE):
        raise ServiceError("invalid", f"choice must be one of {', '.join((*CHOICES, RELEASE))}")
    agent = db.get_agent(agent_id)
    if agent is None:
        raise ServiceError("not_found", f"no agent {agent_id}", 404)
    identity_lock.ensure(agent)
    asking = pending_permissions(db, agent)
    if any(p.via == "prompt" for p in asking):
        raise ServiceError("conflict", f"{agent_id}'s request {TERMINAL}", 409)
    # The one the person answers, by its id: an agent can hold several at once.
    pending = next((p for p in asking if p.dto.request_id == request_id), None)
    if pending is None or pending.dto.digest != digest:
        raise ServiceError("conflict", f"{agent_id} isn't waiting on that request, as shown, any more: "
                                       "fetch the inbox again", 409)
    offered = next((c for c in pending.dto.choices if c.choice == choice), None)
    if offered is None and choice != RELEASE:
        raise ServiceError("conflict", f"{agent_id}'s request offers no {choice}", 409)
    if not HOLDS.claim(agent.id, request_id, digest, choice):
        raise ServiceError("conflict", f"{agent_id}'s request was answered, went to its own prompt, or isn't "
                                       "the one its hook holds: fetch the inbox again", 409)
    try:
        if not HOLDS.release(agent.id, request_id):
            raise ServiceError("conflict", f"{agent_id}'s request went to its own prompt before it "
                                           "was answered: nothing was written; answer it in its terminal", 409)
        # Delivered only once its hook consumed it; until the hold leaves another way
        # (expired, ended, dropped), which its lease bounds.
        _until(lambda: HOLDS.outcome(request_id) is not None, seconds=LEASE_SECONDS + CONFIRM_SECONDS)
        if HOLDS.outcome(request_id) != "consumed":
            raise ServiceError("conflict", f"{agent_id}'s request was no longer held when its answer was ready "
                                           "(it expired or went to its own prompt): nothing was written; "
                                           "answer it in its terminal", 409)
    except BaseException:
        HOLDS.unclaim(agent.id, request_id)
        raise
    if choice == "allow_always" and not pending.suggestions:
        _write_rule(pending.request)    # only now: the hook allowed it this time
    bus.publish_agent(db, "permission.answered", agent.id, request_id=request_id, choice=choice,
                      via="hook", cleared=True)
    return {"choice": choice, "via": "hook", "cleared": True,
            "rule": offered.rule if choice == "allow_always" else None}
