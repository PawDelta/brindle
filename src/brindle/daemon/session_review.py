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
other direct commit the supervisor's. A hunk's added lines are blamed; a hunk
that only deletes takes its file's authors."""

from __future__ import annotations

import logging
import re
from typing import Any

from brindle import agents, autopilot, bus, git, knowledge, workspaces
from brindle.config import load_repo_config
from brindle.db import DB, Agent, Workspace
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)

EDIT_MESSAGE = "review edits"       # a review edit's commit: "review edits: <path>"
MERGED_RE = re.compile(r"^Merge branch '([^']+)'")
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
            if due := autopilot.audit_due(ap, load_repo_config(ws.repo_root), head):
                out.append(AUDIT_REASONS[due])
    elif ap.state != "done":
        # Reaching the goal clears it; "done" is what is left of it.
        out.append("the session has no goal reached yet")
    if live := autopilot.active_workers(db, root.id):
        out.append(f"{len(live)} worker(s) still running: {', '.join(a.id for a in live)}")
    queued = [t for t in db.list_tasks(state="pending")
              if t.caller_id and autopilot.root_of(db, t.caller_id) == root.id]
    if queued:
        out.append(f"{len(queued)} task(s) queued")
    if ap is not None and ap.state == "blocked":
        out.append(f"the session is waiting on you: {ap.note or 'a question'}")
    if ap is not None and autopilot.proposing(ap):
        out.append("checks autopilot proposed are waiting on your answer")
    if not ws.base_branch or ws.branch in ("", "HEAD"):
        out.append("the session isn't on a branch with a base to open a PR against")
    elif git.dirty_files(ws.path, tracked_only=True):
        out.append("the session's checkout has uncommitted changes")
    elif not git.ahead_behind(ws.path, git.base_ref(ws.path, ws.base_branch))[1]:
        out.append(f"the session's branch has no commits ahead of {ws.base_branch}")
    return out


def require_ready(db: DB, root: Agent, ws: Workspace) -> None:
    if why := reasons(db, root, ws):
        raise NotReady(why)


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


def _owners(db: DB, root: Agent, ws: Workspace, mb: str) -> tuple[dict[str, str], dict[str, dict]]:
    """Each commit since ``mb`` -> its author's id, and those authors."""
    people = {SUPERVISOR: {"id": SUPERVISOR, "kind": SUPERVISOR, "profile": root.profile, "branch": ws.branch},
              YOU: {"id": YOU, "kind": YOU, "profile": None, "branch": None}}
    owner: dict[str, str] = {}
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
        for tip in merged:
            for c in git.out(["rev-list", f"{first}..{tip}"], ws.path).split():
                owner.setdefault(c, key)
    return owner, people


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
    """A file's hunks; ``added``: the new-side numbers of the lines each adds."""
    hunks: list[dict[str, Any]] = []
    at = 0      # the new-side number of the next line
    for line in patch.split("\n"):
        if m := HUNK_RE.match(line):
            old_start, old_lines, new_start, new_lines = (int(g) if g is not None else 1 for g in m.groups())
            hunks.append({"header": line, "old_start": old_start, "old_lines": old_lines,
                          "new_start": new_start, "new_lines": new_lines, "lines": [line], "added": []})
            at = new_start
        elif hunks and line[:1] in (" ", "+", "-", "\\"):
            if line[:1] == "+":
                hunks[-1]["added"].append(at)
            at += line[:1] in (" ", "+")
            hunks[-1]["lines"].append(line)
    return hunks


def _blame(ws: Workspace, mb: str, path: str, lines: list[int], owner: dict[str, str]) -> dict[int, str]:
    """New-side line -> its author, for ``lines`` of ``path`` at HEAD."""
    runs: list[list[int]] = []
    for n in lines:
        if runs and runs[-1][1] == n - 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    ranges = [arg for a, b in runs for arg in ("-L", f"{a},{b}")]
    text = git.run(["blame", "--line-porcelain", *ranges, f"{mb}..HEAD", "--", path], ws.path).stdout
    return {int(m.group(2)): owner.get(m.group(1), SUPERVISOR)
            for m in map(BLAME_RE.match, text.splitlines()) if m}


def diff(db: DB, root: Agent, ws: Workspace) -> dict[str, Any]:
    """The session branch against its PR base, attributed."""
    base = workspaces.require_base(ws)
    mb = git.merge_base(ws.path, git.base_ref(ws.path, base))
    owner, people = _owners(db, root, ws, mb)
    by_file = _file_authors(ws, mb, owner)
    opts = ["-c", "core.quotePath=false", "diff", "-M", "--no-color", "--no-ext-diff"]
    names = git.run([*opts, "-z", "--name-status", mb, "HEAD"], ws.path).stdout.split("\0")
    patches = re.split(r"^(?=diff --git )", git.run([*opts, "--src-prefix=a/", "--dst-prefix=b/", "--patch", mb,
                                                     "HEAD"], ws.path).stdout, flags=re.M)[1:]
    files, seen = [], []
    i = 0
    for patch in patches:
        status = names[i]
        old_path, path = (names[i + 1], names[i + 2]) if status[:1] in ("R", "C") else (None, names[i + 1])
        i += 3 if old_path else 2
        hunks = _hunks(patch)
        added = [n for h in hunks for n in h["added"]]
        blamed = _blame(ws, mb, path, added, owner) if added else {}
        fallback = by_file.get(path) or [SUPERVISOR]
        for h in hunks:
            h["authors"] = list(dict.fromkeys(blamed.get(n, SUPERVISOR) for n in h["added"])) or fallback
            h["text"] = "\n".join(h.pop("lines"))
            del h["added"]
        authors = list(dict.fromkeys(a for h in hunks for a in h["authors"])) or fallback
        seen += authors
        files.append({"path": path, "old_path": old_path, "status": status[:1],
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
    ``session.pr_ready`` when one becomes ready. The first sweep only looks."""

    def __init__(self) -> None:
        self.ready: dict[str, bool] | None = None

    def sweep(self, db: DB) -> list[str]:
        """The sessions that just became ready (each announced on the bus)."""
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
        return flipped

    def sweep_safely(self) -> None:
        db = DB()
        try:
            self.sweep(db)
        except Exception:  # noqa: BLE001 - a missed notice never stops the daemon
            log.exception("brindle: the PR readiness sweep failed")
        finally:
            db.conn.close()
