"""Several repos in one session (brindle Pro ``multi_repo``).

A session normally works in one repo: ``assign``/``handoff`` cut worktrees
there, and merges land there. ``brindle repo add <path>`` attaches another
local git repo to the current session under an alias; the supervisor can then
pass ``repo=<alias>`` to put a worker there. Such a worker is an ordinary
workspace of *that* repo: its worktree, ``.brindle`` config (setup, checks,
reviewer), base branch, review and merge all belong to that repo, and nothing
of one repo's pipeline touches another's.

Attachments are per session root (``session_repos`` in the database), so they
end with the session. Using any repo but the session's own needs the
``multi_repo`` entitlement; without it every such call fails closed with
:data:`PRO_MESSAGE`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from brindle import git, workspaces
from brindle.db import DB, Workspace

FEATURE = "multi_repo"
PRO_MESSAGE = (
    "Working across multiple repositories is a restricted feature of brindle Pro. "
    "The free tier is limited to a single repository per session. "
    "To upgrade, run `brindle account upgrade` or visit https://pawdelta.com/brindle."
)


class RepoError(RuntimeError):
    pass


def entitled() -> bool:
    """Whether the account's plan includes working across repos. Fails
    closed: no license, an unreadable one, or any error means no."""
    from brindle.pro import license

    try:
        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 -- an unreadable license is "not entitled"
        return False


def require_entitled() -> None:
    if not entitled():
        raise RepoError(PRO_MESSAGE)


# -- the session's repos ----------------------------------------------------------


@dataclass(frozen=True)
class Attached:
    alias: str
    repo_root: str
    branch: str | None = None    # the session's own branch there (see attach)


def attached(db: DB, root_id: str) -> list[Attached]:
    return [Attached(r.alias, r.repo_root, r.branch) for r in db.session_repos(root_id)]


def own_root(db: DB, root_id: str) -> str | None:
    """The repo the session itself runs in."""
    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    return ws.repo_root if ws else None


def session_roots(db: DB, root_id: str) -> list[tuple[str | None, str]]:
    """``(alias, repo_root)`` for the session's own repo (alias None) and
    every attached one, in that order."""
    out: list[tuple[str | None, str]] = []
    own = own_root(db, root_id)
    if own:
        out.append((None, own))
    out += [(a.alias, a.repo_root) for a in attached(db, root_id)]
    return out


def _normalize(path: str) -> str:
    """The main checkout of the git repo at ``path`` (a linked worktree
    resolves to its main repo), as an absolute path."""
    expanded = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(expanded):
        raise RepoError(f"{path} is not a directory")
    try:
        return git.main_repo_root(expanded)
    except git.GitError as e:
        raise RepoError(f"{path} is not a git repository") from e


def _owned(repo_root: str) -> bool:
    try:
        return os.stat(repo_root).st_uid == os.getuid()
    except (OSError, AttributeError):  # no uid on this platform: don't refuse
        return True


def default_alias(repo_root: str) -> str:
    return git.slug(os.path.basename(repo_root.rstrip(os.sep))) or "repo"


def attach(db: DB, root_id: str, path: str, alias: str | None = None) -> Attached:
    """Attach the repo at ``path`` to session ``root_id`` as ``alias``
    (default: the folder's name). The session gets its own branch and
    worktree there (``brindle/session-N``, cut from what the repo's checkout
    has checked out, as a second session in one repo does), which its
    workers in that repo branch from and merge into: several sessions can
    attach one repo without sharing a branch. Refuses the session's own
    repo, an alias already in use, a repo this session already has, and a
    repo this user doesn't own. Needs the ``multi_repo`` entitlement."""
    require_entitled()
    repo_root = _normalize(path)
    if not _owned(repo_root):
        raise RepoError(f"{repo_root} isn't owned by you; attach only your own local repos")
    own = own_root(db, root_id)
    if own and os.path.realpath(own) == os.path.realpath(repo_root):
        raise RepoError(f"{repo_root} is this session's own repo; it's already where workers go")
    alias = git.slug(alias) if alias else default_alias(repo_root)
    if not alias:
        raise RepoError("the alias is empty")
    for a in attached(db, root_id):
        if a.alias == alias:
            raise RepoError(f"alias {alias!r} is already {a.repo_root}; pick another with --name"
                            if a.repo_root != repo_root else f"{repo_root} is already attached as {alias!r}")
        if a.repo_root == repo_root:
            raise RepoError(f"{repo_root} is already attached as {a.alias!r}")
    from brindle.service import sessions as svc

    try:
        mine = svc.session_worktree(db, workspaces.adopt_root(db, repo_root))
    except (git.GitError, workspaces.WorkspaceError) as e:
        raise RepoError(f"couldn't make this session's branch in {repo_root}: {e}") from e
    db.add_session_repo(root_id, alias, repo_root, mine.branch)
    return Attached(alias, repo_root, mine.branch)


def detach(db: DB, root_id: str, ref: str) -> Attached:
    """Detach by alias or path. The session's branch and worktree there, and
    its workers' worktrees and branches, stay."""
    found = resolve_attached(db, root_id, ref)
    if found is None:
        raise RepoError(f"no attached repo {ref!r}; `brindle repo ls` lists them")
    db.remove_session_repo(root_id, found.alias)
    return found


def resolve_attached(db: DB, root_id: str, ref: str) -> Attached | None:
    """The attached repo ``ref`` names, by alias or by path, or None."""
    items = attached(db, root_id)
    for a in items:
        if a.alias == ref:
            return a
    expanded = os.path.abspath(os.path.expanduser(ref))
    if os.path.isdir(expanded):
        try:
            root = git.main_repo_root(expanded)
        except git.GitError:
            root = expanded
        for a in items:
            if os.path.realpath(a.repo_root) == os.path.realpath(root):
                return a
    return None


def resolve(db: DB, root_id: str, own_repo_root: str, ref: str | None) -> str:
    """The repo root ``ref`` (an alias or a path) names for session
    ``root_id``: ``own_repo_root`` when ``ref`` is empty or names the
    session's own repo (no entitlement needed), else an attached repo
    (needs ``multi_repo``). Raises :class:`RepoError` otherwise."""
    if not ref or not ref.strip():
        return own_repo_root
    ref = ref.strip()
    own_real = os.path.realpath(own_repo_root)
    if ref == default_alias(own_repo_root) and ref not in {a.alias for a in attached(db, root_id)}:
        return own_repo_root
    expanded = os.path.abspath(os.path.expanduser(ref))
    if os.path.isdir(expanded):
        try:
            if os.path.realpath(git.main_repo_root(expanded)) == own_real:
                return own_repo_root
        except git.GitError:
            pass
    found = resolve_attached(db, root_id, ref)
    if found is None:
        aliases = ", ".join(a.alias for a in attached(db, root_id)) or "none"
        raise RepoError(f"no repo {ref!r} in this session (attached: {aliases}); "
                        "the user attaches one with `brindle repo add <path> [--name alias]`")
    require_entitled()
    return found.repo_root


def session_branch(db: DB, root_id: str, repo_root: str) -> str | None:
    """Session ``root_id``'s own branch in attached repo ``repo_root`` (see
    :func:`attach`), None if it has none."""
    real = os.path.realpath(repo_root)
    for r in db.session_repos(root_id):
        if os.path.realpath(r.repo_root) == real:
            return r.branch
    return None


def is_session_branch(db: DB, repo_root: str, branch: str | None) -> bool:
    """Whether ``branch`` is some session's own branch in attached repo
    ``repo_root``: its workers' merge target, kept while it's attached."""
    return bool(branch) and any(r.repo_root == repo_root and r.branch == branch
                                for r in db.session_repos())


def target_workspace(db: DB, root_id: str, repo_root: str) -> Workspace:
    """The checkout of ``repo_root`` that session ``root_id``'s workers there
    branch from and merge into, and its milestone checks run in: the
    session's own branch's worktree (made again if it's gone). A repo
    attached before sessions had their own branch, or not attached at all,
    uses its main checkout."""
    branch = session_branch(db, root_id, repo_root)
    if not branch:
        return workspaces.adopt_root(db, repo_root)
    for ws in db.find_workspaces(repo_root):
        if ws.branch == branch and os.path.isdir(ws.path):
            return ws
    return workspaces.checkout_for(db, repo_root, branch=branch)


def alias_of(db: DB, root_id: str, repo_root: str) -> str | None:
    """The alias ``repo_root`` is attached under in session ``root_id``,
    None for the session's own repo (or an unknown one)."""
    real = os.path.realpath(repo_root)
    for a in attached(db, root_id):
        if os.path.realpath(a.repo_root) == real:
            return a.alias
    return None


def guidance(db: DB, root_id: str) -> str | None:
    """A prompt section for a supervisor whose session has attached repos."""
    items = attached(db, root_id)
    if not items:
        return None
    lines = "\n".join(f"- `{a.alias}`: {a.repo_root}" for a in items)
    return ("## Attached repos\n\n"
            "This session also works in these repos (`brindle repo ls`):\n"
            f"{lines}\n\n"
            "Pass `repo=\"<alias>\"` to assign or handoff to put a worker there: it gets a "
            "worktree in that repo, that repo's own checks and review, and merges into this "
            "session's own branch there (`brindle/session-N`, which other sessions don't touch). "
            "`depends_on` may name a task in another repo. A milestone check "
            "for another repo is written `@<alias>: <command>`. list_agents and list_tasks "
            "cover every repo in the session (or one, with `repo`).")


def listing_text(db: DB, root_id: str) -> str:
    items = attached(db, root_id)
    if not items:
        return "No repos attached to this session. Attach one with `brindle repo add <path> [--name alias]`."
    return "\n".join(f"{a.alias:<16} {a.repo_root}" + (f"  (branch {a.branch})" if a.branch else "")
                     for a in items)


def split_check(check: str | None) -> tuple[str | None, str | None]:
    """``"@web: cmd"`` -> ``("web", "cmd")``; a plain command -> ``(None, cmd)``."""
    if not check:
        return None, None
    text = check.strip()
    if text.startswith("@"):
        alias, sep, cmd = text[1:].partition(":")
        alias = alias.strip()
        if sep and alias and cmd.strip():
            return alias, cmd.strip()
    return None, text or None
