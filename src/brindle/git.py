"""Thin, explicit wrappers around the git CLI.

Every call goes through ``run`` so failures surface as ``GitError`` carrying
git's own stderr, and a hung git process can't hang the orchestrator.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from brindle import spawn

TIMEOUT = 120
BASE_CONFIG_KEY = "brindle-base"
QUICK_FETCH_SECONDS = 5      # a refresh someone waits on, with what's already here as good enough


class GitError(RuntimeError):
    pass


class GitBusy(GitError, spawn.Busy):
    """A git read the machine had no room for, or that its request stopped
    (see brindle.spawn): a GitError like a timeout, and a Busy for the daemon."""


# Commands that change nothing in the repo: they run as brindle.spawn reads
# (stopped at their request's deadline, never taking git's optional locks).
# Everything else (merge, commit, checkout, fetch, push, worktree add ...) is
# an action, which no deadline stops once it has started.
_READS = frozenset({
    "blame", "cat-file", "check-attr", "check-ignore", "cherry", "count-objects", "describe",
    "diff", "diff-files", "diff-index", "diff-tree", "for-each-ref", "grep", "log", "ls-files",
    "ls-tree", "merge-base", "merge-tree", "name-rev", "patch-id", "rev-list", "rev-parse",
    "shortlog", "show", "show-ref", "status", "var",
})
_GLOBAL_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}


def is_read(args: list[str]) -> bool:
    """Whether ``git <args>`` only reads (see _READS)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GLOBAL_WITH_VALUE else 1
    if i >= len(args):
        return False
    cmd, rest = args[i], args[i + 1:]
    if cmd in _READS:
        return True
    words = [a for a in rest if not a.startswith("-")]
    if cmd == "config":
        return any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in rest)
    if cmd == "symbolic-ref":    # a second name would set the ref
        return len(words) <= 1 and "-d" not in rest and "--delete" not in rest
    if cmd == "remote":
        return not words or words[0] == "get-url"
    if cmd == "worktree":
        return bool(words) and words[0] == "list"
    return False


def run(args: list[str], cwd: str | Path, check: bool = True,
        timeout: int | float | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    return run_env(args, cwd, None, check=check, timeout=timeout, input=input)


def run_env(args: list[str], cwd: str | Path, env: dict[str, str] | None,
            check: bool = True, timeout: int | float | None = None,
            input: str | None = None) -> subprocess.CompletedProcess:
    """``run`` with its own environment (e.g. ``GIT_INDEX_FILE`` for a scratch
    index that leaves the checkout's real index alone; see brindle.rewind)."""
    to = TIMEOUT if timeout is None else timeout
    reading = is_read(args)
    start = spawn.read if reading else spawn.action
    if not reading:
        invalidate_branches()
    try:
        proc = start(["git", *args], cwd=cwd, env=env, input=input, timeout=to)
        if not reading:
            invalidate_branches()    # and after, so a lookup during the action can't re-cache old refs
    except spawn.Busy as e:
        raise GitBusy(f"git {' '.join(args)}: {e}") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)}: {e}") from e
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def out(args: list[str], cwd: str | Path) -> str:
    return run(args, cwd).stdout.strip()


def ok(args: list[str], cwd: str | Path) -> bool:
    return run(args, cwd, check=False).returncode == 0


# -- network git that nobody asked for ---------------------------------------
#
# What brindle fetches on its own (a session starting, a sync, a poll) must not
# raise a dialog nobody can explain: on macOS git's keychain helper asks for the
# login password whenever the git running here isn't the one approved in the
# person's terminal. So these calls clear every credential helper, gh's too (its
# token read goes through `security`, which a locked keychain answers with a
# dialog), and can't prompt in any other way. What the person asked for (`git
# push`, `brindle pr`, a named PR's branch) goes through ``run``, with their
# normal credentials.

_GITHUB_HELPER = "credential.https://github.com.helper"
GH_LOOKUP_SECONDS = 5.0
GH_SEEN_SECONDS = 60.0       # how long a sign-in lookup stands: a pane's launch shouldn't each start gh
_gh_seen: dict[str, tuple[float, str | None]] = {}
_FETCH_REFUSED = "brindle-fetch-refused"
# git's and ssh's words for "not without credentials" (LC_ALL=C: see _background_env),
# as opposed to offline or no such repo.
_REFUSED = re.compile(r"terminal prompts disabled|could not read (username|password)|authentication failed"
                      r"|permission denied|host key verification failed|returned error: 40[13]", re.IGNORECASE)
_SHELL_WORD = re.compile(r"""'[^']*'|"[^"]*"|\S+""")


def gh_credential_helper() -> str | None:
    """The git credential helper of a ``gh`` signed in to github.com. Asking
    gh's config touches neither the network nor the keychain."""
    exe = shutil.which("gh")
    if exe is None:
        return None
    now = time.monotonic()
    seen = _gh_seen.get(exe)
    if seen is not None and now - seen[0] < GH_SEEN_SECONDS:
        return seen[1]
    try:
        proc = spawn.read([exe, "config", "get", "user", "-h", "github.com"], timeout=GH_LOOKUP_SECONDS)
    except (OSError, subprocess.SubprocessError, spawn.Busy):
        return None
    helper = f"!{shlex.quote(exe)} auth git-credential" if proc.returncode == 0 and proc.stdout.strip() else None
    _gh_seen[exe] = (now, helper)
    return helper


def pane_git_config() -> str | None:
    """``GIT_CONFIG_PARAMETERS`` for an agent's pane: github.com signs in through
    gh alone (when gh is signed in), the person's other helpers for that host
    cleared, as a helper still listed would also be told to store and erase
    what gh gave it: the keychain dialog over again, and their entry overwritten.
    Every other host keeps the person's helpers; their config isn't edited.

    Not GIT_CONFIG_COUNT and its GIT_CONFIG_KEY_<n>: a name with KEY in it is
    what environment filters drop (brindle's own, Codex's), and git refuses to
    run at all with a count whose key went missing.

    gh reads its token through `security`, so a locked keychain can still ask
    here: an agent's push is one at a time, not a poll."""
    helper = gh_credential_helper()
    if helper is None:
        return None
    entries = (f"{_GITHUB_HELPER}={value}" for value in ("", helper))
    ours = " ".join("'" + entry.replace("'", "'\\''") + "'" for entry in entries)    # git takes single quotes only
    inherited = os.environ.get("GIT_CONFIG_PARAMETERS")
    return f"{inherited} {ours}" if inherited else ours


def _background_env(cwd: str | Path) -> dict[str, str]:
    """The environment of a network call nobody asked for: no askpass program
    (an empty GIT_ASKPASS also passes over core.askPass and SSH_ASKPASS), no
    terminal prompt, and ssh that fails rather than asks (for a passphrase, or
    about a host key), still with an agent's keys and the person's own command."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
           "SSH_ASKPASS_REQUIRE": "never", "LC_ALL": "C"}
    if ssh := _batch_ssh(cwd):
        env["GIT_SSH_COMMAND"] = ssh
    return env


def _batch_ssh(cwd: str | Path) -> str | None:
    """The ssh command git would run, in BatchMode: right after ``ssh`` itself,
    wherever a wrapper puts it (``env A=1 ssh -F x``), as ssh keeps the first
    value of an option and the command may set its own. A program that isn't
    named ssh gets it last, where git's own arguments go: a wrapper passes those
    on, and its own options stay its own (so its ``-o BatchMode=no`` would win).
    None for PuTTY's, which takes no ``-o``."""
    try:
        ssh = os.environ.get("GIT_SSH_COMMAND") or run(["config", "--get", "core.sshCommand"], cwd,
                                                       check=False).stdout.strip()
    except GitError:
        ssh = ""
    ssh = ssh or shlex.quote(os.environ.get("GIT_SSH") or "ssh")     # GIT_SSH: a bare program, no options
    words = [(word.end(), os.path.basename(word.group().strip("'\"")).lower())
             for word in _SHELL_WORD.finditer(ssh) if not re.match(r"\w+=", word.group())]    # not NAME=value
    for end, name in words:
        if name.removesuffix(".exe") == "ssh":
            return f"{ssh[:end]} -o BatchMode=yes{ssh[end:]}"
    if any("plink" in name for _, name in words):
        return None
    return f"{ssh} -o BatchMode=yes"


def background(args: list[str], cwd: str | Path,
               timeout: int | float | None = None) -> subprocess.CompletedProcess | None:
    """``git <args>``, a network call brindle starts on its own: it can't raise a
    credential dialog. None when git couldn't run or finish; a failure is the
    caller's to read."""
    try:
        return run_env(["-c", "credential.helper=", *args], cwd, _background_env(cwd), check=False, timeout=timeout)
    except GitError:
        return None


def background_fetch(root: str | Path, args: list[str], timeout: int | float | None = None) -> bool:
    """``git fetch <args>`` as ``background``: True when it worked. A failure
    leaves the refs as they were; one for want of credentials goes on record
    (fetch_refused), and a later success takes it off."""
    proc = background(["fetch", *args], root, timeout)
    if proc is None:
        return False
    _note_refusal(root, proc)
    return proc.returncode == 0


def _note_refusal(root: str | Path, proc: subprocess.CompletedProcess) -> None:
    """Keep whether origin wants credentials, across brindle's processes."""
    try:
        marker = _refused_marker(root)
        if proc.returncode != 0 and _REFUSED.search(proc.stderr or ""):
            marker.touch()
        elif proc.returncode == 0:
            marker.unlink(missing_ok=True)
    except (GitError, OSError):
        pass


def _refused_marker(root: str | Path) -> Path:
    dot_git = Path(root, ".git")
    common = dot_git if dot_git.is_dir() else Path(out(["rev-parse", "--path-format=absolute", "--git-common-dir"], root))
    return common / _FETCH_REFUSED


def fetch_refused(root: str | Path) -> bool:
    """Whether the last background refresh of this repo failed for want of
    credentials: its remote-tracking refs may be behind origin."""
    try:
        return _refused_marker(root).exists()
    except (GitError, OSError):
        return False


# -- repo discovery --------------------------------------------------------


def main_repo_root(path: str | Path) -> str:
    """The main checkout's root, even when ``path`` is inside a linked worktree."""
    common = out(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    common_path = Path(common)
    if common_path.name == ".git":
        return str(common_path.parent)
    # Bare repo or unusual layout: fall back to the current toplevel.
    return out(["rev-parse", "--show-toplevel"], path)


def toplevel(path: str | Path) -> str:
    return out(["rev-parse", "--show-toplevel"], path)


def current_branch(path: str | Path) -> str | None:
    proc = run(["symbolic-ref", "--quiet", "--short", "HEAD"], path, check=False)
    return proc.stdout.strip() or None


def default_branch(root: str | Path) -> str:
    proc = run(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().split("/", 1)[1]
    for candidate in ("main", "master"):
        if branch_exists(root, candidate):
            return candidate
    return current_branch(root) or "main"


# Local branch names per repo root, from one ``for-each-ref`` that answers every
# lookup. Correctness choice: the snapshot is dropped whenever brindle runs any
# git action (branch create/delete/rename, commit, fetch ...: see run_env) and
# otherwise lives at most _BRANCHES_TTL seconds, so a branch created or deleted
# outside brindle is seen within that window. Bounded, process-local, locked.
_BRANCHES_TTL = 2.0
_BRANCHES_MAX = 64
_BRANCHES: dict[str, tuple[float, frozenset[str]]] = {}
_BRANCHES_LOCK = threading.Lock()
# Bumped by every invalidation. A listing read while one landed is discarded
# (never published), so a slow read can't re-cache refs from before a change.
_BRANCHES_GEN = 0


def invalidate_branches() -> None:
    """Forget every cached branch listing (brindle changed refs)."""
    global _BRANCHES_GEN
    with _BRANCHES_LOCK:
        _BRANCHES_GEN += 1
        _BRANCHES.clear()


def branch_exists(root: str | Path, branch: str) -> bool:
    key = str(root)
    names: frozenset[str] = frozenset()
    for _ in range(2):    # one re-read if an invalidation raced the first
        now = time.monotonic()
        with _BRANCHES_LOCK:
            hit = _BRANCHES.get(key)
            gen = _BRANCHES_GEN
        if hit is not None and now - hit[0] < _BRANCHES_TTL:
            return branch in hit[1]
        proc = run(["for-each-ref", "--format=%(refname)", "refs/heads"], root, check=False)
        if proc.returncode != 0:
            return False
        prefix = "refs/heads/"
        names = frozenset(r[len(prefix):] for r in proc.stdout.splitlines() if r.startswith(prefix))
        with _BRANCHES_LOCK:
            if gen == _BRANCHES_GEN:
                if len(_BRANCHES) >= _BRANCHES_MAX and key not in _BRANCHES:
                    _BRANCHES.pop(next(iter(_BRANCHES)))
                _BRANCHES[key] = (now, names)
                return branch in names
    return branch in names    # raced twice: answer from the last read, uncached


def remote_branch_exists(root: str | Path, branch: str, remote: str = "origin") -> bool:
    return ok(["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"], root)


def merged_into_default(root: str | Path, branch: str) -> bool:
    """True if ``branch`` has already been merged into the default branch
    (main/master) or has no unique changes remaining against it."""
    base = default_branch(root)
    if not branch or branch == base or branch == "HEAD":
        return False
    if has_remote(root, "origin"):
        background_fetch(root, ["origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], QUICK_FETCH_SECONDS)
    refs = ref_shas(root)
    return merged_into(root, branch, default_targets(root, refs, base), refs=refs, thorough=True)


def ref_shas(root: str | Path) -> dict[str, str]:
    """Every local and remote-tracking branch's commit, by full ref name
    (``refs/heads/main``, ``refs/remotes/origin/main``), in one git call. Full
    names keep a local branch called ``origin/x`` apart from origin's ``x``."""
    shas: dict[str, str] = {}
    lines = out(["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes", "refs/heads"],
                root).splitlines()
    for line in lines:
        ref, _, sha = line.partition(" ")
        if sha:
            shas[ref] = sha
    return shas


def _full_ref(name: str) -> str:
    """A branch name as its ref: ``refs/...`` as given, else a local branch."""
    return name if name.startswith("refs/") else f"refs/heads/{name}"


def default_targets(root: str | Path, refs: dict[str, str], base: str | None = None) -> list[str]:
    """The default branch and, when it exists, its origin copy: what
    "merged" means without a more specific target."""
    base = base or default_branch(root)
    remote = f"refs/remotes/origin/{base}"
    return [base] + ([remote] if remote in refs else [])


# Decided "merged" per (branch sha, target sha): a commit graph is content-addressed,
# so an answer for one pair never goes stale. "thorough" answers live under a third key.
_MERGED: dict[tuple[str, ...], bool | None] = {}
_MERGED_MAX = 4096


def merged_into(root: str | Path, branch: str, targets: list[str], *,
                refs: dict[str, str] | None = None, thorough: bool = False) -> bool:
    """Whether local branch ``branch`` has landed in any of ``targets`` (local
    branch names, or full ``refs/...`` names; see ``ref_shas``): an ancestor of one, the same tree, no changes of its own,
    or a merge into it that changes nothing. Never fetches, and each
    (branch, target) pair is decided once per pair of commits. Only when
    those checks can't tell (a conflicting merge-tree, unrelated histories)
    and ``thorough`` is set does it compare patches (cherry, patch-id): a
    squash merge that main has since edited over."""
    if not branch or branch == "HEAD":
        return False
    refs = ref_shas(root) if refs is None else refs
    own = _full_ref(branch)
    b = refs.get(own)
    if b is None:
        return False
    full = [_full_ref(t) for t in targets]
    shas = list(dict.fromkeys(refs[t] for t in full if t != own and t in refs))
    unsure = []
    for t in shas:
        verdict = _merged_cheap(root, b, t)
        if verdict:
            return True
        if verdict is None:
            unsure.append(t)
    if not thorough:
        return False
    for t in unsure:
        key = (b, t, "thorough")
        if key not in _MERGED:
            try:
                _remember(key, _merged_by_patch(root, b, t))
            except GitBusy:
                continue
        if _MERGED[key]:
            return True
    return False


def _remember(key: tuple[str, ...], verdict: bool | None) -> None:
    if len(_MERGED) >= _MERGED_MAX:
        _MERGED.clear()
    _MERGED[key] = verdict


def _merged_cheap(root: str | Path, b: str, t: str) -> bool | None:
    """Commit ``b`` landed in commit ``t``: True, False, or None when the
    cheap checks can't tell. Cached; a busy machine's answer isn't."""
    key = (b, t)
    if key in _MERGED:
        return _MERGED[key]
    try:
        verdict = _cheap_verdict(root, b, t)
    except GitBusy:
        return None
    _remember(key, verdict)
    return verdict


def _cheap_verdict(root: str | Path, b: str, t: str) -> bool | None:
    if b == t:
        return True
    try:
        proc = run(["merge-base", t, b], root, check=False)
        mb = proc.stdout.strip()
        if proc.returncode != 0 or not mb:
            return None
        if mb == b:
            return True
        b_tree, t_tree, mb_tree = out(["rev-parse", f"{b}^{{tree}}", f"{t}^{{tree}}", f"{mb}^{{tree}}"],
                                      root).split()
        if b_tree in (t_tree, mb_tree):
            return True
        merged = run(["merge-tree", "--write-tree", t, b], root, check=False)
    except GitBusy:
        raise
    except (GitError, ValueError):
        return None
    if merged.returncode == 0 and merged.stdout.strip():
        return merged.stdout.split()[0] == t_tree
    return None    # conflicts (or a git too old for --write-tree)


def _merged_by_patch(root: str | Path, b: str, t: str) -> bool:
    """Every patch of commit ``b`` is already in commit ``t`` (cherry), or its
    whole change matches one commit of ``t``'s (patch-id): the slow checks.
    Raises GitBusy rather than answer from a read the machine had no room for."""
    try:
        cherry = out(["cherry", t, b], root).splitlines()
        if cherry and all(line.startswith("-") for line in cherry):
            return True
    except GitBusy:
        raise
    except GitError:
        pass
    try:
        mb = out(["merge-base", t, b], root).strip()
        if mb:
            feat_diff = out(["diff", f"{mb}..{b}"], root)
            if feat_diff:
                feat_pid_res = run(["patch-id"], root, input=feat_diff, check=False)
                if feat_pid_res.returncode == 0 and feat_pid_res.stdout.strip():
                    feat_pid = feat_pid_res.stdout.strip().split()[0]
                    commits = out(["rev-list", f"{mb}..{t}"], root).splitlines()
                    for c in commits:
                        c_diff = out(["diff", f"{c}~1..{c}"], root)
                        c_pid_res = run(["patch-id"], root, input=c_diff, check=False)
                        if c_pid_res.returncode == 0 and c_pid_res.stdout.strip():
                            if c_pid_res.stdout.strip().split()[0] == feat_pid:
                                return True
    except GitBusy:
        raise
    except GitError:
        pass
    return False


def list_branches(root: str | Path, pattern: str = "*") -> list[str]:
    proc = out(["for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"], root)
    return [b for b in proc.splitlines() if b]


def has_remote(root: str | Path, remote: str = "origin") -> bool:
    return remote in out(["remote"], root).split()


# -- branch naming ---------------------------------------------------------

_INVALID = re.compile(r"[^A-Za-z0-9._/-]+")


def sanitize_branch(name: str, max_len: int = 100) -> str:
    """Make ``name`` a valid, readable branch name. Stable: a name it returns
    comes back unchanged, so a name checked once is the name created."""
    s, prev = name, None
    while s != prev:   # truncating can leave a new ".lock" or "." to drop; each pass only shortens
        prev = s
        s = _INVALID.sub("-", s.strip())
        s = re.sub(r"/{2,}", "/", s)
        s = re.sub(r"-{2,}", "-", s)
        s = re.sub(r"\.{2,}", ".", s)
        s = "/".join(p.strip(".-") for p in s.split("/") if p.strip(".-"))
        s = s.removesuffix(".lock")[:max_len].strip("/.-")
        if not s:
            raise ValueError(f"cannot derive a branch name from {name!r}")
    return s


def slug(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", branch).strip("-").lower()


# -- worktrees -------------------------------------------------------------


def resolve_start_point(root: str | Path, base: str, fetch: bool) -> str:
    """Where a new branch should start: freshly fetched ``origin/<base>`` when
    it exists, otherwise the local base branch, otherwise HEAD. A failed fetch
    (offline, no auth) is not fatal; we fall back to what we have."""
    if fetch and has_remote(root):
        background_fetch(root, ["origin", base, "--quiet", "--no-tags"])
    if remote_branch_exists(root, base):
        local_ahead = branch_exists(root, base) and ok(
            ["merge-base", "--is-ancestor", f"origin/{base}", base], root
        )
        # If local base already contains origin (e.g. unpushed local commits),
        # prefer local so those commits aren't silently dropped.
        return base if local_ahead else f"origin/{base}"
    if branch_exists(root, base):
        return base
    return "HEAD"


def fetch_remote_branch(root: str | Path, branch: str, remote: str = "origin") -> str:
    """Fetch ``branch`` from ``remote`` into ``refs/remotes/<remote>/<branch>``.
    Unlike ``resolve_start_point``'s fetch, failure here is fatal: the caller
    needs that exact branch, which the person named: their normal credentials
    apply. Returns the remote-tracking ref name."""
    run(["fetch", "--quiet", "--no-tags", remote,
         f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"], root)
    return f"{remote}/{branch}"


def fast_forward(path: str | Path, branch: str) -> str | None:
    """Bring the checked-out ``branch`` up to ``origin/<branch>`` with a
    fast-forward only. Returns a warning when it couldn't (diverged, or local
    changes in the way), None when it's current, updated, offline or has no
    remote. Never rewrites history or touches uncommitted work."""
    if not has_remote(path):
        return None
    background_fetch(path, ["--quiet", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                     QUICK_FETCH_SECONDS)
    remote = f"origin/{branch}"
    if not remote_branch_exists(path, branch) or ok(["merge-base", "--is-ancestor", remote, "HEAD"], path):
        return None
    if not ok(["merge-base", "--is-ancestor", "HEAD", remote], path):
        return f"'{branch}' has diverged from '{remote}'; left as is."
    if run(["merge", "--ff-only", "--quiet", remote], path, check=False).returncode != 0:
        return f"'{branch}' is behind '{remote}'; run git pull --ff-only."
    return None


def upstream_gone(path: str | Path, branch: str) -> bool:
    """True if ``branch`` tracks origin and that branch no longer exists there."""
    if run(["config", "--get", f"branch.{branch}.remote"], path, check=False).stdout.strip() != "origin":
        return False
    proc = background(["ls-remote", "--exit-code", "--heads", "origin", branch], path, QUICK_FETCH_SECONDS)
    if proc is None:
        return False
    _note_refusal(path, proc)
    return proc.returncode == 2


def worktree_for_branch(root: str | Path, branch: str) -> str | None:
    for wt in list_worktrees(root):
        if wt.get("branch") == f"refs/heads/{branch}":
            return wt["worktree"]
    return None


def add_worktree(root: str | Path, path: str | Path, branch: str, start: str) -> str:
    """Check out ``branch`` at ``path``. Reuses a local or remote branch of that
    name when one exists; otherwise creates it from ``start``.
    Returns ``"existing"``, ``"remote"`` or ``"new"``."""
    elsewhere = worktree_for_branch(root, branch)
    if elsewhere:
        raise GitError(f"branch {branch!r} is already checked out at {elsewhere}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        run(["worktree", "add", str(path), branch], root)
        return "existing"
    if remote_branch_exists(root, branch):
        run(["worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}"], root)
        return "remote"
    run(["worktree", "add", "--no-track", "-b", branch, str(path), start], root)
    return "new"


def exclude(root: str | Path, path: str | Path) -> None:
    """Keep ``path`` (a folder inside the checkout at ``root``) out of ``git
    status`` through the repo's ``info/exclude``. A path outside ``root`` is
    left alone."""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    if rel == "." or rel.startswith(".."):
        return
    _add_exclude(root, "/" + Path(rel).as_posix() + "/")


def exclude_name(root: str | Path, rel: str) -> None:
    """Keep the entry ``rel`` (relative to the checkout at ``root``) out of
    ``git status`` whatever it is: a symlink too, which a ``dir/`` pattern
    doesn't match."""
    _add_exclude(root, "/" + rel.strip("/"))


def _add_exclude(root: str | Path, pattern: str) -> None:
    info = Path(out(["rev-parse", "--path-format=absolute", "--git-common-dir"], root)) / "info"
    info.mkdir(parents=True, exist_ok=True)
    # Locked, so two creates at once neither lose nor repeat the line.
    with open(info / "exclude", "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            existing = fh.read()
            if pattern not in existing.splitlines():
                sep = "" if not existing or existing.endswith("\n") else "\n"
                fh.write(f"{sep}{pattern}\n")
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def remove_worktree(root: str | Path, path: str | Path, force: bool = False) -> None:
    args = ["worktree", "remove", str(path)]
    if force:
        args[2:2] = ["--force", "--force"]
    run(args, root)
    run(["worktree", "prune"], root, check=False)


def list_worktrees(root: str | Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out(["worktree", "list", "--porcelain"], root).splitlines():
        if not line:
            if cur:
                result.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or "true"
    if cur:
        result.append(cur)
    return result


def set_base(root: str | Path, branch: str, base: str) -> None:
    run(["config", f"branch.{branch}.{BASE_CONFIG_KEY}", base], root)


def get_base(root: str | Path, branch: str) -> str | None:
    proc = run(["config", "--get", f"branch.{branch}.{BASE_CONFIG_KEY}"], root, check=False)
    return proc.stdout.strip() or None


def delete_branch(root: str | Path, branch: str, force: bool = False) -> None:
    run(["branch", "-D" if force else "-d", branch], root)


# -- status and diff -------------------------------------------------------


@dataclass
class Status:
    branch: str | None
    base: str | None
    ahead: int
    behind: int
    dirty_files: list[str]
    unpushed: int | None   # None when there's no upstream


def base_ref(path: str | Path, base: str) -> str:
    """The ref to compare a branch against. Local ``<base>`` unless it's
    strictly behind ``origin/<base>`` (i.e. not pulled yet). Comparing to
    origin while local has unpushed commits would count those commits as
    the branch's own work."""
    local = branch_exists(path, base)
    if not remote_branch_exists(path, base):
        return base
    if not local:
        return f"origin/{base}"
    local_behind = ok(["merge-base", "--is-ancestor", base, f"origin/{base}"], path) and not ok(
        ["merge-base", "--is-ancestor", f"origin/{base}", base], path
    )
    return f"origin/{base}" if local_behind else base


def dirty_files(path: str | Path, tracked_only: bool = False) -> list[str]:
    # Deliberately not out(): it strips, and porcelain's status field is two
    # columns whose first is blank when a change is not staged. " M app.py"
    # arrives as "M app.py", and the three-character slice then eats the first
    # letter of the path. Untracked files are "?? path" and were unaffected,
    # which is why this survived.
    untracked = "no" if tracked_only else "all"
    proc = run(["status", "--porcelain", f"--untracked-files={untracked}"], path)
    return [line[3:] for line in proc.stdout.splitlines() if line and not _bookkeeping(line)]


# Autopilot moves a hand-written .brindle/goals.md to the session's own
# (git-ignored) .brindle/goals/<session id>.md. When goals.md was committed,
# that leaves its deletion in the checkout: brindle's bookkeeping, like the
# ignored goals/ dir, not the person's work, so it never counts as dirty
# (and never blocks a merge or a milestone's sha).
CLAIMED_SEED = ".brindle/goals.md"


def _bookkeeping(line: str) -> bool:
    return line[:2] in (" D", "D ") and line[3:] == CLAIMED_SEED


def ahead_behind(path: str | Path, ref: str) -> tuple[int, int]:
    """(behind, ahead) commit counts between ``ref`` and HEAD."""
    proc = run(["rev-list", "--left-right", "--count", f"{ref}...HEAD"], path)
    behind, ahead = (int(x) for x in proc.stdout.split())
    return behind, ahead


def status(path: str | Path, base: str | None) -> Status:
    branch = current_branch(path)
    ahead = behind = 0
    if base:
        behind, ahead = ahead_behind(path, base_ref(path, base))
    unpushed: int | None = None
    up = run(["rev-list", "--count", "@{upstream}..HEAD"], path, check=False)
    if up.returncode == 0:
        unpushed = int(up.stdout.strip())
    return Status(branch, base, ahead, behind, dirty_files(path), unpushed)


def merge_base(path: str | Path, ref: str) -> str:
    return out(["merge-base", ref, "HEAD"], path)


def diff(path: str | Path, base: str, stat: bool = False) -> str:
    """Everything this branch changes relative to where it forked from
    ``base``: committed work plus uncommitted edits, plus untracked files."""
    mb = merge_base(path, base_ref(path, base))
    args = ["diff", "--stat" if stat else "--patch", mb]
    text = out(args, path)
    untracked = out(["ls-files", "--others", "--exclude-standard"], path).splitlines()
    if untracked:
        text += ("\n" if text else "") + summarize_untracked(untracked)
    return text


# Directories that are never work product; an untracked one is collapsed to a count.
NOISE_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", "dist", "build", ".git"}
MAX_UNTRACKED_LISTED = 20


def summarize_untracked(files: list[str]) -> str:
    """Untracked files as a bounded listing: files at the top level are named,
    everything under a directory is a per-directory count, and noise
    directories (.venv, node_modules, ...) are collapsed however deep."""
    loose: list[str] = []
    dirs: dict[str, int] = {}
    for f in files:
        parts = f.split("/")
        noise = next((i for i, p in enumerate(parts[:-1]) if p in NOISE_DIRS), None)
        if noise is not None:
            key = "/".join(parts[: noise + 1])
            dirs[key] = dirs.get(key, 0) + 1
        elif len(parts) == 1:
            loose.append(f)
        else:
            dirs[parts[0]] = dirs.get(parts[0], 0) + 1
    entries = [f"  {f}" for f in loose] + [
        f"  {d}/ ({n} file{'s' if n != 1 else ''})" for d, n in sorted(dirs.items())
    ]
    shown = entries[:MAX_UNTRACKED_LISTED]
    if len(entries) > len(shown):
        shown.append(f"  ... and {len(entries) - len(shown)} more")
    return f"Untracked files ({len(files)}):\n" + "\n".join(shown)


def commit_all(path: str | Path, message: str) -> str | None:
    run(["add", "-A"], path)
    if ok(["diff", "--cached", "--quiet"], path):
        return None
    run(["commit", "-m", message], path)
    return out(["rev-parse", "--short", "HEAD"], path)


def hash_blob(cwd: str | Path, data: bytes, path: str, write: bool = False) -> str:
    """The blob id of ``data`` as git would store it at ``path`` (that
    path's clean filters applied; ``path`` is a name, not a pathspec). With
    ``write`` the blob is stored too."""
    args = ["hash-object", *(["-w"] if write else []), "--stdin", f"--path={path}"]
    start = spawn.action if write else spawn.read     # a clean filter can stall: a read stops with its request
    try:
        proc = start(["git", *args], cwd=cwd, input=data, text=False, timeout=TIMEOUT)
    except spawn.Busy as e:
        raise GitBusy(f"git {' '.join(args)}: {e}") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)}: {e}") from e
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode().strip()


def paths(args: list[str], cwd: str | Path) -> list[str]:
    """The paths a ``git diff --name-only`` / ``ls-files`` style command
    lists, read NUL-separated (``-z``) so a name with a space, a quote or a
    non-ASCII character arrives as itself: without ``-z`` git C-quotes such
    names (``"caf\\303\\251.py"``), and a quoted name matches no glob."""
    proc = run([*args, "-z"], cwd)
    return [p for p in proc.stdout.split("\0") if p]


def conflicting_files(path: str | Path) -> list[str]:
    return paths(["diff", "--name-only", "--diff-filter=U"], path)


def sync(path: str | Path, base: str, strategy: str = "rebase") -> str:
    """Bring ``base``'s latest commits into this branch. Raises on conflict,
    leaving the rebase/merge in progress so an agent or human can resolve it."""
    if has_remote(path):
        background_fetch(path, ["origin", base, "--quiet", "--no-tags"])
    ref = base_ref(path, base)
    if strategy == "rebase":
        proc = run(["rebase", ref], path, check=False)
    elif strategy == "merge":
        proc = run(["merge", "--no-edit", ref], path, check=False)
    else:
        raise ValueError(f"unknown strategy {strategy!r}")
    if proc.returncode != 0:
        conflicts = conflicting_files(path)
        raise GitError(
            f"{strategy} onto {ref} stopped with conflicts in: {', '.join(conflicts) or '?'}\n"
            f"Resolve them in {path} and run `git {strategy} --continue`, "
            f"or `git {strategy} --abort`."
        )
    return ref


def merge_local_base(path: str | Path, base: str, no_ff: bool = False) -> tuple[str, list[str]]:
    """Merge the local ``base`` branch into HEAD, ignoring origin entirely:
    ``base`` here is the local branch a workspace merges back into, not its
    remote-tracking counterpart, so this never fetches. On a content conflict,
    aborts and leaves the worktree clean, returning the pre-merge HEAD sha and
    the conflicting files instead of raising. Returns (new_sha, []) on
    success. A merge failure that isn't a content conflict (``merge.ff=only``,
    a failing hook, a missing git identity, ...) is aborted the same way but
    raised as a GitError, since there's nothing sensible to report as a sync
    or a conflict. ``no_ff``: always a merge commit, even where HEAD could
    fast-forward."""
    before = out(["rev-parse", "HEAD"], path)
    proc = run(["merge", "--no-edit", *(["--no-ff"] if no_ff else []), base], path, check=False)
    if proc.returncode != 0:
        conflicts = conflicting_files(path)
        run(["merge", "--abort"], path, check=False)
        if not conflicts:
            raise GitError(f"merging {base} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return before, conflicts
    return out(["rev-parse", "HEAD"], path), []


def merge_into(root: str | Path, target_path: str | Path, branch: str, squash: bool) -> None:
    """Merge ``branch`` into whatever is checked out at ``target_path``.

    Only tracked changes block this: stray untracked files (build output,
    ``.DS_Store``, ...) sitting in the target checkout shouldn't stop a
    merge. If the merge would actually overwrite one, git itself refuses and
    that failure surfaces below as a GitError."""
    with checkout_lock(target_path):
        if dirty_files(target_path, tracked_only=True):
            raise GitError(f"{target_path} has uncommitted changes; commit or stash them first")
        if squash:
            proc = _retry_transient(["merge", "--squash", branch], target_path)
            if proc.returncode == 0:
                proc = _retry_transient(
                    ["commit", "--no-edit", "-m", f"Squash merge {branch}"], target_path
                )
        else:
            proc = _retry_transient(["merge", "--no-ff", "--no-edit", branch], target_path)
        if proc.returncode != 0:
            run(["merge", "--abort"], target_path, check=False)
            raise GitError(
                f"merging {branch} failed (aborted): {proc.stderr.strip() or proc.stdout.strip()}"
            )


# A merge that fails only because another git process held the index for a
# moment (the supervisor's own git, a status refresh) is retried, not reported.
TRANSIENT_MARKERS = ("index.lock", "Unable to write index")
RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0)


def _is_transient(proc: subprocess.CompletedProcess) -> bool:
    text = f"{proc.stderr}\n{proc.stdout}"
    return any(m in text for m in TRANSIENT_MARKERS)


def _retry_transient(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess:
    """Run a git command, retrying with backoff while it fails on the index
    lock. A merge that failed that way never started, so a retry can't touch
    the checkout's uncommitted changes; any other failure returns at once."""
    proc = run(args, cwd, check=False)
    for delay in RETRY_DELAYS:
        if proc.returncode == 0 or not _is_transient(proc):
            break
        time.sleep(delay)
        proc = run(args, cwd, check=False)
    return proc


# A merge takes seconds; a holder this slow is hung, and waiting on it forever
# would stall every later merge into the checkout.
LOCK_TIMEOUT = 300.0


@contextlib.contextmanager
def checkout_lock(path: str | Path, timeout: float | None = None):
    """Serialize merges into one checkout with an exclusive file lock kept in
    that checkout's own git dir (shared across processes, released if we die).
    Raises GitError if the lock isn't free within ``timeout`` seconds."""
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    git_dir = Path(out(["rev-parse", "--absolute-git-dir"], path))
    with open(git_dir / "brindle-merge.lock", "a") as fh:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GitError(
                        f"another merge into {path} has held its lock for over "
                        f"{timeout:.0f}s; it may be hung"
                    ) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def push(path: str | Path, branch: str, sha: str | None = None) -> None:
    """Push ``branch``; with ``sha``, exactly that commit to it."""
    if sha is None:
        run(["push", "--set-upstream", "origin", branch], path)
        return
    run(["push", "origin", f"{sha}:refs/heads/{branch}"], path)
    run(["branch", "--set-upstream-to", f"origin/{branch}", branch], path, check=False)


def remote_web_url(root: str | Path) -> str | None:
    proc = run(["remote", "get-url", "origin"], root, check=False)
    url = proc.stdout.strip()
    m = re.match(r"(?:git@|ssh://git@|https://)([^/:]+)[/:](.+?)(?:\.git)?$", url)
    return f"https://{m.group(1)}/{m.group(2)}" if m else None
