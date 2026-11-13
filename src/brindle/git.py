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
import sys
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


class GitToolsMissing(GitError):
    """macOS without the Command Line Developer Tools (see spawn.ensure_git):
    its own type so every layer can say what to install instead of "not a repo"."""


def run(args: list[str], cwd: str | Path, check: bool = True,
        timeout: int | float | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    return run_env(args, cwd, None, check=check, timeout=timeout, input=input)


def run_env(args: list[str], cwd: str | Path, env: dict[str, str] | None,
            check: bool = True, timeout: int | float | None = None,
            input: str | None = None, no_terminal: bool = False) -> subprocess.CompletedProcess:
    """``run`` with its own environment (e.g. ``GIT_INDEX_FILE`` for a scratch
    index that leaves the checkout's real index alone; see brindle.rewind).
    ``no_terminal``: nothing it starts can ask at the terminal (a read can't anyway)."""
    to = TIMEOUT if timeout is None else timeout
    reading = is_read(args)
    start = spawn.read if reading else spawn.action
    detached = {"no_terminal": True} if no_terminal and not reading else {}
    if not reading:
        invalidate_branches()
    try:
        proc = start(["git", *args], cwd=cwd, env=env, input=input, timeout=to, **detached)
        if not reading:
            invalidate_branches()    # and after, so a lookup during the action can't re-cache old refs
    except spawn.Busy as e:
        raise GitBusy(f"git {' '.join(args)}: {e}") from e
    except spawn.ToolsMissing as e:
        raise GitToolsMissing(str(e)) from e
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
# person's terminal. So these calls clear every credential helper and can't prompt
# in any other way. The one helper they may get is gh's for github.com, on macOS,
# with its token in gh's own config file (gh_token_in_file), and it runs where the
# keychain can't be reached (_NO_KEYCHAIN): a token in the keychain is read through
# `security`, which a locked keychain can answer with a dialog, and no check made
# beforehand is both silent itself and still true by the time gh reads it.
# What the person asked for (`git push`, `brindle pr`, a named PR's branch) goes
# through ``run``, with their normal credentials.
#
# Someone who connected GitHub to brindle (brindle.github_connect) has neither
# problem: brindle's own helper answers for github.com, from brindle's own store,
# in these calls, in what the person asked for (connected_github_config) and in
# agents' panes. gh's helper is then left out of all three.

_GITHUB_HELPER = "credential.https://github.com.helper"
GH_LOOKUP_SECONDS = 5.0
GH_SEEN_SECONDS = 60.0       # how long a sign-in lookup stands: a pane's launch shouldn't each start gh
_gh_seen: dict[str, tuple[float, str | None]] = {}
_gh_refused: dict[str, float] = {}     # repo: when origin last turned down gh's answer, for as long as GH_SEEN_SECONDS
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# gh's keyring is go-keyring, which on macOS only ever runs this one absolute path; the
# services are what any other way into the keychain would have to ask. Denied, gh's read
# fails at once: it answers from its file or not at all, and SecurityAgent is never asked.
_NO_KEYCHAIN = ('(version 1)(allow default)(deny process-exec* (literal "/usr/bin/security"))'
                '(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd")'
                ' (global-name "com.apple.securityd.xpc"))')
_GH_HOST_BLOCK = re.compile(r"^github\.com:\n((?: +\S.*(?:\n|$))+)", re.MULTILINE)
_GH_FILE_TOKEN = re.compile(r"oauth_token: [\w-]{20,}")
_FETCH_REFUSED = "brindle-fetch-refused"
# git's and ssh's words for "not without credentials" (LC_ALL=C: see _background_env),
# as opposed to offline or no such repo.
_REFUSED = re.compile(r"terminal prompts disabled|could not read (username|password)|authentication failed"
                      r"|permission denied|host key verification failed|returned error: 40[13]"
                      r"|told us to quit", re.IGNORECASE)    # the last: brindle's own helper, with nothing to give
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


def _gh_hosts_file() -> Path:
    """Where gh keeps its hosts, in gh's own order (go-gh's config.ConfigDir)."""
    if path := os.environ.get("GH_CONFIG_DIR"):
        return Path(path, "hosts.yml")
    if path := os.environ.get("XDG_CONFIG_HOME"):
        return Path(path, "gh", "hosts.yml")
    if sys.platform == "win32" and (path := os.environ.get("AppData")):
        return Path(path, "GitHub CLI", "hosts.yml")
    return Path.home() / ".config" / "gh" / "hosts.yml"


def gh_token_in_file() -> bool:
    """Whether `gh auth git-credential` can answer for github.com from gh's config
    file: a non-empty ``oauth_token`` directly under the host wins over the keyring,
    and is never one gh refreshes. One only under ``users`` doesn't: gh tries the
    keyring first, which _NO_KEYCHAIN turns into no answer.

    This says gh is worth starting, not that it's safe to: _NO_KEYCHAIN is what keeps
    the keychain out of it, whatever this file says by the time gh reads it. Only
    the file as gh writes it counts; so a parser's reading of it, and a parser's
    error quoting it, aren't needed. A token's text goes no further than these lines."""
    try:
        block = _GH_HOST_BLOCK.search(_gh_hosts_file().read_text())
    except (OSError, ValueError):
        return False
    if block is None:
        return False
    lines = block[1].splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    keys = [line[indent:] for line in lines if line.startswith(" " * indent) and line[indent] != " "]
    return any(_GH_FILE_TOKEN.fullmatch(key) for key in keys)


def _github_helper_entries(helper: str) -> tuple[str, str]:
    """github.com signs in through this helper alone."""
    return f"{_GITHUB_HELPER}=", f"{_GITHUB_HELPER}={helper}"


def _connected_github_helper() -> str | None:
    """brindle's own GitHub connection as a git helper, for as long as the person
    is connected: one GitHub stopped taking answers nothing, and still keeps every
    other helper out (github_connect.git_helper). Looking is a read of brindle's
    own store: no keychain item of git's or gh's, and no process started."""
    from brindle import github_connect

    return github_connect.git_helper()


def connected_github_config() -> list[str]:
    """``-c`` words that sign git in to github.com through brindle's connection
    alone, for what the person asked for (a clone, a push, a branch they named).
    Empty without a connection: their own helpers apply, as they always did."""
    helper = _connected_github_helper()
    return [word for entry in _github_helper_entries(helper) for word in ("-c", entry)] if helper else []


def pane_git_config(gh: bool = True) -> str | None:
    """``GIT_CONFIG_PARAMETERS`` for an agent's pane: github.com signs in through
    brindle's own connection when there is one, else (with ``gh``) through gh
    alone when gh is signed in. The person's other helpers for that host are
    cleared, as a helper still listed would also be told to store and erase
    what it was given: the keychain dialog over again, and their entry overwritten.
    Every other host keeps the person's helpers; their config isn't edited.

    Not GIT_CONFIG_COUNT and its GIT_CONFIG_KEY_<n>: a name with KEY in it is
    what environment filters drop (brindle's own, Codex's), and git refuses to
    run at all with a count whose key went missing.

    gh reads its token through `security`, so a locked keychain can still ask
    here: an agent's push is one at a time, not a poll. brindle's connection
    reads no keychain item."""
    helper = _connected_github_helper() or (gh_credential_helper() if gh else None)
    if helper is None:
        return None
    entries = _github_helper_entries(helper)
    ours = " ".join("'" + entry.replace("'", "'\\''") + "'" for entry in entries)    # git takes single quotes only
    inherited = os.environ.get("GIT_CONFIG_PARAMETERS")
    return f"{inherited} {ours}" if inherited else ours


def _background_env(cwd: str | Path) -> dict[str, str]:
    """The environment of a network call nobody asked for: no askpass program
    (an empty GIT_ASKPASS also passes over core.askPass and SSH_ASKPASS) and no
    terminal prompt, which with ``background``'s missing terminal leave ssh, under
    any name or wrapper, nowhere to ask. BatchMode has it fail at once where it
    can be given, still with an agent's keys and the person's own command."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
           "SSH_ASKPASS_REQUIRE": "never", "LC_ALL": "C"}
    if ssh := _batch_ssh(cwd):
        env["GIT_SSH_COMMAND"] = ssh
    return env


def _batch_ssh(cwd: str | Path) -> str | None:
    """The ssh command git would run, in BatchMode: right after the OpenSSH
    executable, however it's named (a symlink, ``slogin``) and wherever a wrapper
    puts it (``env A=1 ssh -F x``), as ssh keeps the first value of an option and
    the command may set its own. Any other program gets it last, where git's own
    arguments go: a wrapper passes those on, and its own options stay its own.
    None for PuTTY's, which takes no ``-o``."""
    try:
        ssh = os.environ.get("GIT_SSH_COMMAND") or run(["config", "--get", "core.sshCommand"], cwd,
                                                       check=False).stdout.strip()
    except GitError:
        ssh = ""
    ssh = ssh or shlex.quote(os.environ.get("GIT_SSH") or "ssh")     # GIT_SSH: a bare program, no options
    names = []
    for word in _SHELL_WORD.finditer(ssh):
        if re.match(r"\w+=", word.group()):      # NAME=value in front of a program
            continue
        text = word.group().strip("'\"")
        names.append(os.path.basename(text).lower())
        resolved = os.path.basename(os.path.realpath(shutil.which(text) or text)).lower()
        if resolved.removesuffix(".exe") in ("ssh", "slogin"):
            return f"{ssh[:word.end()]} -o BatchMode=yes{ssh[word.end():]}"
    if any("plink" in name for name in names):
        return None
    return f"{ssh} -o BatchMode=yes"


def _background_gh_helper(cwd: str | Path) -> str | None:
    """gh's helper for a network call nobody asked for, in its sandbox: nothing gh
    does on starting or answering (a migration, a token moved since) can reach the
    keychain, and if the sandbox won't start, git simply gets no answer. Asks gh
    nothing itself, not its sign-in either, as gh outside that could. None without
    the sandbox or a token in gh's file, or just after origin turned down gh's answer."""
    refused = _gh_refused.get(str(cwd))
    if refused is not None and time.monotonic() - refused < GH_SEEN_SECONDS:
        return None
    exe = shutil.which("gh")
    if exe is None or not os.access(_SANDBOX_EXEC, os.X_OK) or not gh_token_in_file():
        return None
    return f"!{shlex.quote(_SANDBOX_EXEC)} -p {shlex.quote(_NO_KEYCHAIN)} {shlex.quote(exe)} auth git-credential"


def background(args: list[str], cwd: str | Path,
               timeout: int | float | None = None) -> subprocess.CompletedProcess | None:
    """``git <args>``, a network call brindle starts on its own: it can't raise a
    credential dialog. Every helper is cleared; github.com over HTTPS gets brindle's
    own connection when there is one, else maybe gh's (_background_gh_helper), and
    git only asks it for that host. None when git couldn't run or finish; a failure
    is the caller's to read."""
    connected = _connected_github_helper()
    gh = None if connected else _background_gh_helper(cwd)
    helper = connected or gh
    config = ["credential.helper=", *(_github_helper_entries(helper) if helper else ())]
    try:
        proc = run_env([*(word for entry in config for word in ("-c", entry)), *args], cwd, _background_env(cwd),
                       check=False, timeout=timeout, no_terminal=True)
    except GitError:
        return None
    # A connection GitHub refuses goes on record by git itself (the helper's erase), and answers nothing after.
    if gh and proc.returncode != 0 and _REFUSED.search(proc.stderr or ""):
        _gh_refused[str(cwd)] = time.monotonic()
    return proc


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


# -- what the git files say, with no git process ---------------------------
#
# Whether a ref exists, what it points at, what origin's default branch is and
# which of two commits is ahead are the same in every checkout of a repo, and
# change only when a ref does. The daemon's sweeps ask them for every checkout
# every few seconds, and each git run is a process an antivirus inspects
# (docs/investigations/process-count.md). So they are read from the repo's own
# files (HEAD, loose refs, packed-refs), and what only git can work out (which
# commit is ahead, the remotes) is asked once and kept: per pair of commits,
# which never goes stale, or until the config files change. Whatever the files
# can't say (a reftable repo, GIT_DIR in the environment, a path that isn't a
# checkout's root) is asked of git every time, as before.


class _Unreadable(Exception):
    """The git files can't answer this: ask git."""


_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_PER_WORKTREE = ("refs/worktree/", "refs/bisect/", "refs/rewritten/")
_GIT_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_NAMESPACE")   # git would look somewhere else
_REMEMBERED_MAX = 4096


def _git_dirs(path: str | Path) -> tuple[Path, Path]:
    """The checkout at ``path``'s own git dir and its repo's common one (the
    same for a main checkout; a linked worktree's ``.git`` file and
    ``commondir`` name them)."""
    if any(os.environ.get(name) for name in _GIT_ENV):
        raise _Unreadable(path)
    try:
        own = Path(path) / ".git"
        if not own.is_dir():
            text = own.read_text().strip()
            if not text.startswith("gitdir:"):
                raise _Unreadable(path)
            own = Path(path) / text.removeprefix("gitdir:").strip()
        common = own
        if (own / "commondir").is_file():
            common = own / (own / "commondir").read_text().strip()
        if (common / "reftable").exists() or not (common / "refs").is_dir():
            raise _Unreadable(path)
    except (OSError, UnicodeDecodeError) as e:
        raise _Unreadable(path) from e
    return own, common


def _stamp(file: Path) -> tuple[int, int, int, int] | None:
    """What changes when ``file`` does (git replaces its files whole); None without one."""
    try:
        st = file.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)


# Per packed-refs file: (its stamp, its refs, whether any is a replace ref).
_PACKED: dict[str, tuple[tuple[int, int, int, int], dict[str, str], bool]] = {}


def _packed_refs(common: Path) -> dict[str, str]:
    """``packed-refs`` by ref name, parsed once per version of the file."""
    file = common / "packed-refs"
    stamp = _stamp(file)
    if stamp is None:
        return {}
    key = str(file)
    hit = _PACKED.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        lines = file.read_text().splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise _Unreadable(file) from e
    refs = {name: sha for sha, _, name in (line.partition(" ") for line in lines) if name and _SHA.fullmatch(sha)}
    if len(_PACKED) >= _BRANCHES_MAX and key not in _PACKED:
        _PACKED.clear()
    # Under the stamp taken before the read: a later version is read again.
    _PACKED[key] = (stamp, refs, any(name.startswith("refs/replace/") for name in refs))
    return refs


def _symref(file: Path) -> str | None:
    """The ref a symbolic ref's ``file`` names; None when it names a commit or isn't there."""
    try:
        if file.is_symlink():       # how git once wrote a symbolic ref
            raise _Unreadable(file)
        text = file.read_text().strip()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise _Unreadable(file) from e
    return text.removeprefix("ref:").strip() if text.startswith("ref:") else None


def _ref(own: Path, common: Path, name: str, depth: int = 0) -> str | None:
    """What the ref ``name`` (``HEAD``, ``refs/...``) points at, from the
    loose or packed refs; None when there is no such ref."""
    parts = Path(name).parts
    if depth > 5 or not parts or ".." in parts or name.startswith("/") or not (
            name == "HEAD" or name.startswith("refs/")):
        raise _Unreadable(name)
    where = own if name == "HEAD" or name.startswith(_PER_WORKTREE) else common
    try:
        text = (where / name).read_text().strip()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return _packed_refs(common).get(name)
    except (OSError, UnicodeDecodeError) as e:
        raise _Unreadable(name) from e
    if text.startswith("ref:"):
        return _ref(own, common, text.removeprefix("ref:").strip(), depth + 1)
    if not _SHA.fullmatch(text):
        raise _Unreadable(name)
    return text


def _rev(own: Path, common: Path, name: str) -> str:
    """The object git resolves the plain name ``name`` to (a sha, ``HEAD``, a
    branch, ``origin/<branch>``, a tag: its rev-parse order)."""
    if _SHA.fullmatch(name):
        return name
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or ".." in name:
        raise _Unreadable(name)      # ``main~2``, ``@{upstream}``, ``a..b``: git's to work out
    names = [name] if name == "HEAD" or name.startswith("refs/") else []
    names += [f"refs/{name}", f"refs/tags/{name}", f"refs/heads/{name}", f"refs/remotes/{name}",
              f"refs/remotes/{name}/HEAD"]
    for full in names:
        if (sha := _ref(own, common, full)) is not None:
            return sha
    raise _Unreadable(name)


def resolve(path: str | Path, *names: str) -> list[str] | None:
    """What each of ``names`` (``HEAD``, a branch, ``origin/<branch>``, a sha)
    points at in the checkout at ``path``, read from the git files; None when
    they can't say (then the caller asks git, by name)."""
    try:
        own, common = _git_dirs(path)
        return [_rev(own, common, name) for name in names]
    except _Unreadable:
        return None


def head(path: str | Path) -> str:
    """The commit ``path`` is on: read from the git files, else git's own answer."""
    found = resolve(path, "HEAD")
    return found[0] if found else out(["rev-parse", "HEAD"], path)


# Answers about commits named by sha: a commit never changes, so neither does
# the answer, in any checkout of the repo. Kept per repo, and per version of
# the two files that change what history a commit has (``shallow``: a deepened
# clone has more to count; ``info/grafts``). A repo with replace refs, which
# put another object in a commit's place, keeps nothing: git is asked.
_ONCE: dict[tuple, object] = {}


def _once_key(path: str | Path, question: list[str]) -> tuple:
    common = _git_dirs(path)[1]
    replace = common / "refs" / "replace"
    _packed_refs(common)
    packed = _PACKED.get(str(common / "packed-refs"))
    if (packed is not None and packed[2]) or (replace.is_dir() and any(replace.iterdir())):
        raise _Unreadable(replace)
    return (str(common.resolve()), _stamp(common / "shallow"), _stamp(common / "info" / "grafts"), *question)


def once(path: str | Path, question: list[str], ask):
    """``ask()``'s answer to ``question``, which names every commit it is
    about by its sha (see ``resolve``): asked once per repo, then answered
    from memory. An ``ask`` that raises is not remembered."""
    try:
        key = _once_key(path, question)
    except (_Unreadable, OSError):
        return ask()
    if key not in _ONCE:
        answer = ask()
        if len(_ONCE) >= _REMEMBERED_MAX:
            _ONCE.clear()
        _ONCE[key] = answer
    return _ONCE[key]


def out_once(args: list[str], cwd: str | Path, raw: bool = False) -> str:
    """``out`` for a question whose ``args`` name every commit by its sha:
    asked of git once (``once``). ``raw``: git's output as it is (``-z``
    lists), not stripped."""
    text = once(cwd, args, lambda: run(args, cwd).stdout)
    return text if raw else text.strip()


def is_ancestor(path: str | Path, older: str, newer: str) -> bool:
    """Whether ``older`` is an ancestor of ``newer`` (or the same commit), as
    ``merge-base --is-ancestor`` says: asked of git once per pair of commits
    when the git files say which commits the names are."""
    shas = resolve(path, older, newer)
    if shas is None:
        return ok(["merge-base", "--is-ancestor", older, newer], path)
    if shas[0] == shas[1]:
        return True
    args = ["merge-base", "--is-ancestor", *shas]

    def ask() -> bool:
        code = run(args, path, check=False).returncode
        if code not in (0, 1):
            raise _Unreadable(args)     # git couldn't say (a commit that isn't here): not remembered
        return code == 0

    try:
        return once(path, args, ask)
    except _Unreadable:
        return False


def current_branch(path: str | Path) -> str | None:
    try:
        target = _symref(_git_dirs(path)[0] / "HEAD")
        if target is None or target.startswith("refs/heads/"):
            return target.removeprefix("refs/heads/") if target else None
    except _Unreadable:
        pass
    proc = run(["symbolic-ref", "--quiet", "--short", "HEAD"], path, check=False)
    return proc.stdout.strip() or None


def default_branch(root: str | Path) -> str:
    try:
        target = _symref(_git_dirs(root)[1] / "refs" / "remotes" / "origin" / "HEAD") or ""
        if target and not target.startswith("refs/remotes/origin/"):
            raise _Unreadable(target)
        short = target.removeprefix("refs/remotes/")
    except _Unreadable:
        proc = run(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root, check=False)
        short = proc.stdout.strip() if proc.returncode == 0 else ""
    if "/" in short:
        return short.split("/", 1)[1]
    for candidate in ("main", "master"):
        if _local_branch_exists(root, candidate):
            return candidate
    return current_branch(root) or "main"


def _local_branch_exists(root: str | Path, branch: str) -> bool:
    """``branch_exists``, from the git files where they can say."""
    try:
        return _ref(*_git_dirs(root), f"refs/heads/{branch}") is not None
    except _Unreadable:
        return branch_exists(root, branch)


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
    name = f"refs/remotes/{remote}/{branch}"
    try:
        return _ref(*_git_dirs(root), name) is not None
    except _Unreadable:
        return ok(["show-ref", "--verify", "--quiet", name], root)


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
    """Whether ``git remote`` lists ``remote``: asked of git once per repo and
    again only when the config it reads there changed, any file it includes
    among them (``status_cache.remotes``, which knows every file git's config
    depends on); every time where that can't be known."""
    from brindle import status_cache    # it builds on this module

    try:
        _git_dirs(root)
    except _Unreadable:
        return remote in out(["remote"], root).split()
    known = status_cache.remotes(root)
    return remote in (out(["remote"], root).split() if known is None else known)


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
    apply (brindle's GitHub connection, when they made one). Returns the
    remote-tracking ref name."""
    run([*connected_github_config(), "fetch", "--quiet", "--no-tags", remote,
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
    from brindle import trash

    if os.path.lexists(path):
        try:
            trash.put(root, path, force=force)
        except trash.TrashUnavailable:
            _remove_in_place(root, path, force)
            return
        except OSError as e:
            raise GitError(f"worktree kept: {e}") from e
        try:
            trash.retain()
        except (OSError, GitError):
            pass  # Removal succeeded; the hourly sweep can apply retention later.
    else:
        run(["worktree", "prune"], root, check=False)


def _remove_in_place(root: str | Path, path: str | Path, force: bool) -> None:
    """0.6.2's removal, for a checkout the trash can't take (another volume,
    or no exclusive rename there): not recoverable. Unforced, git refuses a
    checkout with changes or a lock."""
    if force:
        shutil.rmtree(path, ignore_errors=True)
        if os.path.lexists(path):
            raise GitError(f"could not delete {path}")
    else:
        run(["worktree", "remove", str(path)], root)
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
    try:
        own, common = _git_dirs(path)
        local = _ref(own, common, f"refs/heads/{base}")
        origin = _ref(own, common, f"refs/remotes/origin/{base}")
    except _Unreadable:
        local = origin = None
        if not remote_branch_exists(path, base):
            return base
        if not branch_exists(path, base):
            return f"origin/{base}"
        local_behind = ok(["merge-base", "--is-ancestor", base, f"origin/{base}"], path) and not ok(
            ["merge-base", "--is-ancestor", f"origin/{base}", base], path
        )
        return f"origin/{base}" if local_behind else base
    if origin is None:
        return base
    if local is None:
        return f"origin/{base}"
    # Behind means an ancestor that isn't the same commit: one question, once per pair of commits.
    return f"origin/{base}" if local != origin and is_ancestor(path, local, origin) else base


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
    """(behind, ahead) commit counts between ``ref`` and HEAD: counted once
    per pair of commits when the git files say which they are."""
    shas = resolve(path, ref, "HEAD")
    if shas:
        text = out_once(["rev-list", "--left-right", "--count", f"{shas[0]}...{shas[1]}"], path)
    else:
        text = out(["rev-list", "--left-right", "--count", f"{ref}...HEAD"], path)
    behind, ahead = (int(x) for x in text.split())
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
    shas = resolve(path, ref, "HEAD")
    return out_once(["merge-base", *shas], path) if shas else out(["merge-base", ref, "HEAD"], path)


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


FALLBACK_NAME = "brindle"
FALLBACK_EMAIL = "brindle@localhost"

IDENTITY_MESSAGE = ('Git needs your name and email for commits in this repo. Set them with '
                    '`git config user.name "Your Name"` and `git config user.email you@example.com` '
                    'in this repository, then try again.')


def missing_user_identity(path: str | Path) -> list[str]:
    """The fields, "name" and "email", git has no value for at ``path`` in either
    role, resolved as git does (missing_identity): an identity in the environment counts."""
    missing = {field for _, field in missing_identity(path)}
    return [field for field in ("name", "email") if field in missing]


def require_user_identity(path: str | Path) -> None:
    if missing_user_identity(path):
        raise GitError(IDENTITY_MESSAGE)


def missing_identity(path: str | Path, env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """The (role, field) pairs, role "author" or "committer", field "name" or
    "email", that git has no value for at ``path`` (a new account has none).
    Each is resolved as git does (GIT_<ROLE>_<FIELD> in ``env``, default the
    process's, then <role>.<field>, then user.<field>, and EMAIL for an email),
    so whatever the person set, for either role, is kept."""
    env = dict(os.environ) if env is None else env
    # Read the config with that same environment: a worker's profile can point
    # git at other config files or carry its own settings (GIT_CONFIG_*).
    found = run_env(["config", "--get-regexp", r"^(user|author|committer)\.(name|email)$"],
                    path, env, check=False).stdout
    conf = {k.lower(): v.strip() for k, _, v in (line.partition(" ") for line in found.splitlines())}
    missing = []
    for role in ("author", "committer"):
        for field in ("name", "email"):
            have = (env.get(f"GIT_{role}_{field}".upper()) or conf.get(f"{role}.{field}")
                    or conf.get(f"user.{field}") or (field == "email" and env.get("EMAIL")))
            if not have:
                missing.append((role, field))
    return missing


def _fallback(field: str) -> str:
    return FALLBACK_NAME if field == "name" else FALLBACK_EMAIL


def identity_args(path: str | Path) -> list[str]:
    """``-c`` flags giving brindle's own commits a fallback for each part of
    the identity git has no value for at ``path``. Never replaces a value the
    person set (config or environment); nothing is written to any config file."""
    return [a for role, field in missing_identity(path)
            for a in ("-c", f"{role}.{field}={_fallback(field)}")]


def identity_env(path: str | Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """The same fallback as environment variables, for a worker's own git
    commands, judged against the worker's effective ``env``: only what is
    missing, so an identity set anywhere (a profile's GIT_* lines too) stands."""
    return {f"GIT_{role}_{field}".upper(): _fallback(field)
            for role, field in missing_identity(path, {**os.environ, **(env or {})})}


def commit_all(path: str | Path, message: str) -> str | None:
    run(["add", "-A"], path)
    if ok(["diff", "--cached", "--quiet"], path):
        return None
    run([*identity_args(path), "commit", "-m", message], path)
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
    except spawn.ToolsMissing as e:
        raise GitToolsMissing(str(e)) from e
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
        proc = run([*identity_args(path), "rebase", ref], path, check=False)
    elif strategy == "merge":
        proc = run([*identity_args(path), "merge", "--no-edit", ref], path, check=False)
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
    proc = run([*identity_args(path), "merge", "--no-edit", *(["--no-ff"] if no_ff else []), base],
               path, check=False)
    if proc.returncode != 0:
        conflicts = conflicting_files(path)
        run(["merge", "--abort"], path, check=False)
        if not conflicts:
            raise GitError(f"merging {base} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return before, conflicts
    return out(["rev-parse", "HEAD"], path), []


def _identity_hint(proc: subprocess.CompletedProcess) -> str:
    """How to fix a merge that still failed on a missing identity (say, one
    a hook or config blanked), in plain words."""
    text = f"{proc.stderr}\n{proc.stdout}"
    if "tell me who you are" in text or "empty ident name" in text or "unable to auto-detect" in text:
        return '\n' + IDENTITY_MESSAGE
    return ""


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
                + _identity_hint(proc)
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
    who = identity_args(cwd)     # a new account has no author for the merge commit
    proc = run([*who, *args], cwd, check=False)
    for delay in RETRY_DELAYS:
        if proc.returncode == 0 or not _is_transient(proc):
            break
        time.sleep(delay)
        proc = run([*who, *args], cwd, check=False)
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


def push(path: str | Path, branch: str, sha: str | None = None, track: bool = True) -> None:
    """Push ``branch``; with ``sha``, exactly that commit to it (the local
    branch never moves; ``track``: the local ``branch`` follows the pushed
    one). Signed in to github.com through brindle's connection when the
    person made one."""
    signed_in = connected_github_config()
    if sha is None:
        run([*signed_in, "push", "--set-upstream", "origin", branch], path)
        return
    run([*signed_in, "push", "origin", f"{sha}:refs/heads/{branch}"], path)
    if track:
        run(["branch", "--set-upstream-to", f"origin/{branch}", branch], path, check=False)


def remote_web_url(root: str | Path) -> str | None:
    proc = run(["remote", "get-url", "origin"], root, check=False)
    url = proc.stdout.strip()
    m = re.match(r"(?:git@|ssh://git@|https://)([^/:]+)[/:](.+?)(?:\.git)?$", url)
    return f"https://{m.group(1)}/{m.group(2)}" if m else None
