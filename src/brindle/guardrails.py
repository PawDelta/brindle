"""Guardrails (brindle Pro, feature ``guardrails``): what a profile's workers
may change, read, and find in their environment.

    ---
    name: api-dev
    extends: developer
    write_scope: [src/api/**, tests/api/**]
    read_scope: [src/**, tests/**, docs/**]
    env_allow: [NPM_TOKEN, AWS_*]
    ---

Globs are relative to the worker's worktree (an absolute glob matches an
absolute path, for an ``add_dirs`` directory). ``*`` crosses ``/``, a
trailing ``/`` or a plain directory name means everything under it, and
matching is case-sensitive.

``write_scope`` is enforced three ways:

1. **Hard, on the diff.** Before a branch is reviewed or merged,
   ``brindle.rule_checks`` lists every file the branch changes (added,
   modified, deleted or renamed, mode changes included) and fails the gate
   on any outside the scope, the way a failed check does.
2. **Best effort, on the tools.** Claude Code's PreToolUse hook denies an
   Edit, Write or NotebookEdit outside the scope; the native runner's file
   tools refuse one. A shell command can still write anywhere on every
   runner, and Codex and agy get no tool rule: the diff check is what holds.
3. **In the prompt**: the worker is told its scope (``scope_prompt``).

``read_scope`` is best effort only, and only for file tools: the native
runner's Read, Glob and Grep refuse or leave out files outside it, Claude
Code's hook denies a Read outside it and a Glob or Grep of a directory not
wholly in scope (``dir_covered``), or with a pattern that climbs out. A shell command can read anything, other providers get
only the prompt, and reading leaves no diff to check afterwards.

``env_allow`` lists the only environment variables (names or globs) the
worker's process starts with, besides brindle's own (``BRINDLE_*``, less its
secrets), what a process needs to run at all (``BASE_ENV``), the profile's
own ``env.NAME`` lines and the provider's sign-in
(``secrets.provider_credentials``). brindle's secrets are scrubbed whatever
it lists (see ``brindle.secrets``).

Without the feature (no Pro login, a lapsed plan: ``license.has`` fails
closed) these keys are ignored, with one warning per profile: a profile
written for a Pro seat still runs, unconfined, everywhere else.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import replace

log = logging.getLogger(__name__)

FEATURE = "guardrails"
KEYS = ("write_scope", "read_scope", "env_allow")

_warned: set[str] = set()


def entitled() -> bool:
    try:
        from brindle.pro import license

        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 -- an unreadable license is "not entitled"
        return False


def effective(profile):
    """``profile`` with its guardrail keys as they apply: as written with the
    feature, cleared without it (warning once per profile name)."""
    keys = [k for k in KEYS if getattr(profile, k, None) is not None]
    if not keys or entitled():
        return profile
    if profile.name not in _warned:
        _warned.add(profile.name)
        log.warning("brindle: profile %r sets %s, a brindle Pro feature (guardrails) this machine "
                    "isn't entitled to; ignoring %s", profile.name, ", ".join(keys),
                    "it" if len(keys) == 1 else "them")
    return replace(profile, write_scope=None, read_scope=None, env_allow=None)


# -- paths ------------------------------------------------------------------------------


def _norm(glob: str) -> str:
    g = glob.strip()
    if g.startswith("./"):
        g = g[2:]
    if g.endswith("/"):
        g += "**"
    return g


def in_scope(path: str, globs: list[str]) -> bool:
    """Whether ``path`` (relative to the worktree, ``/``-separated) matches
    one of ``globs``: as a glob, or as a directory it lies under."""
    path = path.replace(os.sep, "/")
    if path.startswith("./"):
        path = path[2:]
    for g in map(_norm, globs):
        # A relative glob is about the worktree: ``*`` crosses ``/``, so
        # ``*.md`` would otherwise match /etc/x.md. Only an absolute glob
        # matches a path outside it (see ``relative``).
        if not g or g.startswith("/") != path.startswith("/"):
            continue
        if fnmatch.fnmatchcase(path, g) or path.startswith(g.rstrip("/") + "/"):
            return True
        # ``src/**/*.py`` means any depth, including none; fnmatch's ``*``
        # already crosses ``/``, so collapse ``**/`` for the zero-depth case.
        if "**/" in g and fnmatch.fnmatchcase(path, g.replace("**/", "")):
            return True
    return False


def _literal_prefix(glob: str) -> str:
    cut = [i for i in (glob.find(c) for c in "*?[") if i >= 0]
    return glob[:min(cut)] if cut else glob


def dir_covered(path: str, globs: list[str]) -> bool:
    """Whether everything under the directory ``path`` (relative; ``""`` is
    the worktree) is in scope. Claude Code's Glob and Grep search a whole
    directory, so allowing one that merely holds something in scope would
    let the search read the rest of it too."""
    path = path.replace(os.sep, "/")
    absolute = path.startswith("/")
    path = "/" + path.strip("/") if absolute else path.strip("/")
    if path == ".":
        path = ""
    d = path.rstrip("/") + "/" if path else ""
    for g in map(_norm, globs):
        # As in in_scope: relative globs for the worktree, absolute ones outside it.
        if not g or g.startswith("/") != absolute:
            continue
        prefix = _literal_prefix(g)
        rest = g[len(prefix):]
        # ``src/**``, ``src/*`` (``*`` crosses ``/``) or a plain directory.
        if rest in ("*", "**") and d.startswith(prefix) and (prefix == "" or prefix.endswith("/")):
            return True
        if not rest and (d.startswith(g.rstrip("/") + "/") or path == g):
            return True
    return False


def relative(path: str, root: str) -> str:
    """``path`` relative to the worktree ``root`` when it lies inside it,
    else the absolute path (which only an absolute glob matches)."""
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    base = os.path.realpath(root)
    if full == base:
        return ""
    if full.startswith(base + os.sep):
        return os.path.relpath(full, base).replace(os.sep, "/")
    return full


# -- the prompt -------------------------------------------------------------------------


def _globs(globs: list[str]) -> str:
    return ", ".join(f"`{g}`" for g in globs)


def scope_prompt(profile) -> str:
    """What the worker is told about its scope (empty without one)."""
    parts = []
    if profile.write_scope:
        parts.append(
            f"Write scope (your profile's guardrails): change only files matching {_globs(profile.write_scope)}. "
            "brindle checks every file the branch changes before it is reviewed or merged, deletions and "
            "renames included, and a file outside the scope fails the check like a failed test. If the task "
            "needs a change outside it, say so in your result instead of making it.")
    if profile.read_scope:
        parts.append(f"Read scope: read only files matching {_globs(profile.read_scope)}; "
                     "file reads outside it are refused.")
    return "\n\n".join(parts)


# -- Claude Code's PreToolUse hook --------------------------------------------------------

WRITE_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")
READ_TOOLS = ("Read", "Glob", "Grep")


def hook_matcher(profile) -> str:
    """The extra tools Claude Code's PreToolUse hook must see for ``profile``."""
    tools = []
    if getattr(profile, "write_scope", None):
        tools += WRITE_TOOLS
    if getattr(profile, "read_scope", None):
        tools += READ_TOOLS
    return "|".join(tools)


PATH_KEYS = ("file_path", "notebook_path", "path")


def _paths(tool_input: dict) -> list[str]:
    """Every path the tool input names. All of them are checked, not the
    first one found: which key a tool actually acts on is the CLI's to
    decide, and checking one while it uses another would let an in-scope
    decoy through."""
    return [str(v) for k in PATH_KEYS if (v := tool_input.get(k)) not in (None, "")]


def tool_denial(profile, tool: str, tool_input: dict, root: str) -> str | None:
    """Why ``tool`` with ``tool_input`` is outside ``profile``'s scope, or
    None when it isn't (or the profile has none)."""
    if tool in WRITE_TOOLS and profile.write_scope:
        for raw in _paths(tool_input):
            rel = relative(raw, root)
            if not in_scope(rel, profile.write_scope):
                return (f"brindle: {rel or raw} is outside your write scope ({_globs(profile.write_scope)}). "
                        "Change only files in scope; if the task needs this one, say so in your result.")
    if tool in READ_TOOLS and profile.read_scope:
        if tool == "Read":
            checks = [(relative(raw, root), in_scope) for raw in _paths(tool_input)]
        else:
            # The search's pattern or file filter could climb out of the
            # directory checked below (``../..``, an absolute or ``~`` path).
            for key in ("pattern", "glob") if tool == "Glob" else ("glob",):
                pat = str(tool_input.get(key) or "")
                if pat.startswith(("/", "~")) or ".." in pat.replace("\\", "/").split("/"):
                    return (f"brindle: the {key} {pat!r} reaches outside the directory searched; "
                            f"search inside your read scope ({_globs(profile.read_scope)}) with a "
                            "relative pattern.")
            # A search with no path searches the worktree. A directory must
            # be wholly in scope: the search reads all of it.
            checks = []
            for raw in _paths(tool_input) or [root]:
                full = raw if os.path.isabs(raw) else os.path.join(root, raw)
                checks.append((relative(raw, root), in_scope if os.path.isfile(full) else dir_covered))
        for rel, ok in checks:
            if not ok(rel, profile.read_scope):
                return (f"brindle: {rel or 'the worktree root'} is outside your read scope "
                        f"({_globs(profile.read_scope)}).")
    return None


__all__ = ["FEATURE", "KEYS", "entitled", "effective", "in_scope", "dir_covered", "relative",
           "scope_prompt", "hook_matcher", "tool_denial"]
