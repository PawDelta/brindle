"""Keep a session branch synced with the repo's default branch on origin
(main), so a session never drifts far from it.

A session's checkout is on a branch other than the default one, cut from
it. At these moments brindle fetches ``origin/<default>`` and, when it has
commits the session lacks, merges it in (a merge commit; never a rebase or
a force):

- a worker's branch merging into the session branch (pipeline._merge);
- a session PR merging on the forge (session_review.poll_merged);
- a PR opening or being pushed from the checkout (gates.pr_checked);
- the session starting or resuming (service.sessions).

A clean merge is reported to the supervisor, and the checks run on the files
it brought in (a detached helper, ``brindle _session-sync-checks``). A
checkout with uncommitted changes is left alone and the supervisor told. A
conflicting merge is aborted, leaving the checkout as it was: the files go to the
supervisor and the person's inbox, and merges into the session branch are
refused (``merge_block``) until the branch contains that origin commit, but
for a branch that already does (a worker resolving the conflict).

A session PR the forge squashed is recorded (``record_squash``) with the
session commit it carried. A later sync merges the squash with ``-s ours``
once git confirms it holds exactly that commit's changes, so the session's
later edits of the same lines don't conflict with their own earlier version.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import ExitStack
from dataclasses import dataclass, field

from brindle import agents, git, spawn
from brindle.config import load_repo_config
from brindle.db import DB, Workspace

log = logging.getLogger(__name__)

FETCH_SECONDS = 30.0


@dataclass
class Drift:
    ref: str                 # origin/<default>
    sha: str                 # what it points at
    behind: int              # its commits the session lacks
    overlap: list[str]       # files both sides changed since they parted


@dataclass
class Outcome:
    state: str               # up_to_date | synced | behind | conflict | busy
    text: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    files: list[str] = field(default_factory=list)


def default_branch(repo_root: str) -> str:
    return load_repo_config(repo_root).base_branch or git.default_branch(repo_root)


def upstream(ws: Workspace) -> str | None:
    """The default branch the checkout ``ws`` keeps up with: when it is on
    another branch cut from it, and origin exists. None otherwise (on the
    default branch itself, detached, a worker's branch of a session branch)."""
    if not ws.branch or ws.branch == "HEAD":
        return None
    try:
        if not git.has_remote(ws.repo_root):
            return None
        default = default_branch(ws.repo_root)
    except git.GitError:
        return None
    if ws.branch == default or ws.base_branch not in (None, default):
        return None
    return default


def fetch(ws: Workspace, default: str) -> None:
    """Bring ``origin/<default>`` up to date; offline, it stays as it was."""
    try:
        git.run(["fetch", "--quiet", "--no-tags", "origin",
                 f"+refs/heads/{default}:refs/remotes/origin/{default}"],
                ws.repo_root, check=False, timeout=FETCH_SECONDS)
    except git.GitError:
        pass


def drift(ws: Workspace, default: str) -> Drift | None:
    """How far ``ws``'s HEAD is behind ``origin/<default>`` as last fetched."""
    if not git.remote_branch_exists(ws.path, default):
        return None
    ref = f"origin/{default}"
    sha = git.out(["rev-parse", ref], ws.path)
    behind = int(git.out(["rev-list", "--count", f"HEAD..{ref}"], ws.path))
    if not behind:
        return Drift(ref, sha, 0, [])
    mb = git.out(["merge-base", "HEAD", ref], ws.path)
    theirs = set(git.paths(["diff", "--name-only", "--no-renames", mb, ref], ws.path))
    mine = set(git.paths(["diff", "--name-only", "--no-renames", mb, "HEAD"], ws.path))
    return Drift(ref, sha, behind, sorted(theirs & mine))


def _both(d: Drift) -> str:
    return f"; files both changed: {', '.join(d.overlap)}" if d.overlap else ""


def _tell(db: DB, ws: Workspace, text: str) -> None:
    """The session's supervisor in ``ws`` (the newest, with several), if one is there yet."""
    roots = [a for a in db.list_agents(ws.id)
             if a.parent_id is None and a.status != "done" and a.dismissed_at is None]
    if roots:
        try:
            agents.send_message(db, roots[-1].id, text)
        except agents.AgentError:
            pass


def _record(db: DB, ws: Workspace, sha: str, state: str, files: list[str] | None = None,
            tell: str | None = None, always: bool = False) -> None:
    """Keep the branch's sync state; ``tell`` the supervisor once per origin
    commit (every time with ``always``)."""
    row = db.conn.execute("SELECT told_sha FROM session_sync WHERE repo_root = ? AND branch = ?",
                          (ws.repo_root, ws.branch)).fetchone()
    told = row["told_sha"] if row else None
    if tell and (always or told != sha):
        _tell(db, ws, tell)
        told = sha
    db.conn.execute(
        "INSERT INTO session_sync (repo_root, branch, upstream_sha, state, files, told_sha, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (repo_root, branch) DO UPDATE SET"
        " upstream_sha = excluded.upstream_sha, state = excluded.state, files = excluded.files,"
        " told_sha = excluded.told_sha, updated_at = excluded.updated_at",
        (ws.repo_root, ws.branch, sha, state, json.dumps(files) if files else None, told, time.time()))


def session_checkout(db: DB, ws: Workspace) -> Workspace | None:
    """The session checkout ``ws`` (a worker's branch) merges into, if its
    base is a session's branch."""
    if not ws.base_branch:
        return None
    for w in db.find_workspaces(ws.repo_root):
        if (w.id != ws.id and w.branch == ws.base_branch
                and any(a.parent_id is None for a in db.list_agents(w.id))):
            return w
    return None


def sync(db: DB, ws: Workspace, *, checks: bool = True) -> Outcome | None:
    """Fetch ``origin/<default>`` and merge it into the session checkout
    ``ws`` (see the module doc). None when ``ws`` keeps up with nothing or
    git failed; ``busy`` while another merge into the branch holds it. With
    ``checks``, a clean merge starts the checks on the files it brought in."""
    default = upstream(ws)
    if default is None:
        return None
    fetch(ws, default)
    from brindle import pipeline

    try:
        with ExitStack() as held:
            held.enter_context(pipeline.base_lock(ws.repo_root, ws.branch, wait=False))
            held.enter_context(git.checkout_lock(ws.path))
            out = _sync_locked(db, ws, default)
    except BlockingIOError:
        return Outcome("busy", f"a merge into {ws.branch} is under way; brindle syncs it with "
                               f"origin/{default} at the next merge")
    except git.GitError:
        log.exception("brindle: couldn't sync %s with origin/%s", ws.branch, default)
        return None
    if out.state == "synced" and checks:
        start_checks(ws, out.old_sha)
    return out


def _sync_locked(db: DB, ws: Workspace, default: str) -> Outcome:
    d = drift(ws, default)
    if d is None:
        return Outcome("up_to_date")
    if not d.behind:
        _record(db, ws, d.sha, "synced")
        return Outcome("up_to_date")
    if git.dirty_files(ws.path, tracked_only=True):
        text = (f"origin/{default} has {d.behind} commit(s) {ws.branch} lacks{_both(d)}. Its checkout has "
                "uncommitted changes, so brindle didn't merge it in: commit them, and brindle syncs "
                f"at the next merge into {ws.branch}.")
        _record(db, ws, d.sha, "behind", tell=text)
        return Outcome("behind", text)
    before = git.out(["rev-parse", "HEAD"], ws.path)
    files = _replay_squashes(db, ws, d.ref)
    if not files:
        _new, files = git.merge_local_base(ws.path, d.ref)
    if files:
        text = (f"origin/{default} has {d.behind} commit(s) {ws.branch} lacks, and merging it conflicts in: "
                f"{', '.join(files)}. Merges into {ws.branch} are refused until it is synced: have a worker "
                f"merge origin/{default} into its branch and resolve them (a branch that contains "
                f"{d.sha[:8]} still merges), or resolve it in {ws.path}.")
        _record(db, ws, d.sha, "conflict", files, tell=text)
        return Outcome("conflict", text, before, files=files)
    new = git.out(["rev-parse", "HEAD"], ws.path)
    text = (f"brindle merged origin/{default} into {ws.branch} ({d.behind} new commit(s), "
            f"now at {new[:8]}){_both(d)}. The checks run on the files it brought in; "
            "their result arrives as a message.")
    _record(db, ws, d.sha, "synced", tell=text, always=True)
    return Outcome("synced", text, before, new)


# -- squashes -------------------------------------------------------------------


def record_squash(db: DB, ws: Workspace, squash_sha: str, covers_sha: str) -> bool:
    """Record that ``squash_sha`` on the default branch carries the session's
    ``covers_sha`` (a PR the forge squashed or rebased). A merge commit,
    which already has ``covers_sha`` as an ancestor, needs nothing."""
    if (not git.ok(["cat-file", "-e", f"{squash_sha}^{{commit}}"], ws.path)
            or git.ok(["merge-base", "--is-ancestor", covers_sha, squash_sha], ws.path)):
        return False
    db.conn.execute("INSERT OR REPLACE INTO session_squashes (repo_root, branch, squash_sha, covers_sha,"
                    " recorded_at) VALUES (?, ?, ?, ?, ?)",
                    (ws.repo_root, ws.branch, squash_sha, covers_sha, time.time()))
    return True


def _replay_squashes(db: DB, ws: Workspace, ref: str) -> list[str]:
    """Merge each recorded squash on ``ref`` the branch lacks as already
    merged: its parent the usual way, then the squash itself with ``-s ours``,
    but only when the squash is exactly the covered commit's changes (anything
    else, e.g. edits made on the forge, is left to the plain merge). The
    conflicting files of a parent's merge, else []."""
    rows = db.conn.execute("SELECT squash_sha, covers_sha FROM session_squashes WHERE repo_root = ?"
                           " AND branch = ? ORDER BY recorded_at", (ws.repo_root, ws.branch)).fetchall()
    for row in rows:
        squash, covers = row["squash_sha"], row["covers_sha"]
        if (not git.ok(["merge-base", "--is-ancestor", squash, ref], ws.path)
                or git.ok(["merge-base", "--is-ancestor", squash, "HEAD"], ws.path)
                or not git.ok(["merge-base", "--is-ancestor", covers, "HEAD"], ws.path)):
            continue
        merged = git.run(["merge-tree", "--write-tree", f"{squash}^1", covers], ws.path, check=False)
        if merged.returncode != 0 or merged.stdout.split()[:1] != [git.out(["rev-parse", f"{squash}^{{tree}}"], ws.path)]:
            continue
        _new, files = git.merge_local_base(ws.path, f"{squash}^1")
        if files:
            return files
        git.run(["merge", "-s", "ours", "--no-edit", "-m",
                 f"Record squash {squash[:8]} of {ws.branch} at {covers[:8]} as merged", squash], ws.path)
    return []


# -- what it blocks ----------------------------------------------------------------


def conflict(db: DB, repo_root: str, branch: str) -> tuple[str, list[str]] | None:
    """(origin commit, files) while ``branch``'s last sync conflicted and it
    still lacks that commit; a branch that has since got it is synced."""
    row = db.conn.execute("SELECT upstream_sha, files FROM session_sync WHERE repo_root = ? AND branch = ?"
                          " AND state = 'conflict'", (repo_root, branch)).fetchone()
    if row is None:
        return None
    if git.ok(["merge-base", "--is-ancestor", row["upstream_sha"], f"refs/heads/{branch}"], repo_root):
        db.conn.execute("UPDATE session_sync SET state = 'synced', files = NULL WHERE repo_root = ?"
                        " AND branch = ?", (repo_root, branch))
        return None
    return row["upstream_sha"], json.loads(row["files"] or "[]")


def merge_block(db: DB, ws: Workspace) -> str | None:
    """Why ``ws``'s branch can't merge into its base yet: the base is a
    session branch whose sync with the default branch conflicted. A branch
    that already contains that commit (a worker resolving it) merges."""
    if not ws.base_branch or (got := conflict(db, ws.repo_root, ws.base_branch)) is None:
        return None
    sha, files = got
    if git.ok(["merge-base", "--is-ancestor", sha, "HEAD"], ws.path):
        return None
    default = default_branch(ws.repo_root)
    return (f"{ws.base_branch} must be synced with origin/{default} first: merging it conflicts in "
            f"{', '.join(files) or '?'}. Merge origin/{default} ({sha[:8]}) into {ws.branch} and resolve "
            "(a branch that contains it merges), or resolve it in the session's checkout.")


def needs_you(db: DB, ws: Workspace) -> str | None:
    """The session checkout ``ws``'s unresolved sync conflict, for the inbox."""
    if not ws.branch or (got := conflict(db, ws.repo_root, ws.branch)) is None:
        return None
    return (f"{ws.branch} conflicts with origin/{default_branch(ws.repo_root)} in: {', '.join(got[1]) or '?'}; "
            "merges into it are blocked until it's synced")


def pr_block(ws: Workspace) -> str | None:
    """Why ``ws``'s branch can't go to a PR yet: ``origin/<default>`` (as last
    fetched) has commits it lacks."""
    default = upstream(ws)
    if default is None:
        return None
    try:
        d = drift(ws, default)
    except git.GitError:
        return None
    if d is None or not d.behind:
        return None
    return (f"origin/{default} has {d.behind} commit(s) {ws.branch} lacks{_both(d)}; "
            f"merge origin/{default} into {ws.branch} before its PR")


# -- events ------------------------------------------------------------------------


def on_pr_merged(db: DB, ws: Workspace, url: str, pushed_sha: str | None) -> Outcome | None:
    """A session PR from ``ws`` merged on the forge: record a squash, then sync."""
    from brindle import workspaces

    default = upstream(ws)
    if default is None:
        return None
    fetch(ws, default)
    landed = workspaces.pull_request_merge_commit(url, ws.path)
    if landed and pushed_sha:
        record_squash(db, ws, landed, pushed_sha)
    return sync(db, ws)


def on_session_start(db: DB, ws: Workspace) -> str | None:
    """A session starting or resuming in ``ws``: pull the default branch in
    before any worker starts. What happened, for the person, or None."""
    try:
        out = sync(db, ws)
    except Exception:  # noqa: BLE001 - a session always starts
        log.exception("brindle: syncing %s at start failed", ws.branch)
        return None
    return out.text if out else None


# -- checks after a sync ------------------------------------------------------------


def _detach(args: list[str]) -> None:
    spawn.detach(args)


def start_checks(ws: Workspace, old_sha: str | None) -> None:
    from brindle.providers import brindle_invocation

    if old_sha:
        _detach([*brindle_invocation(), "_session-sync-checks", ws.id, old_sha])


def run_checks(db: DB, ws_id: str, old_sha: str) -> str | None:
    """Run the session checkout's checks on the files a sync from
    ``old_sha`` brought in (a pytest check narrowed to the tests they reach,
    any other check in full), and tell the supervisor how they went."""
    from brindle import autopilot, check_scope, gates, workspaces

    ws = db.get_workspace(ws_id)
    if ws is None:
        return None
    cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    if not cfg.checks:
        return None
    files = git.paths(["diff", "--name-only", "--no-renames", old_sha, "HEAD"], ws.path)
    env = workspaces.workspace_env(ws)
    failed: list[str] = []
    for cmd in cfg.checks:
        run = check_scope.command(ws, cmd, check_scope.inferred(cmd, cfg.scoped_checks), (old_sha, files))
        if run is None:
            continue
        ok, output = gates.run_checked(db, ws, run, env, cfg.check_timeout, full=True)
        if not ok:
            failed.append(f"{cmd}\n{autopilot.tail(output)}")
    if failed:
        text = (f"The checks failed after brindle merged origin into {ws.branch}:\n\n" + "\n\n".join(failed)
                + "\n\nFix them on the session branch (or have a worker do it).")
    else:
        text = f"The checks passed after brindle merged origin into {ws.branch} ({len(files)} file(s) brought in)."
    _tell(db, ws, text)
    return text
