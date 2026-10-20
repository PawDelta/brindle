"""What needs a person across a repo's sessions: the "needs you" rules the
sidebar uses, plus plans, proposed checks, failed merges, cloud logins,
permission requests and org notices as ``InboxItemDTO`` rows.

A permission request is answered (``answer_permission``) through brindle's
hook while it holds the request (agents._await_app_answer), else by typing
the answer into the CLI's own prompt: the same keys a person at the pane
would press, only ever on the person's request from the app."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from brindle import agents, autopilot, bus, identity_lock, permissions, prompt_answer, tmux, view
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
# OpenCode's prompt: buttons on one line, Right moving the selection (from
# "Allow once" for each new request), Enter picking one. "Allow always" and a
# subagent's "Reject" then ask once more, opening on Confirm.
ROW_MENU = re.compile(r"(Allow once)\s+(Allow always)\s+(Reject)")
# Those second steps, each recognized by its title and its own controls.
ROW_FOLLOW_UP = {
    "Allow always": (re.compile(r"Always allow"), re.compile(r"Confirm\s+Cancel")),
    "Reject": (re.compile(r"Reject permission"), re.compile(r"Tell OpenCode what to do differently")),
}
WORDING_LINES = 12       # of a prompt, shown to the person
CAPTURE_LINES = 200      # of a pane, read for its prompt (the fingerprint covers them all)
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
            # A request the hook holds is pending whatever the screen shows meanwhile.
            asking = (pending_permission(db, row)
                      if a["status"] == "waiting" or db.hook_permission_request(row.id) else None)
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
class Menu:
    """A permission prompt as it shows on screen. ``layout``: "list", a
    numbered menu whose cursor shows the selection (Claude Code, Codex), or
    "row", OpenCode's buttons on one line, moved through with Right."""
    layout: str
    options: list[str]
    wording: str      # its last lines, redacted, to show the person
    above: str = ""   # everything captured above its options, as is

    @property
    def fingerprint(self) -> str:
        """Which prompt this is, whichever option is selected: what an answer
        must still find on screen before each key it sends. Taken over the
        whole capture above the options, unredacted, so a prompt that
        differs anywhere (not only in the lines shown) is another prompt."""
        return hashlib.sha256("\0".join([self.above, *self.options]).encode()).hexdigest()[:16]


@dataclass
class Pending:
    """A permission request ``agent`` waits on, and what answering it needs."""
    via: str                                 # "hook" or "prompt", as PermissionDTO.via
    row: dict                                # its permission_requests row
    request: permissions.Request | None      # what the hook recorded, when it saw it
    suggestions: list | None                 # Claude Code's own "always allow" updates
    menu: Menu | None                        # the prompt on screen ("prompt" only)
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


def read_menu(screen: str) -> Menu:
    """The permission prompt at the bottom of ``screen``: its wording and its
    options (none when its layout is one brindle can't drive, such as
    Antigravity's "Yes / No", whose keys aren't known: answered in its pane)."""
    every = [ln.rstrip() for ln in prompt_answer.plain(screen).rstrip().splitlines()]
    skip = max(0, len(every) - len(prompt_answer._tail(screen)))   # the options are at the bottom
    lines = every[skip:]
    start, options = _menu(lines)
    layout = "list"
    if not options:
        row = next((i for i in range(len(lines) - 1, -1, -1) if ROW_MENU.search(lines[i])), None)
        start, options, layout = ((row, list(ROW_MENU.search(lines[row]).groups()), "row") if row is not None
                                  else (len(lines), [], "none"))
    wording = [BOX.sub("", ln) for ln in lines[:start]]
    return Menu(layout, options, prompt_answer.redact("\n".join([ln for ln in wording if ln][-WORDING_LINES:])),
                "\n".join(every[:skip + start]))


def _choices(menu: Menu) -> list[PermissionChoiceDTO]:
    """The menu's options as choices, each labelled with the option's own
    text (which is what _press selects)."""
    choices: dict[str, PermissionChoiceDTO] = {}
    for label in menu.options:
        choice = next((c for c, pattern in OPTION_CHOICE if pattern.search(label)), None)
        if choice and choice not in choices:
            choices[choice] = PermissionChoiceDTO(
                choice=choice, label=label,
                rule=f"the CLI's own rule, as its option says: {label}" if choice == "allow_always" else None)
    return sorted(choices.values(), key=lambda c: CHOICES.index(c.choice))


def pending_permission(db: DB, agent: Agent) -> Pending | None:
    """The permission request ``agent`` waits on: one brindle's hook holds
    for the app's answer, else its CLI's prompt on screen. Each has its own
    row, whose id the app's answer must name (``request_id``): a prompt the
    hook recorded is its row once the screen names the same request (and
    keeps the prompt it was first shown as); any other prompt gets a row of
    its own the first time it is seen, gone once no prompt is up."""
    def recorded(r: dict) -> permissions.Request | None:
        return permissions.Request.from_dict(json.loads(r["request"])) if r["request"] != "null" else None

    def described(req: permissions.Request | None) -> dict:
        return {"tool": req.tool, "kind": req.kind, "target": req.target()} if req else {}

    rows = [dict(r) for r in db.open_permission_requests(agent.id)]
    hook = next((r for r in rows if r["channel"] == "hook"), None)
    if hook:
        req = recorded(hook)
        suggestions = json.loads(hook["suggestions"]) if hook["suggestions"] else None
        ws = db.get_workspace(agent.workspace_id)
        rule = _always_rule(req, suggestions, ws.repo_root if ws else None)
        choices = [PermissionChoiceDTO(choice="allow_once", label="Allow once"),
                   *([PermissionChoiceDTO(choice="allow_always", label="Allow always", rule=rule)] if rule else []),
                   PermissionChoiceDTO(choice="deny", label="Deny")]
        return Pending("hook", hook, req, suggestions, None, PermissionDTO(
            via="hook", request_id=hook["tool_use_id"], choices=choices, **described(req)))
    if not agent.tmux_window:
        return None
    try:
        screen = tmux.capture(agent.tmux_window, lines=CAPTURE_LINES, server=agents.server_of(agent))
    except tmux.TmuxError:
        return None
    if not get_provider(agent.provider).tool_permission(screen):
        db.drop_screen_requests(agent.id)
        return None
    menu = read_menu(screen)
    fp = menu.fingerprint
    # What the hook recorded is this prompt only when the screen names it, and
    # it wasn't first shown as another prompt (an older request, asked again).
    row = next((r for r in rows if r["request"] != "null" and r["fingerprint"] in (None, fp)
                and _on_screen(recorded(r), menu.wording)), None)   # the prompt's own lines, not the transcript
    if row is not None and row["fingerprint"] is None:
        db.note_permission_fingerprint(agent.id, row["tool_use_id"], fp)
        row["fingerprint"] = fp
    if row is None:
        row = next((r for r in rows if r["request"] == "null" and r["fingerprint"] == fp), None)
    if row is None:
        row = {"tool_use_id": uuid.uuid4().hex[:16], "request": "null", "fingerprint": fp, "channel": "prompt"}
        db.add_permission_request(agent.id, row["tool_use_id"], "null", "", channel="prompt", fingerprint=fp)
    req = recorded(row)
    return Pending("prompt", row, req, None, menu, PermissionDTO(
        via="prompt", request_id=row["tool_use_id"], wording=menu.wording, choices=_choices(menu),
        **described(req)))


def _on_screen(req: permissions.Request, wording: str) -> bool:
    """Whether the prompt's ``wording`` names what ``req`` asks for: the
    start of its command or URL, or its file's name (CLIs shorten paths)."""
    text = " ".join(wording.split())
    lines = req.target().strip().splitlines()
    head = " ".join(lines[0].split())[:80] if lines else ""
    return bool(head) and head in text or bool(req.path) and os.path.basename(req.path) in text


def _until(done: Callable[[], bool], seconds: float | None = None) -> bool:
    deadline = time.monotonic() + (CONFIRM_SECONDS if seconds is None else seconds)
    while not done():
        if time.monotonic() >= deadline:
            return False
        time.sleep(STEP_SECONDS)
    return True


class _Changed(ServiceError):
    """The prompt on screen is no longer the one being answered: no key goes in."""

    def __init__(self, agent_id: str) -> None:
        super().__init__("conflict", f"{agent_id}'s prompt changed before it was answered: "
                                     "fetch the inbox and answer the prompt it shows now", 409)


def _press(agent: Agent, menu: Menu, label: str) -> bool:
    """Pick the option ``label`` in the CLI's permission prompt by its own
    keys, then whether the prompt cleared. Before every key the screen must
    still show the same prompt (``menu.fingerprint``): a replaced prompt,
    even one with the same options, gets no key (_Changed). A list moves its
    cursor with Up/Down and takes Enter only once ``label`` shows selected;
    a row moves Right from its first button (where each new request opens),
    then Enter, then Enter on the Confirm a second step opens on."""
    provider, server = get_provider(agent.provider), agents.server_of(agent)

    def capture() -> str:
        return tmux.capture(agent.tmux_window, lines=CAPTURE_LINES, server=server)

    def same(screen: str) -> bool:
        return provider.tool_permission(screen) and read_menu(screen).fingerprint == menu.fingerprint

    def send(key: str) -> None:
        tmux.send_keys(agent.tmux_window, key, server=server)

    def cleared() -> bool:
        try:
            return _until(lambda: not same(capture()))
        except tmux.TmuxError:
            return False

    if menu.layout == "row":
        for key in ["Right"] * menu.options.index(label) + ["Enter"]:
            if not same(capture()):
                raise _Changed(agent.id)
            send(key)
        if label in ROW_FOLLOW_UP:
            title, controls = ROW_FOLLOW_UP[label]

            def follow_up() -> bool:
                """The second step this option opens, and no permission prompt
                (this one, or a new one) in its place."""
                tail = "\n".join(prompt_answer._tail(capture(), 12))
                return bool(title.search(tail) and controls.search(tail)) and not (
                    ROW_MENU.search(tail) or provider.tool_permission(tail))

            if _until(follow_up, seconds=1.0):
                send("Enter")   # its Confirm (opened on) for "Always allow"; an empty reason for a reject
        return cleared()
    want = prompt_answer.Decision("permission", label)
    seen = None
    for _ in range(prompt_answer.MAX_MOVES * 2):
        screen = capture()
        if not same(screen):
            raise _Changed(agent.id)
        if prompt_answer._tail(screen) == seen:
            time.sleep(STEP_SECONDS)   # the last key hasn't shown yet: don't press another
            continue
        key = prompt_answer.next_key(screen, want)
        if key is None:
            raise _Changed(agent.id)
        send(key)
        if key == "Enter":
            return cleared()
        seen = prompt_answer._tail(screen)
    raise ServiceError("conflict", f"couldn't reach {label!r} in {agent.id}'s prompt: answer it in its terminal", 409)


def _write_rule(db: DB, agent: Agent, request_id: str, req: permissions.Request) -> None:
    """Allow-always through the hook with no rule from Claude Code: brindle's
    exact allow rule, written only while this answer holds the request
    (claimed, not yet signed for the hook to take), and removed again if
    the request is lost meanwhile, unless it was there before."""
    from brindle import antigravity

    kind, match = permissions.learnable_pattern(req)
    existed = any(r.id == permissions.rule_id(kind, "exact", match, "allow") for r in permissions.load_store().rules)
    rule = permissions.add_rule(kind, match, "allow")
    mac = permissions.answer_mac(agent.id, request_id, "allow_always")
    if mac and db.sign_permission_answer(agent.id, request_id, mac):
        antigravity.resync_permissions()
        return
    if not existed:
        permissions.forget(rule.id)
    raise ServiceError("conflict", f"{agent.id}'s request went to its own prompt before it was answered: "
                                   "nothing was written; fetch the inbox and answer it again", 409)


def answer_permission(db: DB, agent_id: str, request_id: str, choice: str) -> dict:
    """Answer ``agent_id``'s permission request ``request_id`` (the one the
    app showed: an answer for any other is refused) with ``choice``
    (allow_once, allow_always or deny), the person's own from the app.

    Through brindle's hook when it holds the request, else by the CLI
    prompt's own keys (_press). Either way the request is claimed first, so
    a second answer, a retry, or an answer racing the hook's timeout gets a
    409 and changes nothing; allow-always writes brindle's rule only while
    holding the claim. Says how, what rule allow-always wrote, and whether
    the request then cleared."""
    if choice not in CHOICES:
        raise ServiceError("invalid", f"choice must be one of {', '.join(CHOICES)}")
    agent = db.get_agent(agent_id)
    if agent is None:
        raise ServiceError("not_found", f"no agent {agent_id}", 404)
    identity_lock.ensure(agent)
    pending = pending_permission(db, agent)
    if pending is None or pending.dto.request_id != request_id:
        raise ServiceError("conflict", f"{agent_id} isn't waiting on request {request_id} any more: "
                                       "fetch the inbox again", 409)
    offered = next((c for c in pending.dto.choices if c.choice == choice), None)
    if offered is None:
        raise ServiceError("conflict", f"{agent_id}'s request offers no {choice} brindle can give: "
                                       "answer it in its terminal", 409)
    if pending.via == "hook":
        if not db.claim_permission_answer(agent.id, request_id, choice):
            raise ServiceError("conflict", f"{agent_id}'s request was answered or went to its own prompt: "
                                           "fetch the inbox again", 409)
        try:
            if choice == "allow_always" and not pending.suggestions:
                _write_rule(db, agent, request_id, pending.request)
            else:
                mac = permissions.answer_mac(agent.id, request_id, choice)
                if not (mac and db.sign_permission_answer(agent.id, request_id, mac)):
                    raise ServiceError("conflict", f"{agent_id}'s request went to its own prompt before it "
                                                   "was answered: fetch the inbox again", 409)
        except BaseException:
            db.unclaim_permission_answer(agent.id, request_id)
            raise
        cleared = _until(lambda: (db.conn.execute(
            "SELECT channel FROM permission_requests WHERE agent_id=? AND tool_use_id=?",
            (agent.id, request_id)).fetchone() or {"channel": "answered"})["channel"] == "answered")
    else:
        if not db.claim_permission_prompt(agent.id, request_id, pending.menu.fingerprint):
            raise ServiceError("conflict", f"{agent_id}'s prompt is already being answered or was answered: "
                                           "fetch the inbox again", 409)
        try:
            cleared = _press(agent, pending.menu, offered.label)
        except BaseException:
            db.finish_permission_prompt(agent.id, request_id, None)
            raise
        db.finish_permission_prompt(agent.id, request_id, choice)
    bus.publish_agent(db, "permission.answered", agent.id, request_id=request_id, choice=choice,
                      via=pending.via, cleared=cleared)
    return {"choice": choice, "via": pending.via, "cleared": cleared,
            "rule": offered.rule if choice == "allow_always" else None}
