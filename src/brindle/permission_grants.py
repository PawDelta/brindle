"""Saved actions and generalized asks, over brindle.permissions.

An **ask** is a request the policy left to a person. Each one carries its
exact form and a ladder of scopes a decision may cover (``generalize``): a
push's destination ("into main from any branch"), a file's top directory, a
command's families (read-only git, read-only shell, test runs, tool queries,
local git changes, installs, file changes in a checkout: "Command families"
below) or else its first two words, an MCP tool (or its
whole server).
The second rung, the general form, is what brindle proposes as the default
grant; pending asks of the same kind and general form make one Inbox item
(``groups``), one decision resolves the item, and the person may narrow to
the exact request.

A **saved action** (permissions.SavedAction) is such a decision kept: a deny
stops every ask its pattern covers; an allow resolves an ask only within the
limits of the ask rule that asked (its scope level or pattern, its lifetime),
never past a deny rule (permissions.decide checks those first), and never
once expired. ``settle`` is what decide() calls; an ask a saved action covers
never reaches the Inbox.

Deciding from the Inbox (``decide_group``, ``allow_all``, ``unblock_denied``)
checks every candidate ask against its own policy, saves the grant, and
answers each hook through the daemon's hold (service.inbox.answer_permission),
so a broad grant never resolves a request its policy forbids."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from brindle import permissions
from brindle.permissions import (
    LEVELS,
    PUSH_ARROW,
    PUSH_AT,
    UNKNOWN_REF,
    Decision,
    Request,
    Rule,
    SavedAction,
    simple_argv,
)
MODES = ("once", "session", "timed", "forever", "deny", "always-deny")


def _error(code: str, message: str, status: int = 400) -> Exception:
    from brindle.service.errors import ServiceError    # lazily: the service layer imports widely

    return ServiceError(code, message, status)
GENERAL = "general"
EXACT = "exact"


# -- generalizing one request ------------------------------------------------------------------


def grant_kind(req: Request) -> str:
    """The kind a saved action for ``req`` has: ``push`` for a git push, else the request's."""
    if req.kind == "bash" and req.command and permissions.push_calls(req.command):
        return "push"
    return req.kind


def ask_keys(req: Request) -> list[str]:
    """Every key a saved action must cover for ``req``: one per push the
    command makes (``"source → destination @ remote"``), else the one ask_key."""
    if grant_kind(req) == "push":
        return [p.key() for p in permissions.push_calls(req.command or "")]
    return [ask_key(req)]


def ask_key(req: Request) -> str:
    """What a saved action's pattern is matched against: a push's "source →
    destination @ remote" (the first, when a command pushes several), a
    file's path relative to the worktree, the command, the URL or the tool name."""
    kind = grant_kind(req)
    if kind == "push":
        return permissions.push_calls(req.command or "")[0].key()
    if req.kind in permissions.PATH_KINDS:
        pattern = permissions.learnable_pattern(req)
        return pattern[1] if pattern else (req.path or "")
    if req.kind == "bash":
        return " ".join((req.command or "").split())
    if req.kind == "fetch":
        return req.url or ""
    return req.tool


def _scope(value: str, pattern: str, label: str, level: str | None = None) -> dict:
    return {"value": value, "pattern": pattern, "label": label, "level": level}


# Between the pushes of a scope that covers several ("fix → main @ origin,
# fix → release @ origin"): a ref name never holds a space, so it can't be
# mistaken for part of one.
PUSH_LIST = ", "


def _join(parts) -> str:
    return PUSH_LIST.join(dict.fromkeys(parts))


def push_scopes(keys: list[str]) -> list[dict]:
    """The scope ladder of a command whose pushes have ask keys ``keys``: the
    exact form lists every push, the general form every destination (a
    grant covers a push when it fits one of them), the remote form every
    remote."""
    pushes = [permissions.push_pattern(k) or ("*", "*", "*") for k in keys]
    remotes = _join(remote for _, _, remote in pushes)
    if PUSH_LIST in remotes:
        dests = _join(f"{dst} on {remote}" for _, dst, remote in pushes)
    else:
        dests = _join(dst for _, dst, _ in pushes)
    exact_label = "This push only · " + _join(f"{src} into {dst} on {remote}" for src, dst, remote in pushes)
    return [_scope(EXACT, _join(keys), exact_label, "branch"),
            _scope(GENERAL, _join(f"*{PUSH_ARROW}{dst}{PUSH_AT}{remote}" for _, dst, remote in pushes),
                   f"Into {dests} from any branch on {remotes}", "destination"),
            _scope("remote", _join(f"*{PUSH_ARROW}*{PUSH_AT}{remote}" for _, _, remote in pushes),
                   f"All branches on {remotes}", "remote"),
            _scope("repository", f"*{PUSH_ARROW}*{PUSH_AT}*", "All pushes in this repository", "repository"),
            _scope("all", f"*{PUSH_ARROW}*{PUSH_AT}*", "All pushes in every repository", "all")]


# -- command families ----------------------------------------------------------------------------
#
# A shell command's families are what its simple commands are, as
# permission_groups classifies them: a read-only git command or a read-only
# shell command (grep, ls, cat, cd ...), or, of a dev tool's commands, only
# a test run or a query whose operation brindle knows (DEV_OPERATIONS; "uv
# run" alone may run anything). Three more change files, each only in a
# checkout (_checkouts: any repo brindle works in or a session attached, and
# its worktrees): a local git command (GIT_LOCAL: never a push), a package
# install into the project (INSTALLS), and a file change (a write, edit,
# move, copy or delete, a redirection) whose every path is in one. An
# approval at the general scope covers any command whose every part falls in
# the families it was granted for, however they are piped or chained. A part
# of any other class (a push, a reset or clean, a delete of a checkout or of
# anything outside one, a write outside, a word only the run knows, a cd
# brindle can't follow before it) puts the command in no family: only its
# own grant covers it.

FAMILY = "family: "
READ_FILTER = "shell/readonly"
TESTS = "dev/tests"
QUERIES = "dev/queries"
LOCAL_GIT = "git/local"
INSTALL = "dev/install"
WRITES = "files/write-worktree"
FAMILY_LABELS = {"git/readonly": "read-only git commands", READ_FILTER: "read-only shell commands",
                 TESTS: "test runs", QUERIES: "package and GitHub queries", LOCAL_GIT: "local git changes",
                 INSTALL: "package installs into the project", WRITES: "file changes in the checkout"}
# git subcommands that change only the local repository -> (long options never in the family,
# short ones, the short options that take a value). A long option counts however git lets it be
# abbreviated (--exe), a short one anywhere in a cluster before a value (-x"cmd", -tf). What
# runs a command (rebase --exec, a merge strategy that isn't git's own) or discards the
# checkout's changes (checkout/switch --force, a restore or checkout of a whole checkout, as
# reset --hard would) is never in it.
GIT_LOCAL = {"add": ((), "", ""), "commit": ((), "", "mFcCt"), "stash": ((), "", "m"), "tag": ((), "", "mFu"),
             "branch": ((), "", ""), "merge": ((), "", "mFsX"), "rebase": (("--exec",), "x", "sXC"),
             "restore": ((), "", "s"), "switch": (("--force", "--discard-changes"), "f", "cC"),
             "checkout": (("--force",), "f", "bB")}
STRATEGIES = frozenset({"ort", "recursive", "resolve", "octopus", "ours", "subtree"})
# Short options whose value is optional and only ever attached (-S, -Skey): never the next word.
GIT_ATTACHED_VALUES = "S"
# A package manager's install or add, into the project where it runs (and pip, from the project's
# own environment: .venv/bin/pip, .venv/bin/python -m pip).
INSTALLS = (("npm", "install"), ("npm", "i"), ("npm", "add"), ("pnpm", "install"), ("pnpm", "i"), ("pnpm", "add"),
            ("yarn", "install"), ("yarn", "add"), ("uv", "add"), ("uv", "sync"), ("uv", "pip", "install"))
# The only install options in the family: none that puts packages, a cache or a log elsewhere or
# fetches from elsewhere. Those that take a value take a name or a path in the project.
INSTALL_FLAGS = frozenset({"-D", "--save-dev", "-E", "--save-exact", "-O", "--save-optional", "-P", "--save-prod",
                           "--save-peer", "-S", "--save", "--no-save", "--dev", "--exact", "--optional", "--peer",
                           "--frozen-lockfile", "--immutable", "--prefer-offline", "--offline", "--no-audit",
                           "--no-fund", "--legacy-peer-deps", "--ignore-scripts", "--silent", "--frozen", "--locked",
                           "--no-dev", "--all-extras", "--all-groups", "--upgrade", "-U", "--no-deps", "--pre",
                           "--no-install-project", "-q", "--quiet", "-v", "--verbose"})
INSTALL_VALUES = frozenset({"--group", "--extra", "--package", "--upgrade-package", "-r", "--requirement", "-c",
                            "--constraint", "-e", "--editable"})
# The files rm and rmdir delete, by the options brindle reads for each.
REMOVE_FLAGS = {"rm": re.compile(r"-[rRfidvI]+|--(recursive|force|dir|verbose)"),
                "rmdir": re.compile(r"-v|--verbose")}
# pytest however it is started, with only these options: none that loads code or a config from
# elsewhere (-p, -c, --rootdir ...) or deletes a directory (--basetemp).
PYTEST = (("pytest",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"), ("uv", "run", "pytest"),
          ("uv", "run", "python", "-m", "pytest"))
PYTEST_FLAGS = re.compile(r"-[qvxsl]+|-r[a-zA-Z]+|-n(\d+|auto)|--(tb|maxfail|durations|color)=[a-z0-9]+"
                          r"|--(lf|ff|last-failed|failed-first|nf|new-first|sw|stepwise|co|collect-only|no-header"
                          r"|strict-markers|disable-warnings|quiet|verbose|exitfirst|showlocals)")
PYTEST_VALUES = ("-k", "-m", "-n")
# A package manager's test script, with nothing after it but test names or paths after "--".
SCRIPT_TESTS = tuple((tool, *verb) for tool in ("npm", "pnpm", "yarn") for verb in (("test",), ("run", "test")))
# What only reads: a package manager's listing or lookup, GitHub's view of a pull request or run.
DEV_QUERIES = (("npm", "ls"), ("npm", "list"), ("npm", "view"), ("npm", "info"), ("npm", "outdated"),
               ("npm", "explain"), ("npm", "config", "get"), ("npm", "config", "list"), ("pnpm", "ls"),
               ("pnpm", "list"), ("pnpm", "why"), ("pnpm", "outdated"), ("yarn", "list"), ("yarn", "info"),
               ("yarn", "why"), ("uv", "pip", "list"), ("uv", "pip", "show"), ("uv", "pip", "freeze"),
               ("gh", "pr", "view"), ("gh", "pr", "list"), ("gh", "pr", "diff"), ("gh", "pr", "checks"),
               ("gh", "pr", "status"), ("gh", "run", "list"), ("gh", "run", "view"), ("gh", "issue", "view"),
               ("gh", "issue", "list"), ("gh", "repo", "view"))


def _local(word: str) -> bool:
    """A test path or name inside the checkout: nothing absolute, from home, or up a level."""
    return not word.startswith(("/", "~", "-", "@")) and ".." not in word.split("/")


def _pytest_args(args: list[str]) -> bool:
    if any(a.startswith("@") for a in args):    # an argument file: options brindle doesn't see, wherever it is
        return False
    i = 0
    while i < len(args):
        if args[i] in PYTEST_VALUES and i + 1 < len(args):
            i += 2
        elif PYTEST_FLAGS.fullmatch(args[i]) or _local(args[i]):
            i += 1
        else:
            return False
    return True


def _dev_operation(words: list[str]) -> str | None:
    """A dev tool command's family: a test run or a query (DEV_QUERIES), else None."""
    def started(prefixes):
        return next((len(p) for p in prefixes if tuple(words[:len(p)]) == p), None)

    if (n := started(PYTEST)) is not None:
        return TESTS if _pytest_args(words[n:]) else None
    if (n := started(SCRIPT_TESTS)) is not None:
        rest = words[n:]
        return TESTS if not rest or rest[0] == "--" and all(_local(w) for w in rest[1:]) else None
    return QUERIES if started(DEV_QUERIES) is not None else None


def _checkouts(req: Request) -> list[str]:
    """Where a family's change may land: ``req``'s worktree and repo, every
    repo brindle has a workspace in or a session attached, and each of their
    worktrees. Read from the database read-only (none yet: just ``req``'s)."""
    from brindle.config import db_path

    roots = permissions._roots(req)
    path = db_path()
    if path.exists():
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                rows = [*conn.execute("SELECT repo_root, path FROM workspaces"),
                        *conn.execute("SELECT repo_root, '' FROM session_repos")]
            finally:
                conn.close()
        except sqlite3.Error:
            rows = []
        roots += [r for row in rows for r in row if r]
    return list(dict.fromkeys(os.path.normpath(r) for r in roots))


def _runs_in(cwd: str, roots: list[str]) -> bool:
    real = os.path.realpath(cwd)
    return any(real == os.path.realpath(r) or permissions._inside(real, r) for r in roots)


def _holds_root(path: str, roots: list[str]) -> bool:
    """``path`` is one of ``roots`` or a folder holding one, as written or resolved."""
    return any(f(r) == f(path) or f(r).startswith(f(path).rstrip(os.sep) + os.sep)
               for f in (os.path.normpath, os.path.realpath) for r in roots)


def _changeable(path: str, roots: list[str]) -> bool:
    """Whether a change to ``path`` stays in a checkout, as written and as
    resolved: inside one of ``roots``, never a root itself or a folder
    holding one, nor in the .git/.brindle/.agents of the root it is in."""
    if _holds_root(path, roots):
        return False
    protected = {d.casefold() for d in permissions.AGY_PROTECTED_DIRS}
    for f in (os.path.normpath, os.path.realpath):
        p, rs = f(path), [f(r) for r in roots]
        inside = [r for r in rs if p.startswith(r + os.sep)]
        if not inside:
            return False
        if protected & {part.casefold() for part in os.path.relpath(p, max(inside, key=len)).split(os.sep)}:
            return False
    return True


def _git_local(words: list[str], cwd: str, roots: list[str]) -> bool:
    """A git command in GIT_LOCAL with none of its banned options (GIT_LOCAL),
    no merge strategy but git's own, no global option (-C, -c), and no restore
    or checkout of a whole checkout."""
    if len(words) < 2 or os.path.basename(words[0]) != "git" or words[1] not in GIT_LOCAL:
        return False
    sub, args = words[1], words[2:]
    longs, shorts, values = GIT_LOCAL[sub]
    strategies = sub in ("merge", "rebase")
    i = 0
    while i < len(args):
        arg, i = args[i], i + 1
        if arg == "--":
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if any(b.startswith(name) for b in longs):
                return False
            if strategies and "--strategy".startswith(name):
                if not eq:
                    value, i = (args[i] if i < len(args) else ""), i + 1
                if value not in STRATEGIES:
                    return False
        elif arg.startswith("-"):
            for j, c in enumerate(arg[1:], 2):    # j: where c's value starts
                if c in shorts:
                    return False
                if c in GIT_ATTACHED_VALUES:
                    break
                if c in values:
                    value = arg[j:] or (args[i] if i < len(args) else "")
                    if strategies and c == "s" and value not in STRATEGIES:
                        return False
                    i += j == len(arg)    # its value is the next word
                    break
    if sub in ("restore", "checkout"):
        names = [a for a in args if not a.startswith("-")]
        if any(n.startswith(":") or any(c in n for c in "*?[") for n in names):
            return False    # pathspec magic (:/ is the whole checkout), a pattern
        if any(_holds_root(os.path.join(cwd, n), roots) for n in names):
            return False    # a checkout's root, or a folder holding one
    return True


def _installs(words: list[str], cwd: str, roots: list[str]) -> bool:
    """A package install into the project (INSTALLS, or the project's own
    pip) with only the options in INSTALL_FLAGS and INSTALL_VALUES, and
    every package, name or file it is given one in the project."""
    n = next((len(p) for p in INSTALLS if tuple(words[:len(p)]) == p), None)
    # A venv's python links to the system's: where it is written is what says whose environment it is.
    program = os.path.normpath(os.path.join(cwd, words[0]))
    if n is None and "/" in words[0] and any(program.startswith(os.path.normpath(r) + os.sep) for r in roots):
        name = os.path.basename(program)
        if name in ("pip", "pip3") and words[1:2] == ["install"]:
            n = 2
        elif name in ("python", "python3") and words[1:4] == ["-m", "pip", "install"]:
            n = 4
    if n is None:
        return False

    def in_project(word: str) -> bool:    # a registry package, or a path in the project (no URL, file: or git+)
        return bool(word) and not word.startswith(("/", "~", "-")) and ".." not in word.split("/") and ":" not in word

    args, i = words[n:], 0
    while i < len(args):
        arg, i = args[i], i + 1
        name, eq, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if name in INSTALL_VALUES:
            if not eq:
                value, i = (args[i] if i < len(args) else ""), i + 1
            if not in_project(value):
                return False
        elif arg.startswith("-") and arg not in INSTALL_FLAGS or not arg.startswith("-") and not in_project(arg):
            return False
    return True


def _removes(words: list[str], cwd: str, roots: list[str]) -> bool:
    """An rm or rmdir, with only the options brindle reads, of files in a checkout."""
    flags = REMOVE_FLAGS.get(os.path.basename(words[0]))
    if flags is None:
        return False
    names, options = [], True
    for arg in words[1:]:
        if options and arg == "--":
            options = False
        elif options and arg.startswith("-"):
            if not flags.fullmatch(arg):
                return False
        else:
            names.append(arg)
    return bool(names) and all(_changeable(os.path.join(cwd, n), roots) for n in names)


def _file_writes(program: str, args: list[str], cwd: str, roots: list[str]) -> bool:
    """Whether every file a FILE_WRITERS program (not ln) or ``sed -i``
    changes stays in a checkout: each name it is given, what cp or mv makes
    in a destination folder (a recursive copy only into a new one; no option
    that names the destination), and no sed backup put in another folder."""
    from brindle import permission_groups

    targets = permission_groups._write_targets(program, args)
    if not targets or program == "ln":
        return False
    if program == "sed" and any(re.fullmatch(r"-[nErsuz]*i.*/.*|--in-place=.*/.*", a, re.S) for a in args):
        return False
    paths = [os.path.join(cwd, t) for t in targets]
    if program in ("cp", "mv"):
        # Only the last name is the destination: no long option (--target-directory=) and no -T.
        if any(a.startswith("--") and a != "--" or re.fullmatch(r"-[A-Za-z]*T[A-Za-z]*", a) for a in args):
            return False
        if len(paths) < 2:
            return False
        dest, sources = paths[-1], targets[:-1]
        into = len(paths) > 2 or os.path.isdir(dest) or targets[-1].endswith("/")
        recursive = program == "cp" and any(re.fullmatch(r"-[A-Za-z]*[rRa][A-Za-z]*", a) for a in args)
        made = [os.path.join(dest, os.path.basename(os.path.normpath(p))) for p in paths[:-1]] if into else []
        # A recursive copy only makes a new folder: into one that's there (or src/'s contents into dest,
        # as BSD cp copies them) it writes below names that may lead anywhere.
        if recursive and (any(os.path.lexists(m) for m in made) or os.path.lexists(dest) and (
                not into or any(s.endswith(("/", "/.")) for s in sources))):
            return False
        paths += made
    return all(_changeable(p, roots) for p in paths)


def _part_family(req: Request, part: permissions.ShellPart, rules: list[Rule],
                 roots: list[str]) -> list[str | None]:
    """A part's family, then one for the files its redirections write (WRITES when every one is in a checkout)."""
    from brindle import permission_groups

    # Where it runs unknown (after a cd that may not have happened), the files it reads are too.
    if part.cwd is None or part.unknown or part.expanded or part.assigned or part.reads or part.redirect_glob:
        return [None]
    writes = [WRITES if all(os.path.isabs(p) and _changeable(p, roots) for p in part.writes) else None
              ] if part.writes else []
    (group, sub), *_ = permission_groups.part_classes(req, part, rules, [])
    if f"{group}/{sub}" in ("git/readonly", READ_FILTER):
        return [f"{group}/{sub}", *writes]
    # What changes or runs the project does so where it runs: in a checkout, never elsewhere.
    if not _runs_in(part.cwd, roots):
        return [None]
    words, program = part.words, os.path.basename(part.words[0])
    if _git_local(words, part.cwd, roots):
        found = LOCAL_GIT
    elif _installs(words, part.cwd, roots):
        found = INSTALL
    elif _removes(words, part.cwd, roots):
        found = WRITES
    elif group == "files":
        found = WRITES if _file_writes(program, words[1:], part.cwd, roots) else None
    elif f"{group}/{sub}" == "shell/dev-tools":
        found = _dev_operation(words)
    else:
        found = None
    return [found, *writes]


def _part_families(req: Request, rules: list[Rule] | None = None) -> list[str | None] | None:
    """Each part of ``req``'s shell command's family, and its redirections'
    (None: in none), or None when brindle can't read what it runs. What a
    part is doesn't depend on the ask rules that make it ask, only on the
    denies (a read of a denied file is no read-only command)."""
    parts = permissions.shell_parts(req) if req.kind == "bash" else None
    if not parts:
        return None
    rules = permissions.all_rules(req.repo_root or None) if rules is None else rules
    denies = [r for r in rules if r.decision == "deny"]
    roots = _checkouts(req)
    return [f for part in parts for f in _part_family(req, part, denies, roots)]


def families(req: Request, rules: list[Rule] | None = None) -> list[str] | None:
    """The families ``req``'s shell command falls in, or None unless every
    part falls in one. A read-only shell part beside another family's (the
    ``| tail -5`` after a test run) only reads what that one prints: the
    command is of the other family alone."""
    found = _part_families(req, rules)
    if found is None or None in found:
        return None
    return sorted(set(found) - {READ_FILTER}) or [READ_FILTER]


def family_pattern(found: list[str]) -> str:
    return FAMILY + PUSH_LIST.join(found)


def pattern_families(pattern: str) -> set[str] | None:
    return set(pattern.removeprefix(FAMILY).split(PUSH_LIST)) if pattern.startswith(FAMILY) else None


def family_label(found: list[str]) -> str:
    names = [FAMILY_LABELS.get(f, f) for f in found]
    text = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return text[0].upper() + text[1:]


def _family_matches(pattern: str, req: Request | None, rules: list[Rule] | None, loose: bool) -> bool:
    """Whether family pattern ``pattern`` covers ``req``: a part in one of its
    families and every other part in one too or read-only (families), or for
    a deny (``loose``) any part in one."""
    wanted = pattern_families(pattern) or set()
    found = _part_families(req, rules) if req is not None else None
    if not found:
        return False
    hit = any(f in wanted for f in found)
    return hit if loose else hit and all(f in wanted or f == READ_FILTER for f in found)


def scopes_for(kind: str, key: str, name: str = "", families: list[str] | None = None) -> list[dict]:
    """The scope ladder for a request of ``kind`` with ask key ``key``,
    narrowest first; the general form is the one valued GENERAL: for a shell
    command whose every part falls in a family (``families``), those families."""
    if kind == "push":
        return push_scopes(key.split(PUSH_LIST))
    if kind in permissions.PATH_KINDS:
        verb = {"read": "reads", "write": "writes", "edit": "edits"}[kind]
        top = key.split("/")[0] if "/" in key else ""
        out = [_scope(EXACT, key, f"This file only · {key}")]
        if top and not any(c in top for c in "*?["):
            out.append(_scope(GENERAL, f"{top}/**", f"Everything under {top}/**"))
        else:
            out.append(_scope(GENERAL, key, f"This file only · {key}"))
        out.append(_scope("repository", "**", f"Any file ({verb} anywhere in the repository)"))
        return out
    if kind == "bash":
        out = [_scope(EXACT, key, f"This command only · {key}")]
        prefix = " ".join(key.split()[:2]) if simple_argv(key) is not None else None
        if families:
            if prefix:
                out.append(_scope("command", f"{prefix} *", f"Commands starting with {prefix}"))
            out.append(_scope(GENERAL, family_pattern(families), family_label(families)))
        elif prefix:
            out.append(_scope(GENERAL, f"{prefix} *", f"Commands starting with {prefix}"))
        else:
            out.append(_scope(GENERAL, key, f"This command only · {key}"))
        out.append(_scope("repository", "*", "Any command in the repository"))
        return out
    if kind == "mcp":
        parts = key.split("__")
        server = parts[1] if key.startswith("mcp__") and len(parts) >= 3 and parts[1] else None
        out = [_scope(GENERAL, key, f"This tool · {name or key}")]
        if server:
            out.append(_scope("server", f"mcp__{server}__*", f"Every {server} tool"))
        out.append(_scope("all", "mcp__*", "Every MCP tool"))
        return out
    if kind == "fetch":
        parts = urlsplit(key)
        host = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
        out = [_scope(EXACT, key, f"This URL only · {key}")]
        out.append(_scope(GENERAL, f"{host}/*", f"Everything on {host}") if host
                   else _scope(GENERAL, key, f"This URL only · {key}"))
        out.append(_scope("all", "*", "Any URL"))
        return out
    return [_scope(EXACT, key, f"This only · {key}"), _scope(GENERAL, key, f"This only · {key}"),
            _scope("all", "*", f"Every {kind} request")]


@dataclass
class Ask:
    """One request, generalized."""
    kind: str
    exact: str
    scopes: list[dict]

    def scope(self, value: str) -> dict | None:
        return next((s for s in self.scopes if s["value"] == value), None)

    @property
    def general(self) -> dict:
        return self.scope(GENERAL) or self.scopes[0]


def generalize(req: Request) -> Ask:
    """``req`` generalized. A command that pushes to several destinations
    keeps one exact form, listing every push, and a general form listing
    every destination (push_scopes)."""
    kind = grant_kind(req)
    if kind == "push":
        keys = list(dict.fromkeys(ask_keys(req)))
        return Ask(kind, _join(keys), push_scopes(keys))
    return Ask(kind, ask_key(req), scopes_for(kind, ask_key(req), req.tool, families(req) if kind == "bash" else None))


# -- matching saved actions ----------------------------------------------------------------------


def _push_fits(pattern: str, key: str, loose: bool) -> bool:
    """Whether push key ``key`` fits one push pattern. A ref only git can
    name (UNKNOWN_REF: HEAD, no refspec, a wildcard) fits only ``*``, unless
    ``loose`` (a deny, which errs toward matching: it may well be that branch)."""
    pats, refs = permissions.push_pattern(pattern), permissions.push_pattern(key)
    if pats is None or refs is None:
        return False
    for pat, ref in zip(pats, refs):
        if ref == UNKNOWN_REF:
            if not (loose or pat == "*"):
                return False
        elif not fnmatch.fnmatchcase(ref, pat):
            return False
    return True


def scope_matches(kind: str, pattern: str, key: str, loose: bool = False, req: Request | None = None,
                  rules: list[Rule] | None = None) -> bool:
    """Whether a saved action's ``pattern`` covers ask key ``key``. A push
    pattern may list several pushes (push_scopes): the key fits any one.
    ``loose``: for a deny, a push whose branch only git knows counts as
    covered whenever it may be the one the pattern names, and a family
    pattern any command with a part in one of its families. A family pattern
    reads the command itself: ``req``, the request ``key`` is of (none: no match)."""
    if pattern in ("*", "**"):
        return True
    if kind == "push":
        return any(_push_fits(p, key, loose) for p in pattern.split(PUSH_LIST))
    if kind == "bash" and pattern.startswith(FAMILY):
        return _family_matches(pattern, req, rules, loose)
    if kind == "bash":
        if simple_argv(key) is None:
            return pattern == key   # a compound or quoted command: only the exact grant
        if pattern.endswith(" *"):
            head = pattern[:-2].split()
            return key.split()[:len(head)] == head
        return pattern == key or fnmatch.fnmatchcase(key, pattern)
    return key == pattern or fnmatch.fnmatchcase(key, pattern)


def _other_parts_allowed(req: Request, rules: list[Rule] | None) -> bool:
    """A push grant covers a compound command only when every part that isn't
    the push is itself allowed by a rule (``cd x && git push`` is fine, ``git
    push && curl ...`` is not)."""
    if simple_argv(req.command) is not None:
        return True
    from dataclasses import replace

    from brindle.native.permissions import split_commands

    rules = rules if rules is not None else permissions.all_rules(req.repo_root or None)
    for part in split_commands(req.command or ""):
        words = part.split()
        if words and words[0] == "git" and "push" in words:
            continue
        if words and words[0] == "cd":
            continue
        got = permissions._rule_decision(replace(req, command=part), rules, None)
        if got is None or got.decision != "allow":
            return False
    return True


def covers(action: SavedAction, req: Request, rules: list[Rule] | None = None) -> bool:
    """Whether ``action``'s pattern covers ``req``. A deny covers it as soon as
    any push the command makes matches, or may (a branch only git can name,
    see scope_matches); an allow only when every push does, strictly, and
    nothing else runs alongside them."""
    if action.kind != grant_kind(req):
        return False
    if action.repo is not None and req.repo_root and action.repo != req.repo_root:
        return False
    deny = action.decision == "deny"
    hits = [scope_matches(action.kind, action.pattern, key, deny, req, rules) for key in ask_keys(req)]
    if deny:
        return any(hits)
    if not all(hits) or action.binding and not _binding_holds(action.binding, req):
        return False
    return action.kind != "push" or _other_parts_allowed(req, rules)


GIT_SECONDS = 5


def _git(repo: str, *args: str) -> str | None:
    try:
        res = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=GIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _owner_now(repo_root: str | None) -> str | None:
    """Who approves now, as brindle 0.4.7 bound its remembered approvals: the
    licensed account (within its org), or "local" without a licence; None when
    an org's member can't be verified."""
    from brindle.pro import license

    lim = org_limits(repo_root)
    owner = lim.owner if lim else "local"
    try:
        subject = license.current(refresh=False).sub
    except license.LicenseError:
        return "local" if owner == "local" else None
    return hashlib.sha256(f"{owner}:{subject}".encode()).hexdigest()


def _binding_holds(binding: dict, req: Request) -> bool:
    """Whether what a saved allow was bound to still holds for ``req``: the
    approving identity (``owner``), the repository's git directory
    (``git_dir``) and, for a push, the URL every pushed-to remote resolves to
    (``url``). Anything that can't be read doesn't hold."""
    repo = req.repo_root or req.worktree or req.cwd
    if "owner" in binding and binding["owner"] != _owner_now(req.repo_root or None):
        return False
    if "git_dir" in binding:
        found = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir") if repo else None
        if found is None or os.path.realpath(found) != os.path.realpath(binding["git_dir"]):
            return False
    if "url" in binding:
        remotes = {p.remote for p in permissions.push_calls(req.command or "")}
        if not remotes or any(r == UNKNOWN_REF or _git(repo, "remote", "get-url", "--push", r) != binding["url"]
                              for r in remotes):
            return False
    return True


def limits_allow(action_level: str | None, action_pattern: str, action_life: str, action_minutes: float | None,
                 rule: Rule | None) -> str | None:
    """Why a saved allow with this scope and lifetime falls outside ``rule``'s
    limits (the ask rule that asked; None for an unmatched ask, which has no
    limits), or None when it is within them."""
    limits = (rule.limits if rule is not None else None) or {}
    if limits.get("life") == "once":
        return "approvable only once at a time"
    scope = limits.get("max_scope")
    if scope:
        if action_level is not None:
            if scope in LEVELS and LEVELS.index(action_level) > LEVELS.index(scope):
                return "outside its approval scope"
        elif scope not in ("*", "**") and not action_pattern.startswith(scope.rstrip("*")):
            return "outside its approval scope"
    life = limits.get("life")
    if life in ("session", "time") and action_life == "forever":
        return "not permitted until revoked"
    if life == "time":
        cap = limits.get("minutes")
        if action_life != "time" or action_minutes is None or (cap is not None and action_minutes > cap):
            return f"over its {cap:g} minute limit" if cap is not None else "over its time limit"
    return None


# -- the org's limits ----------------------------------------------------------------------------
#
# For a member of a Team org, the org policy's ``permission_grants`` block
# (brindle.pro.team_policy.GrantPolicy) bounds what a saved allow may do, read
# again on every use: the kinds it may cover, its lifetimes (forever only
# with allow_persistent), at most max_minutes for a timed one, and its
# scope (branch/remote/repo/all). Its deny rules are in force for every
# provider (permissions.all_rules). No block, a policy that can't be read,
# or a member the org paused: no saved allow applies. Without a Team org,
# nothing here limits the person's own.

# A saved action's level or scope value -> the org scope it needs.
ORG_SCOPES = {"branch": "branch", "destination": "branch", "exact": "branch", "general": "branch", "command": "branch",
              "remote": "remote", "repository": "repo", "server": "repo", "all": "all"}


@dataclass(frozen=True)
class OrgLimits:
    allow: bool
    kinds: tuple[str, ...] = ()
    max_minutes: int = 0
    lifetimes: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    allow_persistent: bool = False
    deny: tuple[Rule, ...] = ()
    why: str = ""                 # why no saved allow applies
    owner: str = "local"          # "org:<id>": whose approvals these are (_owner_now)


def org_rule(spec: str) -> Rule:
    """An org policy deny ("<kind> <match_type> <match>", as a profile's permission_denies) as a rule."""
    kind, match_type, match = spec.split(None, 2)
    return Rule(kind, match, match_type, "deny", "org")


def _plugin(repo_root: str):
    from brindle.pro.team_policy import ProPolicy

    return ProPolicy(repo_root)


def org_limits(repo_root: str | None) -> OrgLimits | None:
    """The Team org's limits in ``repo_root`` now, or None without a Team org."""
    from brindle.policy import Decision as PolicyDecision
    from brindle.pro import status

    if not repo_root:
        return None
    try:
        p = _plugin(repo_root).policy()
    except Exception as e:  # noqa: BLE001 - fail closed
        return OrgLimits(False, why=f"the org policy can't be read ({e})")
    if isinstance(p, PolicyDecision):
        return None if p.allowed else OrgLimits(False, why=p.reason)
    g = p.permission_grants
    deny = tuple(org_rule(s) for s in g.deny) if g else ()
    owner = f"org:{p.org_id}"
    blocked = status.block_reason(p)
    if blocked:
        return OrgLimits(False, deny=deny, why=f"org {p.org_id}: {blocked}", owner=owner)
    if g is None:
        return OrgLimits(False, why=f"org {p.org_id}'s policy sets no permission_grants", owner=owner)
    return OrgLimits(g.allow, tuple(g.kinds), g.max_minutes, tuple(g.lifetimes), tuple(g.scopes),
                     g.allow_persistent, deny, "" if g.allow else f"org {p.org_id} allows no saved approvals", owner)


def org_denies(repo_root: str | None) -> list[Rule]:
    lim = org_limits(repo_root)
    return list(lim.deny) if lim else []


def org_refuses(lim: OrgLimits | None, kind: str, life: str, minutes: float | None, level: str | None,
                scope: str) -> str | None:
    """Why the org's limits ``lim`` don't let a saved allow of ``kind`` with this
    lifetime and reach apply, or None (always None without a Team org)."""
    if lim is None:
        return None
    if not lim.allow:
        return lim.why or "the org allows no saved approvals"
    if ("bash" if kind == "push" else kind) not in lim.kinds:
        return f"the org allows no saved approvals of {kind}"
    if life not in lim.lifetimes or life == "forever" and not lim.allow_persistent:
        return f"the org doesn't allow approvals that last {'until revoked' if life == 'forever' else 'a ' + life}"
    if life == "time" and (minutes is None or minutes > lim.max_minutes):
        return f"over the org's {lim.max_minutes} minute limit"
    if ORG_SCOPES.get(level or scope, "all") not in lim.scopes:
        return "wider than the org allows"
    return None


def settle(req: Request, rule: Rule | None, saved: list[SavedAction], *, session: str | None = None,
           rules: list[Rule] | None = None, now: float | None = None, denies_only: bool = False,
           asks: tuple = (), spend: bool = False) -> Decision | None:
    """The saved action that settles an ask for ``req`` (``rule``: the ask
    rule that asked, None for an unmatched ask; ``asks``: every ask rule that
    matches it, when several do), as a Decision, or None. A saved deny always
    stops it; a saved allow only within the limits of every one of those
    rules and the org's, while it has uses left (``spend``: the call runs on
    it, so one is spent). ``denies_only``: look for a deny alone, before the
    policy's own allows. ``session``: the asking agent's session, for saved
    actions that last one."""
    now = time.time() if now is None else now
    hits = [s for s in saved if not s.expired(now) and (s.life != "session" or (session and s.session == session))
            and (s.decision == "deny" or not denies_only) and covers(s, req, rules)]
    for s in hits:
        if s.decision == "deny":
            return Decision("deny", f"stopped by your saved deny {s.id} ({s.kind} {s.pattern})", saved=s.id)
    if denies_only:
        return None
    lim = org_limits(req.repo_root or None) if hits else None
    for s in hits:
        if (all(limits_allow(s.level, s.pattern, s.life, s.minutes, r) is None for r in asks or [rule])
                and org_refuses(lim, s.kind, s.life, s.minutes, s.level, s.scope) is None
                and (s.uses_left is None or s.uses_left > 0 and (not spend or permissions.spend_saved_use(s.id)))):
            return Decision("allow", f"covered by your saved action {s.id} ({s.kind} {s.pattern})", rule,
                            saved=s.id)
    return None


# -- pending asks, grouped -----------------------------------------------------------------------


@dataclass
class PendingAsk:
    agent_id: str
    root_id: str
    request_id: str
    digest: str
    repo_root: str
    request: Request
    ask: Ask
    created_at: float
    policy: Decision              # what the rules say, saved actions aside: deny (blocked) or ask
    worker: str = ""
    escalation: str = ""          # why its supervisor, which had it first, left it to the person

    @property
    def blocked(self) -> bool:
        return self.policy.decision == "deny"

    @property
    def rule(self) -> Rule | None:
        return self.policy.rule


@dataclass
class Group:
    key: str
    repo_root: str
    kind: str
    items: list[PendingAsk]       # oldest first
    policy: Decision = field(default_factory=lambda: Decision("ask", ""))

    @property
    def rep(self) -> PendingAsk:
        return self.items[0]


def _policy(req: Request, repo_root: str) -> Decision:
    """The rules' answer for ``req`` with saved actions set aside (an ask's rule, for its limits)."""
    from brindle.config import load_repo_config

    checks = list(load_repo_config(repo_root).checks or []) if repo_root else None
    return permissions.decide(req, checks=checks, rules=permissions.all_rules(repo_root or None), saved=[])


def pending_asks(db, repo_root: str | None = None) -> list[PendingAsk]:
    """Every request a hook holds for a person now (across ``repo_root``'s
    sessions, or every repo's), oldest first."""
    from brindle import autopilot
    from brindle.daemon.holds import HOLDS
    from brindle.service import inbox

    out = []
    for agent_id in HOLDS.agents():
        agent = db.get_agent(agent_id)
        if agent is None:
            continue
        ws = db.get_workspace(agent.workspace_id)
        if ws is None or (repo_root and ws.repo_root != repo_root):
            continue
        root = autopilot.root_of(db, agent.id)
        for p in inbox.pending_permissions(db, agent):
            if p.via != "hook" or p.request is None or p.row is None:
                continue
            hold = HOLDS.held(agent.id, p.dto.request_id)
            out.append(PendingAsk(agent.id, root, p.dto.request_id, p.dto.digest, ws.repo_root, p.request,
                                  generalize(p.request), float(p.row["created_at"]), _policy(p.request, ws.repo_root),
                                  worker=agent.profile,
                                  escalation=(hold.routed_note(HOLDS.clock()) if hold else None) or ""))
    return sorted(out, key=lambda a: a.created_at)


def group_key(repo_root: str, kind: str, pattern: str) -> str:
    return f"{repo_root}|{kind}|{pattern}"


def _restrictive(policies: list[Decision]) -> Decision:
    """The policy a group is shown under: a deny, else the tightest lifetime."""
    for p in policies:
        if p.decision == "deny":
            return p

    def rank(p: Decision) -> tuple:
        limits = (p.rule.limits if p.rule else None) or {}
        life = limits.get("life")
        return ({"once": -1, "time": 0, "session": 1}.get(life, 2), limits.get("minutes") or 0)

    return sorted(policies, key=rank)[0]


def groups(asks: list[PendingAsk]) -> list[Group]:
    """One group per repo, kind and general form, each generalized from its oldest ask."""
    by_key: dict[str, Group] = {}
    for a in asks:
        key = group_key(a.repo_root, a.ask.kind, a.ask.general["pattern"])
        g = by_key.get(key)
        if g is None:
            g = by_key[key] = Group(key, a.repo_root, a.ask.kind, [])
        g.items.append(a)
    for g in by_key.values():
        g.policy = _restrictive([a.policy for a in g.items])
    return list(by_key.values())


def eligible(ask: PendingAsk, scope: dict, mode: str, minutes: float | None) -> str | None:
    """Why ``ask`` can't be resolved by a grant of ``scope`` in ``mode``, or None."""
    if ask.blocked:
        return "blocked by a deny rule"
    if mode in ("deny", "always-deny"):
        return None
    if not all(scope_matches(ask.ask.kind, scope["pattern"], key, req=ask.request) for key in ask_keys(ask.request)):
        if not _is_exact(ask, scope):
            return "pushes outside this scope"     # a command pushing to more than the scope covers
        if mode != "once":
            # Its own exact form, which no pattern fits: a push whose branch
            # only git knows (git push origin HEAD). The held request itself
            # can be let through, but a saved grant could cover any branch.
            return "only git knows which branch it pushes: approve it once, or at a wider scope"
    if mode == "once":
        return None
    life = {"session": "session", "timed": "time", "forever": "forever"}[mode]
    return (next((why for r in ask.policy.asks or [ask.rule]
                  if (why := limits_allow(scope.get("level"), scope["pattern"], life, minutes, r))), None)
            or org_refuses(org_limits(ask.repo_root or None), ask.ask.kind, life, minutes, scope.get("level"),
                           scope["value"]))


def _is_exact(ask: PendingAsk, scope: dict) -> bool:
    """Whether ``scope`` is ``ask``'s own exact form (the held request's
    identity, which fits it even where no pattern does: an unknown ref)."""
    return scope["value"] == EXACT and scope["pattern"] == ask.ask.exact


def candidates(asks: list[PendingAsk], g: Group, scope: dict, loose: bool = False) -> list[PendingAsk]:
    """Every pending ask of the group's kind and repo the scope covers (every
    push of a command that makes several, or the ask whose exact form the
    scope is). ``loose``: for a deny, as scope_matches reads it."""
    return [a for a in asks if a.repo_root == g.repo_root and a.ask.kind == g.kind
            and (_is_exact(a, scope)
                 or all(scope_matches(g.kind, scope["pattern"], key, loose, a.request) for key in ask_keys(a.request)))]


def _result(a: PendingAsk, why: str | None = None) -> dict:
    row = {"agent_id": a.agent_id, "request_id": a.request_id, "exact": a.ask.exact}
    return {**row, "why": why} if why else row


def _answer(db, ask: PendingAsk, choice: str) -> str | None:
    """Answer one held ask; why it couldn't be, if it couldn't."""
    from brindle.service import inbox
    from brindle.service.errors import ServiceError

    try:
        inbox.answer_permission(db, ask.agent_id, ask.request_id, ask.digest, choice)
    except ServiceError as e:
        return e.message
    return None


def _grant(g: Group, scope: dict, decision: str, life: str, minutes: float | None, rep: PendingAsk) -> SavedAction:
    return permissions.save_action(
        g.kind, scope["pattern"], decision, scope=scope["value"], level=scope.get("level"),
        request=rep.request.to_dict(), life=life, minutes=minutes if life == "time" else None,
        session=rep.root_id if life == "session" else None,
        repo=None if scope["value"] == "all" else g.repo_root, note=scope["label"])


def _resolve(db, hits: list[PendingAsk], choice: str) -> tuple[list[dict], list[dict]]:
    done, failed = [], []
    for a in hits:
        why = _answer(db, a, choice)
        (failed if why else done).append(_result(a, why))
    return done, failed


def decide_group(db, key: str, scope_value: str, mode: str, minutes: float | None = None,
                 asks: list[PendingAsk] | None = None) -> dict:
    """The person's decision on Inbox item ``key``: ``mode`` once (allow
    these, save nothing), session, timed (``minutes``) or forever (save an
    allow of ``scope_value``), deny (stop these, save nothing) or always-deny
    (save a deny). Every candidate the scope covers is checked against its
    own policy; the grant is saved first, then each eligible ask is answered
    through its hold. What was resumed, stopped, skipped (and why) or failed."""
    if mode not in MODES:
        raise _error("invalid", f"mode must be one of {', '.join(MODES)}")
    asks = pending_asks(db) if asks is None else asks
    g = next((g for g in groups(asks) if g.key == key), None)
    if g is None:
        raise _error("not_found", "no such Inbox item any more: fetch the asks again", 404)
    scope = g.rep.ask.scope(scope_value)
    if scope is None:
        raise _error("invalid", f"scope must be one of {', '.join(s['value'] for s in g.rep.ask.scopes)}")
    if mode == "timed" and (minutes is None or minutes <= 0):
        raise _error("invalid", "a timed grant needs positive minutes")
    # Narrowed to the exact request, "once" and "deny" resolve the oldest ask
    # alone; at any wider scope the whole item (a grant: every ask it covers).
    exact = scope["value"] == EXACT
    wanted = (([g.rep] if exact else list(g.items)) if mode in ("once", "deny")
              else candidates(asks, g, scope, loose=mode == "always-deny"))
    reasons: dict[str, str] = {}
    hits = []
    for a in wanted:
        # A deny needs no coverage: it stops what the person points at.
        why = eligible(a, scope, mode, minutes) if mode not in ("deny", "always-deny") else None
        if why:
            reasons[a.request_id] = why
        else:
            hits.append(a)
    if not hits:
        raise _error("conflict", "nothing in this scope can be approved: "
                     + "; ".join(sorted(set(reasons.values()))), 409)
    # What is saved is the decision on this item: it must be one its own ask permits.
    own = eligible(g.rep, scope, mode, minutes) if mode in ("session", "timed", "forever") else None
    if own:
        raise _error("conflict", f"this can't be saved: {own}", 409)
    grant = None
    if mode in ("session", "timed", "forever"):
        grant = _grant(g, scope, "allow", {"session": "session", "timed": "time", "forever": "forever"}[mode],
                       minutes, g.rep)
    elif mode == "always-deny":
        grant = _grant(g, scope, "deny", "forever", None, g.rep)
    done, failed = _resolve(db, hits, "deny" if mode in ("deny", "always-deny") else "allow_once")
    return {"mode": mode, "scope": scope, "grant": grant.to_dict() if grant else None,
            "resumed": done if mode not in ("deny", "always-deny") else [],
            "stopped": done if mode in ("deny", "always-deny") else [],
            "skipped": [_result(a, reasons[a.request_id]) for a in wanted if a.request_id in reasons],
            "failed": failed}


# -- the stall notice's automated fixes ----------------------------------------------------------


def _mode_for(policy: Decision, forever: bool) -> tuple[str, float | None]:
    """The widest grant ``policy`` permits: forever when asked for, else its
    lifetime (a timed one at its cap), else for the session."""
    if forever:
        return "forever", None
    limits = (policy.rule.limits if policy.rule else None) or {}
    if limits.get("life") == "time":
        return "timed", limits.get("minutes")
    if limits.get("life") == "forever":
        return "forever", None
    return "session", None


def plan_allow_all(asks: list[PendingAsk], forever: bool = False, only: list[PendingAsk] | None = None) -> dict:
    """What "allow everything waiting" would grant: one grant per Inbox item
    at the general scope where every ask's policy permits it, an exact grant
    for each ask whose policy permits only that, and what stays waiting."""
    wanted = asks if only is None else only
    plan, done, skipped = [], [], []
    for g in groups(asks):
        mine = [a for a in g.items if a in wanted]
        if not mine:
            continue
        mode, minutes = _mode_for(g.policy, forever)
        general = g.rep.ask.general
        ok = [a for a in mine if eligible(a, general, mode, minutes) is None]
        if ok:
            plan.append({"group": g, "scope": general, "hits": ok, "mode": mode, "minutes": minutes})
            done += ok
        for a in mine:
            if a in ok:
                continue
            own_mode, own_minutes = _mode_for(a.policy, forever)
            exact = a.ask.scopes[0]
            if eligible(a, exact, own_mode, own_minutes) is None:
                plan.append({"group": g, "scope": exact, "hits": [a], "mode": own_mode, "minutes": own_minutes})
                done.append(a)
            else:
                skipped.append(a)
    return {"plan": plan, "done": done, "skipped": skipped}


def plan_to_json(planned: dict) -> dict:
    return {"grants": [{"group": e["group"].key, "kind": e["group"].kind, "scope": e["scope"],
                        "mode": e["mode"], "minutes": e["minutes"],
                        "requests": [a.request_id for a in e["hits"]]} for e in planned["plan"]],
            "resumes": len(planned["done"]),
            "skipped": [_result(a, "blocked by a deny rule" if a.blocked else "outside its own limits")
                        for a in planned["skipped"]]}


def allow_all(db, repo_root: str | None = None, forever: bool = False, asks: list[PendingAsk] | None = None,
              only: list[PendingAsk] | None = None) -> dict:
    """Carry out plan_allow_all: save each grant, answer each ask through its hold."""
    asks = pending_asks(db, repo_root) if asks is None else asks
    planned = plan_allow_all(asks, forever, only)
    grants, resumed, failed = [], [], []
    for e in planned["plan"]:
        life = {"session": "session", "timed": "time", "forever": "forever"}[e["mode"]]
        grants.append(_grant(e["group"], e["scope"], "allow", life, e["minutes"], e["hits"][0]).to_dict())
        done, lost = _resolve(db, e["hits"], "allow_once")
        resumed += done
        failed += lost
    return {**plan_to_json(planned), "saved": grants, "resumed": resumed, "failed": failed}


def deny_rules_for(ask: PendingAsk, rules: list[Rule]) -> list[Rule]:
    return [r for r in rules if r.decision == "deny" and permissions.rule_matches(r, ask.request)]


def unblock_denied(db, repo_root: str | None = None, forever: bool = False) -> dict:
    """Narrow the deny rules that block waiting asks with an exception for
    exactly each blocked request (a repo's own deny can't be changed here:
    the ask stays blocked), add an ask rule where unmatched actions are
    denied, then allow what was blocked (allow_all over those asks)."""
    asks = pending_asks(db, repo_root)
    denied = [a for a in asks if a.blocked]
    if not denied:
        return {"excepted": 0, "added": 0, "locked": [], **plan_to_json(plan_allow_all([], forever)),
                "saved": [], "resumed": [], "failed": []}
    excepted, added, locked = 0, 0, []
    store_ids = {r.id for r in permissions.load_store().rules}
    for a in denied:
        req = a.request
        key = ask_key(req) if req.kind != "bash" else " ".join((req.command or "").split())
        kind = req.kind
        for r in deny_rules_for(a, permissions.all_rules(a.repo_root)):
            if r.id not in store_ids:
                locked.append(a)     # the repo's own deny, or a profile's: not the person's to narrow
                continue
            if permissions.add_exclusion(r.id, kind, key, "exact") is not None:
                excepted += 1
        if a in locked:
            continue
        a.policy = _policy(req, a.repo_root)
        if a.blocked:    # unmatched actions are denied: a rule for its general form, that asks
            general = a.ask.general
            if general["pattern"].startswith(FAMILY):    # no rule matches a family: its command's own prefix
                general = a.ask.scope("command") or a.ask.scopes[0]
            match_type = "push" if a.ask.kind == "push" else ("exact" if general["pattern"] == key else "glob")
            permissions.add_rule(kind, general["pattern"], "ask", match_type)
            added += 1
            a.policy = _policy(req, a.repo_root)
    free = [a for a in denied if a not in locked and not a.blocked]
    out = allow_all(db, repo_root, forever, asks=asks, only=free)
    return {**out, "excepted": excepted, "added": added, "locked": [_result(a) for a in locked]}
