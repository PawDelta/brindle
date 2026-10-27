"""Capability groups: a blanket answer for each kind of thing an agent does.

Every request falls in a group (files, shell, git, network, mcp) and one of
its subcategories (shell: readonly, dev-tools, install, destructive, other;
...). A group has a baseline (allow, ask or deny) that a subcategory may
override; "inherit" leaves the baseline in force. The baseline answers only
what no rule or saved action settles (brindle.permissions.decide): a deny
first, then an explicit rule, then the subcategory, then the group's
baseline. A shell command is classified part by part (a file it writes is
files/write-*, a fetch network/fetch, a push git/push-*): deny if any part
denies, allow if every part allows, otherwise the strictest part's answer.
shell/unverifiable answers, ask or deny but never allow, for a command
brindle can't verify where it would otherwise ask.

The table lives in the store (Store.groups): {group: {"baseline": ...,
"subcategories": {sub: ...}}}. Each preset seeds its own (PRESET_OVERRIDES
over the preset's unmatched setting); a store with none uses its unmatched
setting for every group."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import replace

from brindle import permissions
from brindle.permissions import Request, Rule

GROUPS: dict[str, tuple[str, ...]] = {
    "files": ("read", "write-worktree", "write-outside"),
    "shell": ("readonly", "dev-tools", "install", "destructive", "other", "unverifiable"),
    "git": ("readonly", "commit", "push-feature", "push-protected", "other"),
    "network": ("fetch", "search"),
    "mcp": ("tool", "other"),
}
BASELINES = ("allow", "ask", "deny")
INHERIT = "inherit"
STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}

# A command brindle can't verify (a $(...) whose output it can't know, eval,
# a heredoc run as code) where it would otherwise ask: "ask", or "deny" with
# guidance the agent can act on. Never allow: inherit (or none) is ask.
UNVERIFIABLE = "shell/unverifiable"
UNVERIFIABLE_GUIDANCE = ("brindle can't verify this command; run it as separate simple commands "
                         "or drop the $(…)")
# preset -> {"group/subcategory": decision} over a baseline of the preset's unmatched setting.
PRESET_OVERRIDES: dict[str, dict[str, str]] = {
    "autonomous": {"git/push-protected": "ask", UNVERIFIABLE: "deny"},
    "permissive": {"files/write-worktree": "allow", "shell/readonly": "allow", "shell/dev-tools": "allow",
                   "git/readonly": "allow", "git/commit": "allow", UNVERIFIABLE: "deny"},
    "standard": {"shell/readonly": "allow", "git/readonly": "allow"},
    "secure": {},
}

# Programs that build, lint or test a project (the presets' everyday commands, and no
# other interpreter that runs whatever it's given).
DEV_TOOLS = frozenset({*permissions.PRESET_COMMANDS, "tsc", "ruff", "mypy", "black", "eslint", "prettier", "jest",
                       "vitest", "tox", "rustc", "cmake", "mvn", "gradle", "swift", "xcodebuild"})
# git subcommands that record work without discarding any (git/commit); any other git
# command that isn't read-only or a push (reset, clean, checkout, rebase, branch -D,
# config ...) is git/other.
COMMITS = frozenset({"add", "commit", "tag", "merge", "switch", "cherry-pick", "revert", "mv", "fetch", "pull"})
DISCARDING = ("-f", "--force", "--discard-changes", "-d", "-D", "--delete", "--worktree", "-W")
# program -> the subcommands that install or add packages ("pip install" two words deep).
INSTALLERS: dict[str, tuple[str, ...]] = {
    "pip": ("install",), "pip3": ("install",), "npm": ("install", "i", "ci", "add"), "pnpm": ("install", "i", "add"),
    "yarn": ("add", "install"), "bun": ("add", "install", "i"), "uv": ("add", "sync", "pip install", "tool install"),
    "cargo": ("install", "add"), "go": ("install", "get"), "gem": ("install",), "brew": ("install", "upgrade"),
    "apt": ("install",), "apt-get": ("install",), "poetry": ("add", "install"), "bundle": ("install", "add"),
}
DESTRUCTIVE = frozenset({"rm", "rmdir", "shred", "dd", "truncate", "kill", "killall", "pkill"})
# Programs that write the files their arguments name (files/write-*), by the short options
# brindle reads for each (one that takes a value takes the next word, itself counted as a
# name), and that fetch from the network.
FILE_WRITERS = {"mkdir": "pvm", "touch": "acfhmdrt", "cp": "RrfipvanlLPHxuTs", "mv": "fivnuT",
                "ln": "sfnvirFhLPT", "tee": "ai"}
SED_FLAGS = re.compile(r"-[nErsuz]+|--(quiet|silent|regexp-extended|separate|unbuffered|null-data|posix|debug)")
FETCHERS = frozenset({"curl", "wget", "http", "https", "xh"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def table(baseline: str, overrides: dict[str, str] | None = None) -> dict[str, dict]:
    """Every group at ``baseline``, each subcategory inherited unless ``overrides`` names it."""
    overrides = overrides or {}
    return {g: {"baseline": baseline, "subcategories": {s: overrides.get(f"{g}/{s}", INHERIT) for s in subs}}
            for g, subs in GROUPS.items()}


def preset_groups(name: str) -> dict[str, dict]:
    return table(permissions.preset_unmatched(name), PRESET_OVERRIDES.get(name))


def normalized(groups: object, baseline: str, preset: str | None = None) -> dict[str, dict]:
    """A stored table read leniently: what's missing or unreadable falls back
    to ``baseline`` (a group) or ``preset``'s seed (a subcategory; inherit
    for none), so a table stored before a subcategory existed gets its preset's."""
    out = table(baseline, PRESET_OVERRIDES.get(preset or ""))
    for g, entry in (groups.items() if isinstance(groups, dict) else ()):
        if g not in GROUPS or not isinstance(entry, dict):
            continue
        if entry.get("baseline") in BASELINES:
            out[g]["baseline"] = entry["baseline"]
        subs = entry.get("subcategories")
        for s, value in (subs.items() if isinstance(subs, dict) else ()):
            if s in GROUPS[g] and value in (INHERIT, *BASELINES):
                out[g]["subcategories"][s] = value
    return out


def merged(groups: dict[str, dict], changes: object) -> dict[str, dict]:
    """``groups`` with ``changes`` ({group: {"baseline"?, "subcategories"?}})
    applied; ValueError for a group, subcategory or value it doesn't know."""
    if not isinstance(changes, dict):
        raise ValueError("groups must be an object of group -> {baseline, subcategories}")
    out = {g: {"baseline": e["baseline"], "subcategories": dict(e["subcategories"])} for g, e in groups.items()}
    for g, entry in changes.items():
        if g not in GROUPS or not isinstance(entry, dict):
            raise ValueError(f"unknown group {g!r}: one of {', '.join(GROUPS)}")
        if "baseline" in entry:
            if entry["baseline"] not in BASELINES:
                raise ValueError(f"a baseline must be one of {', '.join(BASELINES)}")
            out[g]["baseline"] = entry["baseline"]
        subs = entry.get("subcategories") or {}
        if not isinstance(subs, dict):
            raise ValueError("subcategories must be an object of subcategory -> decision")
        for s, value in subs.items():
            if s not in GROUPS[g]:
                raise ValueError(f"unknown {g} subcategory {s!r}: one of {', '.join(GROUPS[g])}")
            if value not in (INHERIT, *BASELINES):
                raise ValueError(f"a subcategory must be one of {INHERIT}, {', '.join(BASELINES)}")
            if f"{g}/{s}" == UNVERIFIABLE and value == "allow":
                raise ValueError("a command brindle can't verify is never allowed: ask or deny")
            out[g]["subcategories"][s] = value
    return out


# -- classification ---------------------------------------------------------------------------


def _protected(push: permissions.Push, repo_root: str) -> bool:
    """A push that forces or deletes, or may land in the default or a release branch."""
    return push.force or push.delete or any(
        permissions.push_fits(f"*{permissions.PUSH_ARROW}{dest}", push, repo_root, loose=True)
        for dest in (permissions.DEFAULT_BRANCH, "release", "release/*"))


def _commits(words: list[str]) -> bool:
    """A git command in COMMITS (``stash`` only to save, ``restore`` only
    ``--staged``, ``tag`` to make one) with no option that discards or deletes."""
    args = words[1:]
    while args and args[0].startswith("-"):    # global options
        args = args[2:] if args[0] in ("-C", "-c") else args[1:]
    if not args or any(a in DISCARDING for a in args[1:]):
        return False
    sub, rest = args[0], args[1:]
    if sub == "stash":
        return rest[:1] in ([], ["push"], ["save"])
    if sub == "restore":
        return "--staged" in rest
    return sub in COMMITS


def _git(req: Request, words: list[str]) -> str:
    pushes = permissions.push_calls(shlex.join(words))
    if pushes:
        return "push-protected" if any(_protected(p, req.repo_root) for p in pushes) else "push-feature"
    if permissions._readonly_part(req, words):
        return "readonly"
    return "commit" if _commits(words) else "other"


def _installs(program: str, words: list[str]) -> bool:
    args = [w for w in words[1:] if not w.startswith("-")]
    return any(args[:len(verb.split())] == verb.split() for verb in INSTALLERS.get(program, ()))


def _written(req: Request, paths: list[str] | None, cwd: str | None) -> tuple[str, str]:
    """files/write-worktree when there are ``paths`` (None: unknown) and every
    one of them (from ``cwd``) is in the worktree."""
    inside = bool(paths) and all(
        not ("$" in p or any(c in p for c in "*?[{")) and (cwd or os.path.isabs(p))
        and permissions._in_worktree(replace(req, path=os.path.join(cwd or "", os.path.expanduser(p))))
        for p in paths)
    return ("files", "write-worktree" if inside else "write-outside")


def _write_targets(program: str, args: list[str]) -> list[str] | None:
    """Every name a FILE_WRITERS program or ``sed -i`` may write, None for an
    option brindle doesn't read (where a destination may hide: ``-t/etc``)."""
    if program == "sed":
        return _sed_targets(args)
    names, options = [], True
    for arg in args:
        if not options or not arg.startswith("-") or arg == "-":
            names.append(arg)
        elif arg == "--":
            options = False
        elif arg.startswith("--"):
            if "=" in arg:
                names.append(arg.partition("=")[2])    # --target-directory=DIR
        elif not set(arg[1:]) <= set(FILE_WRITERS[program]):
            return None
    return names


def _sed_targets(args: list[str]) -> list[str] | None:
    """The files ``sed -i`` edits: its operands, less the script unless -e
    or --expression gives it. None unless every script only edits them
    (_sed_edits_only), or for a script in a file (-f) brindle doesn't read."""
    names, scripts, i = [], [], 0
    while i < len(args):
        arg = args[i]
        if arg in ("-f", "--file") or arg.startswith("--file=") or re.fullmatch(r"-[nErsuz]*f.*", arg):
            return None
        if arg in ("-e", "--expression"):
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if arg.startswith("--expression="):
            scripts.append(arg.partition("=")[2])
        elif re.fullmatch(r"-[nErsuz]*e.+", arg):
            scripts.append(arg[arg.index("e") + 1:])
        elif arg == "-i" or arg.startswith("--in-place"):
            if i + 1 < len(args) and args[i + 1] == "":
                i += 1    # BSD sed's -i '': no backup suffix
        elif re.fullmatch(r"-[nErsuz]*i[^-]*", arg):
            pass    # -i.bak, -ni
        elif arg.startswith("-"):
            if not SED_FLAGS.fullmatch(arg):
                return None
        else:
            names.append(arg)
        i += 1
    if not scripts:
        if not names:
            return None
        scripts, names = names[:1], names[1:]
    return names if all(_sed_edits_only(script) for script in scripts) else None


# sed commands that take no argument and touch no file.
SED_PLAIN = frozenset("dDpPnNgGhHxz=lqQ")


def _sed_edits_only(script: str) -> bool:
    """Whether every command of the sed ``script`` only edits the text it
    reads: s and y (s with no w or e flag), a/i/c, and the commands in
    SED_PLAIN, each with an optional line or /regex/ address. Anything else
    (w, W, r, R, e, a block, a label or branch) brindle doesn't vouch for."""
    i, n = 0, len(script)

    def delimited(i: int, parts: int) -> int | None:
        """The index past ``parts`` texts closed by the delimiter at ``i``."""
        delimiter = script[i] if i < n else ""
        if not delimiter or delimiter in "\\\n":
            return None
        i += 1
        for _ in range(parts):
            while i < n and script[i] != delimiter:
                i += 2 if script[i] == "\\" else 1
            if i >= n:
                return None
            i += 1
        return i

    def address(i: int) -> int | None:
        if i < n and script[i] == "/":
            return delimited(i, 1)
        if i < n and script[i] == "\\":
            return delimited(i + 1, 1)
        found = re.match(r"\d+(~\d+)?|\$", script[i:])
        return i + found.end() if found else i

    while i < n:
        if script[i] in " \t\n;":
            i += 1
            continue
        for _ in range(2):    # an address, then perhaps ,address
            i = address(i)
            if i is None:
                return False
            if i < n and script[i] == ",":
                i += 1
            else:
                break
        while i < n and script[i] in " \t!":
            i += 1
        command = script[i] if i < n else ""
        if command == "s":
            i = delimited(i + 1, 2)
            if i is None:
                return False
            flags = re.match(r"[gpiIM0-9]*", script[i:])
            i += flags.end()
        elif command == "y":
            i = delimited(i + 1, 2)
            if i is None:
                return False
        elif command in ("a", "i", "c"):
            end = script.find("\n", i)
            i = n if end < 0 else end + 1    # its text, to the end of the line
        elif command and command in SED_PLAIN:
            i += 1
            if command in "qQ":
                i += re.match(r"\s*\d*", script[i:]).end()
        else:
            return False
        if i < n and script[i] not in " \t\n;":
            return False
    return True


def _shell_part(req: Request, words: list[str], rules: list[Rule], checks: list[str],
                unknown: bool = False, cwd: str | None = "") -> tuple[str, str]:
    """``words``' group and subcategory; ``unknown``: a word in it only the
    run knows, so it is never read-only and a push of it may be protected.
    ``cwd``: where the files it names are (None: unknown; "": ``req``'s)."""
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if not words:
        return ("shell", "other")
    program = os.path.basename(words[0])
    if program == "git":
        sub = _git(req, words)
        if unknown and sub in ("readonly", "push-feature"):
            return ("git", "other" if sub == "readonly" else "push-protected")
        return ("git", sub)
    command = shlex.join(words)
    if program in DESTRUCTIVE or permissions.destructive_rm(command):
        return ("shell", "destructive")
    if _installs(program, words):
        return ("shell", "install")
    if program == "cd" or not unknown and permissions.readonly_pipeline(replace(req, command=command), rules, checks):
        return ("shell", "readonly")
    if program in FILE_WRITERS or program == "sed" and any(w.startswith(("-i", "--in-place")) or
                                                           re.fullmatch(r"-[nErsuz]*i.*", w) for w in words[1:]):
        return _written(req, _write_targets(program, words[1:]), req.cwd or req.worktree if cwd == "" else cwd)
    if program in FETCHERS:
        return ("network", "fetch")
    if program in DEV_TOOLS or command in [c.strip() for c in checks]:
        return ("shell", "dev-tools")
    return ("shell", "other")


def part_classes(req: Request, part: permissions.ShellPart, rules: list[Rule],
                 checks: list[str]) -> list[tuple[str, str]]:
    """A ShellPart's group and subcategory, then each file it redirects
    to's (files/write-*), then each it reads from's (files/read). After a cd
    brindle can't follow, a relative name may be anywhere."""
    at = replace(req, cwd=part.cwd or req.cwd)
    return [_shell_part(at, part.words, rules, checks, part.unknown or part.tilde, part.cwd),
            *(_written(at, [path], part.cwd) for path in part.writes), *(("files", "read") for _ in part.reads)]


def classify(req: Request, rules: list[Rule], checks: list[str] | None = None,
             parts: list[list[str]] | None = None) -> list[tuple[str, str]]:
    """The (group, subcategory) of ``req``: one per simple command of a shell
    command (``parts``: just those), else one. A shell command is read part
    by part where it runs (permissions.shell_parts: each file a redirection
    writes or reads too); one brindle can't verify is shell/other and also
    each command the restriction parsers find in it, so none hides a
    restricted subcategory."""
    if req.kind == "read":
        return [("files", "read")]
    if req.kind in ("write", "edit"):
        return [("files", "write-worktree" if permissions._in_worktree(req) else "write-outside")]
    if req.kind == "fetch":
        return [("network", "search" if "search" in req.tool.lower() else "fetch")]
    if req.kind == "mcp":
        return [("mcp", "tool")]
    if req.kind != "bash":
        return [("mcp", "other")]
    command = (req.command or "").strip()
    unread = []
    if parts is None and command:
        read = permissions.shell_parts(req)
        if read is not None:
            return [c for part in read for c in part_classes(req, part, rules, checks or [])]
        parts = permissions.compound_parts(command)
        if parts is None:
            unread = [("shell", "other")]
            parts = (permissions._restriction_parts(command)
                     or permissions._restriction_parts(command, word_pattern=permissions.RESTRICTION_WORD)
                     or permissions._segments(command))
    if not parts:
        return [("shell", "other")]
    return [*unread, *(_shell_part(req, words, rules, checks or []) for words in parts)]


def answer(groups: dict[str, dict], group: str, sub: str) -> str:
    entry = groups[group]
    override = entry["subcategories"].get(sub, INHERIT)
    if f"{group}/{sub}" == UNVERIFIABLE:
        return "deny" if override == "deny" else "ask"
    return entry["baseline"] if override == INHERIT else override


def unverifiable(groups: dict[str, dict], unmatched: str) -> str:
    """What a command brindle can't verify gets where it would ask: the
    table's answer, or with none, deny where unmatched actions run."""
    if groups:
        return answer(groups, *UNVERIFIABLE.split("/"))
    return "deny" if unmatched == "allow" else "ask"


def baseline(req: Request, groups: dict[str, dict], rules: list[Rule], checks: list[str] | None = None,
             parts: list[list[str]] | None = None) -> tuple[str, str]:
    """What ``groups`` answers for ``req`` no rule covers, and which group
    and subcategory said so ("shell/install"): the strictest of its parts."""
    found = [(answer(groups, g, s), f"{g}/{s}") for g, s in classify(req, rules, checks, parts)]
    return max(found, key=lambda pair: STRICTNESS[pair[0]])


def parts_decision(req: Request, parts: list[permissions.ShellPart], found: permissions.Decision | None,
                   groups: dict[str, dict], unmatched: str, rules: list[Rule],
                   checks: list[str] | None = None) -> permissions.Decision:
    """The answer for a command read part by part (permissions.read_parts;
    ``found``: the deny or ask a rule gives a part): each part's rule, else
    its subcategory's answer (``unmatched`` with no table), and each file a
    redirection writes or reads, the same. Deny if any part denies, allow if every
    part allows, otherwise the strictest part's: an ask a rule gives first,
    so a saved allow can settle it."""
    Decision = permissions.Decision
    checks = checks or []

    def fallback(group: str, sub: str) -> Decision:
        if not groups:
            return Decision(unmatched, f"no rule covers its {group}/{sub} part, and unmatched actions {unmatched}")
        got = answer(groups, group, sub)
        return Decision(got, f"the {group}/{sub} baseline is {got}")

    answers = [found] if found is not None else []
    # Each file a redirection names is a write or read of its own: no rule that asks git what it tracks.
    untracked = [r for r in rules if r.match_type != "tracked"]
    for part in parts:
        at = replace(req, cwd=part.cwd or req.cwd)
        (group, sub), *files = part_classes(req, part, rules, checks)
        ruled = None if part.unknown or part.expanded else permissions._part_decision(at, part.words, checks, rules)
        answers.append(ruled if ruled is not None and ruled.rule is not None else fallback(group, sub))
        for (kind, path), (group, sub) in zip([*(("write", p) for p in part.writes),
                                               *(("read", p) for p in part.reads)], files):
            file = replace(at, kind=kind, command=None, path=path)
            ruled = permissions._rule_decision(file, untracked, checks) if os.path.isabs(path) else None
            answers.append(ruled if ruled is not None else fallback(group, sub))
    denied = next((a for a in answers if a.decision == "deny"), None)
    if denied is not None:
        return denied
    if all(a.decision == "allow" for a in answers):
        return Decision("allow", "every part of the command is allowed (" + "; ".join(
            dict.fromkeys(a.reason for a in answers)) + ")")
    asks = [a for a in answers if a.decision == "ask"]
    return next((a for a in asks if a.rule is not None), asks[0])
