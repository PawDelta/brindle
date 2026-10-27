"""Capability groups: a blanket answer for each kind of thing an agent does.

Every request falls in a group (files, shell, git, network, mcp) and one of
its subcategories (shell: readonly, dev-tools, install, destructive, other;
...). A group has a baseline (allow, ask or deny) that a subcategory may
override; "inherit" leaves the baseline in force. The baseline answers only
what no rule or saved action settles (brindle.permissions.decide): a deny
first, then an explicit rule, then the subcategory, then the group's
baseline. A shell command is classified part by part, and the strictest
part's answer is the whole command's.

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
    "shell": ("readonly", "dev-tools", "install", "destructive", "other"),
    "git": ("readonly", "commit", "push-feature", "push-protected", "other"),
    "network": ("fetch", "search"),
    "mcp": ("tool", "other"),
}
BASELINES = ("allow", "ask", "deny")
INHERIT = "inherit"
STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}

# preset -> {"group/subcategory": decision} over a baseline of the preset's unmatched setting.
PRESET_OVERRIDES: dict[str, dict[str, str]] = {
    "autonomous": {"git/push-protected": "ask"},
    "permissive": {"files/write-worktree": "allow", "shell/readonly": "allow", "shell/dev-tools": "allow",
                   "git/readonly": "allow", "git/commit": "allow"},
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
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def table(baseline: str, overrides: dict[str, str] | None = None) -> dict[str, dict]:
    """Every group at ``baseline``, each subcategory inherited unless ``overrides`` names it."""
    overrides = overrides or {}
    return {g: {"baseline": baseline, "subcategories": {s: overrides.get(f"{g}/{s}", INHERIT) for s in subs}}
            for g, subs in GROUPS.items()}


def preset_groups(name: str) -> dict[str, dict]:
    return table(permissions.preset_unmatched(name), PRESET_OVERRIDES.get(name))


def normalized(groups: object, baseline: str) -> dict[str, dict]:
    """A stored table read leniently: what's missing or unreadable falls back
    to ``baseline`` (a group) or inherit (a subcategory)."""
    out = table(baseline)
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


def _shell_part(req: Request, words: list[str], rules: list[Rule], checks: list[str]) -> tuple[str, str]:
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if not words:
        return ("shell", "other")
    program = os.path.basename(words[0])
    if program == "git":
        return ("git", _git(req, words))
    command = shlex.join(words)
    if program in DESTRUCTIVE or permissions.destructive_rm(command):
        return ("shell", "destructive")
    if _installs(program, words):
        return ("shell", "install")
    if program == "cd" or permissions.readonly_pipeline(replace(req, command=command), rules, checks):
        return ("shell", "readonly")
    if program in DEV_TOOLS or command in [c.strip() for c in checks]:
        return ("shell", "dev-tools")
    return ("shell", "other")


def classify(req: Request, rules: list[Rule], checks: list[str] | None = None,
             parts: list[list[str]] | None = None) -> list[tuple[str, str]]:
    """The (group, subcategory) of ``req``: one per simple command of a shell
    command (``parts``: just those), else one. A command compound_parts
    can't read (a redirection, a substitution) is shell/other and also each
    command the restriction parsers find in it, so none hides a restricted
    subcategory."""
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
    return entry["baseline"] if override == INHERIT else override


def baseline(req: Request, groups: dict[str, dict], rules: list[Rule], checks: list[str] | None = None,
             parts: list[list[str]] | None = None) -> tuple[str, str]:
    """What ``groups`` answers for ``req`` no rule covers, and which group
    and subcategory said so ("shell/install"): the strictest of its parts."""
    found = [(answer(groups, g, s), f"{g}/{s}") for g, s in classify(req, rules, checks, parts)]
    return max(found, key=lambda pair: STRICTNESS[pair[0]])
