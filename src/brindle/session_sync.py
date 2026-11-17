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
- the daemon's periodic sweep, while the session is quiet.

A clean merge is reported to the supervisor, and the checks run on the files
it brought in (a detached helper, ``brindle _session-sync-checks``). A
checkout with uncommitted changes is left alone and the supervisor told. A
conflicting merge is aborted, leaving the checkout as it was: the files go to the
supervisor and a queued resolver task. The person's inbox is involved only
if resolution cannot proceed. Merges into the session branch are
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
import re
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field

from brindle import agents, bus, git, spawn
from brindle.config import load_repo_config
from brindle.db import DB, Agent, Task, Workspace

log = logging.getLogger(__name__)

FETCH_SECONDS = 30.0
MAX_LANDING_LOOKUPS = 5      # failed lookups of how a merged PR landed before a sync stops waiting on it
IDLE_FETCH_SECONDS = 300.0
# How long a sweep believes a session's upstream (its repo's remote and default
# branch: two git runs to learn): as long as it waits between fetches.
UPSTREAM_SECONDS = IDLE_FETCH_SECONDS


@dataclass
class Seen:
    """What the daemon's sweeps (every few seconds) remember between rounds,
    so a session whose refs haven't moved starts no git at all: each git run
    is a process the machine starts, and an antivirus inspects, every round."""
    upstreams: dict[str, tuple[float, str | None]] = field(default_factory=dict)   # repo root -> its default
    settled: dict[str, tuple[str, str | None]] = field(default_factory=dict)   # ws id -> refs an idle sync left synced
    drift: dict[str, tuple[tuple, int | None, int]] = field(default_factory=dict)   # ws id -> (refs, oldest, count)

    def upstream(self, ws: Workspace) -> str | None:
        """``upstream(ws)``, its repo's remote and default branch believed for
        UPSTREAM_SECONDS: learned once per repo, however many sessions it has."""
        if not ws.branch or ws.branch == "HEAD":
            return None
        key, now = ws.repo_root, time.monotonic()
        hit = self.upstreams.get(key)
        if hit is None or now - hit[0] >= UPSTREAM_SECONDS:
            hit = self.upstreams[key] = (now, _remote_default(ws.repo_root))
        return None if hit[1] is None or ws.branch == hit[1] else hit[1]


def _refs(ws: Workspace, default: str) -> tuple[str, str | None] | None:
    """The checkout's HEAD and ``origin/<default>``, read from its git files
    without running git; None when they can't be read that way (then the
    caller asks git, every time)."""
    from brindle.daemon.session_review import _fingerprint

    fp = _fingerprint(ws, default)
    return None if fp is None else (fp.head, fp.origin)


@dataclass
class Drift:
    ref: str                 # origin/<default>
    sha: str                 # what it points at
    behind: int              # its commits the session lacks
    overlap: list[str]       # files both sides changed since they parted
    head: str | None = None  # the session's HEAD it was measured from


@dataclass
class Outcome:
    state: str               # up_to_date | synced | behind | conflict | busy
    text: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    # up_to_date: the (HEAD, origin/<default>) found synced; conflict: the ones the merge conflicted at
    checked: tuple[str, str] | None = None
    files: list[str] = field(default_factory=list)


def default_branch(repo_root: str) -> str:
    return load_repo_config(repo_root).base_branch or git.default_branch(repo_root)


def upstream(ws: Workspace) -> str | None:
    """The default branch the session checkout ``ws`` keeps up with: when it
    is on another branch, and origin exists (whatever base the branch was
    once cut from). None otherwise. Callers decide that ``ws`` is a session's
    checkout (``keeps_up``)."""
    if not ws.branch or ws.branch == "HEAD":
        return None
    default = _remote_default(ws.repo_root)
    return None if default is None or ws.branch == default else default


def _remote_default(repo_root: str) -> str | None:
    """The default branch of ``repo_root``, which has a remote; None without one."""
    try:
        if not git.has_remote(repo_root):
            return None
        return default_branch(repo_root)
    except git.GitError:
        return None


def keeps_up(db: DB, ws: Workspace) -> bool:
    """Whether ``ws`` keeps up with the default branch: a session's checkout
    (a root agent runs there), or a branch cut from the default branch.
    Never a worker's branch of a session branch."""
    if any(a.parent_id is None for a in db.list_agents(ws.id)):
        return True
    return ws.base_branch in (None, default_branch(ws.repo_root))


def fetch(repo_root: str, default: str) -> bool:
    """Bring ``origin/<default>`` up to date. False when that failed
    (offline, no credentials a background fetch may use): it stays as it was."""
    return git.background_fetch(repo_root, ["--quiet", "--no-tags", "origin",
                                            f"+refs/heads/{default}:refs/remotes/origin/{default}"], FETCH_SECONDS)


def fresh_start(repo_root: str) -> str:
    """Where a new session branch starts: ``origin/<default>`` as just
    fetched, never the local default branch (stale, or with commits of its
    own) nor a stale fetch. WorkspaceError when origin exists but can't be
    fetched; a repo without origin starts from its local default branch.

    A fetch origin refused for want of credentials is the exception: those are
    never asked for here (git.background), so refusing would close the repo to
    every new session. It starts from ``origin/<default>`` as last fetched, and
    the supervisor hears that it may be behind (tell_refused_fetches)."""
    from brindle import workspaces

    default = default_branch(repo_root)
    if not git.has_remote(repo_root):
        return git.out(["rev-parse", default if git.branch_exists(repo_root, default) else "HEAD"], repo_root)
    fetched = fetch(repo_root, default) or git.fetch_refused(repo_root)
    if not fetched or not git.remote_branch_exists(repo_root, default):
        raise workspaces.WorkspaceError(
            f"couldn't fetch origin/{default}, and a new session branch starts only from it as just "
            "fetched: check the network and your git access to origin, then start the session again")
    return git.out(["rev-parse", f"origin/{default}"], repo_root)


def drift(ws: Workspace, default: str) -> Drift | None:
    """How far ``ws``'s HEAD is behind ``origin/<default>`` as last fetched.
    Both commits are read from the git files where they can say, and what git
    works out for a pair of commits (which never changes) is asked once: a
    session behind origin while its workers are at work is measured on every
    sweep that judges its readiness."""
    if not git.remote_branch_exists(ws.path, default):
        return None
    ref = f"origin/{default}"
    head, sha = git.resolve(ws.path, "HEAD", f"refs/remotes/{ref}") or git.out(
        ["rev-parse", "HEAD", ref], ws.path).split()
    behind = int(git.out_once(["rev-list", "--count", f"{head}..{sha}"], ws.path))
    if not behind:
        return Drift(ref, sha, 0, [], head)
    mb = git.out_once(["merge-base", head, sha], ws.path)
    theirs = set(_changed(ws, mb, sha))
    mine = set(_changed(ws, mb, head))
    return Drift(ref, sha, behind, sorted(theirs & mine), head)


def _changed(ws: Workspace, old: str, new: str) -> list[str]:
    """The files that differ between commits ``old`` and ``new`` (shas)."""
    listed = git.out_once(["diff", "--name-only", "--no-renames", "-z", old, new], ws.path, raw=True)
    return [p for p in listed.split("\0") if p]


def _both(d: Drift) -> str:
    return f"; files both changed: {', '.join(d.overlap)}" if d.overlap else ""


def refresh_drift_flags(db: DB, seen: Seen | None = None) -> None:
    """Measure live sessions against origin as last fetched, once per sweep.

    A notice is armed again only after the branch falls below both limits.
    Commit timestamps come from unlanded work, excluding integration merges
    and history carried by verified squash/rebase landings on origin.
    ``seen``: the measurements of earlier sweeps, reused while the session's
    HEAD, origin's branch and its recorded landings are as they were.
    """
    seen = Seen() if seen is None else seen
    rows = db.conn.execute(
        "SELECT DISTINCT w.id FROM workspaces w JOIN agents a ON a.workspace_id = w.id"
        " WHERE a.parent_id IS NULL AND a.status != 'done' AND a.dismissed_at IS NULL").fetchall()
    for row in rows:
        ws = db.get_workspace(row["id"])
        try:
            if (default := seen.upstream(ws)) is None:
                continue
            refs = _refs(ws, default)
            if refs is not None:
                if refs[1] is None:      # no origin/<default>
                    continue
                key = (refs, tuple(tuple(r) for r in db.conn.execute(
                    "SELECT squash_sha, covers_sha FROM session_squashes WHERE repo_root = ? AND branch = ?"
                    " ORDER BY recorded_at", (ws.repo_root, ws.branch))))
                last = seen.drift.get(ws.id)
                measured = _record_drift(db, ws, default, last[1:] if last and last[0] == key else None)
                seen.drift[ws.id] = (key, *measured)
            elif git.remote_branch_exists(ws.path, default):
                _record_drift(db, ws, default)
        except git.GitError:
            log.debug("brindle: couldn't measure drift for %s", ws.branch, exc_info=True)
    for gone in set(seen.drift) - {row["id"] for row in rows}:
        del seen.drift[gone]


def refresh_idle_sessions(db: DB, fetched_at: dict[str, float], seen: Seen | None = None) -> None:
    """Refresh origin once per repo every few minutes, then sync quiet sessions.

    The daemon watch owns the fetch clock; a busy checkout can become quiet
    on the next sweep without waiting for another fetch. ``seen``: a session
    an earlier sweep found synced is left alone, with no git run, while
    neither its HEAD nor ``origin/<default>`` has moved.
    """
    seen = Seen() if seen is None else seen
    rows = db.conn.execute(
        "SELECT DISTINCT w.id FROM workspaces w JOIN agents a ON a.workspace_id = w.id"
        " WHERE w.base_branch IS NOT NULL AND a.parent_id IS NULL"
        " AND a.status != 'done' AND a.dismissed_at IS NULL"
        " UNION SELECT w.id FROM workspaces w JOIN session_repos r ON r.repo_root = w.repo_root"
        " JOIN session_repo_branches b ON b.root_id = r.root_id AND b.alias = r.alias AND b.branch = w.branch"
        " JOIN agents a ON a.id = r.root_id"
        " WHERE w.base_branch IS NOT NULL AND a.parent_id IS NULL"
        " AND a.status != 'done' AND a.dismissed_at IS NULL").fetchall()
    for row in rows:
        ws = db.get_workspace(row["id"])
        try:
            if (default := seen.upstream(ws)) is None:
                continue
            now = time.monotonic()
            last = fetched_at.get(ws.repo_root)
            if last is None or now - last >= IDLE_FETCH_SECONDS:
                fetched_at[ws.repo_root] = now
                fetch(ws.repo_root, default)
            refs = _refs(ws, default)
            if refs is not None and (refs[1] is None or seen.settled.get(ws.id) == refs):
                continue    # no origin/<default> to sync from, or synced with it as it is
            seen.settled.pop(ws.id, None)
            # Only the refs the sync itself found synced, or found conflicting (a merge
            # tried again at the same refs conflicts again), are remembered (a merge it
            # made is found synced on the next sweep): never a later look, which another
            # fetch or commit may have moved past what was checked. Quietness is judged
            # from the cached status (no git while the checkout is unchanged); the sync
            # judges it again with git, under its locks, before it merges anything.
            if _quiet(db, ws, cached=True) and (out := sync(db, ws, idle=True)) is not None and out.checked:
                seen.settled[ws.id] = out.checked
        except git.GitError:
            log.debug("brindle: couldn't sync idle session %s", ws.branch, exc_info=True)
    for gone in set(seen.settled) - {row["id"] for row in rows}:
        del seen.settled[gone]


def _quiet(db: DB, ws: Workspace, *, cached: bool = False) -> bool:
    """Read current activity, merge queue and checkout state, never a snapshot.
    ``cached``: the checkout's state from ``status_cache`` (git only when its
    files moved), for the sweep's look before a sync; without it, git's."""
    from brindle import autopilot, pipeline, status_cache

    current = db.get_workspace(ws.id)
    if current is None or current != ws or not current.base_branch:
        return False
    roots = _supervisors(db, ws)
    if not roots or any(a.status in autopilot.BUSY for a in roots):
        return False
    if pipeline.ready_branches(db, ws.repo_root, ws.branch):
        return False
    for root in roots:
        if any(a.dismissed_at is None and a.status not in ("paused", "done") and agents.is_alive(a)
               for a in agents.tree(db, root.id)[1:]):
            return False
    if cached:      # brindle's own claimed goals.md isn't the person's work (git.CLAIMED_SEED)
        return not [p for p in status_cache.cached_status(ws.path, ws.base_branch).dirty_files
                    if p != git.CLAIMED_SEED]
    return not git.dirty_files(ws.path)


def _record_drift(db: DB, ws: Workspace, default: str,
                  measured: tuple[int | None, int] | None = None) -> tuple[int | None, int]:
    """Record whether ``ws`` drifts from ``origin/<default>``: its oldest
    unlanded commit's time and how many files differ, measured with git
    unless ``measured`` (an earlier sweep's, for the same refs) is given.
    Whether that age is too old is judged now, every time. Returns them."""
    ref = f"origin/{default}"
    if measured is None:
        covered = [f"^{covers}" for _squash, covers in _verified_squashes(db, ws, ref)]
        stamps = git.out(["log", "--no-merges", "--format=%ct", f"{ref}..HEAD", *covered], ws.path).splitlines()
        measured = (min(map(int, stamps)) if stamps else None,
                    len(git.paths(["diff", "--name-only", "--no-renames", ref, "HEAD"], ws.path)))
    oldest, count = measured
    drifting = (oldest is not None and time.time() - oldest > 48 * 3600) or count > 60
    row = db.conn.execute("SELECT oldest_at, file_count, drifting FROM session_drift"
                          " WHERE repo_root = ? AND branch = ?", (ws.repo_root, ws.branch)).fetchone()
    if row is None or (row["oldest_at"], row["file_count"], bool(row["drifting"])) != (oldest, count, drifting):
        db.conn.execute(
            "INSERT INTO session_drift (repo_root, branch, oldest_at, file_count, drifting) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (repo_root, branch) DO UPDATE SET oldest_at = excluded.oldest_at,"
            " file_count = excluded.file_count, drifting = excluded.drifting",
            (ws.repo_root, ws.branch, oldest, count, drifting))
    if bool(row and row["drifting"]) != drifting:
        for root in db.list_agents(ws.id):
            if root.parent_id is None:
                bus.publish_agent(db, "session.pr_drift", root.id, drifting=drifting)
    if drifting and not (row and row["drifting"]):
        age = (max(0, time.time() - oldest) / 3600) if oldest is not None else 0
        _tell(db, ws, f"{ws.branch} is drifting from {ref}: its oldest unlanded commit is "
              f"{age:.1f} hours old and {count} files differ. Land its PR to keep the session close to main.")
    return measured


def drifting(db: DB, root_id: str) -> bool:
    """The persisted drift flag for this session's current branch."""
    return db.conn.execute(
        "SELECT 1 FROM agents a JOIN workspaces w ON w.id = a.workspace_id"
        " JOIN session_drift d ON d.repo_root = w.repo_root AND d.branch = w.branch"
        " WHERE a.id = ? AND d.drifting = 1", (root_id,)).fetchone() is not None


def _supervisors(db: DB, ws: Workspace) -> list[Agent]:
    """Live session owners, including supervisors in another attached repo."""
    roots = {a.id: a for a in db.list_agents(ws.id) if a.parent_id is None}
    for attached in db.session_repos():
        if attached.repo_root == ws.repo_root and attached.branch == ws.branch:
            if root := db.get_agent(attached.root_id):
                roots[root.id] = root
    return sorted((a for a in roots.values()
                   if a.parent_id is None and a.status != "done" and a.dismissed_at is None),
                  key=lambda a: a.created_at)


def _tell(db: DB, ws: Workspace, text: str) -> None:
    """Tell the session's newest supervisor, including an attached checkout's owner."""
    roots = _supervisors(db, ws)
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


def tell_refused_fetches(db: DB) -> None:
    """Each live session's supervisor hears once that a repo it works in no
    longer refreshes on its own (git.fetch_refused), and again only if that
    starts over after origin answered: a poll's refusals add nothing."""
    rows = db.conn.execute(
        "SELECT a.id AS root_id, w.repo_root AS repo_root FROM agents a JOIN workspaces w ON w.id = a.workspace_id"
        " WHERE a.parent_id IS NULL AND a.status != 'done' AND a.dismissed_at IS NULL"
        " UNION SELECT r.root_id, r.repo_root FROM session_repos r JOIN agents a ON a.id = r.root_id"
        " WHERE a.status != 'done' AND a.dismissed_at IS NULL").fetchall()
    told = {(r["root_id"], r["repo_root"]) for r in db.conn.execute(
        "SELECT root_id, repo_root FROM fetch_refused_notices")}
    for row in rows:
        key = (row["root_id"], row["repo_root"])
        if not git.fetch_refused(row["repo_root"]):
            if key in told:
                db.conn.execute("DELETE FROM fetch_refused_notices WHERE root_id = ? AND repo_root = ?", key)
        elif key not in told:
            try:
                agents.send_message(db, row["root_id"], (
                    f"brindle can't refresh origin in {row['repo_root']} on its own: origin wants credentials, "
                    "and background fetches never ask for them (no keychain dialogs). New branches and syncs "
                    "use origin as last fetched, so they may be behind it. `git fetch origin` refreshes it now "
                    "(run by hand or in a pane, it may ask for credentials). For github.com, connecting GitHub "
                    "to brindle (Settings, or `brindle github connect`) lets brindle refresh it itself with its "
                    "own sign-in. So does an SSH remote with its key in ssh-agent. On macOS, so does gh signed "
                    "in to github.com with "
                    "its token in gh's config file (`gh auth login --insecure-storage`, which keeps it in plain "
                    "text): a token in the keychain isn't used here, as reading it could raise a dialog."))
            except agents.AgentError:
                continue
            db.conn.execute("INSERT INTO fetch_refused_notices (root_id, repo_root, told_at) VALUES (?, ?, ?)",
                            (*key, time.time()))


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


def sync(db: DB, ws: Workspace, *, checks: bool = True, idle: bool = False) -> Outcome | None:
    """Fetch ``origin/<default>`` and merge it into the session checkout
    ``ws`` (see the module doc). None when ``ws`` keeps up with nothing or
    git failed; ``busy`` while another merge into the branch holds it. With
    ``checks``, a clean merge starts the checks on the files it brought in.
    ``idle`` uses the sweep's fetched ref and rechecks quietness under the locks."""
    default = upstream(ws)
    if default is None:
        return None
    if not idle:
        fetch(ws.repo_root, default)
    _land_merged_prs(db, ws)
    if _awaiting_landing(db, ws):
        # A merged PR's landing commit isn't known yet: merging now could
        # conflict with the session's own squashed work. The next event syncs.
        return Outcome("pending", f"waiting to learn how {ws.branch}'s merged PR landed on origin/{default}")
    from brindle import pipeline

    try:
        with ExitStack() as held:
            held.enter_context(pipeline.base_lock(ws.repo_root, ws.branch, wait=False))
            held.enter_context(git.checkout_lock(ws.path))
            out = _sync_locked(db, ws, default, idle=True) if idle else _sync_locked(db, ws, default)
    except BlockingIOError:
        return Outcome("busy", f"a merge into {ws.branch} is under way; brindle syncs it with "
                               f"origin/{default} at the next merge")
    except git.GitError:
        log.exception("brindle: couldn't sync %s with origin/%s", ws.branch, default)
        return None
    if out.state == "synced" and checks:
        start_checks(ws, out.old_sha)
    return out


def _sync_locked(db: DB, ws: Workspace, default: str, *, idle: bool = False) -> Outcome:
    d = drift(ws, default)
    if d is None:
        return Outcome("up_to_date")
    if not d.behind:
        _record(db, ws, d.sha, "synced")
        return Outcome("up_to_date", checked=(d.head, d.sha) if d.head else None)
    if idle and not _quiet(db, ws):
        return Outcome("busy")
    if git.dirty_files(ws.path, tracked_only=True):
        text = (f"origin/{default} has {d.behind} commit(s) {ws.branch} lacks{_both(d)}. Its checkout has "
                "uncommitted changes, so brindle didn't merge it in: commit them, and brindle syncs "
                f"at the next merge into {ws.branch}.")
        _record(db, ws, d.sha, "behind", tell=text)
        return Outcome("behind", text)
    before = git.out(["rev-parse", "HEAD"], ws.path)
    files = _replay_squashes(db, ws, d.ref)
    if not files:
        _new, files = git.merge_local_base(ws.path, d.ref, no_ff=True)
    if files:
        text = (f"origin/{default} has {d.behind} commit(s) {ws.branch} lacks, and merging it conflicts in: "
                f"{', '.join(files)}. Brindle will try to queue a resolver for when the session is quiet. "
                f"Merges into {ws.branch} are refused until it is synced (a branch that contains "
                f"{d.sha[:8]} still merges), or resolve it in {ws.path}.")
        _record(db, ws, d.sha, "conflict", files, tell=text)
        _queue_resolver(db, ws, d, files)
        bus.publish_ws(db, "conflict", ws, files=files, source="session_sync", branch=ws.branch)
        return Outcome("conflict", text, before, files=files, checked=(d.head, d.sha) if d.head else None)
    new = git.out(["rev-parse", "HEAD"], ws.path)
    text = (f"brindle merged origin/{default} into {ws.branch} ({d.behind} new commit(s), "
            f"now at {new[:8]}){_both(d)}. The checks run on the files it brought in; "
            "their result arrives as a message.")
    _record(db, ws, d.sha, "synced", tell=text, always=not idle)
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


def _verified_squashes(db: DB, ws: Workspace, ref: str) -> Iterator[tuple[str, str]]:
    """Landings on ``ref`` proven to carry the recorded session history.

    A record alone isn't proof: edits on the forge may differ from the
    session's changes. Replay and drift must agree on which history landed.
    """
    rows = db.conn.execute("SELECT squash_sha, covers_sha FROM session_squashes WHERE repo_root = ?"
                           " AND branch = ? ORDER BY recorded_at", (ws.repo_root, ws.branch)).fetchall()
    for row in rows:
        squash, covers = row["squash_sha"], row["covers_sha"]
        if (not git.ok(["merge-base", "--is-ancestor", squash, ref], ws.path)
                or not git.ok(["merge-base", "--is-ancestor", covers, "HEAD"], ws.path)):
            continue
        merged = git.run(["merge-tree", "--write-tree", f"{squash}^1", covers], ws.path, check=False)
        if merged.returncode != 0 or merged.stdout.split()[:1] != [git.out(["rev-parse", f"{squash}^{{tree}}"], ws.path)]:
            continue
        yield squash, covers


def _replay_squashes(db: DB, ws: Workspace, ref: str) -> list[str]:
    """Merge each verified landing the branch lacks: its parent normally,
    then the squash with ``-s ours``. Return conflicting files, else []."""
    for squash, covers in _verified_squashes(db, ws, ref):
        if git.ok(["merge-base", "--is-ancestor", squash, "HEAD"], ws.path):
            continue
        _new, files = git.merge_local_base(ws.path, f"{squash}^1", no_ff=True)
        if files:
            return files
        git.run([*git.identity_args(ws.path), "merge", "-s", "ours", "--no-edit", "-m",
                 f"Record squash {squash[:8]} of {ws.branch} at {covers[:8]} as merged", squash], ws.path)
    return []


# -- what it blocks ----------------------------------------------------------------


def _queue_resolver(db: DB, ws: Workspace, d: Drift, files: list[str]) -> None:
    """Queue one normal, isolated task; retain failed attempts across syncs.

    Called under the session base lock. A supervisor's explicit merge task
    takes precedence over an automatic one for the same conflict.
    """
    from brindle import autopilot, tasks

    key = (ws.repo_root, ws.branch, d.sha)
    if db.conn.execute("SELECT 1 FROM session_sync_resolvers WHERE repo_root=? AND branch=?"
                       " AND upstream_sha=?", key).fetchone():
        return
    roots = _supervisors(db, ws)
    if not roots:
        return
    owners = {a.id for a in roots}
    previous = {r["task_id"] for r in db.conn.execute(
        "SELECT task_id FROM session_sync_resolvers WHERE repo_root=? AND branch=?",
        (ws.repo_root, ws.branch))}
    existing = next((t for t in reversed(db.list_tasks(ws.repo_root))
                     if t.caller_id in owners and t.caller_ws_id == ws.id
                     and t.id not in previous
                     and _current_resolver(db, t, d.sha)
                     and d.ref.lower() in t.task_text.lower()
                     and "merg" in t.task_text.lower()
                     and "resolv" in t.task_text.lower()
                     and (d.sha in t.task_text or all(f in t.task_text for f in files)
                          or set(files).issubset(json.loads(t.files or "[]")))), None)
    task_id = existing.id if existing else None
    # Record even a refused attempt: it needs the person, not another sweep.
    db.conn.execute("INSERT INTO session_sync_resolvers (repo_root, branch, upstream_sha, task_id)"
                    " VALUES (?, ?, ?, ?)", (*key, task_id))
    if existing:
        return
    brief = f"merge {d.ref} into the session branch and resolve the conflict in {', '.join(files)}"
    try:
        profile, _ = autopilot.choose_profile(db, roots[-1].id, ws.repo_root, task=brief, files=files)
        task = tasks.enqueue(db, roots[-1], ws, profile, f"{brief}\n\nConflicting origin commit: {d.sha}.",
                             "assign", isolate=True, branch=None, done_when=None, files=files,
                             depends_on=None, plan_first=False, unanswered=True)
    except (agents.AgentError, autopilot.AutopilotError) as e:
        log.warning("brindle: couldn't queue sync resolver for %s: %s", ws.branch, e)
        return
    db.conn.execute("UPDATE session_sync_resolvers SET task_id=? WHERE repo_root=? AND branch=?"
                    " AND upstream_sha=?", (task.id, *key))
    # Publish its quietness constraint before any sweep may dispatch it.
    tasks.answered(db, [task.id], start=set())


def _current_resolver(db: DB, task: Task, sha: str) -> bool:
    """Prefer explicit commit identity; filenames alone require active work."""
    if sha in task.task_text:
        return True
    if re.search(r"\b[0-9a-fA-F]{40}\b", task.task_text):
        return False
    if task.state in ("pending", "held", "unanswered", "starting"):
        return True
    if task.state != "started" or not task.agent_id:
        return False
    worker = db.get_agent(task.agent_id)
    return bool(worker and worker.dismissed_at is None and worker.status != "failed"
                and not worker.merge_failed and not db.mark(worker.id, "pipeline-escalated")
                and (worker.pipeline or worker.status != "done"))


def resolver_wait(db: DB, task_id: str) -> bool:
    """A sync resolver must wait for the same quiet session as idle sync."""
    row = db.conn.execute("SELECT 1 FROM session_sync_resolvers WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return False
    task = db.get_task(task_id)
    ws = db.get_workspace(task.caller_ws_id) if task else None
    return ws is None or not _quiet(db, ws)


def _resolving(db: DB, ws: Workspace, sha: str) -> bool:
    row = db.conn.execute("SELECT task_id FROM session_sync_resolvers WHERE repo_root=? AND branch=?"
                          " AND upstream_sha=?", (ws.repo_root, ws.branch, sha)).fetchone()
    task = db.get_task(row["task_id"]) if row and row["task_id"] else None
    if task is None or task.state in ("cancelled", "failed", "merged"):
        return False
    if task.agent_id:
        worker = db.get_agent(task.agent_id)
        if (worker is None or worker.dismissed_at is not None or worker.status == "failed" or worker.merge_failed
                or db.mark(worker.id, "pipeline-escalated")):
            return False
        if worker.status == "done" and not worker.result and not worker.pipeline:
            return False
    return True


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
    if _resolving(db, ws, got[0]):
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


def on_pr_merged(db: DB, root_id: str, ws: Workspace, url: str, pushed_sha: str | None) -> Outcome | None:
    """Session ``root_id``'s PR from ``ws`` merged on the forge: keep that
    (``session_pr_landings``) until how it landed is known, then sync."""
    if pushed_sha:
        db.conn.execute("INSERT OR IGNORE INTO session_pr_landings (root_id, repo_root, branch, url, covers_sha)"
                        " VALUES (?, ?, ?, ?, ?)", (root_id, ws.repo_root, ws.branch, url, pushed_sha))
    return sync(db, ws)


def unlanded(db: DB) -> list[str]:
    """The sessions with a merged PR whose landing commit isn't known yet."""
    return [r["root_id"] for r in db.conn.execute("SELECT DISTINCT root_id FROM session_pr_landings")]


def resume_landing(db: DB, ws: Workspace) -> Outcome | None:
    """Look up again how ``ws``'s merged PRs landed; once one is known, sync."""
    return sync(db, ws) if _land_merged_prs(db, ws) else None


def _land_merged_prs(db: DB, ws: Workspace) -> bool:
    """Look up how each merged PR of ``ws``'s branch landed and record a
    squash: True when any became known. A lookup that fails keeps the PR for
    the next event however many times it failed (only the sync stops waiting
    on it, after MAX_LANDING_LOOKUPS), so a squash learnt late still counts."""
    from brindle import workspaces

    rows = db.conn.execute("SELECT url, covers_sha FROM session_pr_landings WHERE repo_root = ? AND branch = ?",
                           (ws.repo_root, ws.branch)).fetchall()
    known = False
    for row in rows:
        key = (ws.repo_root, ws.branch, row["url"])
        landed = workspaces.pull_request_merge_commit(row["url"], ws.path)
        here = bool(landed) and git.ok(["cat-file", "-e", f"{landed}^{{commit}}"], ws.path)
        if landed and not here and (default := upstream(ws)):
            here = fetch(ws.repo_root, default) and git.ok(["cat-file", "-e", f"{landed}^{{commit}}"], ws.path)
        if not here:
            db.conn.execute("UPDATE session_pr_landings SET attempts = attempts + 1 WHERE repo_root = ?"
                            " AND branch = ? AND url = ?", key)
            continue
        record_squash(db, ws, landed, row["covers_sha"])
        db.conn.execute("DELETE FROM session_pr_landings WHERE repo_root = ? AND branch = ? AND url = ?", key)
        known = True
    return known


def _awaiting_landing(db: DB, ws: Workspace) -> bool:
    """A merged PR the sync still waits to learn the landing of."""
    return db.conn.execute("SELECT 1 FROM session_pr_landings WHERE repo_root = ? AND branch = ? AND attempts < ?",
                           (ws.repo_root, ws.branch, MAX_LANDING_LOOKUPS)).fetchone() is not None


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
