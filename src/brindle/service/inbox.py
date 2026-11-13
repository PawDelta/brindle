"""What needs a person across a repo's sessions: the "needs you" rules the
sidebar uses, plus plans, proposed checks, failed merges, PRs brindle
prepared for approval, cloud logins, permission requests and org notices as
``InboxItemDTO`` rows.

A permission request is answered from the app (``answer_permission``) only
while brindle's hook holds it (agents._hold_for_app), for its answer window.
A request already at the CLI's own prompt is an item too, to answer in the
agent's terminal: brindle never types an answer into that prompt, since the
screen can't show reliably which request it is."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from brindle import agents, autopilot, bus, git, identity_lock, permissions, prompt_answer, session_sync, tmux, view
from brindle.config import load_repo_config
from brindle.daemon.holds import HOLDS, LEASE_SECONDS
from brindle.db import DB, Agent, Workspace, request_subagent
from brindle.providers import get_provider
from brindle.service.dto import (
    InboxActionDTO,
    InboxItemDTO,
    PermissionChoiceDTO,
    PermissionDTO,
    PlanOfferDTO,
    PlanOfferMilestoneDTO,
)
from brindle.service.errors import ServiceError

WORKERS = ("assign", "handoff")
CHOICES = ("allow_once", "allow_always", "deny")
RELEASE = "terminal"     # the person's "answer in the terminal instead": the hold ends now
# Whether a CLI draws its own permission prompt while brindle's hook holds
# the request (PermissionDTO.terminal_prompt_visible). Claude Code 2.1.304, in
# a real pane: yes, the prompt shows while its PermissionRequest hook runs, and
# answering there runs the tool at once (its PostToolUse then ends the hold).
# Not for a subagent's request (2.1.318): its prompt comes only once the hold ends.
# Native workers and agy under agy_approvals "brindle" have no prompt at all.
# Codex 0.162.1: no. It awaits its PermissionRequest hooks before it asks
# (codex-rs core/src/tools/approvals.rs, "Approval precedence is: 1. Hooks"),
# so its pane reads "Running hook" while held and its prompt comes only once
# the hold ends (app/docs/permission-e2e.md); an interrupted turn kills the hook.
TERMINAL_PROMPT_VISIBLE: dict[str, bool] = {"claude": True, "codex": False, "native": False, "antigravity": False}
# Providers whose own prompt takes a request once the hold ends; for the rest
# (no prompt of their own) it is refused then.
PROMPT_AFTER_HOLD = ("claude", "codex")
BOX = re.compile(r"^[\s│┃╭╮╰╯─━]+|[\s│┃╭╮╰╯─━]+$")
# Why a request at the CLI's own prompt goes to the agent's terminal (PermissionDTO.terminal).
TERMINAL = "it is at the CLI's own prompt now: answer it in the agent's terminal"
WORDING_LINES = 12       # of a prompt, shown to the person
STEP_SECONDS = 0.2       # between looks at the hook
CONFIRM_SECONDS = 5.0    # for the hook to take the answer
# Why a failed agent waits on the person (Agent.failed_why). Only an auth
# failure asks for a sign-in; a cause brindle didn't keep (an agent failed
# before it did) isn't taken for one.
FAILED = {"auth": "credentials need attention",
          "model": "failed: its model isn't available on this route",
          "delivery": "failed: its brief wasn't delivered; check its pane and restart it"}
FAILED_UNKNOWN = "failed: check its pane and restart it"


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
        if (agent.get("failed_why") or "").startswith("review: "):
            return agent["failed_why"].removeprefix("review: ")
        return FAILED.get(agent.get("failed_why"), FAILED_UNKNOWN)
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
        return "credentials" if agent.get("failed_why") == "auth" else "failed"
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


ORG_BLOCK_WINDOW = 24 * 3600


def _org_blocks(db: DB, repo_root: str | None, now: float | None = None) -> list[InboxItemDTO]:
    """One calm notice per org Block that stopped an agent in the last day
    (permission_asks), however many requests it stopped: "Blocked by <org>
    policy: <reason>", the first line of the agent's denial."""
    from brindle.permission_grants import ORG_BLOCKED

    since = (time.time() if now is None else now) - ORG_BLOCK_WINDOW
    rows = db.conn.execute(
        "SELECT reason, COUNT(*) AS n, MAX(ts) AS last FROM permission_asks WHERE outcome = 'deny' AND ts >= ?"
        " AND reason LIKE ? AND (? IS NULL OR repo_root = ?) GROUP BY reason ORDER BY last DESC",
        (since, ORG_BLOCKED + "%", repo_root, repo_root)).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        head = r["reason"].split("\n", 1)[0]
        counts[head] = counts.get(head, 0) + r["n"]
    return [InboxItemDTO(kind="org_blocked", reason=head,
                         detail=f"{n} request{'s' if n != 1 else ''} stopped today. Agents were told to work around it.")
            for head, n in counts.items()]


def _setup_items() -> list[InboxItemDTO]:
    """Setup problems the app can repair itself (a ``fix`` names the repair).
    The hook's trust only when a person turned the hook off: the rest of what
    happens to that trust is brindle's to put right (codex_hook.keep_trusted)."""
    from brindle import codex_hook

    try:
        state = codex_hook.status()
    except Exception:  # noqa: BLE001 - an extra: never break the inbox
        return []
    if state != codex_hook.REMOVED:
        return []
    return [InboxItemDTO(kind="setup", fix="codex_hook_trust",
                         reason="brindle's permission hook is turned off in Codex",
                         action=InboxActionDTO(method="POST", path="/v1/setup/codex-hook"))]


def _sign_in(provider: str) -> InboxActionDTO | None:
    """The provider CLI's own sign-in the app can start (routes_actions.provider_login)."""
    from brindle.pro.team_policy import CLI_NAMES

    return InboxActionDTO(method="POST", path=f"/v1/providers/{provider}/login") if provider in CLI_NAMES else None


def _opened_pr(db: DB, root_id: str, branch: str, path: str) -> tuple[str, dict | None]:
    """HEAD and the open PR holding it (``{number, title, url}``): GitHub's last
    answer, else the one brindle recorded (Open PR, or adopted earlier), which
    stands while that answer is stale or missing."""
    from brindle.daemon import session_review

    try:
        head = git.head(path)       # from the git files: every Inbox read asks, for every session
        opened = session_review.open_pr_for_head(head, path, root_id)
    except (git.GitError, OSError):
        head, opened = "", None  # An unavailable checkout must not break the rest of the inbox.
    if not (opened and opened.get("url")) and not session_review.head_pr_known_none(head, path):
        mine = db.conn.execute("SELECT url FROM session_prs WHERE root_id = ? AND branch = ? AND url IS NOT NULL"
                               " AND merged_at IS NULL", (root_id, branch)).fetchone()
        number = re.search(r"/pull/(\d+)$", mine["url"]) if mine else None
        prep = db.conn.execute("SELECT title FROM session_pr_prep WHERE root_id = ? AND branch = ?",
                               (root_id, branch)).fetchone()
        opened = {"url": mine["url"], "number": number.group(1), "title": (prep["title"] if prep else "") or ""} if number else None
    return head, opened


def _failure_marker(*parts: str | None) -> str:
    return json.dumps(["failed", *parts])


def _pr_problem(db: DB, root_id: str, branch: str, opened: dict | None) -> tuple[str, str] | None:
    from brindle.daemon import session_review

    recorded = db.conn.execute("SELECT url FROM session_prs WHERE root_id = ? AND branch = ?"
                               " AND url IS NOT NULL AND merged_at IS NULL", (root_id, branch)).fetchone()
    # A local fix can move HEAD beyond the PR: its failure still needs attention.
    urls = dict.fromkeys([r["url"] for r in (recorded, opened) if r])
    for url in urls:
        if problem := session_review.pr_problem(db, root_id, url):
            return url, problem
    return None


def dismiss_marker(db: DB, root_id: str, branch: str, path: str) -> str | None:
    """Identify the PR card's commit and, for a failure, the current problem.

    Ready/opened markers keep their existing format so saved dismissals stand.
    """
    head, opened = _opened_pr(db, root_id, branch, path)
    if problem := _pr_problem(db, root_id, branch, opened):
        return _failure_marker(head, *problem)
    if opened:
        return f"{head}|{opened['url']}"
    prep = db.conn.execute("SELECT sha, state, check_cmd, output FROM session_pr_prep WHERE root_id = ? AND branch = ?"
                           " AND state IN ('passed', 'failed')", (root_id, branch)).fetchone()
    if prep:
        return prep["sha"] if prep["state"] == "passed" else _failure_marker(prep["sha"], prep["check_cmd"], prep["output"])
    return None


def _pr_items(db: DB, root_id: str, branch: str, home: Workspace, link: dict) -> list[InboxItemDTO]:
    """The session's PR as brindle prepared it (routes_actions.prepare_pr),
    until the person opens it: ready for their approval, or its checks failed.
    An open PR already holding the session's HEAD (a supervisor's own
    ``gh pr create``, from any branch name) is a ``pr_opened`` item instead.
    Nothing while its PR has merged, until the branch has commits for its next."""
    from brindle.daemon import session_review

    path = home.path
    head, opened = _opened_pr(db, root_id, branch, path)
    declined = db.conn.execute("SELECT sha FROM session_pr_declined WHERE root_id = ? AND branch = ?",
                               (root_id, branch)).fetchone()
    if problem := _pr_problem(db, root_id, branch, opened):
        if declined and declined["sha"] == _failure_marker(head, *problem):
            return []
        url, detail = problem
        return [InboxItemDTO(kind="pr_failed", reason="PR needs attention", detail=detail,
                             url=url, **link)]
    if opened:
        # Dismissed for this PR at any commit: a push to it doesn't bring the card back.
        if declined and declined["sha"].endswith(f"|{opened['url']}"):
            return []
        return [InboxItemDTO(kind="pr_opened", reason="PR opened", detail=f"#{opened.get('number')} {opened.get('title') or ''}".strip(),
                             url=opened["url"], **link)]
    rows = db.conn.execute(
        "SELECT sha, state, title, check_cmd, output FROM session_pr_prep WHERE root_id = ? AND branch = ?"
        " AND state IN ('passed', 'failed')", (root_id, branch)).fetchall()
    merged = db.conn.execute("SELECT pushed_sha FROM session_prs WHERE root_id = ? AND branch = ? AND url IS NOT NULL"
                             " AND merged_at IS NOT NULL", (root_id, branch)).fetchone()
    if rows and merged and not session_review.new_since_merge(db, home,merged["pushed_sha"]):
        return []
    if declined:
        rows = [r for r in rows if r["state"] != "failed"
                or declined["sha"] != _failure_marker(r["sha"], r["check_cmd"], r["output"])]
    if declined and any(r["state"] == "passed" for r in rows):
        try:
            if git.head(path) == declined["sha"]:
                rows = [r for r in rows if r["state"] != "passed"]
        except (git.GitError, OSError):
            pass  # An unavailable checkout must not break the rest of the inbox.
    return [InboxItemDTO(kind="pr_ready", reason="PR ready for your approval", detail=r["title"], **link)
            if r["state"] == "passed" else
            InboxItemDTO(kind="pr_failed", reason=f"PR checks failed: {r['check_cmd']}", detail=r["output"], **link)
            for r in rows]


def items(db: DB, repo_root: str | None, panes: dict[str, bool] | None = None) -> list[InboxItemDTO]:
    """Everything waiting on a person in ``repo_root``'s sessions (every
    session when None), then the org's unread notices. Each agent's item
    names its session (``root_id``), so the app can link into it."""
    out: list[InboxItemDTO] = []
    shown: set[str] = set()
    for ws in view.snapshot(db, repo_root, panes=panes):
        shown.update(a["id"] for a in ws["agents"])
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
            drop_orphan_requests(db, row)   # whatever the agent's status: a cancelled call's row
            asking = [p for p in (pending_permissions(db, row)
                                  if a["status"] == "waiting" or HOLDS.of_agent(row.id) else [])
                      if _answerable(row, p)]
            out += [InboxItemDTO(kind="permission", reason="asks permission",
                                 detail=p.request.summary() if p.request else None, permission=p.dto, **link)
                    for p in asking]
            if reason and not asking:
                kind = _kind(a, ws, reason)
                out.append(InboxItemDTO(kind=kind, reason=reason, **link,
                                        detail=row.failed_why.removeprefix("review: ")
                                        if (row.failed_why or "").startswith("review: ") else None,
                                        action=_sign_in(row.provider) if kind == "credentials" else None))
            # Only a plan agents.approve_plan would take: a plan-first worker with a supervisor.
            if row.plan_state == "proposed" and row.plan_first and row.parent_id is not None:
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
            home = db.get_workspace(row.workspace_id)
            if home:
                out += _pr_items(db, row.id, ws["branch"], home, link)
            if home and (stuck := session_sync.needs_you(db, home)):
                out.append(InboxItemDTO(kind="sync_conflict", reason="session branch conflicts with origin",
                                        detail=stuck, **link))
            login = db.get_login_state(row.id)
            if login and login.state == "auth_paused":
                out.append(InboxItemDTO(kind="login", reason="cloud login expired",
                                        **{**link, "agent_id": login.agent_id or a["id"]},
                                        detail=login.refresh_cmd))
    all_items = (out + _idle_items(db, repo_root) + _doc_items(db, repo_root, shown) + _review_items(db, repo_root)
                 + _visual_items(db, repo_root)
                 + _plan_offer_items(db, repo_root) + _setup_items()
                 + _org_blocks(db, repo_root) + _org_notices())
    # Requests can outlive the live snapshot (notably document requests).
    # Paused sessions keep theirs, but dismissed/replaced workers do not ask
    # for attention again just because a historical request is still stored.
    return [i for i in all_items if not i.agent_id
            or (a := db.get_agent(i.agent_id)) is None or not view.past_worker(a)]


def _idle_items(db: DB, repo_root: str | None) -> list[InboxItemDTO]:
    """Supervisors the sweep stopped after days without input (brindle.reap),
    until they are continued or closed."""
    from brindle import reap

    out = []
    for a, detail in reap.idle_stopped(db):
        ws = db.get_workspace(a.workspace_id)
        if ws is None or (repo_root and ws.repo_root != repo_root) or identity_lock.locked_by(a.id):
            continue
        out.append(InboxItemDTO(kind="idle_stopped", reason="stopped while idle; `brindle continue` resumes it",
                                detail=detail, agent_id=a.id, workspace_id=ws.id, root_id=a.id))
    return out


def _visual_items(db: DB, repo_root: str | None) -> list[InboxItemDTO]:
    """Unread visual revisions and open questions (brindle.visuals)."""
    from brindle import visuals

    out = []
    # An Approve held for the Library that couldn't be applied: the person was told nowhere else.
    refused = {r["plan_id"]: r["approve_refused"] for r in db.conn.execute(
        "SELECT s.plan_id, s.approve_refused FROM plan_shares s JOIN plans p ON p.id=s.plan_id"
        " WHERE s.approve_refused IS NOT NULL AND p.state='open'")} if visuals.enabled() else {}
    pending = visuals.inbox_pending(db, repo_root)
    listed = {v["id"] for v in pending}
    pending += [v for v in (visuals.get(db, pid) for pid in refused.keys() - listed)
                if not repo_root or v["repo_root"] == repo_root]
    for v in pending:
        root = db.get_agent(v["root_id"])
        if root is None or identity_lock.locked_by(root.id):
            continue
        reason = refused.get(v["id"]) or ("asks you to choose" if v["options"] else "visual is ready")
        out.append(InboxItemDTO(kind="visual", reason=reason, detail=v["title"],
                                visual_id=v["id"], agent_id=root.id, workspace_id=root.workspace_id,
                                root_id=root.id))
    return out


def _plan_offer_items(db: DB, repo_root: str | None) -> list[InboxItemDTO]:
    """brindle's one-time offer to plan a big goal first, answered Yes / Not now from here
    (POST /goal/plan-offer names the offer id, carried in request_id). Listed
    from the stored offer, not the live snapshot: a paused session keeps its offer."""
    from brindle import plans
    from brindle.pro import analysis_brief

    out = []
    for r in db.conn.execute("SELECT root_id FROM goal_doc_requests WHERE offer LIKE ? ORDER BY rowid",
                             (plans.OFFER_OPEN + "%",)):
        root = db.get_agent(r["root_id"])
        ap = db.get_autopilot(r["root_id"])
        ws = db.get_workspace(root.workspace_id) if root else None
        offer_id = plans.open_offer_id(ap.plan_offer) if ap else None
        if (offer_id is None or ws is None or (repo_root and ws.repo_root != repo_root)
                or identity_lock.locked_by(root.id)):
            continue
        milestones, repos, workers, text = autopilot.plan_offer_scope(db, root.id, ap)
        context = PlanOfferDTO(
            session_name=root.name or ap.goal or ws.name, goal=ap.goal or "", detail=ap.detail,
            milestones=[PlanOfferMilestoneDTO(title=m.title, verified=m.status == "passed")
                        for m in milestones], workers=workers, repos=repos,
            reasons=analysis_brief.offer_reasons(len(milestones), repos, text, workers))
        out.append(InboxItemDTO(kind="plan_offer", reason="This looks big: plan it first?",
                                detail=ap.goal, request_id=offer_id, agent_id=root.id,
                                workspace_id=ws.id, root_id=root.id, plan_offer=context))
    return out


def _doc_items(db: DB, repo_root: str | None, shown: set[str]) -> list[InboxItemDTO]:
    """Unread requests survive a session leaving the sidebar, until dismissed."""
    out = []
    for ws in view.session_workspaces(db, repo_root):
        # A replacement was already resolved by the snapshot. Those still
        # shown (including deliberately concurrent workers) must stay current.
        everyone = db.list_agents(ws.id)
        current = {a.id for a in view.current_agents(everyone, shown)} if ws.kind == "worktree" else None
        for r in db.conn.execute(
                "SELECT r.* FROM doc_requests r JOIN agents a ON a.id = r.agent_id"
                " WHERE a.workspace_id = ? ORDER BY r.created_at, r.rowid", (ws.id,)):
            if (current is not None and r["agent_id"] not in current) or identity_lock.locked_by(r["agent_id"]):
                continue
            out.append(InboxItemDTO(kind="doc", reason=r["why"], detail=r["title"],
                                    request_id=r["id"], doc=r["doc_id"], section=r["section"],
                                    agent_id=r["agent_id"], workspace_id=ws.id,
                                    root_id=autopilot.root_of(db, r["agent_id"])))
    return out


def _my_sub() -> str | None:
    """The signed-in account, or None offline, logged out or air-gapped: no review cards, never an error."""
    from brindle.pro import license

    try:
        return license.current(refresh=False).sub
    except Exception:  # noqa: BLE001 - the inbox must still load without Pro
        return None


def _review_items(db: DB, repo_root: str | None) -> list[InboxItemDTO]:
    """Cards for shared design docs, derived from the stored review state so
    they clear on their own once acted on: "review_requested" while the
    Library's queue asks this account (pro/review_queue);
    "changes_requested" / "signed_off" for the author, until the plan is
    revised or approved. Each opens the doc (``doc`` = the Library's id)."""
    from brindle import plans

    me = _my_sub()
    if me is None:
        return []
    out = []
    links: dict[str, dict] = {}     # a doc this machine has a live plan for: what enriches its queue card
    for r in db.conn.execute("SELECT plan_id, doc_id, owner FROM plan_shares WHERE doc_id IS NOT NULL"):
        try:
            p = plans._row(db, r["plan_id"])
        except Exception:  # noqa: BLE001 - a plan that went away or a bad row is skipped, not shown raw
            continue
        root = db.get_agent(p["root_id"])
        ws = db.get_workspace(root.workspace_id) if root else None
        if root and identity_lock.locked_by(root.id):
            continue        # another identity's session: no local link or feedback (the queue is the person's own)
        if p["state"] not in plans.HOLDING or (repo_root and ws and ws.repo_root != repo_root):
            continue
        link = {"doc": r["doc_id"], "request_id": p["id"], "agent_id": root.id if root else None,
                "workspace_id": ws.id if ws else None, "root_id": root.id if root else None}
        links[r["doc_id"]] = {**link, "title": plans._title(p["markdown"])}
        if r["owner"] == me:
            found = plans.gate(db, p)
            state = found and found["state"]
            if state == plans.CHANGES:
                out.append(InboxItemDTO(kind="changes_requested", reason="Changes requested",
                                        detail=links[r["doc_id"]]["title"], **link))
            elif state == "ready":
                out.append(InboxItemDTO(kind="signed_off", reason="Signed off",
                                        detail=links[r["doc_id"]]["title"], **link))
    # Who is asked to review is the Library's per-person queue, for every doc
    # (a plan here only adds the session link): it clears when the item leaves.
    from brindle.pro import review_queue

    for q in review_queue.waiting(me):
        link = links.get(q["doc_id"]) or {"doc": q["doc_id"]}
        out.append(InboxItemDTO(kind="review_requested", reason="Review requested",
                                detail=link.get("title") or q["title"], **{k: v for k, v in link.items() if k != "title"}))
    return out


def request_doc_read(db: DB, agent: Agent, doc: int | str, why: str, section: str | None = None) -> str:
    """Keep a session's explicit reading request until the person dismisses it."""
    from brindle import knowledge

    identity_lock.ensure(agent)
    why = why.strip()
    if not why or len(why.splitlines()) != 1:
        raise ServiceError("invalid", "why must be one nonempty line")
    ws = db.get_workspace(agent.workspace_id)
    if isinstance(doc, str):
        from urllib.parse import quote

        from brindle.pro import analysis_docs, auth

        load_repo_config(ws.repo_root)   # apply the repo's air-gap setting before any request
        try:
            title = analysis_docs.api("GET", f"/analysis-docs/{quote(doc, safe='')}").get("title")
        except (auth.AuthError, RuntimeError) as e:
            raise ServiceError("invalid", str(e)) from e
    else:
        found = db.conn.execute(
            "SELECT d.title FROM kb_docs d JOIN kb_sources s ON s.id = d.source_id"
            " WHERE d.id = ? AND NOT d.tombstoned AND s.repo_key = ?",
            (doc, knowledge.repo_key(ws.repo_root))).fetchone()
        if found is None:
            raise ServiceError("not_found", f"no document {doc} in this session's repo", 404)
        title = found["title"]
    request = uuid.uuid4().hex
    with db.tx() as c:
        c.execute("INSERT INTO doc_requests (id, agent_id, doc_id, title, section, why, created_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?)", (request, agent.id, doc, title, section, why, time.time()))
    bus.publish_agent(db, "doc.requested", agent.id, request_id=request)
    return request


def dismiss_doc_read(db: DB, request: str) -> dict:
    row = db.conn.execute("SELECT agent_id FROM doc_requests WHERE id = ?", (request,)).fetchone()
    if row is None:
        raise ServiceError("conflict", "document request is no longer waiting", 409)
    identity_lock.ensure(db.get_agent(row["agent_id"]))
    with db.tx() as c:
        c.execute("DELETE FROM doc_requests WHERE id = ?", (request,))
    bus.publish_agent(db, "doc.dismissed", row["agent_id"], request_id=request)
    return {"dismissed": request}


# -- permission requests ---------------------------------------------------------------------


@dataclass
class Pending:
    """A permission request ``agent`` waits on, and what answering it needs."""
    via: str                                 # "hook" or "prompt", as PermissionDTO.via
    row: dict | None                         # the hook's permission_requests row ("hook" only)
    request: permissions.Request | None
    suggestions: list | None                 # Claude Code's own "always allow" updates
    dto: PermissionDTO


def _answerable(agent: Agent, p: Pending) -> bool:
    """Whether answer_permission would take ``p`` (HOLDS.claim's conditions): a
    hook-held request whose answer is already claimed, or whose digest differs
    from the one its hook registered, is refused, so it isn't listed. A
    request at the CLI's own prompt stays, as a terminal-only card."""
    if p.via != "hook":
        return True
    hold = HOLDS.held(agent.id, p.dto.request_id)
    return hold is not None and hold.answer is None and hold.digest == p.dto.digest


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
    return agent.status in ("done", "paused") or view.past_worker(agent)


def drop_orphan_requests(db: DB, agent: Agent) -> None:
    """Remove a non-Claude agent's hook-held request rows whose hold is gone
    (the CLI cancelled the call and its hook died): nothing pairs with them.
    Claude's are kept (agents.note_permission_outcome)."""
    if agent.provider == "claude":
        return
    # Rows before holds: a hook registers its hold before it records its row, so
    # every row read here already has its hold in the snapshot taken after.
    rows = db.permission_requests_of(agent.id)
    live = {h.request_id for h in HOLDS.of_agent(agent.id)}
    for row in rows:
        if row["channel"] in ("hook", "supervisor") and row["tool_use_id"] not in live:
            db.pop_permission_request(agent.id, lambda r, t=row["tool_use_id"]: r["tool_use_id"] == t)


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
    out, supervised = [], False
    for row in db.permission_requests_of(agent.id):
        row = dict(row)
        hold = live.get(row["tool_use_id"])
        if hold is None:
            if row["channel"] in ("hook", "supervisor"):
                if agent.provider == "claude":
                    # Kept: its PostToolUse pairs with it (agents.note_permission_outcome).
                    db.update_permission_request(agent.id, row["tool_use_id"], channel="prompt")
                else:
                    drop_orphan_requests(db, agent)
            continue
        if row["channel"] == "supervisor" and hold.supervised(HOLDS.clock()):
            supervised = True   # its supervisor's to answer first (brindle.permission_routing)
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
            terminal_prompt_visible=TERMINAL_PROMPT_VISIBLE.get(agent.provider) and not request_subagent(row))))
    # A request with its supervisor shows at the CLI's own prompt too: not the person's yet.
    if out or supervised or not agent.tmux_window or _closed(agent):
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
