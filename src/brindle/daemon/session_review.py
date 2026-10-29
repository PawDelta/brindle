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
neither (a mode change, a binary file) takes its file's authors."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from brindle import agents, autopilot, bus, git, knowledge, pipeline, tasks, workspaces
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
    if not git.ok(["merge-base", "--is-ancestor", sha, "HEAD"], ws.path):
        return False
    recorded = {r["sha"] for r in db.conn.execute(
        "SELECT sha FROM review_edits WHERE workspace_id = ?", (ws.id,))}
    return set(git.out(["rev-list", f"{sha}..HEAD"], ws.path).split()) <= recorded


def reasons(db: DB, root: Agent, ws: Workspace) -> list[str]:
    """What keeps the session from being ready for a PR; none when it is."""
    out: list[str] = []
    ap = db.get_autopilot(root.id)
    ms = db.milestones(root.id)
    if ap is None:
        out.append("autopilot isn't on for this session, so there is no goal to judge it by")
    elif ms:
        out += [f"milestone {m.position} ({m.title}) hasn't passed its check"
                for m in ms if m.status != "passed"]
        if not out:
            head = None if git.dirty_files(ws.path, tracked_only=True) else git.out(["rev-parse", "HEAD"], ws.path)
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
    elif git.dirty_files(ws.path, tracked_only=True):
        out.append("the session's checkout has uncommitted changes")
    elif not git.ahead_behind(ws.path, git.base_ref(ws.path, ws.base_branch))[1]:
        out.append(f"the session's branch has no commits ahead of {ws.base_branch}")
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
        out.append(f"the session is waiting on you: {ap.note or 'a question'}")
    if ap is not None and autopilot.proposing(ap):
        out.append("checks autopilot proposed are waiting on your answer")
    if (login := db.get_login_state(root.id)) and login.state == "auth_paused":
        out.append("the session's cloud login has expired")
    return out


def has_pr_target(ws: Workspace) -> bool:
    """Whether the session's branch could be opened as a PR: it is on a branch
    with a base and has commits ahead of it (unlike ``reasons``, whatever
    only temporarily blocks a PR doesn't count)."""
    return bool(ws.base_branch) and ws.branch not in ("", "HEAD") and bool(
        git.ahead_behind(ws.path, git.base_ref(ws.path, ws.base_branch))[1])


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
    pr = db.conn.execute("SELECT url, merged_at FROM session_prs WHERE root_id = ? AND url IS NOT NULL"
                         " ORDER BY opened_at DESC LIMIT 1", (root_id,)).fetchone()
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


MERGE_POLL_SECONDS = 60.0
_merge_polled = 0.0


def poll_merged(db: DB) -> None:
    """Record, at most once a minute, the open session PRs ``gh`` reports merged."""
    global _merge_polled
    if time.monotonic() - _merge_polled < MERGE_POLL_SECONDS:
        return
    _merge_polled = time.monotonic()
    for row in db.conn.execute("SELECT root_id, branch, url FROM session_prs"
                               " WHERE url IS NOT NULL AND merged_at IS NULL").fetchall():
        root = db.get_agent(row["root_id"])
        ws = db.get_workspace(root.workspace_id) if root else None
        if ws is not None and workspaces.pull_request_merged(row["url"], ws.repo_root):
            # Only the PR that was asked about: a replacement recorded meanwhile has its own state.
            if db.conn.execute("UPDATE session_prs SET merged_at = ? WHERE root_id = ? AND branch = ?"
                               " AND url = ?", (time.time(), row["root_id"], row["branch"],
                                                row["url"])).rowcount:
                bus.publish_agent(db, "session.pr_merged", row["root_id"], url=row["url"])


def require_ready(db: DB, root: Agent, ws: Workspace) -> None:
    if why := reasons(db, root, ws):
        raise NotReady(why)


def open_pr(db: DB, root: Agent, ws: Workspace) -> dict[str, Any] | None:
    """The PR the person opened for the session's branch: its ``url`` and
    ``unpushed``, how many session commits aren't on it yet (brindle never
    pushes them on its own: the person's Push does, POST .../pr/push)."""
    row = db.conn.execute("SELECT url, pushed_sha FROM session_prs WHERE root_id = ? AND branch = ?"
                          " AND url IS NOT NULL", (root.id, ws.branch)).fetchone()
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


def _owners(db: DB, root: Agent, ws: Workspace,
            mb: str) -> tuple[dict[str, str], dict[str, dict], list[tuple[str, str]]]:
    """Each commit since ``mb`` -> its author's id, those authors, and the
    first-parent history (each commit and its first parent, oldest first)."""
    people = {SUPERVISOR: {"id": SUPERVISOR, "kind": SUPERVISOR, "profile": root.profile, "branch": ws.branch},
              YOU: {"id": YOU, "kind": YOU, "profile": None, "branch": None}}
    owner: dict[str, str] = {}
    chain: list[tuple[str, str]] = []
    log_text = git.run(["log", "--first-parent", "--format=%H%x00%P%x00%s", f"{mb}..HEAD"], ws.path).stdout
    for line in log_text.splitlines():
        sha, parents, subject = line.split("\0", 2)
        first, *merged = parents.split()
        m = (MERGED_RE if merged else SQUASHED_RE).match(subject)
        if m:
            who = _worker(db, ws, m.group(1))
            people.setdefault(who["id"], who)
            key = who["id"]
        else:
            key = YOU if subject.startswith(f"{EDIT_MESSAGE}: ") else SUPERVISOR
        owner[sha] = key
        chain.insert(0, (sha, first))
        for tip in merged:
            for c in git.out(["rev-list", f"{first}..{tip}"], ws.path).split():
                owner.setdefault(c, key)
    return owner, people, chain


def _file_authors(ws: Workspace, mb: str, owner: dict[str, str]) -> dict[str, list[str]]:
    """Path -> the authors of the commits since ``mb`` that touched it."""
    text = git.run(["-c", "core.quotePath=false", "log", "--format=%x01%H", "--name-only", f"{mb}..HEAD"],
                   ws.path).stdout
    found: dict[str, list[str]] = {}
    for chunk in text.split("\x01")[1:]:
        sha, *files = chunk.split("\n")
        for f in filter(None, files):
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


def _blame(ws: Workspace, mb: str, path: str, lines: list[int], owner: dict[str, str]) -> dict[int, str]:
    """New-side line -> its author, for ``lines`` of ``path`` at HEAD."""
    text = git.run(["blame", "--line-porcelain", *_ranges(lines), f"{mb}..HEAD", "--", path], ws.path).stdout
    return {int(m.group(2)): owner.get(m.group(1), SUPERVISOR)
            for m in map(BLAME_RE.match, text.splitlines()) if m}


def _file_diffs(ws: Workspace, a: str, b: str, *opts: str) -> list[tuple[str, str | None, str, str]]:
    """``a``..``b`` per file: (status letter, old path if renamed, path, its patch)."""
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


def _deleters(ws: Workspace, chain: list[tuple[str, str]], wanted: dict[str, list[int]],
              owner: dict[str, str]) -> dict[str, dict[int, str]]:
    """Base path -> its deleted base lines -> who deleted each. Each line is
    carried along the session's first-parent history (``chain``: each commit
    and its first parent, oldest first) and goes to the commit whose diff
    from its first parent removes it: a merge's for a merged worker's
    deletion, never a diff against a worker's branch."""
    at = {path: {n: n for n in lines} for path, lines in wanted.items()}     # base line -> its number now
    named = {path: path for path in wanted}                                  # its name now -> base path
    found: dict[str, dict[int, str]] = {path: {} for path in wanted}
    for sha, first in chain:
        if not named:
            break
        for _, old_path, path, patch in _file_diffs(ws, first, sha, "-U0"):
            base = named.pop(old_path or path, None)
            if base is None:
                continue
            hunks = [(h["old_start"], h["old_lines"], h["new_lines"]) for h in _hunks(patch)]
            moved = {}
            for line, n in at[base].items():
                if any(start <= n < start + count for start, count, _ in hunks if count):
                    found[base][line] = owner.get(sha, SUPERVISOR)
                else:   # shifted by every hunk before it (an insertion lands after its old line)
                    moved[line] = n + sum(new - count for start, count, new in hunks
                                          if (start + count <= n if count else start < n))
            at[base] = moved
            if moved:
                named[path] = base
    return found


def diff(db: DB, root: Agent, ws: Workspace) -> dict[str, Any]:
    """The session branch against its PR base, attributed."""
    base = workspaces.require_base(ws)
    mb = git.merge_base(ws.path, git.base_ref(ws.path, base))
    owner, people, chain = _owners(db, root, ws, mb)
    by_file = _file_authors(ws, mb, owner)
    changed = [(path, old_path, status, patch, _hunks(patch))
               for status, old_path, path, patch in _file_diffs(ws, mb, "HEAD")]
    deleters = _deleters(ws, chain, {old_path or path: [n for h in hunks for n in h["deleted"]]
                                     for path, old_path, _, _, hunks in changed
                                     if any(h["deleted"] for h in hunks)}, owner)
    files, seen = [], []
    for path, old_path, status, patch, hunks in changed:
        added = [n for h in hunks for n in h["added"]]
        blamed = _blame(ws, mb, path, added, owner) if added else {}
        unblamed = deleters.get(old_path or path, {})
        fallback = by_file.get(path) or [SUPERVISOR]
        for h in hunks:
            who = [blamed.get(n, SUPERVISOR) for n in h["added"]] + [unblamed.get(n, SUPERVISOR) for n in h["deleted"]]
            h["authors"] = list(dict.fromkeys(who)) or fallback
            h["text"] = "\n".join(h.pop("lines"))
            del h["added"], h["deleted"]
        authors = list(dict.fromkeys(a for h in hunks for a in h["authors"])) or fallback
        seen += authors
        files.append({"path": path, "old_path": old_path, "status": status,
                      "binary": "\nBinary files " in patch or patch.startswith("Binary files "),
                      "authors": authors, "hunks": hunks})
    return {"root_id": root.id, "branch": ws.branch, "base": base, "base_sha": mb,
            "head": git.out(["rev-parse", "HEAD"], ws.path), "files": files,
            "workers": [people[k] for k in dict.fromkeys(seen)]}


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
    push), and forgets what it prepared for one no longer ready. brindle
    never opens or pushes a PR itself. The first sweep only looks."""

    def __init__(self) -> None:
        self.ready: dict[str, bool] | None = None

    def sweep(self, db: DB) -> list[str]:
        """The sessions that just became ready (each announced on the bus)."""
        poll_merged(db)
        now: dict[str, bool] = {}
        for (root_id,) in db.conn.execute("SELECT root_id FROM autopilot").fetchall():
            ap, ms = db.get_autopilot(root_id), db.milestones(root_id)
            # Only a reached goal (or one whose every milestone passes) can be ready: skip the git work otherwise.
            if ap is None or not (ap.state == "done" or (ms and all(m.status == "passed" for m in ms))):
                now[root_id] = False
                continue
            try:
                now[root_id] = not reasons(db, *session(db, root_id))
            except (ServiceError, git.GitError, workspaces.WorkspaceError):
                now[root_id] = False
        was, self.ready = self.ready, now
        if was is None:
            return []
        flipped = [r for r, ok in now.items() if ok and not was.get(r)]
        for r in flipped:
            bus.publish_agent(db, "session.pr_ready", r)
        from brindle.daemon import routes_actions

        for r, ok in now.items():
            if not ok:      # its Inbox item goes: what was checked may no longer be what a PR would carry
                db.conn.execute("DELETE FROM session_pr_prep WHERE root_id = ?", (r,))
                continue
            try:
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
