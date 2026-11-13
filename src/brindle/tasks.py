"""Task coordination between parallel workers.

``assign``/``handoff`` may declare ``files`` (paths/globs a task expects to
touch) and ``depends_on`` (earlier tasks, by task id, agent id or branch name, that
must be merged first). A task with unmet dependencies is recorded here as
'pending' (no worker started yet) and started once ``merge_workspace``
resolves its dependencies, cut from the updated base branch. A task with
declared ``files`` is checked against other active workers' declared and
actually-changed files, so the caller learns about likely collisions without
being blocked by them.

A task whose worker would start on a machine with no room for it
(``spawn.no_room``; ``agents.delegate`` raises ``NoRoom``) is queued the
same way, 'pending' with the reason in ``waiting``, and started by a
detached helper (``start_when_room``) once there's room.

assign/handoff never start a worker inside the MCP request: under load its
worktree, setup and launch can take minutes. They queue the task
'unanswered' (``UNANSWERED``), reply with its id, and only then release it
(``answered``) to that detached helper; a request cancelled before its
reply cancels it instead (``drop_unanswered``). Before replying they check
only what the database knows (``declared_clash``); the start checks what
takes git (``inspected_clash``, a branch's ancestry) and the repo's policy
again (``_claim_admitted``) before it launches.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import PurePath

from brindle import agents, bus, conflicts, git, savings, spawn
from brindle.db import DB, Agent, Task, Workspace


def new_id() -> str:
    return agents.new_id()


def _dumps(items: list[str] | None) -> str | None:
    return json.dumps(list(items)) if items else None


def _loads(text: str | None) -> list[str]:
    return json.loads(text) if text else []


# -- overlap warnings ---------------------------------------------------------


# A declared region: "src/a.py:120-180" (or "src/a.py:42"), the lines of the
# task's base it expects to change.
_REGION = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")

Region = tuple[int, int]


def _region(spec: str) -> tuple[str, Region | None]:
    """``spec``'s path and declared line range (None: the whole file)."""
    m = _REGION.match(spec or "")
    if not m:
        return spec, None
    start = int(m["start"])
    end = int(m["end"]) if m["end"] else start
    return m["path"], (min(start, end), max(start, end))


def _meets(a: Region, b: Region) -> bool:
    """Whether line ranges ``a`` and ``b`` overlap or nearly touch, with the
    margin ``conflicts.overlapping_hunks`` uses."""
    return a[0] <= b[1] + conflicts.ADJACENCY and b[0] <= a[1] + conflicts.ADJACENCY


def overlap_granularity(repo_root: str) -> str:
    """The repo's ``overlap_granularity``: "hunk" judges a shared file by the
    regions each task declares or has changed; anything else, "file"."""
    from brindle.config import load_repo_config
    return "hunk" if getattr(load_repo_config(repo_root), "overlap_granularity", None) == "hunk" else "file"


def _normpath(p: str) -> str:
    """``p`` without a declared region, with a leading './' and redundant
    separators collapsed, so "./src/a.py:3-9" compares equal to "src/a.py"."""
    p = _region(p)[0]
    return os.path.normpath(p) if p else p


def _glob_match(a: str, b: str) -> bool:
    """Whether globs/paths ``a`` and ``b`` could refer to the same file(s)."""
    a, b = _normpath(a), _normpath(b)
    if a == b:
        return True
    if fnmatch.fnmatch(b, a) or fnmatch.fnmatch(a, b):
        return True
    if "**" in a or "**" in b:
        try:
            return PurePath(b).match(a) or PurePath(a).match(b)
        except ValueError:
            return False
    return False


def _changed_files(ws: Workspace) -> list[str]:
    """Files ``ws``'s branch has touched relative to its base, cheaply (no
    process beyond a couple of git calls)."""
    return conflicts.changed_files(ws)


def _has_glob(path: str) -> bool:
    return any(c in path for c in "*?[")


def _overlap_files(files: list[str], declared: list[str], changed: list[str],
                   ws: Workspace | None = None, *, hunks: bool = False,
                   holder_hunks: conflicts.Hunks | None = None) -> tuple[str, ...]:
    """Broad declarations need concrete evidence from the holder's diff.

    Defer globs on the database-only path. At inspection a recursive tree
    glob, or one matching more than 20 tracked files, is broad. Narrow
    declarations still reserve files before the worker edits them.

    With ``hunks`` (``overlap_granularity`` "hunk"), a shared file whose
    regions both sides know ("path:start-end", ``_region``) is shared only
    when they meet: a declared region against the other's declared one, or
    against ``holder_hunks``, the hunks the holder's branch actually changed
    (old side, against its base). A file either side declared whole, or
    whose changed hunks can't be read, is shared as before.
    """
    if hunks:
        return _overlap_regions(files, declared, changed, ws, holder_hunks)
    return _overlap_whole_files(files, declared, changed, ws)


def _overlap_regions(files: list[str], declared: list[str], changed: list[str],
                     ws: Workspace | None, holder_hunks: conflicts.Hunks | None) -> tuple[str, ...]:
    shared = set(_overlap_whole_files(files, declared, changed, ws))
    if not shared:
        return ()
    mine = [_region(f) for f in files]
    theirs = [_region(d) for d in declared]
    changed_set = {_normpath(p) for p in changed}
    kept = set()
    for path in shared:
        ours = [r for pat, r in mine if _glob_match(pat, path)]
        if not ours or any(r is None for r in ours):
            kept.add(path)
            continue
        held = [r for pat, r in theirs if _glob_match(pat, path)]
        if any(r is None or any(_meets(a, r) for a in ours) for r in held):
            kept.add(path)
            continue
        if _normpath(path) in changed_set:
            actual = (holder_hunks or {}).get(_normpath(path))
            if not actual or any(_meets(a, b) for a in ours for b in actual):
                kept.add(path)
    return tuple(sorted(kept))


def _overlap_whole_files(files: list[str], declared: list[str], changed: list[str],
                         ws: Workspace | None = None) -> tuple[str, ...]:
    tracked = []
    if ws and any(_has_glob(p) for p in files + declared):
        try:
            tracked = git.paths(["ls-files"], ws.path)
        except git.GitError:
            ws = None   # unknown breadth: require actual changed files

    def broad(pattern: str) -> bool:
        pattern = _normpath(pattern)
        return _has_glob(pattern) and (
            ws is None or pattern == "**" or pattern.endswith("/**")
            or sum(_glob_match(pattern, p) for p in tracked) > 20)

    shared = {p for p in changed if any(_glob_match(mine, p) for mine in files)}
    for mine in files:
        if broad(mine):
            continue
        for theirs in declared:
            if not broad(theirs) and _glob_match(mine, theirs):
                shared.add(_region(theirs)[0])
    return tuple(sorted(shared))


@lru_cache(maxsize=512)
def _branch_files(repo_root: str, base_sha: str, head_sha: str) -> tuple[str, ...]:
    """Content-addressed: moving either branch naturally invalidates its diff."""
    return tuple(git.paths(["diff", "--name-only", f"{base_sha}...{head_sha}"], repo_root))


def _session_changes(db: DB, ws: Workspace, own: set[str],
                     start: str | None = None, caller: Agent | None = None,
                     isolate: bool = True) -> Iterator[tuple[str, str, str, tuple[str, ...]]]:
    """Other sessions' accumulated work, including after their workers are removed.

    Yield (session root, dependency holder, branch, changed files). Attached
    repositories have their own recorded session branch, independent of the
    root agent's workspace.
    """
    branches: dict[tuple[str, str], str] = {}
    for other_ws in db.find_workspaces(ws.repo_root):
        for agent in db.list_agents(other_ws.id):
            if agent.parent_id is None and agent.mode == "interactive" and agent.id not in own:
                branches[agent.id, other_ws.branch] = f"workspace:{other_ws.id}"
    for attached in db.session_repos():
        if attached.repo_root == ws.repo_root and attached.root_id not in own and attached.branch:
            branches.setdefault((attached.root_id, attached.branch), attached.branch)
    if not branches:
        return
    try:
        refs = git.ref_shas(ws.repo_root)
        if not refs:
            return
        base = git.default_branch(ws.repo_root)
        launch_base = agents.delegation_base(db, caller, ws) if isolate else ws.branch
        # Use delegate's base policy, including merge_into and its session
        # exceptions. An explicit dependency start takes precedence; when
        # the chosen local branch is absent, delegate falls back to HEAD.
        base_sha = start or refs.get(git._full_ref(launch_base)) or git.out(["rev-parse", "HEAD"], ws.path).strip()
        if not base_sha:
            return
        for (root, branch), holder in branches.items():
            if branch == base:
                continue
            head = refs.get(git._full_ref(branch))
            if head:
                try:
                    yield root, holder, branch, _branch_files(ws.repo_root, base_sha, head)
                except git.GitError:
                    continue
    except git.GitError:
        return


def _session_overlaps(db: DB, ws: Workspace, files: list[str], own: set[str],
                      task_id: str | None = None, caller_id: str | None = None) -> Iterator[Clash]:
    pending = db.get_task(task_id) if task_id else None
    isolate = bool(pending.isolate) if pending else True
    # A satisfied cross-session dependency can supply the new branch's
    # starting commit. Its inherited changes must not hold the task again.
    start = _holder_start(db, ws, _loads(pending.depends_on)) if pending and isolate else None
    caller = db.get_agent(caller_id) if caller_id else None
    for root, holder, branch, changed in _session_changes(db, ws, own, start, caller, isolate):
        shared = _overlap_files(files, [], list(changed))
        if shared:
            yield _unless_stalled(db, Clash(f"overlaps with session {root} ({branch}) on {', '.join(shared)}; "
                                            "consider depends_on or merging first", holder, root, True, shared),
                                  ws.repo_root)


def active_tasks(db: DB, repo_root: str) -> list[Task]:
    """Started tasks in ``repo_root`` whose worker hasn't reported yet."""
    out = []
    for t in db.list_tasks(repo_root, state="started"):
        if not t.agent_id:
            continue
        agent = db.get_agent(t.agent_id)
        if agent and agent.result is None:
            out.append(t)
    return out


RUNNING = ("starting", "processing", "waiting", "idle", "unknown")


def running_tasks(db: DB, repo_root: str) -> list[tuple[Task, Agent, Workspace | None]]:
    """Active tasks whose worker is still running (not paused, done or
    exited), with the worker and its workspace."""
    out = []
    for t in active_tasks(db, repo_root):
        agent = db.get_agent(t.agent_id) if t.agent_id else None
        if agent is None or agent.status not in RUNNING:
            continue
        out.append((t, agent, db.get_workspace(agent.workspace_id)))
    return out


# A "#NNN" right after one of these cites history (a pull request, what came
# after what, a commit subject's "(#NNN)"), not the issue the task is about.
_CITES_HISTORY = re.compile(r"(?:\b(?:PRs?|pull requests?|after|since|from|per|see|via|merged|landed|reverted)"
                            r"\s+|\()$", re.IGNORECASE)


def _issue_numbers(text: str | None) -> set[str]:
    """The issues ``text`` names as its subject: every #NNN but those it only
    cites as history (``_CITES_HISTORY``), which must not hold a task."""
    text = text or ""
    return {m.group(1) for m in re.finditer(r"#(\d+)", text)
            if not _CITES_HISTORY.search(text[max(0, m.start() - 24):m.start()])}


def _reported_unmerged(db: DB, repo_root: str) -> list[tuple[Task, Agent, Workspace]]:
    """Started tasks in ``repo_root``, from any session, whose worker has
    reported but whose branch isn't merged into the repo's default branch, or
    into its workspace's base branch (the session branch), yet: the work still
    exists and will land later. Cheap on the assign path: no fetch, one ref
    listing, and each branch decided once per (branch, target) commit pair
    without the patch-id fallback (git.merged_into)."""
    out = []
    refs: dict[str, str] | None = None
    defaults: list[str] = []
    for t in db.list_tasks(repo_root, state="started"):
        agent = db.get_agent(t.agent_id) if t.agent_id else None
        if agent is None or agent.result is None:
            continue
        other_ws = db.get_workspace(agent.workspace_id)
        if other_ws is None or other_ws.kind != "worktree":
            continue
        try:
            if refs is None:
                refs = git.ref_shas(repo_root)
                defaults = git.default_targets(repo_root, refs)
            session = [other_ws.base_branch] if other_ws.base_branch else []
            if git.merged_into(repo_root, other_ws.branch, session + defaults, refs=refs):
                continue
        except git.GitError:
            pass    # can't tell: still unmerged work as far as overlap goes
        out.append((t, agent, other_ws))
    return out


def _where(other_ws: Workspace | None) -> str:
    if other_ws is None:
        return "?"
    session = f", session {other_ws.base_branch}" if other_ws.base_branch else ""
    return f"{other_ws.branch}{session}"


@dataclass
class Clash:
    """What a new task's ``files`` (or issue number) collides with: the
    ``warning`` text, the worker holding the files (None for an intent, which
    has no worker to wait for) and whether it belongs to another session."""
    warning: str
    holder: str | None = None
    holder_root: str | None = None
    cross_session: bool = False
    shared_files: tuple[str, ...] = ()


def _own_roots(db: DB, ws: Workspace, caller_id: str | None) -> set[str]:
    from brindle import autopilot
    if caller_id:
        return {autopilot.root_of(db, caller_id)}
    return {autopilot.root_of(db, a.id) for a in db.list_agents(ws.id)}


def _held(db: DB, own: set[str], warning: str, holder: str | None, repo_root: str,
          shared_files: tuple[str, ...] = (), *, probe: bool = True) -> Clash:
    from brindle import autopilot
    root = autopilot.root_of(db, holder) if holder else None
    return _unless_stalled(db, Clash(warning, holder, root, root is not None and root not in own, shared_files),
                           repo_root, probe=probe)


# An agent that has had no live process, and no status change, for this long
# counts as stalled: what it holds won't land on its own.
STALLED_AFTER = 12 * 3600


def _agent_stalled(a: Agent, probe: bool, now: float) -> str | None:
    """Why session root or worker ``a`` can't make progress, or None."""
    from brindle import identity_lock

    if a.status == "paused":
        return "is paused"
    if a.dismissed_at is not None:
        return "was closed"
    if probe and (who := identity_lock.locked_by(a.id)):
        return f"is locked to another sign-in ({who})"
    since = a.status_since
    # A reported worker has no process by design: its branch waits on its supervisor.
    if (a.result is None and a.status not in ("starting", "processing", "waiting") and since is not None
            and now - since >= STALLED_AFTER and not agents.is_alive(a)):
        return f"has had no live process for {int((now - since) // 3600)} h"
    return None


def stalled_holder(db: DB, holder: str, repo_root: str, *, probe: bool = True) -> str | None:
    """Why another session's ``holder`` in ``repo_root`` can't make progress,
    or None. The holder is a worker id or ``task:<id>`` (stalled when its own
    worker is paused, closed, locked to another sign-in or long idle with no
    live process, or when its session is), or a session's ``workspace:<id>``
    or attached-repo branch (stalled when every session owning it is). A task
    is never held behind such work: it starts with a warning instead. Without
    ``probe`` the sign-in lock isn't asked (assign's reply reads only the
    database)."""
    from brindle import autopilot

    worker: Agent | None = None
    if holder.startswith("task:"):
        t = db.get_task(holder.removeprefix("task:"))
        worker = db.get_agent(t.agent_id) if t and t.agent_id else None
        root_ids = [autopilot.root_of(db, t.caller_id)] if t and t.caller_id else []
    elif not holder.startswith("workspace:") and (agent := db.get_agent(holder)) is not None:
        worker, root_ids = agent, [autopilot.root_of(db, agent.id)]
    else:   # a session's own workspace or attached branch: only its supervisors count
        root_ids = _holder_session(db, holder, repo_root)[0]
    now = time.time()
    if worker is not None and worker.parent_id and (why := _agent_stalled(worker, probe, now)):
        return f"its worker {worker.id} {why}"
    roots = [r for r in map(db.get_agent, dict.fromkeys(root_ids)) if r is not None]
    whys = [_agent_stalled(r, probe, now) for r in roots]
    if not whys or not all(whys):
        return None
    return "; ".join(f"session {r.id} {why}" for r, why in zip(roots, whys))


def _unless_stalled(db: DB, clash: Clash, repo_root: str, *, probe: bool = True) -> Clash:
    """``clash``, or, when it is another session's work that can't make
    progress (``stalled_holder``), the same clash with no holder to wait for:
    the task starts and its supervisor is warned of the overlap instead."""
    if not (clash.cross_session and clash.holder):
        return clash
    why = stalled_holder(db, clash.holder, repo_root, probe=probe)
    if not why:
        return clash
    return replace(clash, holder=None,
                   warning=f"{clash.warning}; not waiting for it: {why}, so it can't land soon")


def _intent_overlaps(db: DB, ws: Workspace, files: list[str], own: set[str]) -> Iterator[Clash]:
    """A clash if ``files`` matches a live intent another session announced
    for this repo, naming that session and its goal line."""
    from brindle import knowledge
    for intent in knowledge.intents(db, knowledge.repo_key(ws.repo_root)):
        if intent["status"] != "live" or intent["session_root"] in own:
            continue
        shared = _overlap_files(files, intent["paths"], [], ws)
        if shared:
            goal = intent["summary"].strip().splitlines()[0] if intent["summary"].strip() else ""
            yield Clash(f"overlaps with session {intent['session_root']}'s intent on {', '.join(shared)}"
                        + (f" ({goal})" if goal else "")
                        + "; consider depends_on or merging first",
                        None, intent["session_root"], True, shared)


def _queued_overlap(db: DB, ws: Workspace, files: list[str], own: set[str], *,
                    inspect: bool = False, task_id: str | None = None, hunks: bool = False) -> Clash | None:
    return next(_queued_overlaps(db, ws, files, own, inspect=inspect, task_id=task_id, hunks=hunks), None)


def _queued_overlaps(db: DB, ws: Workspace, files: list[str], own: set[str], *,
                     inspect: bool = False, task_id: str | None = None, hunks: bool = False) -> Iterator[Clash]:
    """A clash if ``files`` matches the declared files of a task queued in
    ``ws``'s repo that hasn't started its worker yet (its request still
    replying, waiting, or starting in the background): assign/handoff start
    workers after replying, so two tasks on the same files would otherwise
    both pass the check before either worker exists. Its holder is the task
    (``task:<id>``), a dependency that resolves once it merges."""
    from brindle import autopilot
    current = db.get_task(task_id) if task_id else None
    for t in (t for state in (UNANSWERED, *QUEUED, "starting") for t in db.list_tasks(ws.repo_root, state=state)):
        # At launch only earlier reservations can hold this task, never
        # itself or a later task which may already be waiting for this one.
        if current and (t.created_at, t.id) >= (current.created_at, current.id):
            continue
        shared = _overlap_files(files, _loads(t.files), [], ws if inspect else None, hunks=hunks)
        if shared:
            root = autopilot.root_of(db, t.caller_id) if t.caller_id else None
            yield _unless_stalled(db, Clash(
                f"overlaps with queued task {t.id} ({t.branch or 'branch not cut yet'}) on "
                f"{', '.join(shared)}; consider depends_on or merging first",
                f"task:{t.id}", root, root is not None and root not in own, shared), ws.repo_root, probe=inspect)


def find_clash(db: DB, ws: Workspace, files: list[str] | None, task_text: str | None = None,
               caller_id: str | None = None) -> Clash | None:
    """What ``files`` overlaps: another active task's declared or
    actually-changed files, a task from another session whose worker has
    reported but whose branch isn't merged yet, a task text naming the same
    issue (#NNN) as one of those, another session's accumulated branch changes,
    or another session's live intent. A task is
    another session's when its root differs from ``caller_id``'s (without
    one: from the roots of ``ws``'s agents)."""
    return (declared_clash(db, ws, files, caller_id)
            or inspected_clash(db, ws, files, task_text, caller_id))


def _running_overlap(db: DB, ws: Workspace, files: list[str], own: set[str], changed: bool,
                     hunks: bool = False) -> Clash | None:
    return next(_running_overlaps(db, ws, files, own, changed, hunks), None)


def _holder_overlap(files: list[str], declared: list[str], actual: list[str], ws: Workspace | None,
                    other_ws: Workspace | None, hunks: bool) -> tuple[str, ...]:
    """``_overlap_files`` against a holder's declared and changed files,
    reading its changed hunks (``conflicts.hunks``) only when a region is
    all that could tell the shared files apart."""
    shared = _overlap_files(files, declared, actual, ws)
    if not (shared and hunks):
        return shared
    regions = None
    if other_ws is not None and actual and any(_region(f)[1] for f in files):
        regions = conflicts.hunks(other_ws)
    return _overlap_files(files, declared, actual, ws, hunks=True, holder_hunks=regions)


def _running_overlaps(db: DB, ws: Workspace, files: list[str], own: set[str], changed: bool,
                      hunks: bool = False) -> Iterator[Clash]:
    """Clashes with running tasks' declared files, or (``changed``) with
    the files their branches have actually changed, which takes git to tell."""
    for t, _agent, other_ws in running_tasks(db, ws.repo_root):
        actual = _changed_files(other_ws) if changed and other_ws else []
        shared = _holder_overlap(files, _loads(t.files), actual, ws if changed else None, other_ws, hunks)
        if shared:
            branch = other_ws.branch if other_ws else "?"
            yield _held(db, own, f"overlaps with {t.agent_id} ({branch}) on {', '.join(shared)}; "
                        "consider depends_on or merging first", t.agent_id, ws.repo_root, shared, probe=changed)


def declared_clash(db: DB, ws: Workspace, files: list[str] | None,
                   caller_id: str | None = None) -> Clash | None:
    """``find_clash`` on what the database knows alone: the declared files of
    running and queued tasks. No git and no process: assign/handoff check
    this before replying; globs need breadth inspection at launch."""
    if not files:
        return None
    own = _own_roots(db, ws, caller_id)
    hunks = overlap_granularity(ws.repo_root) == "hunk"
    return (_running_overlap(db, ws, files, own, changed=False, hunks=hunks)
            or _queued_overlap(db, ws, files, own, hunks=hunks))


def inspected_clash(db: DB, ws: Workspace, files: list[str] | None, task_text: str | None = None,
                    caller_id: str | None = None, *, task_id: str | None = None) -> Clash | None:
    """The rest of ``find_clash``, which reads git and can wait minutes on a
    loaded machine: running tasks' changed files, other sessions' intents
    and reported but unmerged work. A queued task's start checks it just
    before launching (``_start_if_ready``)."""
    return next(inspected_clashes(db, ws, files, task_text, caller_id, task_id=task_id), None)


def inspected_clashes(db: DB, ws: Workspace, files: list[str] | None, task_text: str | None = None,
                      caller_id: str | None = None, *, task_id: str | None = None) -> Iterator[Clash]:
    """Every clash ``inspected_clash`` would consider, first one first and
    each read only when asked for: a launch passes over the holders its
    task no longer waits on (``_launch_overlap``) without missing the rest."""
    own = _own_roots(db, ws, caller_id)
    hunks = bool(files) and overlap_granularity(ws.repo_root) == "hunk"
    if files:
        yield from _running_overlaps(db, ws, files, own, changed=True, hunks=hunks)
        yield from _queued_overlaps(db, ws, files, own, inspect=True, task_id=task_id, hunks=hunks)
        yield from _session_overlaps(db, ws, files, own, task_id, caller_id)
        yield from _intent_overlaps(db, ws, files, own)
    mine_issues = _issue_numbers(task_text)
    if not files and not mine_issues:
        return
    for t, _agent, other_ws in _reported_unmerged(db, ws.repo_root):
        where = f"{t.agent_id} ({_where(other_ws)})"
        shared_files = (_holder_overlap(files, _loads(t.files), _changed_files(other_ws), ws, other_ws, hunks)
                        if files else ())
        if shared_files:
            yield _held(db, own, f"overlaps with {where}, reported but not yet merged, on "
                        f"{', '.join(shared_files)}; consider depends_on or merging first", t.agent_id, ws.repo_root,
                        shared_files)
            continue
        shared = mine_issues & _issue_numbers(t.task_text)
        if shared:
            yield _held(db, own, f"names issue #{min(shared, key=int)} like {where}, reported but not yet "
                        "merged; check it isn't already fixed there", t.agent_id, ws.repo_root)


def overlap_warning(db: DB, ws: Workspace, files: list[str] | None,
                    task_text: str | None = None) -> str | None:
    """``find_clash``'s warning text, or None."""
    clash = find_clash(db, ws, files, task_text)
    return clash.warning if clash else None


def merge_hunk_warning(db: DB, ws: Workspace) -> str | None:
    """Under ``overlap_granularity`` "hunk", the running tasks whose changed
    hunks meet ``ws``'s branch's (read before it merges): they started on
    regions that didn't meet, and have drifted into each other since. A
    warning only: the merge's own sync and gates still catch a conflict."""
    if ws.kind != "worktree" or overlap_granularity(ws.repo_root) != "hunk":
        return None
    mine = conflicts.hunks(ws)
    if not mine:
        return None
    hits = []
    for _t, agent, other_ws in running_tasks(db, ws.repo_root):
        if other_ws is None or other_ws.id == ws.id:
            continue
        theirs = conflicts.hunks(other_ws)
        files = [p for p in sorted(mine) if conflicts.overlapping_hunks({p: mine[p]}, theirs)]
        if files:
            hits.append(f"{agent.id} ({other_ws.branch}) on {', '.join(files)}")
    if not hits:
        return None
    return ("its changes meet running work's changed regions: " + "; ".join(hits)
            + ". Those workers will have conflicts to resolve when they sync")


def overlap_mode(setting: str | None, cross_session: bool) -> str:
    """How a clash is handled: the repo's ``overlap`` setting ("block", "warn"
    or "queue") when it has one; unset, "block" within a session and "queue"
    behind another session's task, whose work is never cancelled or replaced."""
    if setting in ("block", "warn", "queue"):
        return setting
    return "queue" if cross_session else "block"


def _goal_line(db: DB, root_id: str) -> str:
    """A session's goal line: its autopilot goal, else its live intent."""
    pilot = db.get_autopilot(root_id)
    text = pilot.goal if pilot and pilot.goal else ""
    if not text:
        row = db.conn.execute("SELECT summary FROM intents WHERE session_root = ? AND status = 'live' "
                              "ORDER BY updated_at DESC", (root_id,)).fetchone()
        text = row["summary"] if row else ""
    return text.strip().splitlines()[0] if text.strip() else ""


def notify_clash(db: DB, caller_root: str, clash: Clash, outcome: str,
                 task_key: str | None = None) -> None:
    """Tell both sessions' supervisors about a cross-session clash, each
    naming the other session's goal line. ``outcome`` says what happened to
    the new task (queued, refused, started with a warning)."""
    from brindle import gates, knowledge
    if not clash.holder_root:
        return
    if task_key is not None:
        # A logical task survives repeated assign calls and the later launch
        # check. Keep the mark in the DB so separate MCP processes share it.
        key = hashlib.sha256(json.dumps([caller_root, task_key]).encode()).hexdigest()
        pair = json.dumps([clash.holder or clash.holder_root, sorted(clash.shared_files) or [clash.warning]])
        kind = "clash:" + hashlib.sha256(pair.encode()).hexdigest()
        with db.tx() as c:
            if not c.execute("INSERT OR IGNORE INTO task_notes (task_id, kind, noted) VALUES (?,?,?)",
                             (key, kind, time.time())).rowcount:
                return
    bus.publish_agent(db, "overlap", caller_root, files=list(clash.shared_files), holder=clash.holder,
                      holder_root=clash.holder_root, outcome=outcome)
    # The event a repo's coordinator wakes on (brindle.coordinator).
    knowledge.note(db, caller_root, knowledge.CLASH,
                   {"warning": clash.warning, "holder": clash.holder,
                    "holder_root": clash.holder_root, "outcome": outcome})
    what = clash.warning.split(";")[0]
    for to, other in ((caller_root, clash.holder_root), (clash.holder_root, caller_root)):
        goal = _goal_line(db, other)
        who = f"session {other}" + (f" (goal: {goal})" if goal else "")
        if to == caller_root:
            text = f"[brindle] Your new task {what}, held by {who}: {outcome}."
        else:
            text = (f"[brindle] A new task from {who} {what}, your session's work: "
                    f"{outcome}. Your work is not changed.")
        if db.get_agent(to):
            gates.tell(db, to, text)


# -- waiting on another session's work -------------------------------------------

# As long as a session branch may go unlanded before it counts as drifting
# (session_sync): a task queued behind another session's work starts anyway
# after this, with a warning, rather than wait for good.
CROSS_SESSION_WAIT_LIMIT = 48 * 3600


def _released_prefix(holder: str) -> str:
    # A release note's kind: "cross-released:<holder>\n<why>\n<holder's head then>".
    return f"cross-released:{holder}\n"


def note_cross_wait(db: DB, task_id: str, holder: str) -> None:
    """Record that task ``task_id`` waits for another session's ``holder``
    to land, from the first time it did; a repeat keeps that time. A wait
    that had ended and is held again by new work starts over."""
    prefix = _released_prefix(holder)
    with db.tx() as c:
        if c.execute("DELETE FROM task_notes WHERE task_id=? AND substr(kind, 1, ?) = ?",
                     (task_id, len(prefix), prefix)).rowcount:
            c.execute("DELETE FROM task_notes WHERE task_id=? AND kind=?", (task_id, f"cross-wait:{holder}"))
        c.execute("INSERT OR IGNORE INTO task_notes (task_id, kind, noted) VALUES (?,?,?)",
                  (task_id, f"cross-wait:{holder}", time.time()))


def _cross_note(db: DB, task_id: str, kind: str) -> float | None:
    row = db.conn.execute("SELECT noted FROM task_notes WHERE task_id=? AND kind=?",
                          (task_id, kind)).fetchone()
    return row["noted"] if row else None


def _cross_release(db: DB, task_id: str, holder: str) -> tuple[str, str] | None:
    """How ``task_id``'s wait for ``holder`` ended: (why, the holder's head then)."""
    prefix = _released_prefix(holder)
    row = db.conn.execute("SELECT kind FROM task_notes WHERE task_id=? AND substr(kind, 1, ?) = ?",
                          (task_id, len(prefix), prefix)).fetchone()
    if row is None:
        return None
    why, _, head = row["kind"][len(prefix):].partition("\n")
    return why, head


def _holder_session(db: DB, dep: str, repo_root: str) -> tuple[list[str], str | None]:
    """The session roots whose landing ends a wait on ``dep``, and the
    branch that landing must come from: a ``workspace:<id>`` holder's
    supervisors and branch, or the sessions an attached branch belongs to."""
    if dep.startswith("workspace:"):
        ws = db.get_workspace(dep.removeprefix("workspace:"))
        roots = [a.id for a in db.list_agents(dep.removeprefix("workspace:"))
                 if a.parent_id is None and a.mode == "interactive"]
        return (roots, ws.branch) if ws else ([], None)
    return [r.root_id for r in db.session_repos() if r.repo_root == repo_root and r.branch == dep], dep


def _holder_head(repo_root: str, branch: str | None) -> str:
    """``branch``'s commit now, or "" when git can't tell."""
    if not branch:
        return ""
    try:
        return git.ref_shas(repo_root).get(git._full_ref(branch), "")
    except git.GitError:
        return ""


def _holder_root(db: DB, dep: str, repo_root: str) -> str | None:
    from brindle import autopilot

    roots, _branch = _holder_session(db, dep, repo_root)
    if roots:
        return roots[0]
    task = db.get_task(dep.removeprefix("task:"))
    who = task.caller_id if task and dep.startswith("task:") else dep
    return autopilot.root_of(db, who) if who and db.get_agent(who) else None


def cross_wait_excused(db: DB, t: Task, clash: Clash, repo_root: str) -> bool:
    """Whether the clash read again at ``t``'s launch is work ``t`` already
    stopped waiting for, so it warns instead of queueing ``t`` behind it
    again: any of the holder's work once the wait ran out, or, after a
    landing, the holder's branch as it landed (new commits on its files
    hold ``t`` again). A branch that synced off the files has no excuse:
    holding them again is new work."""
    if not (clash.cross_session and clash.holder):
        return False
    release = _cross_release(db, t.id, clash.holder)
    if release is None:
        return False
    why, landed = release
    if why == "expired":
        return True
    if why != "landed" or not landed:
        return False
    head = _holder_head(repo_root, _holder_session(db, clash.holder, repo_root)[1])
    if not head:
        return False
    if head == landed:
        return True
    try:
        since_landing = _branch_files(repo_root, landed, head)
    except git.GitError:
        return False
    return not _overlap_files(_loads(t.files), [], list(since_landing))


def _cross_wait_over(db: DB, t: Task, caller_ws: Workspace, dep: str, since: float,
                     now: float) -> tuple[str, str] | None:
    """Why ``t``'s wait for another session's ``dep`` is over, or None:
    "expired" after CROSS_SESSION_WAIT_LIMIT, "landed" once that session's
    PR from the holding branch merged after the wait began, "synced" once
    that branch no longer holds the task's files. With it, for a landing,
    the commit that landed (the PR's pushed head; "" when unknown, which
    excuses nothing later). "stalled", with why, once that work can't make
    progress (``stalled_holder``). A worker or task holder otherwise ends
    its wait by merging, as any dependency does."""
    if now - since >= CROSS_SESSION_WAIT_LIMIT:
        return "expired", ""
    if stalled := stalled_holder(db, dep, caller_ws.repo_root):
        # Its launch warns of the overlap instead (``_launch_overlap``'s ``released``).
        return "stalled", stalled
    roots, branch = _holder_session(db, dep, caller_ws.repo_root)
    if not roots or not branch:
        return None
    marks = ",".join("?" * len(roots))
    landing = db.conn.execute(f"SELECT pushed_sha FROM session_prs WHERE root_id IN ({marks}) AND branch = ?"
                              " AND merged_at >= ? ORDER BY merged_at DESC LIMIT 1",
                              (*roots, branch, since)).fetchone()
    if landing:
        # Never the branch's head now: work committed after the landing is still unlanded.
        return "landed", landing["pushed_sha"] or ""
    files = _loads(t.files)
    deps = _loads(t.depends_on)
    isolate = bool(t.isolate)
    start = _holder_start(db, caller_ws, deps) if isolate else None
    caller = db.get_agent(t.caller_id) if t.caller_id else None
    own = _own_roots(db, caller_ws, t.caller_id)
    for _root, holder, _branch, changed in _session_changes(db, caller_ws, own, start, caller, isolate):
        if holder != dep:
            continue
        if _overlap_files(files, [], list(changed)):
            return None
        return "synced", ""
    return None   # only a branch actually read releases it: one git couldn't read keeps waiting


def _release_cross_waits(db: DB, t: Task, caller_ws: Workspace) -> Task:
    """End queued task ``t``'s waits on other sessions' work that has landed,
    no longer holds its files, can't make progress (its launch warns of that
    overlap: ``_stalled_releases``), or has been waited on for the drift
    limit (both supervisors are told of that one; nothing goes to the
    Inbox), and return it as it is now. The other session's work is never
    changed. The clash is read again at launch (``_launch_overlap``)."""
    deps = _loads(t.depends_on)
    waits = {d: since for d in deps
             if (since := _cross_note(db, t.id, f"cross-wait:{d}")) is not None}
    if not waits:
        return t
    now = time.time()
    over = {d: why for d, since in waits.items() if (why := _cross_wait_over(db, t, caller_ws, d, since, now))}
    if not over or not set_dependencies(db, t.id, [d for d in deps if d not in over], deps):
        return db.get_task(t.id) or t
    with db.tx() as c:
        for dep, (why, landed) in over.items():
            prefix = _released_prefix(dep)
            c.execute("DELETE FROM task_notes WHERE task_id=? AND substr(kind, 1, ?) = ?",
                      (t.id, len(prefix), prefix))
            # For "stalled", ``landed`` is why: kept for the launch's warning.
            c.execute("INSERT INTO task_notes (task_id, kind, noted) VALUES (?,?,?)",
                      (t.id, f"{prefix}{why}\n{landed}", now))
    expired = [d for d, (why, _landed) in over.items() if why == "expired"]
    if expired and t.caller_id:
        from brindle import autopilot, gates

        caller_root = autopilot.root_of(db, t.caller_id)
        hours = CROSS_SESSION_WAIT_LIMIT // 3600
        for dep in expired:
            other = _holder_root(db, dep, caller_ws.repo_root)
            gates.tell(db, caller_root, f"[brindle] Task {t.id} waited {hours} h for {dep} to land; "
                       "it starts now on the same files, with a warning.")
            if other and other != caller_root and db.get_agent(other):
                gates.tell(db, other, f"[brindle] A task from session {caller_root} waited {hours} h "
                           f"for your session's work ({dep}) to land and starts now on the same "
                           "files. Your work is not changed.")
    return db.get_task(t.id) or t


def _stalled_releases(db: DB, t: Task, repo_root: str) -> Iterator[Clash]:
    """A holderless clash for each wait of ``t``'s that ended because the
    work it waited on stalled (``_release_cross_waits``): its launch warns of
    that overlap even when it can't read it again (a paused worker is no
    longer running, so no overlap scan finds it)."""
    prefix = "cross-released:"
    rows = db.conn.execute("SELECT kind FROM task_notes WHERE task_id=? AND substr(kind, 1, ?) = ?",
                           (t.id, len(prefix), prefix)).fetchall()
    for row in rows:
        dep, _, rest = row["kind"][len(prefix):].partition("\n")
        why, _, stalled = rest.partition("\n")
        if why == "stalled":
            yield Clash(f"overlaps with {dep}, which it waited for; not waiting for it: {stalled}, "
                        "so it can't land soon", None, _holder_root(db, dep, repo_root), True)


# -- predicted conflicts between unmerged branches ------------------------------


def conflict_forecast(db: DB, ws: Workspace, worker: Agent) -> list[tuple[Agent, Workspace, list[str]]]:
    """Other running or reported-but-unmerged branches that have changed a file
    ``ws``'s branch has changed too (actual diffs, not declared ``files``):
    (their worker, their workspace, the shared files). These are the pairs
    that will conflict when the second of them merges."""
    mine = _changed_files(ws)
    if not mine:
        return []
    found = []
    for _t, other, other_ws in running_tasks(db, ws.repo_root) + _reported_unmerged(db, ws.repo_root):
        if other.id == worker.id or other_ws is None or other_ws.id == ws.id:
            continue
        if other_ws.kind != "worktree":
            continue
        shared = conflicts.shared_files(mine, _changed_files(other_ws))
        if shared:
            found.append((other, other_ws, shared))
    return found


def warn_predicted_conflicts(db: DB, worker: Agent) -> str | None:
    """A worker's turn ended: if its branch now shares changed files with
    another running or reported-but-unmerged branch, tell its supervisor,
    once per pair of branches (again only when more files join the overlap). Returns the warning sent,
    or None. Never raises: this runs inside the worker's Stop hook."""
    try:
        if worker.mode not in ("assign", "handoff", "handoff_detached") or not worker.parent_id:
            return None
        ws = db.get_workspace(worker.workspace_id)
        if ws is None or ws.kind != "worktree" or db.get_agent(worker.parent_id) is None:
            return None
        lines = []
        for other, other_ws, shared in conflict_forecast(db, ws, worker):
            told = db.conflict_notice(ws.id, other_ws.id)
            if told is not None and set(shared) <= set(told):
                continue
            db.set_conflict_notice(ws.id, other_ws.id, shared)
            shown = ", ".join(shared[:6]) + (f" and {len(shared) - 6} more" if len(shared) > 6 else "")
            lines.append(f"`{ws.branch}` ({worker.id}) and `{other_ws.branch}` ({other.id}) "
                         f"have both changed {shown}.")
        if not lines:
            return None
        text = ("[brindle] Likely merge conflict: " + " ".join(lines)
                + " Whichever merges second will have to resolve it (brindle asks that "
                "branch's worker when the time comes). To head it off: have one worker "
                "wait for the other's merge, or tell them which one owns those files.")
        from brindle import gates

        gates.tell(db, worker.parent_id, text)
        return text
    except Exception:  # noqa: BLE001 - a forecast must never break a hook
        return None


# -- dependencies ---------------------------------------------------------------


def _dep_branch(db: DB, dep: str) -> str:
    """A dependency identifier's branch: ``dep`` may be an agent id (its
    workspace's branch) or already a branch name."""
    if dep.startswith("workspace:"):
        ws = db.get_workspace(dep.removeprefix("workspace:"))
        return ws.branch if ws else dep
    agent = db.get_agent(dep)
    if agent:
        ws = db.get_workspace(agent.workspace_id)
        if ws:
            return ws.branch
    for t in db.list_tasks():
        if t.agent_id == dep and t.branch:
            return t.branch
    return dep


def _session_repos(db: DB, caller_ws: Workspace, caller_id: str | None = None) -> list[str]:
    from brindle import autopilot, repos

    roots = [caller_ws.repo_root]
    if not caller_id:
        return roots
    root_id = autopilot.root_of(db, caller_id)
    for _, path in repos.session_roots(db, root_id):
        if path not in roots:
            roots.append(path)
    return roots


def _resolve_dependency(
    db: DB, caller_ws: Workspace, dep: str, session_roots: list[str],
    *, dependent_task_id: str | None = None,
) -> tuple[str, Task | None, Workspace | None]:
    """Resolve ``dep`` (task id, agent id or branch name) to its target repository,
    task, and workspace once.

    Exact task IDs use the retained task row, including after its worker
    and workspace have been removed. Exact worker/agent IDs use task.agent_id matches
    regardless of whether the live agent row still exists (e.g. after
    workspace cleanup). Branch names use local-name precedence (checking
    caller_ws.repo_root for either a task or workspace first), then search
    the attached repos of the session, raising AgentError on ambiguity.
    Internal ``task:<id>`` and ``workspace:<id>`` references preserve a requeued
    task's upstream even before it has a worker. Exclude the depending task itself:
    enqueueing a task with the same branch name as its upstream must not
    create a new local dependency target."""
    if dep.startswith("workspace:"):
        ws = db.get_workspace(dep.removeprefix("workspace:"))
        if ws is None:
            raise agents.AgentError(f"dependency {dep} no longer exists")
        return ws.repo_root, None, ws
    task = db.get_task(dep.removeprefix("task:"))
    if task is not None or dep.startswith("task:"):
        if task is None:
            raise agents.AgentError(f"dependency {dep} no longer exists")
        agent = db.get_agent(task.agent_id) if task.agent_id else None
        return task.repo_root, task, db.get_workspace(agent.workspace_id) if agent else None
    agent = db.get_agent(dep)
    agent_tasks = [t for t in db.list_tasks(caller_ws.repo_root) if t.agent_id == dep]
    if not agent_tasks:
        agent_tasks = [t for t in db.list_tasks() if t.agent_id == dep]
    if agent or agent_tasks:
        ws = db.get_workspace(agent.workspace_id) if agent else None
        task = max(agent_tasks, key=lambda t: t.created_at) if agent_tasks else None
        target_repo = ws.repo_root if ws else (task.repo_root if task else caller_ws.repo_root)
        return target_repo, task, ws

    branch = dep
    # Check local repo first for either task or workspace (local-name precedence)
    local_tasks = [t for t in db.list_tasks(caller_ws.repo_root)
                   if t.branch == branch and t.id != dependent_task_id]
    local_workspaces = [w for w in db.find_workspaces(caller_ws.repo_root) if w.branch == branch]
    if local_tasks or local_workspaces:
        task = max(local_tasks, key=lambda t: t.created_at) if local_tasks else None
        ws = local_workspaces[-1] if local_workspaces else None
        return caller_ws.repo_root, task, ws

    # Check other attached repos of the caller session
    other_repos = [r for r in session_roots if r != caller_ws.repo_root]
    repo_tasks = {r: [t for t in db.list_tasks(r) if t.branch == branch] for r in other_repos}
    repo_workspaces = {r: [w for w in db.find_workspaces(r) if w.branch == branch] for r in other_repos}
    matching_repos = [r for r in other_repos if repo_tasks[r] or repo_workspaces[r]]
    if len(matching_repos) > 1:
        raise agents.AgentError(f"dependency {dep!r} is ambiguous: found in multiple attached repos")
    if len(matching_repos) == 1:
        target_repo = matching_repos[0]
        task = max(repo_tasks[target_repo], key=lambda t: t.created_at) if repo_tasks[target_repo] else None
        ws = repo_workspaces[target_repo][-1] if repo_workspaces[target_repo] else None
        return target_repo, task, ws

    return caller_ws.repo_root, None, None


def _dep_task(db: DB, repo_root: str, dep: str) -> Task | None:
    agent_tasks = [t for t in db.list_tasks(repo_root) if t.agent_id == dep]
    if not agent_tasks:
        agent_tasks = [t for t in db.list_tasks() if t.agent_id == dep]
    if agent_tasks:
        return max(agent_tasks, key=lambda t: t.created_at)
    branch = dep
    candidates = [t for t in db.list_tasks(repo_root) if t.branch == branch]
    return max(candidates, key=lambda t: t.created_at) if candidates else None


def unmet_dependencies(
    db: DB, caller_ws: Workspace, depends_on: list[str] | None, caller_id: str | None = None,
    *, dependent_task_id: str | None = None, inspect: bool = True, lock_identities: bool = True,
) -> list[str]:
    """``depends_on`` entries not yet merged into ``caller_ws``'s branch.

    Raises ``agents.AgentError`` if any dependency's task was cancelled: it
    can never merge, so there's nothing left to wait for.

    A dependency a task hasn't diverged from yet (no commits beyond its base)
    would trivially satisfy a plain git ancestry check even though it hasn't
    been merged, so a dependency brindle is tracking as a task is judged by its
    recorded state (only 'merged' counts) instead; only a dependency brindle
    never started falls back to ancestry. Without ``inspect`` (assign/handoff
    before replying) that git check, and the check that no other identity
    holds a dependency's worktree (``identity_lock``), are left to the
    start, which makes them before launching: such a dependency doesn't
    count as unmet here. ``lock_identities`` False keeps the git check but
    skips the identity one (the fair share's readiness: ``fair_share._runnable``)."""
    if not depends_on:
        return []
    from brindle import identity_lock

    session_roots = _session_repos(db, caller_ws, caller_id)

    resolved: dict[str, tuple[str, Task | None, Workspace | None]] = {}
    for dep in depends_on:
        target_repo, task, ws = _resolve_dependency(
            db, caller_ws, dep, session_roots, dependent_task_id=dependent_task_id)
        resolved[dep] = (target_repo, task, ws)
        if ws is not None and inspect and lock_identities and not _other_sessions_landing(db, dep, ws, caller_id):
            # its lookup can probe sign-ins for a minute: the start's job
            identity_lock.ensure_workspace(db, ws)

    unmet = []
    for dep in depends_on:
        target_repo, task, ws = resolved[dep]
        if task is not None:
            if task.state == "cancelled":
                raise agents.AgentError(f"dependency {dep} was cancelled and will never merge")
            if task.state != "merged":
                unmet.append(dep)
        elif target_repo != caller_ws.repo_root:
            # A dependency in another attached repo that brindle isn't tracking as a task:
            # its branch can't be an ancestor here; judge it by its merge record.
            if ws is None or not db.merged_sha(ws.id):
                unmet.append(dep)
        elif inspect and not git.ok(["merge-base", "--is-ancestor", _dep_branch(db, dep), "HEAD"],
                                    caller_ws.path):
            unmet.append(dep)
    return unmet


def _other_sessions_landing(db: DB, dep: str, ws: Workspace, caller_id: str | None) -> bool:
    """Whether ``dep`` (resolved to worktree ``ws``) is another session's
    work: a ``workspace:<id>`` its supervisors hold, or a worker or task of
    theirs. The task only waits for that work to land and never acts in its
    worktree, so whoever that session is signed in as doesn't concern it, and
    its sign-in lock must never cancel the task."""
    if caller_id is None:
        return False
    from brindle import autopilot

    # Only a real session's work: a worker with no supervisor belongs to none.
    roots = {r.id for r in map(db.get_agent, {autopilot.root_of(db, a.id) for a in db.list_agents(ws.id)})
             if r is not None and r.mode == "interactive"}
    if dep.startswith("workspace:"):
        roots.update(_holder_session(db, dep, "")[0])
    return bool(roots) and autopilot.root_of(db, caller_id) not in roots


def _dep_matches(dep: str, ws: Workspace, worker_id: str | None) -> bool:
    return dep == ws.branch or (worker_id is not None and dep == worker_id)


def _refers_to(dep: str, t: Task, from_repo: str | None = None) -> bool:
    """Whether a dependency names ``t`` by task ID, worker ID or branch.

    Task and worker IDs match from any repo. Branch names match only from
    ``from_repo`` (the depending task's repo), since two repos can have a
    branch with the same name."""
    if dep in (t.id, f"task:{t.id}") or (t.agent_id is not None and dep == t.agent_id):
        return True
    return dep == t.branch and (from_repo is None or from_repo == t.repo_root)


def _cancel(db: DB, task_id: str, reason: str) -> list[str]:
    """Cancel a still-pending task and tell its caller, then cascade the
    cancellation to any pending task depending on it, recursively: a task
    waiting on one that can never merge can itself never merge. Re-fetches
    and checks state so cancelling the same task twice (reachable via more
    than one dependency path) is a no-op the second time. Returns the ids
    cancelled, the task first."""
    t = db.get_task(task_id)
    if t is None or t.state not in QUEUED:
        return []
    cancelled = [t.id]
    db.update_task(t.id, state="cancelled")
    bus.publish(db, "task.cancelled", repo_root=t.repo_root, task_id=t.id, reason=reason)
    if t.caller_id:
        try:
            agents.send_message(
                db, t.caller_id, f"Cancelled queued task {t.id}: {reason}", sender_id=None,
            )
        except agents.AgentError:
            pass
    for dependent in queued(db):   # any repo of the session may wait on it
        if any(_refers_to(d, t, dependent.repo_root) for d in _loads(dependent.depends_on)):
            cancelled += _cancel(
                db, dependent.id, f"its dependency {t.id} ({t.branch or t.id}) was cancelled")
    return cancelled


def cancel(db: DB, caller: Agent | None, task_id: str, reason: str = "") -> str:
    """Cancel a pending task on behalf of ``caller`` (its own caller, or the
    root of its session) and say what was cancelled, dependents included.
    Raises ``ValueError`` if the task is unknown, not the caller's, or has
    already started."""
    from brindle import autopilot

    t = db.get_task(task_id)
    if t is None:
        raise ValueError(f"No task {task_id}. list_tasks shows what's queued.")
    mine = caller.id == t.caller_id if caller else t.caller_id is None
    if not mine and caller and t.caller_id:
        mine = autopilot.root_of(db, t.caller_id) == caller.id
    if not mine:
        raise ValueError(f"Task {task_id} isn't yours to cancel: only its caller or its session root can.")
    if t.state not in QUEUED:
        raise ValueError(f"Task {task_id} is {t.state}, not queued: only a pending task can be cancelled.")
    ids = _cancel(db, t.id, reason or "cancelled by its caller")
    text = f"Cancelled task {ids[0]}."
    if len(ids) > 1:
        text += f" Also cancelled its dependents: {', '.join(ids[1:])}."
    return text


# -- holds (brindle.coordinator) ---------------------------------------------------

# A queued task: 'pending' starts once its dependencies merge; 'held' (by the
# coordinator) waits until it is released, whatever has merged.
QUEUED = ("pending", "held")


def queued(db: DB, repo_root: str | None = None) -> list[Task]:
    """Pending and held tasks, oldest first."""
    return sorted(db.list_tasks(repo_root, state="pending") + db.list_tasks(repo_root, state="held"),
                  key=lambda t: t.created_at)


def move(db: DB, task_id: str, from_state: str, to_state: str) -> bool:
    """Move a task from ``from_state`` to ``to_state`` atomically; False if
    it was no longer in ``from_state`` (started, cancelled, held already)."""
    with db.tx() as c:
        moved = c.execute("UPDATE tasks SET state=? WHERE id=? AND state=?",
                          (to_state, task_id, from_state)).rowcount == 1
    if moved:
        bus.publish(db, f"task.{to_state}", task_id=task_id)
    return moved


def set_dependencies(db: DB, task_id: str, depends_on: list[str], expect: list[str]) -> bool:
    """Replace a queued task's ``depends_on`` with ``depends_on`` if it is
    still ``expect`` and the task hasn't started; False otherwise."""
    with db.tx() as c:
        row = c.execute("SELECT state, depends_on FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["state"] not in QUEUED or _loads(row["depends_on"]) != expect:
            return False
        c.execute("UPDATE tasks SET depends_on=? WHERE id=?", (_dumps(depends_on), task_id))
        return True


def known_dependency(db: DB, repo_root: str, dep: str) -> bool:
    """Whether ``dep`` names a task, agent or a branch brindle knows in
    ``repo_root``: a typo would leave a task waiting for good.

    Task IDs, like agent IDs, are exact references across repositories.
    Their retained rows remain valid after the worker's workspace is gone."""
    return (db.get_task(dep.removeprefix("task:")) is not None
            or (dep.startswith("workspace:") and db.get_workspace(dep.removeprefix("workspace:")) is not None)
            or db.get_agent(dep) is not None
            or any(w.branch == dep for w in db.find_workspaces(repo_root))
            or any(t.branch == dep for t in db.list_tasks(repo_root)))


# -- queueing and starting -------------------------------------------------------


def enqueue(
    db: DB, caller: Agent | None, caller_ws: Workspace, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None, plan_first: bool | None = None,
    weight: str | None = None, waiting: str | None = None,
    pipeline: str | None = None, unanswered: bool = False, milestone: str | None = None,
) -> Task:
    """Record a task that can't start yet: no worker, no workspace, just what
    it takes to start it once its dependencies are merged, or (``waiting``,
    why) once the machine has room for it. ``pipeline``: the pipeline state
    its worker starts in (a branch's second attempt or conflict resolver).
    ``unanswered``: queued by a request that hasn't replied yet, so nothing
    starts it until ``answered`` (or ``drop_unanswered`` cancels it)."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=None,
        caller_id=caller.id if caller else None, caller_ws_id=caller_ws.id,
        profile=profile, task_text=task_text, mode=mode, isolate=int(isolate),
        branch=branch, done_when=done_when, files=_dumps(files),
        depends_on=_dumps(depends_on), state=UNANSWERED if unanswered else "pending",
        created_at=time.time(), weight=weight,
    )
    db.add_task(t)
    if plan_first is not None:
        db.update_task(t.id, plan_first=int(plan_first))
    if milestone:
        db.set_task_milestone(t.id, milestone)
    if pipeline:
        db.update_task(t.id, pipeline=pipeline)
    bus.publish(db, "task.queued", repo_root=t.repo_root, task_id=t.id, caller_id=t.caller_id,
                depends_on=depends_on or [], waiting=waiting)
    if waiting and unanswered:
        db.update_task(t.id, waiting=waiting, waiting_since=time.time())   # its helper starts at answered
    elif waiting:
        wait_for_room(db, t, waiting)
    return db.get_task(t.id) or t


# A task an MCP request (assign/handoff) queued before it replied: nothing
# starts it, no sweep included, until the reply is on its way (``answered``).
# A request its client cancelled or dropped never replies: ``drop_unanswered``.
UNANSWERED = "unanswered"

# Set while a request releases its tasks: a worker started right there is
# named by the reply that follows, so its caller gets no "Started" message.
_replying: contextvars.ContextVar[bool] = contextvars.ContextVar("brindle_tasks_replying", default=False)


def answered(db: DB, task_ids: list[str], start: set[str]) -> None:
    """The request that queued ``task_ids`` is replying: they may start now.
    Those in ``start`` (nothing to wait on) get their detached start
    (``start_in_background``); the others start once their dependencies
    merge, as any queued task."""
    token = _replying.set(True)
    try:
        for task_id in task_ids:
            if move(db, task_id, UNANSWERED, "pending") and task_id in start:
                start_in_background(task_id)
    finally:
        _replying.reset(token)


def drop_unanswered(db: DB, task_ids: list[str]) -> None:
    """The request that queued ``task_ids`` was cancelled, or its client went,
    before it replied: cancel them, so no worker ever starts for them."""
    for task_id in task_ids:
        move(db, task_id, UNANSWERED, "cancelled")


def cancel_reason(db: DB, task_id: str) -> str | None:
    """Why task ``task_id`` was cancelled, as its caller was told."""
    row = db.conn.execute("SELECT payload FROM events WHERE kind='task.cancelled' AND task_id=? "
                          "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row[0]).get("reason") if row and row[0] else None


def start_in_background(task_id: str) -> None:
    """Start pending task ``task_id`` from a detached helper (``start_when_room``):
    it looks for room, cuts the worktree and launches the worker, all after
    the request that queued it has replied. Tests replace it."""
    _wait_in_background(task_id)


# -- waiting for room ---------------------------------------------------------------

ROOM_POLL = 5.0   # seconds between the waiting helper's looks at the machine


FORCED = "started despite load by supervisor"   # what a forced start's message says


def room_wait(repo_root: str | None = None, profile: str | None = None) -> str | None:
    """Why a worker should not start now (``spawn.no_room``), or None: the
    machine has room, or the repo's ``wait_for_room`` is off, or ``profile``
    is a subagent profile (it starts no process of brindle's)."""
    if profile is not None and repo_root is not None and not agents.launches_process(profile, repo_root):
        return None
    return spawn.no_room(repo_root)


def task_wait(db: DB, t: Task) -> str | None:
    """Why task ``t``'s worker should not start now: no room on the machine
    (``room_wait``), or its session must wait its turn for the person's
    machine-wide worker limit (``fair_share``); None: it may start."""
    if not agents.launches_process(t.profile, t.repo_root):
        return None
    from brindle import fair_share

    return spawn.no_room(t.repo_root) or fair_share.wait_reason(db, t)


def _note_long_wait(db: DB, t: Task, why: str) -> bool:
    """Tell ``t``'s supervisor, once, that it has waited for room longer than
    the repo's ``room_wait_max``: what waits, for how long and why. It never
    starts the task (that takes room, or the supervisor's ``start_now``) and
    tells nobody else. ``claim_wait_note`` lets one of the concurrent sweeps
    send it. Returns whether this call sent it."""
    from brindle.config import load_repo_config

    if t.waiting_since is None or not t.caller_id:
        return False
    elapsed = time.time() - t.waiting_since
    if elapsed <= load_repo_config(t.repo_root).room_wait_max:
        return False
    at = time.time()
    if not db.claim_wait_note(t.id, at):
        return False
    text = (f"Task {t.id} ({t.profile}) has waited {elapsed / 60:.0f} min to start: {why}. "
            "It starts on its own when there's room; start_now starts it now.")
    try:
        agents.send_message(db, t.caller_id, text, sender_id=None)
    except agents.AgentError:
        db.release_wait_note(t.id, at)
        return False
    return True


def load_counts(db: DB) -> tuple[int, int]:
    """(running workers, running check runs): workers are the process-running
    agents of a task (not supervisors or reviewers); check runs are counted
    per workspace, whether or not their worker is still running."""
    from brindle import gates

    every = db.list_agents()
    workers = sum(a.mode in ("handoff", "handoff_detached", "assign")
                  and a.status in ("starting", "processing") and agents.runs_process(a) for a in every)
    checks = sum(gates.marked("checks-running", ws_id) for ws_id in {a.workspace_id for a in every})
    return workers, checks


def load_note(db: DB) -> str:
    """Where the load comes from: the machine's (``spawn.why``) and how many
    of brindle's own workers and check runs are running."""
    workers, checks = load_counts(db)
    return f"{spawn.why()}; brindle has {workers} workers and {checks} check runs running"


def start_now(db: DB, caller: Agent | None, task_id: str) -> str:
    """Start task ``task_id``, waiting for room, now despite the load, on
    behalf of ``caller`` (its caller or session root). Only a worker start can
    be forced, and only one forced start may be in flight (a forced worker
    still starting): else ``ValueError`` says why."""
    from brindle import autopilot, session_sync

    t = db.get_task(task_id)
    if t is None:
        raise ValueError(f"No task {task_id}. list_tasks shows what's queued.")
    mine = caller.id == t.caller_id if caller else t.caller_id is None
    if not mine and caller and t.caller_id:
        mine = autopilot.root_of(db, t.caller_id) == caller.id
    if not mine:
        raise ValueError(f"Task {task_id} isn't yours to start: only its caller or its session root can.")
    if t.state != "pending" or t.waiting is None:
        raise ValueError(f"Task {task_id} is {t.state}, not waiting for room: nothing to force.")
    if session_sync.resolver_wait(db, t.id):
        raise ValueError(f"Task {task_id} waits for its session to be quiet.")
    caller_ws = db.get_workspace(t.caller_ws_id)
    if not caller_ws or session_paused(db, t):
        raise ValueError(f"Task {task_id} can't start now (its session is paused or gone).")
    if plan_holds(db, t):
        raise ValueError(f"Task {task_id} waits on the session's plan: it starts when the person approves it "
                         "(or chooses not to plan first).")
    try:
        unmet = unmet_dependencies(db, caller_ws, _loads(t.depends_on), caller_id=t.caller_id,
                                   dependent_task_id=t.id)
    except agents.AgentError as e:
        raise ValueError(str(e)) from e
    if unmet:
        raise ValueError(f"Task {task_id} still waits on {', '.join(unmet)}.")
    launch, overlap = _launch_overlap(db, t, caller_ws)   # forced past the load, never past overlap
    if not launch:
        raise ValueError(f"Task {task_id} didn't start: it {overlap}.")
    # The single forced-start slot, taken with the task, if the policy lets it start.
    claimed, refused = _claim_admitted(db, t, db.claim_forced_task)
    if refused:
        raise ValueError(f"Task {task_id} can't start now: the repo's policy refused it: {refused}")
    if not claimed:
        now = db.get_task(t.id)
        if now is not None and now.state == "pending":
            raise ValueError("Another worker was just started despite the load and is still starting; "
                             "try again once it is up, or let this one wait for room.")
        raise ValueError(f"Task {task_id} was just started or cancelled.")
    try:
        worker = start_queued(db, t)
    except agents.AgentError as e:
        db.release_forced_task(t.id)   # frees the slot
        db.update_task(t.id, started_at=None)
        raise ValueError(f"It could not start: {e}") from e
    text = f"Started {worker.id} for task {t.id} ({FORCED})."
    if t.caller_id and (caller is None or caller.id != t.caller_id):
        try:
            agents.send_message(db, t.caller_id, text, sender_id=None)
        except agents.AgentError:
            pass
    return text


def room_duplicate(db: DB, caller_id: str | None, repo_root: str, task_text: str,
                   branch: str | None = None) -> Task | None:
    """A task ``caller_id`` queued in ``repo_root`` with the same brief and
    branch that hasn't started its worker yet (queued by a request still
    replying, waiting for room or dependencies, starting in the background,
    or being started right now): a repeated assign of it must never start a
    second worker, whatever the load is by then."""
    return next((t for state in (UNANSWERED, "pending", "starting") for t in db.list_tasks(repo_root, state=state)
                 if t.caller_id == caller_id and t.task_text == task_text and t.branch == branch), None)


def wait_for_room(db: DB, t: Task, why: str) -> None:
    """Keep pending task ``t`` queued until there's room, with ``why`` as
    the reason list_tasks shows; the first time, start the helper that
    starts it then."""
    first = t.waiting_since is None
    # Only while it is still pending: a task claimed meanwhile (started, forced) is left as it is.
    if db.set_task_waiting(t.id, why, time.time()) and first:
        _wait_in_background(t.id)
    _note_long_wait(db, t, why)


def _wait_in_background(task_id: str) -> None:
    from brindle.providers import brindle_invocation

    agents._detach_orphan([*brindle_invocation(), "_start-when-room", task_id])


def start_when_room(db: DB, task_id: str, poll: float = ROOM_POLL) -> bool:
    """The detached helper's loop: start task ``task_id`` once there's room
    (``_start_if_ready`` looks), telling its caller how it was admitted;
    past ``room_wait_max`` it tells the caller once that it still waits, and
    never starts it before there's room. Ends when it starts, or
    when the task is no longer pending (cancelled, held, started by a
    sweep) or its session is paused (``on_resumed`` takes over). Returns
    whether it started it. A task assign/handoff just queued
    (``start_in_background``) has no ``waiting`` until it finds no room."""
    pause = threading.Event()
    while True:
        t = db.get_task(task_id)
        if t is None or t.state != "pending" or held(db, t):
            return False
        why = task_wait(db, t)
        if why is None:
            # Unless a dependency added since holds it: merging that starts it.
            return _start_if_ready(db, t, "room on the machine", check_room=False)
        if why != t.waiting:
            db.set_task_waiting(t.id, why, time.time())   # list_tasks shows the machine as it is now
        _note_long_wait(db, t, why)
        pause.wait(poll)


def session_paused(db: DB, t: Task) -> bool:
    """Whether ``t``'s session (its caller, or the caller's root) is paused:
    a paused session starts nothing, whatever wakes its queued tasks."""
    if not t.caller_id:
        return False
    from brindle import autopilot

    caller, root = db.get_agent(t.caller_id), db.get_agent(autopilot.root_of(db, t.caller_id))
    return any(a is not None and a.status == "paused" for a in (caller, root))


def plan_holds(db: DB, t: Task) -> bool:
    """Whether ``t`` waits on its session's planning doc (brindle.plans), or
    on brindle's offer to plan the goal first: while the person hasn't
    approved the plan (or answered No to the offer), nothing the session
    assigned starts, whatever wakes the task; that answer starts it (``on_resumed``)."""
    if not t.caller_id:
        return False
    from brindle import autopilot, plans

    return plans.holding(db, autopilot.root_of(db, t.caller_id)) is not None


def build_gate_refusal(db: DB, caller_id: str) -> str | None:
    """Why ``caller_id`` may not give out new work, while its session's plan
    is waiting on required reviewers (brindle.plans.gate); None otherwise."""
    from brindle import autopilot, plans

    plan = plans.gate_shut(db, autopilot.root_of(db, caller_id))
    if plan is None:
        return None
    return (f"Not started: plan {plan['id']}'s build gate is closed. {plan['gate']['reason']} "
            "Nothing was queued. Assign this once the person has started the build.")


def held(db: DB, t: Task) -> bool:
    """Whether ``t``'s session holds it: paused, or waiting on its plan."""
    return session_paused(db, t) or plan_holds(db, t)


def on_resumed(db: DB, root_id: str, why: str = "the session's resume") -> None:
    """Session ``root_id`` was resumed (or ``why``: the person approved its
    plan): start its queued tasks that can start now, and give each one
    still waiting for room its helper back (the old one ended at the pause)."""
    from brindle import autopilot

    for t in db.list_tasks(state="pending"):
        if not t.caller_id or autopilot.root_of(db, t.caller_id) != root_id:
            continue
        deps = _loads(t.depends_on)
        if _start_if_ready(db, t, ", ".join(deps) if deps else why):
            continue
        now = db.get_task(t.id)
        if now is not None and now.state == "pending" and now.waiting is not None:
            _wait_in_background(t.id)


def start_with_room_now(db: DB, t: Task) -> bool:
    """Start pending task ``t`` now if the machine has room (and its
    dependencies are merged), telling its
    caller; else it keeps waiting."""
    return _start_if_ready(db, t, "room on the machine")


def start_with_room(db: DB) -> int:
    """Start every task waiting only for room that can start now, in case
    its helper died (the cull sweep calls this). Returns how many started."""
    return sum(start_with_room_now(db, t) for t in db.list_tasks(state="pending") if t.waiting is not None)


def waiting_for_room(db: DB, root_id: str) -> list[Task]:
    """The tasks of session ``root_id`` queued only until the machine has room."""
    from brindle import autopilot

    return [t for t in db.list_tasks(state="pending")
            if t.waiting is not None and t.caller_id and autopilot.root_of(db, t.caller_id) == root_id]


def record_started(
    db: DB, caller_ws: Workspace, worker: Agent, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None, weight: str | None = None,
) -> Task:
    """Record a task that started right away, so its ``files`` can be checked
    for overlap against later tasks."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=worker.id,
        caller_id=worker.parent_id, caller_ws_id=caller_ws.id, profile=profile,
        task_text=task_text, mode=mode, isolate=int(isolate), branch=branch,
        done_when=done_when, files=_dumps(files), depends_on=_dumps(depends_on),
        state="started", created_at=time.time(), started_at=time.time(), weight=weight,
    )
    db.add_task(t)
    bus.publish_agent(db, "task.started", worker.id, task_id=t.id)
    return t


def pending_duplicate(db: DB, caller_id: str | None, repo_root: str, task_text: str) -> Task | None:
    """A task ``caller_id`` already queued in ``repo_root`` with the same brief,
    so a second assign of it leaves the first in place instead of adding one."""
    return next((t for t in db.list_tasks(repo_root, state="pending")
                 if t.caller_id == caller_id and t.task_text == task_text), None)


def _holder_start(db: DB, caller_ws: Workspace, depends_on: list[str]) -> str | None:
    """The commit a queued task's branch must start from: a dependency's
    merge commit that ``caller_ws``'s branch lacks, as when the holder merged
    into another session. Dependencies may name tasks, workers or workspaces
    directly; None means start from the caller's branch."""
    for dep in depends_on:
        if dep.startswith("workspace:"):
            dep_ws = db.get_workspace(dep.removeprefix("workspace:"))
        else:
            task = db.get_task(dep.removeprefix("task:"))
            agent = db.get_agent(task.agent_id) if task and task.agent_id else db.get_agent(dep)
            dep_ws = db.get_workspace(agent.workspace_id) if agent else None
        if dep_ws is None or dep_ws.repo_root != caller_ws.repo_root:
            continue
        sha = db.merged_sha(dep_ws.id)
        if not sha or not git.ok(["cat-file", "-e", f"{sha}^{{commit}}"], caller_ws.path):
            continue   # no merge record, or its commit is gone
        if not git.ok(["merge-base", "--is-ancestor", sha, caller_ws.branch], caller_ws.path):
            return sha
    return None


def start_queued(db: DB, task: Task) -> Agent:
    """Start a queued task now that its dependencies are met, cutting its
    branch from the caller workspace's current (updated) base, or from a
    dependency's merge commit that base lacks (``_holder_start``)."""
    from brindle.config import load_repo_config

    caller_ws = db.get_workspace(task.caller_ws_id)
    if caller_ws is None:
        raise agents.AgentError(f"workspace {task.caller_ws_id} for queued task {task.id} is gone")
    caller = db.get_agent(task.caller_id) if task.caller_id else None
    profile = task.profile
    launch_weight = task.weight
    cfg = load_repo_config(caller_ws.repo_root)
    weight_order = {"light": 0, "medium": 1, "heavy": 2, "advanced": 3, "reasoning": 3, "complex": 3}
    ceiling = cfg.max_weight if (cfg.max_weight and cfg.max_weight in weight_order) else None
    if ceiling and profile:
        from brindle.autopilot import profile_weight, choose_profile
        pw = profile_weight(profile, cfg, caller_ws.repo_root)
        if weight_order.get(pw, 0) > weight_order[ceiling]:
            if task.weight:
                effective_weight = ceiling if weight_order.get(task.weight, 0) > weight_order[ceiling] else task.weight
                routed_profile, _learned = choose_profile(
                    db, task.caller_id or "", caller_ws.repo_root,
                    requested=None,
                    weight=effective_weight,
                    task=task.task_text,
                    files=_loads(task.files),
                )
                profile = routed_profile
                launch_weight = effective_weight
                db.update_task(task.id, profile=profile, weight=effective_weight)
            else:
                choose_profile(db, task.caller_id or "", caller_ws.repo_root, requested=profile, weight=None)

    worker, _wws = agents.delegate(
        db, caller, caller_ws, profile, task.task_text, task.mode,
        isolate=bool(task.isolate), branch=task.branch, done_when=task.done_when,
        plan_first=None if task.plan_first is None else bool(task.plan_first),
        start=_holder_start(db, caller_ws, _loads(task.depends_on)), files=_loads(task.files),
        weight=launch_weight, milestone=db.task_milestone(task.id),
        admit=False,   # every caller has just looked for room (_start_if_ready, requeue)
        # Nobody waits on a queued handoff (its reply said "queued" long ago):
        # detached from its first row, so even a result it reports before this
        # returns is forwarded to its caller like an assign's.
        **({"detached": True} if task.mode == "handoff" else {}),
    )
    if task.pipeline:
        db.update_agent(worker.id, pipeline=task.pipeline, pipeline_rounds=0)
    db.update_task(task.id, agent_id=worker.id, state="started", started_at=time.time())
    bus.publish_agent(db, "task.started", worker.id, task_id=task.id)
    savings.attach_agent(db, task.id, worker.id)
    return worker


def on_merged(db: DB, ws: Workspace) -> None:
    """``ws``'s branch was just merged into its base: mark its own task
    'merged' (so dependents judge it correctly, and it stops counting as
    started), then start any pending task that was only waiting on it and now
    has every dependency merged, and tell its caller. A pending task may be
    in another attached repo of the session (brindle.repos)."""
    worker = agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    if worker:
        for t in db.list_tasks(ws.repo_root, state="started"):
            if t.agent_id == worker.id:
                db.update_task(t.id, state="merged")
    for t in queued(db):
        worker_id = worker.id if worker else None
        caller_ws = db.get_workspace(t.caller_ws_id)
        if not caller_ws:
            continue
        session_roots = _session_repos(db, caller_ws, t.caller_id)
        deps = _loads(t.depends_on)
        matches = False
        for d in deps:
            if d == f"workspace:{ws.id}" or (worker_id is not None and d == worker_id):
                matches = True
                break
            upstream = db.get_task(d.removeprefix("task:"))
            if upstream and worker_id is not None and upstream.agent_id == worker_id:
                matches = True
                break
            if d == ws.branch and ws.repo_root in session_roots:
                try:
                    target_repo, _, _ = _resolve_dependency(
                        db, caller_ws, d, session_roots, dependent_task_id=t.id)
                    if target_repo == ws.repo_root:
                        matches = True
                        break
                except agents.AgentError:
                    matches = True
                    break
        if not matches:
            continue
        # An untracked upstream has no retained Task state after workspace
        # cleanup. Persist its satisfaction in the remaining dependencies,
        # including while held or waiting for another upstream to merge.
        completed = []
        for dep in deps:
            try:
                _, upstream, dep_ws = _resolve_dependency(
                    db, caller_ws, dep, session_roots, dependent_task_id=t.id)
                if (upstream is None and dep_ws is not None and dep_ws.id == ws.id
                        and not unmet_dependencies(db, caller_ws, [dep], caller_id=t.caller_id,
                                                   dependent_task_id=t.id)):
                    completed.append(dep)
            except agents.AgentError:
                continue  # readiness handles invalid dependencies without marking them satisfied
        if completed:
            # Merge completion into the current list under the write lock so
            # another merge or queue edit cannot lose either update.
            with db.tx() as c:
                current = c.execute("SELECT state, depends_on FROM tasks WHERE id=?", (t.id,)).fetchone()
                if current is not None and current["state"] in QUEUED:
                    remaining = [dep for dep in _loads(current["depends_on"]) if dep not in completed]
                    c.execute("UPDATE tasks SET depends_on=? WHERE id=?", (_dumps(remaining), t.id))
            t = db.get_task(t.id)
        if t is not None and t.state == "pending":
            _start_if_ready(db, t, dep_ref)


def launching_workers(db: DB, repo_root: str, workers: list[Agent]) -> int:
    """How many of ``repo_root``'s tasks claimed to start ('starting') have no
    worker among ``workers`` (``policy.worker_agents``) yet: each is about to
    be one more worker at work. A claimed task's worker is the first one of
    its caller made since the claim (``started_at``) that no task names yet,
    so a worker whose task is still linking it isn't counted twice."""
    linked = {t.agent_id for t in db.list_tasks(repo_root) if t.agent_id}
    spare = sorted((a for a in workers if a.id not in linked), key=lambda a: a.created_at)
    count = 0
    for t in db.list_tasks(repo_root, state="starting"):
        since = t.started_at or t.created_at
        mine = next((a for a in spare if a.parent_id == t.caller_id and a.created_at >= since), None)
        if mine is None:
            count += 1
        else:
            spare.remove(mine)
    return count


@contextlib.contextmanager
def _launch_lock(repo_root: str) -> Iterator[None]:
    """Launch admission in ``repo_root``, one start at a time across every
    brindle process: the count of workers at work, the policy's decision on
    it and the claim happen together, so two starts can't both take the last
    slot the policy allows (``_claim_admitted``)."""
    from brindle.config import brindle_home

    locks = brindle_home() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(repo_root.encode()).hexdigest()[:16]
    with open(locks / f"launch-{name}.lock", "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _launch_refusal(db: DB, t: Task) -> str | None:
    """Why the repo's policy refuses to start ``t`` now, else None: decided
    again at launch, since assign/handoff decided before queueing it, and
    counting the workers being started (``launching_workers``) as at work."""
    from brindle import policy
    from brindle.config import load_repo_config

    try:
        # One snapshot: a start that finishes between the two reads (its
        # task 'started', its worker made) would be in neither.
        with db.snapshot():
            workers = policy.worker_agents(db, t.repo_root)
            running: int | None = len(workers) + launching_workers(db, t.repo_root, workers)
    except Exception:  # noqa: BLE001 - the policy decides what an unknown count means
        running = None
    caller = db.get_agent(t.caller_id) if t.caller_id else None
    d = policy.check_assign(
        load_repo_config(t.repo_root), t.repo_root, t.profile, t.task_text, t.mode,
        files=_loads(t.files) or None, weight=t.weight, branch=t.branch, actor=caller,
        running_workers=running,
    )
    return None if d.allowed else d.reason


def _claim_admitted(db: DB, t: Task, claim) -> tuple[bool, str | None]:
    """Claim pending task ``t`` with ``claim`` (``db.claim_task`` or
    ``db.claim_forced_task``) if the repo's policy lets it start now:
    (claimed, the policy's refusal). Under ``_launch_lock``, so the slot it
    takes counts for the next start's decision."""
    with _launch_lock(t.repo_root):
        now = db.get_task(t.id)
        if now is None or now.state != "pending":
            return False, None
        refused = _launch_refusal(db, now)
        if refused:
            return False, refused
        if not claim(t.id):
            return False, None
        db.update_task(t.id, started_at=time.time())   # when it took its slot (launching_workers)
        return True, None


def _claim_fair(db: DB, t: Task) -> tuple[bool, str | None, str | None]:
    """``_claim_admitted`` with ``db.claim_task``, deciding the machine-wide
    fair share (``fair_share.wait_reason``) again under ``fair_share.machine_lock``
    so the check and the slot it allows are one step across every repo:
    (claimed, the policy's refusal, why it must wait its turn instead). A
    task told to wait stays pending."""
    from brindle import fair_share

    with fair_share.machine_lock():
        why = fair_share.wait_reason(db, t) if agents.launches_process(t.profile, t.repo_root) else None
        if why:
            return False, None, why
        claimed, refused = _claim_admitted(db, t, db.claim_task)
        return claimed, refused, None


def _launch_overlap(db: DB, t: Task, caller_ws: Workspace) -> tuple[bool, str | None]:
    """Check pending task ``t`` for the overlap only git can tell
    (``inspected_clash``) just before it launches, and handle it by the
    repo's ``overlap`` setting as assign handles a declared one: (whether it
    may launch, the clash's warning). Queueing makes it wait on the holder's
    merge; blocking cancels it; either way its caller is told."""
    from brindle import autopilot
    from brindle.config import load_repo_config

    setting = load_repo_config(t.repo_root).overlap
    excused: Clash | None = None
    warned: Clash | None = None
    for clash in inspected_clashes(db, caller_ws, _loads(t.files), t.task_text, t.caller_id, task_id=t.id):
        how = overlap_mode(setting, clash.cross_session)
        if how == "queue" and not clash.holder:   # an intent, or stalled work: nothing to wait for
            how = "warn"
        # Work it already stopped waiting for never hides another holder's.
        if how == "queue" and cross_wait_excused(db, t, clash, caller_ws.repo_root):
            excused = excused or clash
            continue
        if how == "warn":   # nor does a warning: a later clash may still hold or refuse it
            warned = warned or clash
            continue
        break
    else:
        # Stalled work it stopped waiting for, which no scan may find again.
        warned = warned or next(_stalled_releases(db, t, caller_ws.repo_root), None)
        if warned is None:
            return True, excused.warning if excused else None
        clash, how = warned, "warn"
    deps = _loads(t.depends_on)
    proceed = False
    if how == "queue":
        outcome = f"queued as task {t.id} until {clash.holder} merges"
        if clash.cross_session:
            note_cross_wait(db, t.id, str(clash.holder))
        # False: it was claimed, cancelled or changed meanwhile, and isn't this start's to launch.
        if (str(clash.holder) not in deps
                and set_dependencies(db, t.id, [*deps, str(clash.holder)], deps) and t.caller_id):
            try:
                agents.send_message(db, t.caller_id, f"Task {t.id} waits for {clash.holder} to merge "
                                    f"before its worker starts: it {clash.warning}.", sender_id=None)
            except agents.AgentError:
                pass
    elif how == "block":
        outcome = "refused"
        _cancel(db, t.id, f"it {clash.warning}. Two workers editing the same files end in conflicts: "
                "fold it into that worker's task (send_message), or requeue it with depends_on")
    else:
        outcome, proceed = "allowed to start with a warning", True
    if clash.cross_session and t.caller_id:
        notify_clash(db, autopilot.root_of(db, t.caller_id), clash, outcome,
                     task_key=json.dumps([t.repo_root, t.caller_id, t.task_text]))
    return proceed, clash.warning


def _start_if_ready(db: DB, t: Task, dep_ref: str, check_room: bool = True) -> bool:
    """Start pending task ``t`` if every dependency is merged and the
    machine has room for it (else it waits for room: ``wait_for_room``),
    and tell its caller why it started.
    The task is claimed first, so a second call (a sweep racing a
    merge) finds it taken. A dependency that can never merge cancels it; a
    start that fails cancels it too, with the reason sent to its caller
    (``requeue`` makes a new one). Returns whether it started."""
    from brindle import session_sync

    if session_sync.resolver_wait(db, t.id):
        return False
    caller_ws = db.get_workspace(t.caller_ws_id)
    if not caller_ws or held(db, t):
        # A paused session's tasks stay queued, and so do those behind its
        # open plan: resuming it, or the person's approval, starts them (on_resumed).
        return False
    t = _release_cross_waits(db, t, caller_ws)
    try:
        if unmet_dependencies(db, caller_ws, _loads(t.depends_on), caller_id=t.caller_id,
                              dependent_task_id=t.id):
            return False
    except agents.AgentError as e:
        _cancel(db, t.id, str(e))
        return False
    why = task_wait(db, t) if check_room else None
    if why:
        wait_for_room(db, t, why)
        return False
    launch, overlap = _launch_overlap(db, t, caller_ws)
    if not launch:
        return False
    claimed, refused, turn = _claim_fair(db, t)
    if turn:
        wait_for_room(db, t, turn)
        return False
    if refused:
        _cancel(db, t.id, f"the repo's policy refused it: {refused}")
    if not claimed:
        return False
    try:
        new_worker = start_queued(db, t)
    except agents.Cancelled:
        # A resume stopped on the daemon's way out (agents.resume's gate): still queued, for the next resume.
        db.update_task(t.id, state="pending", started_at=None)
        raise
    except Exception as e:  # noqa: BLE001 - from a detached helper: its caller must hear why
        db.update_task(t.id, state="pending", started_at=None)
        reason = str(e).strip()
        if not isinstance(e, agents.AgentError) or not reason:
            reason = f"{type(e).__name__}: {reason}" if reason else type(e).__name__
        _cancel(db, t.id, f"couldn't start a worker on profile {t.profile!r}: {reason}")
        return False
    if t.caller_id and not _replying.get():
        try:
            new_ws = db.get_workspace(new_worker.workspace_id)
            if t.waiting:
                text = f"Started {new_worker.id} for task {t.id} (was waiting for room: {t.waiting})."
            elif _loads(t.depends_on):
                text = f"Started {new_worker.id} (was waiting on {dep_ref})."
            else:   # just queued by assign/handoff, which replied with the task id
                text = (f"Started worker {new_worker.id} ({new_worker.profile}) for task {t.id}"
                        + (f" in workspace {new_ws.id} on branch {new_ws.branch}." if new_ws else "."))
            if not agents.runs_process(new_worker) and new_ws:   # the caller's own Agent tool runs it
                text = f"Task {t.id} is ready.\n\n{agents.subagent_brief(new_worker, new_ws)}"
            for warning in [*([overlap] if overlap else []), *_launch_warnings(new_worker, new_ws)]:
                text += f"\nWarning: {warning}"
            agents.send_message(db, t.caller_id, text, sender_id=None)
        except agents.AgentError:
            pass
    return True


def _launch_warnings(worker: Agent, ws: Workspace | None) -> list[str]:
    """What the caller should know about a worker started from the queue:
    the warnings assign's reply gave when it started the worker itself."""
    if ws is None:
        return []
    from brindle import antigravity, codex_hook

    found = (agents.add_dirs_warning(worker, ws), codex_hook.launch_warning(worker.provider, ws.path),
             antigravity.launch_warning(worker, ws))
    return [w for w in found if w]


def sweep(db: DB) -> int:
    """Start every pending task whose dependencies are all merged, whatever
    stranded it (a merge run cut off before ``on_merged``, a branch merged by
    hand). A started task whose workspace has a merge record is first marked
    merged, as ``on_merged`` would have. Safe to repeat: ``_start_if_ready``
    claims each task. Returns how many started."""
    pending = db.list_tasks(state="pending")
    if not pending:
        return 0
    for t in db.list_tasks(state="started"):
        agent = db.get_agent(t.agent_id) if t.agent_id else None
        if agent and db.merged_sha(agent.workspace_id):
            db.update_task(t.id, state="merged")
    started = 0
    for t in pending:
        deps = _loads(t.depends_on)
        started += _start_if_ready(db, t, ", ".join(deps) if deps else "its dependencies")
    return started


def on_removed_unmerged(db: DB, ws: Workspace, *, worker: Agent | None = None) -> None:
    """``ws`` was removed while its branch still had commits not in its base:
    cancel any pending task depending on it -- and, recursively, any pending
    task that in turn depends on those -- and tell each one's caller. Pass
    ``worker`` when successful removal has already deleted its agent row:
    pinned worker and task references must still resolve to that identity."""
    worker = worker or agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    worker_id = worker.id if worker else None
    for t in queued(db):
        caller_ws = db.get_workspace(t.caller_ws_id)
        if not caller_ws:
            continue
        session_roots = _session_repos(db, caller_ws, t.caller_id)
        deps = _loads(t.depends_on)
        matches = False
        for d in deps:
            if d == f"workspace:{ws.id}" or (worker_id is not None and d == worker_id):
                matches = True
                break
            upstream = db.get_task(d.removeprefix("task:"))
            if upstream and worker_id is not None and upstream.agent_id == worker_id:
                matches = True
                break
            if d == ws.branch and ws.repo_root in session_roots:
                try:
                    target_repo, _, _ = _resolve_dependency(
                        db, caller_ws, d, session_roots, dependent_task_id=t.id)
                    if target_repo == ws.repo_root:
                        matches = True
                        break
                except agents.AgentError:
                    matches = True
                    break
        if matches:
            _cancel(db, t.id, f"it was waiting on {dep_ref}, which was removed unmerged.")


def requeue(db: DB, caller: Agent | None, caller_ws: Workspace, task_id: str,
            depends_on: list[str] | None = None) -> str:
    """Make a new task from a cancelled one: same brief, done_when, files,
    profile, weight and (unless ``depends_on`` overrides them) dependencies.
    It starts at once when nothing is left to wait on. Raises ``ValueError``
    if the task is unknown or not cancelled, or a dependency can never merge."""
    old = db.get_task(task_id)
    if old is None:
        raise ValueError(f"No task {task_id}. list_tasks shows what's queued.")
    if old.state != "cancelled":
        raise ValueError(f"Task {task_id} is {old.state}, not cancelled: only a cancelled task can be requeued.")
    ws = db.get_workspace(old.caller_ws_id) or caller_ws
    deps = _loads(old.depends_on) if depends_on is None else list(depends_on)
    try:
        caller_id = caller.id if caller else None
        roots = _session_repos(db, ws, caller_id)
        # Pin a same-name upstream before copying the task: excluding only the
        # old row here would let it shadow the upstream again in the new queue.
        for i, dep in enumerate(deps):
            if dep == old.branch:
                _, upstream, upstream_ws = _resolve_dependency(db, ws, dep, roots, dependent_task_id=old.id)
                if upstream is not None:
                    deps[i] = f"task:{upstream.id}"
                elif upstream_ws is not None:
                    worker = agents.workspace_worker(db, upstream_ws)
                    deps[i] = worker.id if worker else f"workspace:{upstream_ws.id}"
        unmet = unmet_dependencies(db, ws, deps, caller_id=caller_id, dependent_task_id=old.id)
    except agents.AgentError as e:
        raise ValueError(f"{e}. Pass depends_on to replace the dependencies (an empty list for none).") from e
    new = enqueue(
        db, caller, ws, old.profile, old.task_text, old.mode, isolate=bool(old.isolate),
        branch=old.branch, done_when=old.done_when, files=_loads(old.files), depends_on=deps,
        plan_first=None if old.plan_first is None else bool(old.plan_first), weight=old.weight,
        milestone=db.task_milestone(old.id),
    )
    if unmet:
        return f"Requeued task {old.id} as {new.id}, waiting on {', '.join(unmet)}."
    return start_requeued(db, new, f"task {old.id}")


def start_requeued(db: DB, new: Task, label: str) -> str:
    """Start ``new``, the pending replacement of ``label``, or leave it waiting
    for room. Says which; raises ``ValueError`` if it can't start."""
    why = room_wait(new.repo_root)
    claimed, refused = False, None
    if not why:
        claimed, refused, why = _claim_fair(db, new)   # the slot it takes counts for others
    if why:
        wait_for_room(db, new, why)
        return (f"Requeued {label} as {new.id}, waiting for room: {why}. "
                "It starts on its own when there's room.")
    if not claimed:
        reason = f"the repo's policy refused it: {refused}" if refused else "it was started or cancelled meanwhile"
        _cancel(db, new.id, reason)
        raise ValueError(f"Requeued {label} as {new.id}, but it didn't start: {reason}.")
    try:
        worker = start_queued(db, new)
    except agents.AgentError as e:
        db.update_task(new.id, state="pending", started_at=None)   # so the cancel below applies
        _cancel(db, new.id, f"it could not start: {e}")
        raise ValueError(f"Nothing left to wait on, but the worker could not start: {e}") from e
    return f"Requeued {label} as {new.id}: nothing to wait on, started worker {worker.id}."


def _matches(glob: str, path: str) -> bool:
    g = _normpath(glob)
    return _glob_match(g, path) or path.startswith(g.rstrip("/") + "/")


def dry_run_text(db: DB, ws: Workspace, profile: str, why: list[str], files: list[str] | None,
                 depends_on: list[str] | None, overlap: str | None,
                 caller_id: str | None = None) -> str:
    """What ``assign``/``handoff`` would do, without doing it: the profile, the
    files each glob matches in ``ws``'s checked-out branch, overlaps with
    active workers, and whether it would queue."""
    lines = ["Dry run: nothing started or queued.", f"profile: {profile}"]
    lines += [f"profile: {w}" for w in why]
    if files:
        tracked = git.paths(["ls-files"], ws.path)
        for glob in files:
            hits = [p for p in tracked if _matches(glob, p)]
            if hits:
                shown = ", ".join(hits[:5]) + (f" and {len(hits) - 5} more" if len(hits) > 5 else "")
                lines.append(f"files: {glob} matches {len(hits)}: {shown}")
            else:
                lines.append(f"Warning: files glob {glob!r} matches nothing in {ws.branch}, "
                             "so overlap detection won't cover it")
    else:
        lines.append("files: none declared, so overlap detection won't cover this task")
    clash = find_clash(db, ws, files, caller_id=caller_id)
    warning = clash.warning if clash else None
    mode = overlap_mode(overlap, clash.cross_session) if clash else "warn"
    if mode == "queue" and not (clash and clash.holder):
        mode = "warn"
    if clash:
        done = {"block": "refused", "warn": "warned", "queue": "queued"}[mode]
        lines.append(f"overlap: this task {warning} ({done} by the repo's overlap setting)")
    else:
        lines.append("overlap: none")
    try:
        unmet = unmet_dependencies(db, ws, depends_on, caller_id=caller_id)
    except agents.AgentError as e:
        lines.append(f"queue: would not start: {e}")
    else:
        if unmet:
            lines.append(f"queue: would queue until {', '.join(unmet)} merge"
                         f"{'s' if len(unmet) == 1 else ''}")
        elif mode == "block":
            lines.append("queue: would not start (overlap)")
        elif mode == "queue":
            lines.append(f"queue: would queue until {clash.holder} merges")
        elif busy := room_wait(ws.repo_root):
            lines.append(f"queue: would wait for room: {busy}")
        else:
            lines.append("queue: would start now")
    return "\n".join(lines)


def list_text(db: DB, repo_root: str, full: bool = False) -> str:
    """Queued/cancelled tasks and started branches still in review.

    Follow the task's worker to its workspace, never a branch-name lookup:
    attached repositories can have identically named branches. ``full`` adds
    each task's brief and done_when.
    """
    from brindle import pipeline

    lines = []
    for t in db.list_tasks(repo_root):
        ws, review = None, None
        if t.state == "started":
            worker = db.get_agent(t.agent_id) if t.agent_id else None
            ws = db.get_workspace(worker.workspace_id) if worker else None
            if ws is None or ws.repo_root != repo_root:
                continue
            review = pipeline.review_state(db, ws)
            if review is None:
                continue   # unreported or already merged; keep completed history out of the queue
        elif t.state not in (*QUEUED, "cancelled"):
            continue
        deps = _loads(t.depends_on)
        branch = ws.branch if ws else t.branch
        parts = [t.id, t.state, t.profile, f"branch={branch or '(auto)'}"]
        if deps:
            parts.append(f"depends_on={','.join(deps)}")
        if t.files:
            parts.append(f"files={','.join(_loads(t.files))}")
        if review is not None:
            parts.append(f"agent={t.agent_id} ws={ws.id} review={review}")
        if t.state in QUEUED and t.waiting:
            parts.append(f"waiting for room: {t.waiting}")
        line = " ".join(parts)
        if full:
            line += f"\n  task_text: {t.task_text}"
            if t.done_when:
                line += f"\n  done_when: {t.done_when}"
        lines.append(line)
    return "\n".join(lines) or "No pending or cancelled tasks."
