"""Leftover worktrees, cleaned up on their own (the hourly sweep, see
``disk.now_and_then``), conservatively:

1. A brindle workspace kept at merge or report (a process or pane was in it
   then, or it had uncommitted changes) is looked at again: once its branch
   is merged, or its workers are all closed, nothing runs in it and it has
   been quiet for ``disk.FRESH``, it goes.
2. ``git worktree prune`` forgets worktrees whose folder is gone.
3. A worktree an agent made by hand (``git worktree add``) in its Claude
   Code session's scratchpad goes once that session has ended. So does a
   Claude Code subagent's one under ``.claude/worktrees`` once its branch
   is merged and it has been quiet for a day.

Uncommitted changes are saved first, as a patch under
``brindle_home()/worktree-patches/<date>/`` (``git apply`` takes it), and
the folder itself goes to brindle's trash (``git.remove_worktree``). No
branch is ever deleted. A worktree anywhere else (the person's own
checkouts) is never touched. Everything that made a worktree removable is
read again right before it goes, and nothing goes on a doubt."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from brindle import config, disk, git

log = logging.getLogger(__name__)

PATCHES_DIR = "worktree-patches"
CLAUDE_WORKTREES = os.path.join(".claude", "worktrees")
SUBAGENT_QUIET = disk.DAY      # a .claude/worktrees one, merged, untouched this long, goes


# -- what holds a worktree ----------------------------------------------------------


def _alive(db, agents_list) -> bool:
    from brindle import agents

    return any(agents.is_alive(a) for a in agents_list)


def _all_finished(agents_list) -> bool:
    from brindle import view

    return all(view._finished(a) for a in agents_list)


def _tip(root: str, ref: str) -> str:
    return git.run(["rev-parse", "--verify", "--quiet", ref], root, check=False).stdout.strip()


def _in_use(db, path: str, own=None) -> bool:
    """A process whose folder is in it, a pane there, or a live worker of a
    workspace inside it; unknown counts as in use (``disk.live_in``)."""
    try:
        return disk.live_in(db, path, own=own, stopping=())
    except Exception:  # noqa: BLE001 - whatever couldn't be told keeps it
        log.debug("brindle: couldn't tell whether %s is in use", path, exc_info=True)
        return True


# -- saving uncommitted changes -----------------------------------------------------


def _label(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "worktree"


def save_patch(path: str, label: str, now: float | None = None) -> Path | None:
    """Every uncommitted change of the worktree at ``path`` (tracked and
    untracked files, not ignored ones) as a binary patch against its HEAD,
    under ``brindle_home()/worktree-patches/<date>/``, built in a scratch
    index so the worktree's own index is left alone. None when there is
    nothing to save. Raises (``git.GitError``, ``OSError``) when it couldn't
    be saved: the worktree must then stay."""
    if not git.dirty_files(path):
        return None
    now = time.time() if now is None else now
    day = config.brindle_home() / PATCHES_DIR / time.strftime("%Y-%m-%d", time.localtime(now))
    day.mkdir(mode=0o700, parents=True, exist_ok=True)
    stem = f"{_label(label)}-{time.strftime('%H%M%S', time.localtime(now))}"
    dest = day / f"{stem}.patch"
    n = 1
    while dest.exists():
        n += 1
        dest = day / f"{stem}-{n}.patch"
    git_dir = git.out(["rev-parse", "--path-format=absolute", "--git-dir"], path)
    index = os.path.join(git_dir, "brindle-patch-index")
    raw = Path(f"{dest}.part")
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        git.run_env(["read-tree", "HEAD"], path, env)
        git.run_env(["add", "-A", "--", "."], path, env)
        git.run_env(["diff", "--cached", "--binary", f"--output={raw}", "HEAD"], path, env)
        head = git.out(["rev-parse", "HEAD"], path)
        body = raw.read_bytes()
        header = (f"# brindle saved the uncommitted changes of {path} (at {head}) before removing it.\n"
                  f"# Apply them in a checkout of that commit with: git apply --binary {dest}\n\n")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(header.encode() + body)
    finally:
        for leftover in (index, str(raw)):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
    return dest


# -- 1. brindle's own workspaces ----------------------------------------------------


@dataclass
class _Judged:
    ws_id: str
    path: str
    branch: str
    tip: str


def _workspace_done(db, ws, now: float) -> _Judged | None:
    """``ws`` if it can go now: one of brindle's worktrees (its row, under
    brindle's worktree roots) that nothing of anyone's is running in or may
    resume in, finished (its branch merged into its base, or every worker
    of it closed), and quiet for ``disk.FRESH``."""
    from brindle import repos, view, workspaces

    if ws.kind != "worktree" or not os.path.isdir(ws.path) or not disk._owned(ws.repo_root, ws.path):
        return None
    if repos.is_session_branch(db, ws.repo_root, ws.branch):
        return None
    everyone = db.list_agents(ws.id)
    if not _all_finished(everyone) or _alive(db, everyone):
        return None
    tip = _tip(ws.repo_root, f"refs/heads/{ws.branch}")
    if not tip:
        return None
    merged = db.merged_sha(ws.id) == tip or workspaces._merged_into_base(ws, db)
    closed = bool(everyone) and all(a.dismissed_at is not None for a in everyone)
    if not (merged or closed):
        return None
    if not everyone and now - ws.created_at <= view.UNUSED_WORKTREE_GRACE:
        return None
    if now - disk._last_activity(ws.repo_root, ws.path, ws.branch, [ws]) < disk.FRESH:
        return None
    return _Judged(ws.id, ws.path, ws.branch, tip)


def _sharing_live(db, ws) -> bool:
    from brindle import workspaces

    return any(workspaces.live_workers(db, o)
               for o in db.workspaces_sharing(ws.repo_root, path=ws.path, exclude_id=ws.id))


def sweep_workspaces(db, now: float) -> list[str]:
    from brindle import workspaces

    done = []
    for ws in db.find_workspaces():
        try:
            judged = _workspace_done(db, ws, now)
        except git.GitError:
            continue
        if judged is None:
            continue
        # Right before it goes: everything that made it removable, read again.
        ws = db.get_workspace(judged.ws_id)
        try:
            again = _workspace_done(db, ws, now) if ws is not None else None
        except git.GitError:
            continue
        if again != judged or _sharing_live(db, ws) or _in_use(db, ws.path, own=ws):
            continue
        try:
            patch = save_patch(ws.path, ws.branch, now)
            workspaces.remove(db, ws, force=True, delete_branch=False, stop=())
        except (workspaces.WorkspaceError, git.GitError, OSError) as e:
            log.info("brindle: kept worktree %s: %s", ws.path, e)
            continue
        done.append(f"removed finished worktree {ws.path} (branch {ws.branch} kept"
                    + (f"; uncommitted changes saved to {patch})" if patch else ")"))
    return done


# -- 3. worktrees agents made by hand --------------------------------------------------


def _slug(path: str | Path) -> str:
    """A folder as Claude Code names its project folder: every other character a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _tmp_base() -> str | None:
    """``<tmp>/claude-<uid>``, where Claude Code keeps its sessions'
    scratchpads, when it is this person's; None otherwise."""
    if not hasattr(os, "getuid"):
        return None
    base = os.path.join(os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp", f"claude-{os.getuid()}")
    try:
        if os.stat(base).st_uid != os.getuid():
            return None
    except OSError:
        return None
    return os.path.realpath(base)


def scratchpad_of(path: str, base: str | None) -> tuple[str, str] | None:
    """``(project, session)`` when ``path`` is inside a Claude Code session's
    scratchpad, ``<base>/<project>/<session>/scratchpad/...``."""
    if base is None:
        return None
    real = os.path.realpath(path)
    if not disk._inside(real, base) or real == base:
        return None
    parts = os.path.relpath(real, base).split(os.sep)
    if len(parts) < 4 or parts[2] != "scratchpad":
        return None
    return parts[0], parts[1]


def _brindle_project(db, project: str) -> bool:
    """Whether the folder a Claude Code session started in (its slug) was
    one of brindle's worktrees."""
    roots = {ws.repo_root for ws in db.find_workspaces()} | set(disk._repos(db, None))
    for root in roots:
        try:
            owned = [config.worktree_root(root), config.worktrees_dir()]
        except ValueError:
            owned = [config.worktrees_dir()]
        for r in owned:
            if any(project.startswith(_slug(p) + "-") for p in {str(r), os.path.realpath(r)}):
                return True
    return False


def session_ended(db, project: str, session: str) -> bool:
    """Whether the Claude Code session ``session`` (started in the folder
    whose slug is ``project``) was brindle's and is over: its agents are
    known and all finished, none running; or none is known any more (its
    records went with its workspace) and it started in one of brindle's
    worktrees that is gone. A session brindle can't place is never over."""
    mine = [a for a in db.list_agents() if a.session_ref == session]
    if mine:
        return _all_finished(mine) and not _alive(db, mine)
    if not _brindle_project(db, project):
        return False
    return not any(_slug(p) == project for ws in db.find_workspaces()
                   for p in {ws.path, os.path.realpath(ws.path)})


def _subagent_tree(db, root: str, path: str) -> bool:
    """A Claude Code subagent's worktree: under ``.claude/worktrees`` of the
    repo or of one of brindle's worktrees."""
    real = os.path.realpath(path)
    homes = [root, *(ws.path for ws in db.find_workspaces(root) if disk._owned(root, ws.path))]
    return any(disk._inside(real, os.path.join(os.path.realpath(h), CLAUDE_WORKTREES)) and
               real != os.path.join(os.path.realpath(h), CLAUDE_WORKTREES) for h in homes)


@dataclass
class _Loose:
    root: str
    path: str
    head: str
    branch: str | None
    why: str


def _loose_done(db, root: str, wt: dict, base: str | None, now: float) -> _Loose | None:
    """A worktree outside brindle's rows that can go: one in a session's
    scratchpad whose session ended, or a subagent's that is merged and quiet."""
    path = wt.get("worktree", "")
    if not path or not os.path.isdir(path) or "locked" in wt or db.workspace_by_path(path) \
            or db.workspace_by_path(os.path.realpath(path)):
        return None
    head = wt.get("HEAD", "")
    ref = wt.get("branch", "")
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
    if branch and branch.startswith(disk.POOL_PREFIX):
        return None
    pad = scratchpad_of(path, base)
    if pad is not None:
        return _Loose(root, path, head, branch, "its session ended") if session_ended(db, *pad) else None
    if not _subagent_tree(db, root, path) or not head:
        return None
    default = git.default_branch(root)
    if not default or branch == default:
        return None
    merged = (git.ok(["merge-base", "--is-ancestor", head, f"refs/heads/{default}"], root)
              or (branch is not None and disk.merged(root, branch, default)))
    if not merged or now - disk._last_activity(root, path, branch or "", []) < SUBAGENT_QUIET:
        return None
    return _Loose(root, path, head, branch, "merged and untouched for a day")


def _worktree_at(root: str, path: str) -> dict | None:
    real = os.path.realpath(path)
    return next((wt for wt in git.list_worktrees(root)[1:]
                 if os.path.realpath(wt.get("worktree", "")) == real), None)


def sweep_loose(db, root: str, now: float) -> list[str]:
    base = _tmp_base()
    done = []
    for wt in git.list_worktrees(root)[1:]:   # the first is the main checkout
        try:
            judged = _loose_done(db, root, wt, base, now)
        except git.GitError:
            continue
        if judged is None:
            continue
        # Right before it goes: still that worktree, at that commit, still done.
        try:
            now_wt = _worktree_at(root, judged.path)
            again = _loose_done(db, root, now_wt, base, now) if now_wt is not None else None
        except git.GitError:
            continue
        if again != judged or _in_use(db, judged.path):
            continue
        try:
            patch = save_patch(judged.path, judged.branch or os.path.basename(judged.path), now)
            git.remove_worktree(root, judged.path, force=True)
        except (git.GitError, OSError) as e:
            log.info("brindle: kept worktree %s: %s", judged.path, e)
            continue
        kept = f"branch {judged.branch} kept" if judged.branch else "detached"
        done.append(f"removed worktree {judged.path} ({judged.why}; {kept}"
                    + (f"; uncommitted changes saved to {patch})" if patch else ")"))
    return done


# -- the sweep ------------------------------------------------------------------------


def sweep(db, now: float | None = None) -> list[str]:
    """All of the above, for every repo brindle knows. Never raises: a step
    that fails is logged and the others still run."""
    now = time.time() if now is None else now
    done: list[str] = []
    try:
        roots = disk._repos(db, None)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't list the repos to sweep for leftover worktrees")
        return done
    for root in roots:   # 2. folders that are gone
        try:
            git.run(["worktree", "prune"], root, check=False)
        except git.GitError as e:
            log.info("brindle: couldn't prune %s's worktrees: %s", root, e)
    try:
        done += sweep_workspaces(db, now)
    except Exception:  # noqa: BLE001
        log.exception("brindle: the finished-worktree sweep failed")
    for root in roots:
        try:
            done += sweep_loose(db, root, now)
        except Exception:  # noqa: BLE001
            log.exception("brindle: the leftover-worktree sweep of %s failed", root)
    for line in done:
        log.info("brindle: %s", line)
    return done
