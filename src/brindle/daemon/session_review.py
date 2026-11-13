"""A supervisor session's 'Review & open PR': whether brindle judges the
session ready for a PR, its branch's changes against the PR base with each
hunk attributed to whoever wrote it, and the PR's draft text.

Reviews are per session, never per worker. Readiness reads autopilot's own
records (milestones, the completion audit, an open question), the session's
workers and queued tasks, and the session branch. ``ReadinessWatch`` puts
``session.pr_ready`` on the bus when a session becomes ready.

Attribution walks the session branch's first-parent history since the PR
base: the commits a merge brought in belong to the worker whose branch it
merged ("Merge branch 'x'", or brindle's "Squash merge x"); a commit made
from the review sheet ("review edits: ...") is the person's ("you"), any
other direct commit the supervisor's. A hunk's added lines are blamed; its
deleted ones go to the first-parent step (a merge, for a worker's deletion)
that removed them; a change with
neither (a mode change, a binary file) takes its file's authors.

The diff is polled, so its git work is kept: a commit's diff and a merge's
commits never change, and a session's last result is reused while its
fingerprint (HEAD, the base refs, the index's mtime, read from git's files
without starting git) holds. When new commits land only their diffs run, and
only the files they touched are blamed again. One computation runs per
session at a time; who asks meanwhile shares it. Who wrote what is read from
the db on every ask, so a worker's id is never stale."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from brindle import agents, autopilot, bus, gates, git, knowledge, pipeline, session_sync, spawn, status_cache, tasks, workspaces
from brindle.config import load_repo_config
from brindle.db import DB, Agent, Workspace
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)

EDIT_MESSAGE = "review edits"       # a review edit's commit: "review edits: <path>"
MERGED_RE =re.compile(r"^Merge branch '([^']+)'")
SQUASHED_RE = re.compile(r"^Squash merge (\S+)$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BLAME_RE = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")
SUPERVISOR, YOU = "supervisor", "you"
AUDIT_REASONS = {
    "start": "the completion audit hasn't approved the session's current commit",
    "gaps": "the completion audit found gaps in the work",
    "dirty": "the completion audit can't judge uncommitted changes",
}


class NotReady(ServiceError):
    """A 409 that says what is missing, in plain words."""

    def __init__(self, reasons: list[str]):
        super().__init__("not_ready", "the session isn't ready for a PR: " + "; ".join(reasons), 409)
        self.reasons = reasons

    def to_json(self) -> dict[str, Any]:
        return {**super().to_json(), "reasons": self.reasons}


def session(db: DB, root_id: str) -> tuple[Agent, Workspace]:
    """The supervisor ``root_id`` and its checkout, on its current branch."""
    root = db.get_agent(root_id)
    if root is None:
        raise ServiceError("not_found", f"no session {root_id}", 404)
    if root.parent_id:
        raise ServiceError("invalid", f"{root_id} is a worker; a review covers a whole session")
    ws = db.get_workspace(root.workspace_id)
    if ws is None:
        raise ServiceError("not_found", f"session {root_id} has no checkout", 404)
    return root, workspaces.refresh_branch(db, ws)


def _only_review_edits_since(db: DB, ws: Workspace, sha: str) -> bool:
    """Every commit after ``sha`` (an ancestor of HEAD) is one the daemon
    recorded as a review edit (never judged by a commit's own message)."""
    if not git.is_ancestor(ws.path, sha, "HEAD"):
        return False
    recorded = {r["sha"] for r in db.conn.execute(
        "SELECT sha FROM review_edits WHERE workspace_id = ?", (ws.id,))}
    head = git.resolve(ws.path, "HEAD")      # by commit: the same two commits are listed once
    listed = (git.out_once(["rev-list", f"{sha}..{head[0]}"], ws.path) if head
              else git.out(["rev-list", f"{sha}..HEAD"], ws.path))
    return set(listed.split()) <= recorded


def reasons(db: DB, root: Agent, ws: Workspace) -> list[str]:
    """What keeps the session from being ready for a PR; none when it is.
    Its checkout's uncommitted files come from ``status_cache.tracked_dirty``
    (git only when the checkout moved): a sweep judges a session again
    whenever one of its workers' rows changes."""
    out: list[str] = []
    ap = db.get_autopilot(root.id)
    ms = db.milestones(root.id)
    if ap is None:
        out.append("the session has no goal to judge it by")
    elif ms:
        out += [f"milestone {m.position} ({m.title}) hasn't passed its check"
                for m in ms if m.status != "passed"]
        if not out:
            head = None if status_cache.tracked_dirty(ws.path) else git.head(ws.path)
            if head and ap.audit_sha and _only_review_edits_since(db, ws, ap.audit_sha):
                head = ap.audit_sha     # the person's own edits don't un-audit the work
            if due := autopilot.audit_due(ap, load_repo_config(ws.repo_root), head):
                out.append(AUDIT_REASONS[due])
    elif ap.state != "done":
        # Reaching the goal clears it; "done" is what is left of it.
        out.append("the session has no goal reached yet")
    if live := autopilot.active_workers(db, root.id):
        out.append(f"{len(live)} worker(s) still running: {', '.join(a.id for a in live)}")
    queued = [t for t in tasks.queued(db)       # held by the coordinator too: still work to come
              if t.caller_id and autopilot.root_of(db, t.caller_id) == root.id]
    if queued:
        out.append(f"{len(queued)} task(s) queued")
    out += needs_you(db, root, ap)
    if not ws.base_branch or ws.branch in ("", "HEAD"):
        out.append("the session isn't on a branch with a base to open a PR against")
    elif status_cache.tracked_dirty(ws.path):
        out.append("the session's checkout has uncommitted changes")
    elif not git.ahead_behind(ws.path, git.base_ref(ws.path, ws.base_branch))[1]:
        out.append(f"the session's branch has no commits ahead of {ws.base_branch}")
    elif (pr := current_pr(db, root.id, ws)) is not None and pr["merged_at"]:
        out.append(f"the session's PR merged and {ws.branch} has no new commits since")
    elif behind := session_sync.pr_block(ws):
        out.append(behind)
    return out


def phase_reasons(db: DB, root: Agent, ws: Workspace, phase: str, sha: str) -> list[str]:
    """What keeps brindle from landing goal phase ``phase`` at ``sha``, the
    commit its milestone checks passed on (autopilot.phase_landing); none when
    it can. Unlike ``reasons``, later phases' milestones, running workers and
    queued tasks don't count: the PR carries ``sha``, not the branch's tip."""
    ms = [m for m in db.milestones(root.id) if m.phase_name == phase]
    out = [f"milestone {m.position} ({m.title}) hasn't passed its check" for m in ms if m.status != "passed"]
    if not ms:
        out.append(f"the goal has no phase {phase!r} any more")
    if not ws.base_branch or ws.branch in ("", "HEAD"):
        out.append("the session isn't on a branch with a base to open a PR against")
    elif status_cache.tracked_dirty(ws.path):
        out.append("the session's checkout has uncommitted changes")
    elif not git.is_ancestor(ws.path, sha, "HEAD"):
        out.append(f"{sha[:12]}, the commit phase {phase!r} passed on, isn't on {ws.branch} any more")
    elif not int(git.out(["rev-list", "--count", f"{git.base_ref(ws.path, ws.base_branch)}..{sha}"], ws.path)):
        out.append(f"{sha[:12]} has no commits ahead of {ws.base_branch}")
    return out


def needs_you(db: DB, root: Agent, ap) -> list[str]:
    """What in the session waits on the person: the inbox's items (see
    service.inbox), for this session's agents only."""
    out = []
    tree = agents.tree(db, root.id)
    for a in tree:
        if a.status == "waiting":
            out.append(f"{a.id} is waiting on you at a prompt")
        elif a.status == "failed":
            out.append(f"{a.id}'s credentials need attention")
        if a.plan_state == "proposed":
            out.append(f"{a.id}'s plan is waiting for approval")
    # A worker's approved branch the pipeline couldn't merge (and that hasn't merged since).
    for ws_id in dict.fromkeys(a.workspace_id for a in tree if a.workspace_id != root.workspace_id):
        ws = db.get_workspace(ws_id)
        state = pipeline.review_state(db, ws) if ws else None
        if state and state.startswith("merge failed"):
            out.append(f"{ws.branch}: {state}")
    if ap is not None and ap.state == "blocked":
        out.append(f"the session is waiting on you: {autopilot.person_note(ap) or 'a question'}")
    if ap is not None and autopilot.proposing(ap):
        out.append("proposed checks are waiting on your answer")
    if (login := db.get_login_state(root.id)) and login.state == "auth_paused":
        out.append("the session's cloud login has expired")
    if (home := db.get_workspace(root.workspace_id)) and (stuck := session_sync.needs_you(db, home)):
        out.append(stuck)
    return out


def has_pr_target(ws: Workspace) -> bool:
    """Whether the session's branch could be opened as a PR: it is on a branch
    with a base and has commits ahead of it (unlike ``reasons``, whatever
    only temporarily blocks a PR doesn't count)."""
    return bool(ws.base_branch) and ws.branch not in ("", "HEAD") and bool(
        git.ahead_behind(ws.path, git.base_ref(ws.path, ws.base_branch))[1])


def new_since_merge(db: DB, ws: Workspace, pushed_sha: str | None) -> bool:
    """Whether the session's branch has commits of its own beyond
    ``pushed_sha``, its merged PR's: its next PR's. What a sync brings in
    from the base (a squash of that PR included) doesn't count, nor does a
    merge that only joins its parents: git's own clean merge of them, the
    incoming side taken whole over a conflict, or the sync's ``-s ours``
    record of a squash (``session_squashes``, its tree the branch's own). A merge whose resolution adds
    anything else does. An unknown or vanished ``pushed_sha`` counts as nothing new."""
    if not pushed_sha:
        return False
    try:
        bases = [pushed_sha]
        if ws.base_branch:
            bases.append(git.base_ref(ws.path, ws.base_branch))
        if (default := session_sync.upstream(ws)) and git.remote_branch_exists(ws.path, default):
            bases.append(f"origin/{default}")
        # By commit where the git files say which each name is: what git lists for the same
        # commits never changes, so a session judged on every sweep is counted once.
        shas = git.resolve(ws.path, "HEAD", *bases)
        tip, *bases = shas or ["HEAD", *bases]
        ask = git.out_once if shas else git.out
        excluded = [f"^{base}" for base in dict.fromkeys(bases)]
        if int(ask(["rev-list", "--count", "--no-merges", tip, *excluded], ws.path)) > 0:
            return True
        squashes = {r["squash_sha"] for r in db.conn.execute(
            "SELECT squash_sha FROM session_squashes WHERE repo_root = ? AND branch = ?", (ws.repo_root, ws.branch))}
        return any(_merge_adds(ws, line.split(), squashes)
                   for line in ask(["rev-list", "--merges", "--parents", tip, *excluded], ws.path).splitlines())
    except (git.GitError, ValueError):
        return False


def _merge_adds(ws: Workspace, shas: list[str], squashes: set[str]) -> bool:
    """Whether the merge ``shas[0]`` (parents ``shas[1:]``) carries content of
    its own: worked out once per merge commit, which never changes (``git.once``)."""
    merge, parents = shas[0], shas[1:]
    if len(parents) != 2:
        return True
    squash = parents[1] in squashes
    return git.once(ws.path, ["merge adds", merge, str(squash)], lambda: _merge_content(ws, merge, parents, squash))


def _merge_content(ws: Workspace, merge: str, parents: list[str], squash: bool) -> bool:
    tree = git.out(["rev-parse", f"{merge}^{{tree}}"], ws.path)
    # The sync's `-s ours` record of a squash keeps the branch's tree; a squash with forge-side
    # edits is merged the plain way instead, and its resolution is judged like any other.
    if squash and tree == git.out(["rev-parse", f"{parents[0]}^{{tree}}"], ws.path):
        return False
    joined = git.run(["merge-tree", "--write-tree", *parents], ws.path, check=False)
    if joined.returncode == 0:
        return joined.stdout.split("\n", 1)[0].strip() != tree
    return tree != git.out(["rev-parse", f"{parents[1]}^{{tree}}"], ws.path)


def current_pr(db: DB, root_id: str, ws: Workspace):
    """The PR the session's branch is on (``url``, ``pushed_sha``,
    ``merged_at``): its open PR, or the one that merged until the branch has
    commits beyond it (``new_since_merge``); then None: its next PR is to be
    opened, and replaces the merged one's row when it is."""
    row = db.conn.execute("SELECT url, pushed_sha, merged_at FROM session_prs WHERE root_id = ? AND branch = ?"
                          " AND url IS NOT NULL", (root_id, ws.branch)).fetchone()
    if row is not None and row["merged_at"] and new_since_merge(db, ws, row["pushed_sha"]):
        return None
    return row


def adopt_head_pr(db: DB, root_id: str) -> None:
    """Record, as brindle's own Open PR does (``session_prs``), the open PR a
    supervisor made with ``gh`` when it holds the session's HEAD, so the
    sidebar's PR line, its checks and its merge are followed too. Never waits
    on ``gh`` (``open_pr_for_head``); a session that has a PR already keeps it
    (a merged one only until the branch has commits beyond it)."""
    if db.conn.execute("SELECT 1 FROM session_prs WHERE root_id = ? AND url IS NOT NULL AND merged_at IS NULL",
                       (root_id,)).fetchone():
        return
    try:
        root, ws = session(db, root_id)
        if current_pr(db, root_id, ws) is not None:
            return
        head = status_cache.read_head(ws.path)    # a file read: this runs on every goal read
    except (ServiceError, git.GitError, workspaces.WorkspaceError, OSError):
        return
    if not head:
        return
    found = open_pr_for_head(head, ws.path, root_id)
    if found and found.get("url"):
        # head_ref: where the PR really lives, when that isn't the session's branch: a Push must not go to ws.branch.
        db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at) VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT (root_id, branch) DO UPDATE SET url = excluded.url,"
                        " pushed_sha = excluded.pushed_sha, opened_at = excluded.opened_at,"
                        " merged_at = CASE WHEN url = excluded.url THEN merged_at END",
                        (root_id, ws.branch, found["url"], head, time.time()))
        db.conn.execute("DELETE FROM session_pr_heads WHERE root_id = ? AND branch = ?", (root_id, ws.branch))
        # Every adopted PR is managed outside brindle (its head may be another branch or a fork): Push is refused.
        db.conn.execute("INSERT INTO session_pr_heads (root_id, branch, head_ref) VALUES (?, ?, ?)",
                        (root_id, ws.branch, found.get("headRefName") or ws.branch))


def goal_phase(db: DB, root_id: str) -> tuple[str, str | None]:
    """The session goal's one path: (``in_progress`` | ``in_review`` |
    ``ready_for_pr`` | ``pr_open`` | ``done``, the PR's URL or None).
    ``done`` needs brindle's own decision that the goal is reached (autopilot
    state done: the audit approved it, or goal_audit is off); passing
    milestones alone don't. A PR-backed goal is done only once its PR is
    merged: an audit-approved goal with an open PR stays ``pr_open``. A
    session with no PR to open (no base branch, or no commits ahead of it) is
    done on that decision alone; any other reached goal awaits its PR, however
    temporarily it isn't ready (dirty checkout, workers running)."""
    ap, ms = db.get_autopilot(root_id), db.milestones(root_id)
    adopt_head_pr(db, root_id)
    pr = db.conn.execute("SELECT url, branch, pushed_sha, merged_at FROM session_prs WHERE root_id = ?"
                         " AND url IS NOT NULL ORDER BY opened_at DESC LIMIT 1", (root_id,)).fetchone()
    if pr and pr["merged_at"] and pr["pushed_sha"]:
        try:     # a merged PR the branch has moved on from: its next one is on its way
            _, ws = session(db, root_id)
            if ws.branch == pr["branch"] and current_pr(db, root_id, ws) is None:
                pr = None
        except (ServiceError, git.GitError, workspaces.WorkspaceError):
            pass
    # Brindle's own decision that the goal is reached (the audit approved it, or
    # goal_audit is off): passing milestones alone aren't it.
    reached = ap is not None and ap.state == "done"
    if pr:
        return ("done" if pr["merged_at"] and reached else "pr_open"), pr["url"]
    if ap is not None and (reached or ms and all(m.status == "passed" for m in ms)):
        try:
            root, ws = session(db, root_id)
            if not reasons(db, root, ws):
                return "ready_for_pr", None
            if reached and not has_pr_target(ws):
                return "done", None
        except (ServiceError, git.GitError, workspaces.WorkspaceError):
            pass
    return ("in_review" if "in_review" in autopilot.milestone_phases(db, root_id).values()
            else "in_progress"), None


PR_STATE_TTL = 90.0    # how long a PR's checks are believed; a failed read is believed as long
_PR_FAILED = ("CANCELLED", "ERROR", "FAILURE", "STARTUP_FAILURE", "TIMED_OUT", "ACTION_REQUIRED")
_PR_PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")
_pr_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pr-checks")
_pr_lock = threading.RLock()
_pr_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_pr_flights: dict[str, Future] = {}
_pr_changed: set[str] = set()      # PRs whose line changed, until refresh_pr_states announces them


def pr_state_of(url: str, payload: dict | None) -> dict[str, Any] | None:
    """What the sidebar's PR line says about the PR at ``url``, from ``gh pr
    view --json state,mergeable,statusCheckRollup``: ``{number, state, done,
    total}`` with state ``ci`` (checks still running; ``done`` of ``total``
    finished), ``failed``, ``ready`` (every check passed, no conflicts) or
    ``unknown`` (no payload, no checks yet, conflicts: never a guess at
    ready). ``conflicting`` flags a known conflict for the Inbox.
    None when the PR is no longer open."""
    found = re.search(r"/pull/(\d+)", url)
    if not found:
        return None
    out: dict[str, Any] = {"number": int(found.group(1)), "state": "unknown", "done": 0, "total": 0}
    if not payload:
        return out
    if payload.get("state") not in (None, "OPEN"):
        return None
    if payload.get("mergeable") == "CONFLICTING":
        out["conflicting"] = True
    marks = []      # per check: "pass", "fail" or "wait" (a check run not completed, a status still pending)
    for check in payload.get("statusCheckRollup") or []:
        if "status" in check and check["status"] != "COMPLETED":
            marks.append("wait")
            continue
        verdict = str(check.get("conclusion") or check.get("state") or "").upper()
        marks.append("fail" if verdict in _PR_FAILED else "pass" if verdict in _PR_PASSED else "wait")
        if marks[-1] == "fail" and (name := check.get("name") or check.get("context")):
            out.setdefault("failed_checks", []).append(name)
    out["total"] = len(marks)
    out["done"] = marks.count("pass") + marks.count("fail")
    if "fail" in marks:
        out["state"] = "failed"
    elif "wait" in marks:
        out["state"] = "ci"
    elif marks and payload.get("mergeable") == "MERGEABLE":
        out["state"] = "ready"
    return out


def _read_pr_state(url: str, cwd: str) -> None:
    try:
        # The pool's threads copy no context: bound the read with its own slot.
        with spawn.deadline(workspaces.PR_CHECKS_SECONDS):
            state = pr_state_of(url, workspaces.pull_request_checks(url, cwd))
    except Exception:  # noqa: BLE001 - an unreadable PR is just unknown
        log.exception("brindle: couldn't read a session PR's checks")
        state = pr_state_of(url, None)
    _set_pr_state(url, state)


def _set_pr_state(url: str, state: dict[str, Any] | None) -> None:
    with _pr_lock:
        before = _pr_cache[url][1] if url in _pr_cache else pr_state_of(url, None)
        _pr_cache[url] = (time.monotonic(), state)
        _pr_flights.pop(url, None)
        if state != before:
            _pr_changed.add(url)


def _ask_pr_state(url: str, cwd: str) -> None:
    """Start a read of the PR's checks unless the cache is fresh or one is
    already running (decided together under the lock, so overlapping askers
    make one read between them)."""
    with _pr_lock:
        cached = _pr_cache.get(url)
        if url in _pr_flights or (cached and time.monotonic() - cached[0] < PR_STATE_TTL):
            return
        flight = _pr_pool.submit(_read_pr_state, url, cwd)
        if not flight.done():
            _pr_flights[url] = flight


def session_pr_state(db: DB, root_id: str, url: str | None) -> dict[str, Any] | None:
    """The PR line for the session's open PR at ``url``: one cached read per
    PR however many rows or clients ask (refreshed in the background once
    ``PR_STATE_TTL`` old, a failed read cached like any other, so GitHub is
    asked at most once per window). Until the first read lands: unknown.
    ``refresh_pr_states`` keeps it fresh and tells the app when it changes."""
    if not url:
        return None
    with _pr_lock:
        cached = _pr_cache.get(url)
    if cached is None or time.monotonic() - cached[0] >= PR_STATE_TTL:
        root = db.get_agent(root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        if ws is not None:
            _ask_pr_state(url, ws.repo_root)
            with _pr_lock:
                cached = _pr_cache.get(url)
    state = cached[1] if cached else pr_state_of(url, None)
    if state and state["state"] == "failed":
        root = db.get_agent(root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        local = open_pr(db, root, ws) if root and ws else None
        if local and local["url"] == url:
            return {**state, "unpushed": local["unpushed"]}
    return state


_head_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}
_head_flights: set[tuple[str, str]] = set()
_head_roots: dict[tuple[str, str], str] = {}     # the session each lookup is for
_head_changed: set[tuple[str, str]] = set()      # lookups whose answer changed, until refresh_pr_states announces


def _read_head_pr(key: tuple[str, str]) -> None:
    sha, cwd = key
    try:
        with spawn.deadline(workspaces.PR_CHECKS_SECONDS):
            found = workspaces.open_pull_request_for_commit(sha, cwd)
    except Exception:  # noqa: BLE001 - an unreadable answer is just no PR
        log.exception("brindle: couldn't look up the PR holding a session's HEAD")
        found = None
    with _pr_lock:
        before = _head_cache[key][1] if key in _head_cache else None
        _head_cache[key] = (time.monotonic(), found)
        _head_flights.discard(key)
        if (found or {}).get("url") != (before or {}).get("url") and key in _head_roots:
            _head_changed.add(key)    # refresh_pr_states tells the Inbox


def note_head_pr(sha: str, cwd: str, pr: dict[str, Any]) -> None:
    """A PR brindle itself just opened on ``sha``: known at once, so the Inbox
    doesn't wait on (or trust an older "none" from) a read of GitHub."""
    with _pr_lock:
        _head_cache[(sha, cwd)] = (time.monotonic(), pr)


def open_pr_for_head(sha: str, cwd: str, root_id: str | None = None) -> dict[str, Any] | None:
    """The open PR (``{number, title, url, headRefName}``) containing ``sha``,
    whatever branch it was pushed under, as last read: never waits on ``gh``.
    A read is started in the background when the answer is missing or older
    than ``PR_STATE_TTL``; until the first lands: None. ``root_id``: the
    session to announce a change to (``refresh_pr_states``)."""
    key = (sha, cwd)
    with _pr_lock:
        if root_id:
            _head_roots[key] = root_id
        cached = _head_cache.get(key)
        if key not in _head_flights and (cached is None or time.monotonic() - cached[0] >= PR_STATE_TTL):
            flight = _pr_pool.submit(_read_head_pr, key)
            if not flight.done():
                _head_flights.add(key)
    return (cached[1] or None) if cached else None


def head_pr_known_none(sha: str, cwd: str) -> bool:
    """GitHub answered that no PR is open on ``sha`` (not merely: no answer yet)."""
    with _pr_lock:
        cached = _head_cache.get((sha, cwd))
    return cached is not None and cached[1] == {}


def _pr_open_state(db: DB, root_id: str) -> bool | None:
    """Whether the session has an open PR: True (GitHub found one on its HEAD, or brindle recorded
    one and GitHub hasn't said otherwise), False (GitHub says none is open), or None while that
    isn't known (the first look is on its way, or GitHub couldn't be asked and nothing recorded says).
    One snapshot of the lookup decides, so a read finishing meanwhile can't split the answer."""
    try:
        _, ws = session(db, root_id)
        head = status_cache.read_head(ws.path)
    except (ServiceError, git.GitError, workspaces.WorkspaceError, OSError):
        return False
    if not head:
        return False
    open_pr_for_head(head, ws.path, root_id)     # starts a read when none is fresh
    with _pr_lock:
        cached = _head_cache.get((head, ws.path))
    if cached is None:
        return None
    if cached[1]:
        return True
    if cached[1] == {}:
        return False
    recorded = db.conn.execute("SELECT 1 FROM session_prs WHERE root_id = ? AND url IS NOT NULL AND merged_at IS NULL",
                               (root_id,)).fetchone()
    return True if recorded else None


def _reconcile_pr_merge(db: DB, row, ws: Workspace) -> bool:
    """An existing attempt excludes another submission. Reconcile a departed owner.

    A unique marker token identifies ownership without PID reuse or a time limit.
    The OS releases the marker when the owner exits. An interrupted submission's
    outcome is uncertain, so check the forge and otherwise ask the person.
    """
    url = row["url"]
    attempt = db.conn.execute("SELECT state, owner FROM session_pr_merges WHERE url = ?", (url,)).fetchone()
    if attempt is None:
        return False
    if attempt["state"] == "pushing":     # a phase landing's push (routes_actions._land_phase)
        if not (attempt["owner"] and gates.marked("session-pr-merge", attempt["owner"])):
            _reconcile_push(db, row, ws, attempt["owner"])
        return True
    if attempt["state"] != "merging" or (attempt["owner"] and gates.marked("session-pr-merge", attempt["owner"])):
        return True
    owner = uuid4().hex
    with gates.marker("session-pr-merge", owner):
        if not db.conn.execute("UPDATE session_pr_merges SET owner = ? WHERE url = ?"
                               " AND state = 'merging' AND owner IS ?", (owner, url, attempt["owner"])).rowcount:
            return True
        payload = workspaces.pull_request_checks(url, ws.repo_root)
        _set_pr_state(url, pr_state_of(url, payload))
        finished = payload and payload.get("state") in ("MERGED", "CLOSED")
        error = None if finished else "The automatic PR merge was interrupted. Check the PR on GitHub before merging it."
        db.conn.execute("UPDATE session_pr_merges SET state = ?, error = ? WHERE url = ? AND owner = ?",
                        ("submitted" if finished else "failed", error, url, owner))
        bus.publish_agent(db, "session.pr_checks", row["root_id"], url=url)
    return True


INTERRUPTED_PUSH = ("A push to the PR was interrupted and its head isn't a commit brindle recorded."
                    " Check the PR on GitHub before merging it.")


def _reconcile_push(db: DB, row, ws: Workspace, departed: str | None) -> None:
    """A phase landing's push to the PR ``row`` whose owner went away: the
    merge stays blocked until the PR's head says how the push ended. Its
    intended commit (``session_pr_pushes``) on the PR: that is what the PR
    carries now (``pushed_sha``, which its merge's landing is judged by). The
    commit recorded before: it never got there. Anything else, or no head:
    the person checks the PR (a failed attempt, in the Inbox); no answer from
    GitHub: asked again next round."""
    url = row["url"]
    owner = uuid4().hex
    with gates.marker("session-pr-merge", owner):
        if not db.conn.execute("UPDATE session_pr_merges SET owner = ? WHERE url = ? AND state = 'pushing'"
                               " AND owner IS ?", (owner, url, departed)).rowcount:
            return
        intent = db.conn.execute("SELECT sha FROM session_pr_pushes WHERE url = ?", (url,)).fetchone()
        payload = workspaces.pull_request_checks(url, ws.repo_root)
        if payload is None:
            db.conn.execute("UPDATE session_pr_merges SET owner = NULL WHERE url = ? AND owner = ?", (url, owner))
            return
        head = payload.get("headRefOid")
        with db.tx() as c:
            if head and intent and head == intent["sha"]:
                c.execute("UPDATE session_prs SET pushed_sha = ? WHERE url = ? AND merged_at IS NULL", (head, url))
            if head and (intent and head == intent["sha"] or head == row["pushed_sha"]):
                c.execute("DELETE FROM session_pr_merges WHERE url = ? AND owner = ?", (url, owner))
            else:
                c.execute("UPDATE session_pr_merges SET state = 'failed', error = ? WHERE url = ? AND owner = ?",
                          (INTERRUPTED_PUSH, url, owner))
            c.execute("DELETE FROM session_pr_pushes WHERE url = ?", (url,))
    bus.publish_agent(db, "session.pr_checks", row["root_id"], url=url)


def _auto_merge_session_pr(db: DB, row, ws: Workspace) -> None:
    """Claim once across daemons; recheck both the forge and our record before acting."""
    url = row["url"]
    if _reconcile_pr_merge(db, row, ws):
        return
    with _pr_lock:
        cached = _pr_cache.get(url)
    if not cached or not cached[1] or cached[1]["state"] != "ready":
        return
    if not load_repo_config(ws.repo_root).auto_merge_session_prs:
        return
    current = ("SELECT 1 FROM session_prs WHERE root_id = ? AND branch = ? AND url = ?"
               " AND pushed_sha IS ? AND merged_at IS NULL")
    identity = (row["root_id"], row["branch"], url, row["pushed_sha"])
    owner = uuid4().hex
    with gates.marker("session-pr-merge", owner):
        if not db.conn.execute(
            f"INSERT OR IGNORE INTO session_pr_merges (url, state, owner) SELECT ?, 'merging', ? WHERE EXISTS ({current})",
            (url, owner, *identity),
        ).rowcount:
            return
        acting = False
        try:
            payload = workspaces.pull_request_checks(url, ws.repo_root)
            fresh = pr_state_of(url, payload)
            _set_pr_state(url, fresh)
            if not payload or payload.get("state") != "OPEN" or not fresh or fresh["state"] != "ready":
                return
            head = payload.get("headRefOid")
            root = db.get_agent(row["root_id"])
            if (not head or not root or db.get_workspace(root.workspace_id) != ws
                    or not load_repo_config(ws.repo_root).auto_merge_session_prs
                    or not db.conn.execute(current, identity).fetchone()):
                return
            acting = True
            workspaces.merge_pull_request(url, ws.repo_root, head)
            db.conn.execute("UPDATE session_pr_merges SET state = 'submitted' WHERE url = ?", (url,))
        except workspaces.WorkspaceError as exc:
            db.conn.execute("UPDATE session_pr_merges SET state = 'failed', error = ? WHERE url = ?", (str(exc), url))
            bus.publish_agent(db, "session.pr_checks", row["root_id"], url=url)
        finally:
            if not acting:
                db.conn.execute("DELETE FROM session_pr_merges WHERE url = ? AND state = 'merging'", (url,))


def pr_problem(db: DB, root_id: str, url: str) -> str | None:
    """A failed merge, or failed/conflicting PR nobody in the session is working on."""
    with _pr_lock:
        cached = _pr_cache.get(url)
    if cached is not None and cached[1] is None:  # the forge knows the PR closed or merged
        return None
    if db.conn.execute("SELECT 1 FROM session_prs WHERE root_id = ? AND url = ? AND merged_at IS NOT NULL",
                       (root_id, url)).fetchone():
        return None
    attempt = db.conn.execute("SELECT error FROM session_pr_merges WHERE url = ? AND state = 'failed'", (url,)).fetchone()
    if attempt:
        return attempt["error"]
    state = cached[1] if cached else None
    if not state or any(a.status in ("processing", "starting") and a.dismissed_at is None
                        for a in agents.tree(db, root_id)):
        return None
    if state.get("conflicting"):
        return "The PR conflicts with its base branch."
    if state["state"] == "failed":
        return "The PR's CI checks failed."
    return None


def refresh_pr_states(db: DB) -> None:
    """Daemon thread, every sweep: keep each open session PR's checks fresh
    and merge ready ones when enabled. Announce (``session.pr_checks``, which
    the app's goal reloads on) the ones whose line changed since it was shown."""
    for row in db.conn.execute("SELECT root_id, branch, url, pushed_sha FROM session_prs WHERE url IS NOT NULL"
                               " AND merged_at IS NULL").fetchall():
        root = db.get_agent(row["root_id"])
        ws = db.get_workspace(root.workspace_id) if root else None
        if ws is not None:
            _ask_pr_state(row["url"], ws.repo_root)
            _auto_merge_session_pr(db, row, ws)
    with _pr_lock:
        found = {_head_roots[k] for k in _head_changed if k in _head_roots}
        _head_changed.clear()
    for root_id in found:
        bus.publish_agent(db, "session.pr_opened", root_id)    # the Inbox and goal read again
    with _pr_lock:
        changed = set(_pr_changed)
        _pr_changed.clear()
    if not changed:
        return
    for row in db.conn.execute("SELECT root_id, url FROM session_prs WHERE url IS NOT NULL").fetchall():
        if row["url"] in changed:
            bus.publish_agent(db, "session.pr_checks", row["root_id"], url=row["url"])


MERGE_POLL_SECONDS = 60.0
MERGE_CHECK_SECONDS = 3.0
_merge_polled = 0.0
_merge_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pr-merged")
_merge_lock = threading.Lock()
_merge_checks: dict[tuple[str, str, str, str], Future] = {}
_merge_order: dict[str, list[tuple[str, str, str, str]]] = {}


def poll_merged(db: DB) -> None:
    """Check open PRs concurrently, with one bounded wait for the round.

    Only the daemon thread touches its DB. A check still running is shared
    by later rounds; the fixed pool bounds even a check that ignores its
    deadline. The first PR advances each round, independent of whether a
    queued check was cancelled or ran after its execution window expired.
    """
    global _merge_polled
    if not _merge_lock.acquire(blocking=False):
        return
    try:
        due = time.monotonic() - _merge_polled >= MERGE_POLL_SECONDS
        if due:
            _merge_polled = time.monotonic()
            for root_id in session_sync.unlanded(db):    # a merge whose landing an earlier round missed
                _sync_after_merge(db, root_id)
            with spawn.deadline(MERGE_CHECK_SECONDS) as limit:
                check = spawn.carry(workspaces.pull_request_merged)
                path = str(db.path)
                priority = {key: n for n, key in enumerate(_merge_order.get(path, []))}
                rows = db.conn.execute("SELECT root_id, branch, url FROM session_prs"
                                       " WHERE url IS NOT NULL AND merged_at IS NULL").fetchall()
                checks = [((path, row["root_id"], row["branch"], row["url"]), row) for row in rows]
                if priority:
                    checks.sort(key=lambda item: priority.get(item[0], len(priority)))
                # Rotate by one so every PR eventually gets the first fresh
                # window, even if the gate only admits one read per round.
                if checks:
                    _merge_order[path] = [key for key, _ in checks[1:] + checks[:1]]
                for key, row in checks:
                    root = db.get_agent(row["root_id"])
                    ws = db.get_workspace(root.workspace_id) if root else None
                    if ws is not None and key not in _merge_checks:
                        _merge_checks[key] = _merge_pool.submit(check, row["url"], ws.repo_root)
                pending = [f for k, f in _merge_checks.items() if k[0] == str(db.path)]
                if pending:
                    remaining = max(0.0, limit.left())
                    wait(pending, timeout=remaining)
                for future in pending:
                    future.cancel()  # running reads stop at their deadline; queued ones never start
        for key, future in list(_merge_checks.items()):
            if key[0] != str(db.path) or not future.done():
                continue
            del _merge_checks[key]
            if future.cancelled():
                continue
            try:
                merged = future.result()
            except Exception:  # noqa: BLE001 - an unavailable PR never stops the round
                log.exception("brindle: couldn't check a session PR")
                continue
            _, root_id, branch, url = key
            if merged and db.conn.execute(
                "UPDATE session_prs SET merged_at = ? WHERE root_id = ? AND branch = ?"
                " AND url = ? AND merged_at IS NULL", (time.time(), root_id, branch, url)
            ).rowcount:
                bus.publish_agent(db, "session.pr_merged", root_id, url=url)
                _sync_after_merge(db, root_id, branch, url)
                try:   # tasks queued behind this session's work start now it has landed
                    tasks.sweep(db)
                except Exception:  # noqa: BLE001 - a queue sweep never stops the round
                    log.exception("brindle: couldn't start tasks waiting on %s", branch)
    finally:
        _merge_lock.release()


def _sync_after_merge(db: DB, root_id: str, branch: str | None = None, url: str | None = None) -> None:
    """A session PR merged (``url``; none: one merged earlier whose landing
    commit wasn't known yet): note it until how it landed is known, then pull
    the default branch back into the session (brindle.session_sync)."""
    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    if ws is None or (branch and ws.branch != branch):
        return
    try:
        if url is None:
            session_sync.resume_landing(db, ws)
            return
        row = db.conn.execute("SELECT pushed_sha FROM session_prs WHERE root_id = ? AND branch = ?",
                              (root_id, branch)).fetchone()
        session_sync.on_pr_merged(db, root_id, ws, url, row["pushed_sha"] if row else None)
        if row:
            from brindle import disk

            disk.delete_pushed_remote(ws, row["pushed_sha"])   # opt-in: delete_pushed_branches
    except Exception:  # noqa: BLE001 - one session's sync never stops the round
        log.exception("brindle: couldn't sync %s after its PR merged", ws.branch)


def require_ready(db: DB, root: Agent, ws: Workspace) -> None:
    if why := reasons(db, root, ws):
        raise NotReady(why)


def open_pr(db: DB, root: Agent, ws: Workspace) -> dict[str, Any] | None:
    """The open PR the person opened for the session's branch: its ``url`` and
    ``unpushed``, how many session commits aren't on it yet (the person's
    Push sends them, POST .../pr/push; brindle pushes on its own only a
    completed phase's commit, routes_actions.land_phase_prs). A
    merged PR is none: nothing more goes onto it (the branch's next PR does)."""
    row = db.conn.execute("SELECT url, pushed_sha FROM session_prs WHERE root_id = ? AND branch = ?"
                          " AND url IS NOT NULL AND merged_at IS NULL", (root.id, ws.branch)).fetchone()
    if row is None:
        return None
    try:
        unpushed = int(git.out(["rev-list", "--count", f"{row['pushed_sha']}..HEAD"], ws.path)) if row["pushed_sha"] else 0
    except git.GitError:     # the pushed commit is gone (a rewrite): nothing to count against
        unpushed = 0
    return {"url": row["url"], "unpushed": unpushed}


# -- the diff ----------------------------------------------------------------


def _worker(db: DB, ws: Workspace, branch: str) -> dict[str, Any]:
    """Whoever's branch ``branch`` was: its worker while its workspace is
    there, else the worker that last reported on it."""
    for w in db.find_workspaces(ws.repo_root):
        if w.branch == branch and w.kind == "worktree" and (a := agents.workspace_worker(db, w)):
            return {"id": a.id, "kind": "worker", "profile": a.profile, "branch": branch}
    row = db.conn.execute("SELECT agent_id, profile FROM history WHERE repo_root = ? AND branch = ?"
                          " AND kind = 'worker_result' AND agent_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                          (ws.repo_root, branch)).fetchone()
    if row:
        return {"id": row["agent_id"], "kind": "worker", "profile": row["profile"], "branch": branch}
    return {"id": branch, "kind": "worker", "profile": None, "branch": branch}


class _Lru:
    """Values by key; the least recently used go first once their total
    weight passes ``most``."""

    def __init__(self, most: int, weigh: Callable[[Any], int] = lambda value: 1) -> None:
        self.most, self.weigh, self.used = most, weigh, 0
        self.items: OrderedDict[Any, Any] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self.lock:
            if key not in self.items:
                return None
            self.items.move_to_end(key)
            return self.items[key]

    def put(self, key: Any, value: Any) -> None:
        with self.lock:
            if key in self.items:
                self.used -= self.weigh(self.items.pop(key))
            self.items[key] = value
            self.used += self.weigh(value)
            while self.used > self.most and len(self.items) > 1:
                self.used -= self.weigh(self.items.popitem(last=False)[1])

    def clear(self) -> None:
        with self.lock:
            self.items.clear()
            self.used = 0


# Commits never change, so neither does what is kept by their shas: a diff
# between two commits (weighed by its patches' size), a merge's commits and
# a commit's files.
_DIFFS = _Lru(64 << 20, lambda files: 1 + sum(len(f[3]) for f in files))
_MERGED = _Lru(1 << 20, lambda commits: 1 + len(commits))
_TREES = _Lru(1 << 20, lambda paths: 1 + len(paths))
_RESULTS = _Lru(32)     # session id -> (its _Fingerprint, its _Facts)


@dataclass(frozen=True)
class _Facts:
    """A session diff's git side for one HEAD and base: everything but who
    its commits' authors are, which the db says on each ask."""
    path: str
    base_sha: str
    head: str
    # The first-parent history, newest first: each commit, its first parent,
    # its subject and, for a merge, each merged tip's commits.
    log: tuple[tuple[str, str, str, tuple[tuple[str, ...], ...]], ...]
    named: tuple[tuple[str, tuple[str, ...]], ...]          # each commit since the base and the files it touched
    changed: tuple[tuple[str, str | None, str, str], ...]   # _file_diffs(base_sha, head)
    deleted: dict[str, dict[int, str]]          # base path -> base line -> the commit that deleted it
    blamed: dict[str, tuple[tuple[int, ...], dict[int, str]]]   # path -> (its added lines, line -> their commit)


def _merged(ws: Workspace, first: str, tip: str) -> tuple[str, ...]:
    """The commits a merge of ``tip`` onto ``first`` brought in."""
    if (hit := _MERGED.get((first, tip))) is None:
        hit = tuple(git.out(["rev-list", f"{first}..{tip}"], ws.path).split())
        _MERGED.put((first, tip), hit)
    return hit


def _log(ws: Workspace, mb: str, head: str) -> list[tuple[str, str, str, tuple[tuple[str, ...], ...]]]:
    """``_Facts.log`` for ``mb``..``head``."""
    out = []
    for line in git.run(["log", "--first-parent", "--format=%H%x00%P%x00%s", f"{mb}..{head}"],
                        ws.path).stdout.splitlines():
        sha, parents, subject = line.split("\0", 2)
        first, *tips = parents.split()
        out.append((sha, first, subject, tuple(_merged(ws, first, tip) for tip in tips)))
    return out


def _named(ws: Workspace, mb: str, head: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Each commit since ``mb`` and the files it touched."""
    text = git.run(["-c", "core.quotePath=false", "log", "--format=%x01%H", "--name-only", f"{mb}..{head}"],
                   ws.path).stdout
    return tuple((sha, tuple(filter(None, files)))
                 for sha, *files in (chunk.split("\n") for chunk in text.split("\x01")[1:]))


def _owners(db: DB, root: Agent, ws: Workspace, facts: _Facts) -> tuple[dict[str, str], dict[str, dict]]:
    """Each commit since the base -> its author's id, and those authors."""
    people = {SUPERVISOR: {"id": SUPERVISOR, "kind": SUPERVISOR, "profile": root.profile, "branch": ws.branch},
              YOU: {"id": YOU, "kind": YOU, "profile": None, "branch": None}}
    owner: dict[str, str] = {}
    for sha, _, subject, merged in facts.log:
        m = (MERGED_RE if merged else SQUASHED_RE).match(subject)
        if m:
            who = _worker(db, ws, m.group(1))
            people.setdefault(who["id"], who)
            key = who["id"]
        else:
            key = YOU if subject.startswith(f"{EDIT_MESSAGE}: ") else SUPERVISOR
        owner[sha] = key
        for commits in merged:
            for c in commits:
                owner.setdefault(c, key)
    return owner, people


def _file_authors(facts: _Facts, owner: dict[str, str]) -> dict[str, list[str]]:
    """Path -> the authors of the commits since the base that touched it."""
    found: dict[str, list[str]] = {}
    for sha, files in facts.named:
        for f in files:
            who = owner.get(sha, SUPERVISOR)
            if who not in found.setdefault(f, []):
                found[f].append(who)
    return found


def _hunks(patch: str) -> list[dict[str, Any]]:
    """A file's hunks; ``added``: the new-side numbers of the lines each
    adds, ``deleted``: the old-side numbers of those it deletes."""
    hunks: list[dict[str, Any]] = []
    new_at = old_at = 0      # the numbers of the next line on each side
    for line in patch.split("\n"):
        if m := HUNK_RE.match(line):
            old_start, old_lines, new_start, new_lines = (int(g) if g is not None else 1 for g in m.groups())
            hunks.append({"header": line, "old_start": old_start, "old_lines": old_lines,
                          "new_start": new_start, "new_lines": new_lines, "lines": [line],
                          "added": [], "deleted": []})
            new_at, old_at = new_start, old_start
        elif hunks and line[:1] in (" ", "+", "-", "\\"):
            if line[:1] == "+":
                hunks[-1]["added"].append(new_at)
            elif line[:1] == "-":
                hunks[-1]["deleted"].append(old_at)
            new_at += line[:1] in (" ", "+")
            old_at += line[:1] in (" ", "-")
            hunks[-1]["lines"].append(line)
    return hunks


def _ranges(lines: list[int]) -> list[str]:
    """``-L`` arguments covering ``lines`` (ascending), one per run."""
    runs: list[list[int]] = []
    for n in lines:
        if runs and runs[-1][1] == n - 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return [arg for a, b in runs for arg in ("-L", f"{a},{b}")]


def _blame(ws: Workspace, mb: str, head: str, path: str, lines: list[int]) -> dict[int, str]:
    """New-side line -> the commit that wrote it, for ``lines`` of ``path`` at ``head``."""
    text = git.run(["blame", "--line-porcelain", *_ranges(lines), f"{mb}..{head}", "--", path], ws.path).stdout
    return {int(m.group(2)): m.group(1) for m in map(BLAME_RE.match, text.splitlines()) if m}


def _file_diffs(ws: Workspace, a: str, b: str, *opts: str) -> tuple[tuple[str, str | None, str, str], ...]:
    """``a``..``b`` per file: (status letter, old path if renamed, path, its
    patch). ``a`` and ``b`` are commits' shas, so what is found is kept."""
    if (hit := _DIFFS.get((a, b, opts))) is None:
        hit = tuple(_run_file_diffs(ws, a, b, *opts))
        _DIFFS.put((a, b, opts), hit)
    return hit


def _run_file_diffs(ws: Workspace, a: str, b: str, *opts: str) -> list[tuple[str, str | None, str, str]]:
    base = ["-c", "core.quotePath=false", "diff", "-M", "--no-color", "--no-ext-diff", *opts]
    names = git.run([*base, "-z", "--name-status", a, b], ws.path).stdout.split("\0")
    patches = re.split(r"^(?=diff --git )", git.run([*base, "--src-prefix=a/", "--dst-prefix=b/", "--patch", a, b],
                                                     ws.path).stdout, flags=re.M)[1:]
    out, i = [], 0
    for patch in patches:      # the same order as the names: the same diff
        status = names[i]
        old_path, path = (names[i + 1], names[i + 2]) if status[:1] in ("R", "C") else (None, names[i + 1])
        i += 3 if old_path else 2
        out.append((status[:1], old_path, path, patch))
    return out


def _touches(ws: Workspace, mb: str, head: str) -> dict[str, frozenset[str]]:
    """Each first-parent commit since ``mb`` -> the paths its diff from its
    first parent touches (a rename's both names)."""
    text = git.run(["log", "--first-parent", "-m", "--no-renames", "--name-only", "-z", "--format=%x01%H",
                    f"{mb}..{head}"], ws.path).stdout
    out: dict[str, set[str]] = {}
    sha, fresh = None, False
    for token in text.split("\0"):
        if fresh and token.startswith("\n"):    # the line break after a commit's header
            token = token[1:]
        if token.startswith("\x01"):
            sha, fresh = token[1:], True
            out[sha] = set()
        elif token and sha is not None:
            out[sha].add(token)
            fresh = False
    return {sha: frozenset(paths) for sha, paths in out.items()}


def _base_paths(ws: Workspace, mb: str) -> frozenset[str]:
    """The files at ``mb``: the only ones a base line can be deleted from."""
    if (hit := _TREES.get(mb)) is None:
        hit = frozenset(filter(None, git.run(["ls-tree", "-r", "-z", "--name-only", mb], ws.path).stdout.split("\0")))
        _TREES.put(mb, hit)
    return hit


def _deleters(ws: Workspace, chain: list[tuple[str, str]], wanted: dict[str, list[int]],
              touches: dict[str, frozenset[str]]) -> dict[str, dict[int, str]]:
    """Base path -> its deleted base lines -> the commit that deleted each. Each line is
    carried along the session's first-parent history (``chain``: each commit
    and its first parent, oldest first) and goes to the commit whose diff
    from its first parent removes it: a merge's for a merged worker's
    deletion, never a diff against a worker's branch. A commit that
    ``touches`` none of the files followed can't move a line: skipped."""
    at = {path: {n: n for n in lines} for path, lines in wanted.items()}     # base line -> its number now
    named = {path: path for path in wanted}                                  # its name now -> base path
    found: dict[str, dict[int, str]] = {path: {} for path in wanted}
    for sha, first in chain:
        if not named:
            break
        if sha in touches and touches[sha].isdisjoint(named):
            continue
        for _, old_path, path, patch in _file_diffs(ws, first, sha, "-U0"):
            base = named.pop(old_path or path, None)
            if base is None:
                continue
            hunks = [(h["old_start"], h["old_lines"], h["new_lines"]) for h in _hunks(patch)]
            moved = {}
            for line, n in at[base].items():
                if any(start <= n < start + count for start, count, _ in hunks if count):
                    found[base][line] = sha
                else:   # shifted by every hunk before it (an insertion lands after its old line)
                    moved[line] = n + sum(new - count for start, count, new in hunks
                                          if (start + count <= n if count else start < n))
            at[base] = moved
            if moved:
                named[path] = base
    return found


def _blames(ws: Workspace, mb: str, head: str, chain: list[tuple[str, str]], touches: dict[str, frozenset[str]],
            added: dict[str, tuple[int, ...]], prev: _Facts | None) -> dict[str, tuple[tuple[int, ...], dict[int, str]]]:
    """``_Facts.blamed`` for the files with ``added`` lines. A file that no
    first-parent commit since ``prev``'s HEAD ``touches`` keeps ``prev``'s blame:
    blame hands a file its first parent has unchanged wholly to that parent,
    so it walks down to ``prev``'s HEAD and finds what it found then."""
    shas = [sha for sha, _ in chain]
    kept: dict[str, tuple[tuple[int, ...], dict[int, str]]] = {}
    if (prev is not None and prev.path == ws.path and prev.base_sha == mb and prev.head in shas
            and all(sha in touches for sha in shas)):
        touched = set().union(*(touches[sha] for sha in shas[shas.index(prev.head) + 1:]))
        kept = {path: found for path, found in prev.blamed.items() if path not in touched}
    out = {}
    for path, lines in added.items():
        hit = kept.get(path)
        out[path] = hit if hit is not None and hit[0] == lines else (lines, _blame(ws, mb, head, path, list(lines)))
    return out


def _compute(ws: Workspace, base: str, fp: _Fingerprint | None, prev: _Facts | None) -> _Facts:
    """The session diff's git side, at ``fp``'s HEAD and base when there is
    one; ``prev``: the session's last, whose blame is reused where it holds."""
    head = fp.head if fp else git.out(["rev-parse", "HEAD"], ws.path)
    ref = git.base_ref(ws.path, base)
    if fp:
        ref = {base: fp.local or fp.other, f"origin/{base}": fp.origin}.get(ref) or ref
    mb = git.out(["merge-base", ref, head], ws.path)
    log_ = _log(ws, mb, head)
    chain = [(sha, first) for sha, first, _, _ in reversed(log_)]
    touches, in_base = _touches(ws, mb, head), _base_paths(ws, mb)
    renamed: set[str] = set()       # the names base files took since
    for sha, first in chain:
        # A base line deleted later may need any commit that touched its file (under any of its
        # names) to follow it: each runs once, then is kept.
        if sha in touches and touches[sha].isdisjoint(in_base) and touches[sha].isdisjoint(renamed):
            continue
        for _, old_path, path, _ in _file_diffs(ws, first, sha, "-U0"):
            if old_path is not None and (old_path in in_base or old_path in renamed):
                renamed.add(path)
    changed = _file_diffs(ws, mb, head)
    hunks = [(old_path or path, path, _hunks(patch)) for _, old_path, path, patch in changed]
    deleted = _deleters(ws, chain, {was: [n for h in hs for n in h["deleted"]]
                                    for was, _, hs in hunks if any(h["deleted"] for h in hs)}, touches)
    added = {path: tuple(n for h in hs for n in h["added"]) for _, path, hs in hunks}
    blamed = _blames(ws, mb, head, chain, touches, {path: lines for path, lines in added.items() if lines}, prev)
    return _Facts(ws.path, mb, head, tuple(log_), _named(ws, mb, head), changed, deleted, blamed)


# -- what changed, without starting git ---------------------------------------


@dataclass(frozen=True)
class _Fingerprint:
    """What a session diff's git side depends on: while it holds, so does
    the last one."""
    path: str
    base: str
    head: str
    local: str | None       # refs/heads/<base>
    origin: str | None      # refs/remotes/origin/<base>
    other: str | None       # without either: what git resolves <base> to (origin/main, refs/heads/main, a tag)
    index: int              # the index's mtime (ns); 0 without one


SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_PER_WORKTREE = ("refs/worktree/", "refs/bisect/", "refs/rewritten/")


def _git_dirs(path: str) -> tuple[Path, Path]:
    """A checkout's own git dir and its repo's common one."""
    own = Path(path) / ".git"
    if own.is_file():
        text = own.read_text().strip()
        if not text.startswith("gitdir:"):
            raise ValueError(f"{own}: not a gitdir link")
        own = Path(path) / text.removeprefix("gitdir:").strip()
    common = own
    if (own / "commondir").is_file():
        common = own / (own / "commondir").read_text().strip()
    return own, common


def _packed(common: Path) -> dict[str, str]:
    try:
        lines = (common / "packed-refs").read_text().splitlines()
    except FileNotFoundError:
        return {}
    return {name: sha for sha, _, name in (line.partition(" ") for line in lines) if SHA_RE.fullmatch(sha)}


def _ref(own: Path, common: Path, name: str, depth: int = 0) -> str | None:
    """``name``'s commit from the loose or packed refs; None when there is no such ref."""
    if depth > 4:
        raise ValueError(f"{name}: symbolic refs too deep")
    where = own if name == "HEAD" or name.startswith(_PER_WORKTREE) else common
    try:
        text = (where / name).read_text().strip()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return _packed(common).get(name)
    if text.startswith("ref:"):
        return _ref(own, common, text.removeprefix("ref:").strip(), depth + 1)
    if not SHA_RE.fullmatch(text):
        raise ValueError(f"{name}: {text!r} is not a commit")
    return text


def _resolved(own: Path, common: Path, base: str) -> str | None:
    """What git resolves ``base`` to (its rev-parse rules), when it is
    neither a branch nor one of origin's: a qualified ref, a tag, a sha."""
    if SHA_RE.fullmatch(base):
        return base
    for name in (base, f"refs/{base}", f"refs/tags/{base}", f"refs/heads/{base}", f"refs/remotes/{base}",
                 f"refs/remotes/{base}/HEAD"):
        if (sha := _ref(own, common, name)) is not None:
            return sha
    return None


def _fingerprint(ws: Workspace, base: str) -> _Fingerprint | None:
    """The checkout's fingerprint, read from its git files; None when they
    can't be read that way (a reftable repo, no commit yet, a base only git
    itself can resolve): then nothing is reused."""
    try:
        own, common = _git_dirs(ws.path)
        if (common / "reftable").exists() or (head := _ref(own, common, "HEAD")) is None:
            return None
        local, origin = _ref(own, common, f"refs/heads/{base}"), _ref(own, common, f"refs/remotes/origin/{base}")
        other = None
        if local is None and origin is None and (other := _resolved(own, common, base)) is None:
            return None
        try:
            index = (own / "index").stat().st_mtime_ns
        except FileNotFoundError:
            index = 0
        return _Fingerprint(ws.path, base, head, local, origin, other, index)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


class _Flight:
    """A session's computation under way, which whoever asks meanwhile shares."""

    def __init__(self, fp: _Fingerprint | None) -> None:
        self.fp, self.done, self.joined = fp, False, 0
        self.facts: _Facts | None = None
        self.error: BaseException | None = None


_lock = threading.Condition()
_flights: dict[str, _Flight] = {}


def _session_facts(root_id: str, ws: Workspace, base: str) -> _Facts:
    """The session's last facts while its fingerprint holds; else one
    computation, shared, kept only once it completes."""
    fp = _fingerprint(ws, base)
    while True:
        with _lock:
            stored = _RESULTS.get(root_id)
            if fp is not None and stored is not None and stored[0] == fp:
                return stored[1]
            flight = _flights.get(root_id)
            if flight is None:
                flight = _flights[root_id] = _Flight(fp)
                prev = stored[1] if stored else None
                break
            flight.joined += 1
            _lock.notify_all()
            _lock.wait_for(lambda f=flight: f.done)
            if flight.fp == fp:
                if flight.error is not None:
                    raise flight.error
                assert flight.facts is not None
                return flight.facts
            # It computed for another fingerprint: look again.
    try:
        facts = _compute(ws, base, fp, prev)
    except BaseException as e:
        with _lock:
            flight.error, flight.done = e, True
            del _flights[root_id]
            _lock.notify_all()
        raise
    with _lock:
        if fp is not None:
            _RESULTS.put(root_id, (fp, facts))
        flight.facts, flight.done = facts, True
        del _flights[root_id]
        _lock.notify_all()
    return facts


def _forget() -> None:
    """Drop everything kept."""
    for kept in (_DIFFS, _MERGED, _TREES, _RESULTS):
        kept.clear()


def diff(db: DB, root: Agent, ws: Workspace) -> dict[str, Any]:
    """The session branch against its PR base, attributed."""
    base = workspaces.require_base(ws)
    facts = _session_facts(root.id, ws, base)
    owner, people = _owners(db, root, ws, facts)
    by_file = _file_authors(facts, owner)

    def author(commits: dict[int, str], n: int) -> str:
        return owner.get(commits[n], SUPERVISOR) if n in commits else SUPERVISOR

    files, seen = [], []
    for status, old_path, path, patch in facts.changed:
        hunks = _hunks(patch)
        blamed = facts.blamed[path][1] if path in facts.blamed else {}
        unblamed = facts.deleted.get(old_path or path, {})
        fallback = by_file.get(path) or [SUPERVISOR]
        for h in hunks:
            who = [author(blamed, n) for n in h["added"]] + [author(unblamed, n) for n in h["deleted"]]
            h["authors"] = list(dict.fromkeys(who)) or fallback
            h["text"] = "\n".join(h.pop("lines"))
            del h["added"], h["deleted"]
        authors = list(dict.fromkeys(a for h in hunks for a in h["authors"])) or fallback
        seen += authors
        files.append({"path": path, "old_path": old_path, "status": status,
                      "binary": "\nBinary files " in patch or patch.startswith("Binary files "),
                      "authors": authors, "hunks": hunks})
    return {"root_id": root.id, "branch": ws.branch, "base": base, "base_sha": facts.base_sha,
            "head": facts.head, "files": files, "workers": [people[k] for k in dict.fromkeys(seen)]}


# -- the PR ------------------------------------------------------------------


def draft(db: DB, root: Agent, ws: Workspace) -> dict[str, Any]:
    """The PR's title and body, from the goal and its milestones (once the
    goal is reached and cleared, from what the session last announced)."""
    ap = db.get_autopilot(root.id)
    if ap and ap.goal:
        goal, titles, detail = ap.goal, [m.title for m in db.milestones(root.id)], ap.detail
    else:
        goal, *titles = (knowledge.last_intent(db, root.id) or "").splitlines() or [""]
        detail = None
    if not goal.strip():
        goal = git.out(["log", "-1", "--format=%s"], ws.path)
    parts = [detail.strip()] if detail and detail.strip() else []
    if titles:
        parts.append("Milestones, each verified by its check:\n" + "\n".join(f"- {t}" for t in titles))
    return {"title": goal.strip().splitlines()[0], "body": "\n\n".join(parts),
            "base": workspaces.require_base(ws), "branch": ws.branch}


class ReadinessWatch:
    """Remembers which sessions are ready for a PR and publishes
    ``session.pr_ready`` when one becomes ready; prepares a ready session's
    PR for the person's approval (routes_actions.prepare_pr: its checks, no
    push), and forgets what it prepared for one no longer ready. The only PR
    brindle opens or pushes itself is a completed goal phase's, pinned to the
    commit its checks passed on (routes_actions.land_phase_prs, every sweep).
    The first sweep only looks."""

    def __init__(self) -> None:
        self.ready: dict[str, bool] | None = None
        self.fingerprints: dict[str, tuple] = {}
        self.idle_fetched_at: dict[str, float] = {}
        self.sync_seen = session_sync.Seen()   # idle sync and drift: sessions whose refs haven't moved
        self._tracked: dict[str, tuple[tuple[str, int], list[str] | None]] = {}
        self.announce: set[str] = set()    # became ready while GitHub's first look at its PR was pending

    def _working_tree(self, ws: Workspace, fp: _Fingerprint) -> tuple | None:
        """Stat only tracked paths, including files below nested directories.

        The index supplies their names once, then only when it changes.
        Unstaged edits, deletions, restorations and mode changes invalidate
        readiness without launching git in an unchanged checkout.
        A gitlink's directory timestamps cannot describe its nested working
        tree. Keep its presence in the index cache, but let git judge its
        readiness on each round rather than keeping incomplete facts.
        """
        key = (ws.path, fp.index)
        cached = self._tracked.get(ws.id)
        if cached is None or cached[0] != key:
            entries = git.run(["ls-files", "--stage", "-z"], ws.path).stdout.split("\0")
            names = [entry.partition("\t")[2] for entry in entries if entry]
            if any(entry.startswith("160000 ") for entry in entries):
                names = None
            cached = (key, names)
            self._tracked[ws.id] = cached
        if cached[1] is None:
            return None
        try:
            own, _ = _git_dirs(ws.path)
            head = (own / "HEAD").read_text()
            stamps = []
            for name in cached[1]:
                try:
                    stat = (Path(ws.path) / name).lstat()
                    stamps.append((stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
                except FileNotFoundError:
                    stamps.append(None)
            return head, tuple(stamps)
        except OSError:
            return None

    def _state(self, db: DB, root_id: str, ap, ms) -> tuple | None:
        """Readiness's git, working-tree and DB inputs.

        Worker, queue and login changes matter even when HEAD hasn't moved.
        Unknown git layouts are never cached. Preparation rows are outputs,
        so their writes must not invalidate an otherwise unchanged session.
        """
        root = db.get_agent(root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        if ws is None or not ws.base_branch:
            return None
        fp = _fingerprint(ws, ws.base_branch)
        if fp is None:
            return None
        working = self._working_tree(ws, fp)
        if working is None:
            return None
        tree = agents.tree(db, root_id)
        return (fp, working, repr(ws), repr(ap), repr(ms), repr(tree),
                repr([db.get_workspace(a.workspace_id) for a in tree]),
                repr(tasks.queued(db)), repr(db.get_login_state(root_id)),
                repr(load_repo_config(ws.repo_root)),
                # its PR merging (or the next one opening) changes readiness without a new commit
                repr([tuple(r) for r in db.conn.execute("SELECT branch, url, merged_at FROM session_prs"
                                                        " WHERE root_id = ?", (root_id,))]))

    def sweep(self, db: DB) -> list[str]:
        """The sessions that just became ready (each announced on the bus)."""
        try:
            session_sync.refresh_idle_sessions(db, self.idle_fetched_at, self.sync_seen)
        except Exception:  # noqa: BLE001 - one missed sync never stops the sweep
            log.exception("brindle: couldn't sync idle sessions")
        try:
            session_sync.refresh_drift_flags(db, self.sync_seen)
        except Exception:  # noqa: BLE001 - one missed measurement never stops the sweep
            log.exception("brindle: couldn't refresh session drift flags")
        if spawn.room() < 1:
            return []
        poll_merged(db)
        try:
            refresh_pr_states(db)
        except Exception:  # noqa: BLE001 - a missed refresh never stops the sweep
            log.exception("brindle: couldn't refresh session PR checks")
        try:
            session_sync.tell_refused_fetches(db)
        except Exception:  # noqa: BLE001 - a missed notice never stops the sweep
            log.exception("brindle: couldn't tell a supervisor about a refused fetch")
        from brindle.daemon import routes_actions

        try:
            routes_actions.land_phase_prs(db)
        except Exception:  # noqa: BLE001 - a missed landing is tried again next sweep
            log.exception("brindle: couldn't land a goal phase's PR")
        now: dict[str, bool] = {}
        fingerprints: dict[str, tuple] = {}
        for (root_id,) in db.conn.execute("SELECT root_id FROM autopilot").fetchall():
            ap, ms = db.get_autopilot(root_id), db.milestones(root_id)
            # Only a reached goal (or one whose every milestone passes) can be ready: skip the git work otherwise.
            if ap is None or not (ap.state == "done" or (ms and all(m.status == "passed" for m in ms))):
                now[root_id] = False
                continue
            # Nor can a session with no branch and base to open a PR from (one on the repo's own
            # checkout): ``reasons`` would run its git checks every sweep only to say so again.
            root = db.get_agent(root_id)
            ws = db.get_workspace(root.workspace_id) if root else None
            if ws is None or not ws.base_branch or ws.branch in ("", "HEAD"):
                now[root_id] = False
                continue
            try:
                state = self._state(db, root_id, ap, ms)
                if state is not None:
                    fingerprints[root_id] = state
                    if self.fingerprints.get(root_id) == state and self.ready is not None:
                        now[root_id] = self.ready.get(root_id, False)
                        continue
                now[root_id] = not reasons(db, *session(db, root_id))
            except (ServiceError, git.GitError, workspaces.WorkspaceError):
                fingerprints.pop(root_id, None)
                now[root_id] = False
        self.fingerprints = fingerprints
        was, self.ready = self.ready, now
        if was is None:
            return []
        flipped = [r for r, ok in now.items() if ok and not was.get(r)]
        self.announce = {r for r in self.announce | set(flipped) if now.get(r)}
        for r in sorted(self.announce):
            opened = _pr_open_state(db, r)
            if opened is None:
                continue        # asked again next sweep: "open the PR" would be stale advice if one exists
            self.announce.discard(r)
            if not opened:
                bus.publish_agent(db, "session.pr_ready", r)
        for r, ok in now.items():
            if not ok:      # its Inbox item goes: what was checked may no longer be what a PR would carry
                db.conn.execute("DELETE FROM session_pr_prep WHERE root_id = ?", (r,))
                continue
            try:
                # Preparation has its own pending/in-flight/backoff state;
                # cached readiness must not hide a newly eligible preparation.
                routes_actions.prepare_pr(db, r)
            except Exception:  # noqa: BLE001 - one session's PR never stops the others'
                log.exception("brindle: couldn't prepare %s's PR", r)
        return flipped

    def sweep_safely(self) -> None:
        db = DB()
        try:
            self.sweep(db)
        except Exception:  # noqa: BLE001 - a missed notice never stops the daemon
            log.exception("brindle: the PR readiness sweep failed")
        finally:
            db.conn.close()
