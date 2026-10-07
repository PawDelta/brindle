"""Conflict-aware merging: what branches actually changed, and in what order
to merge them.

Three questions, answered from git alone (no model call):

- **Predict.** Which running branches have touched the same files? Declared
  ``files`` on a task are a guess; the branch's actual diff is the truth, and
  two branches editing one file are the branches that will conflict.
- **Order.** Of several branches ready to merge, which first? The one whose
  hunks overlap the others' least goes first: it merges cleanly, and the
  rest are synced onto the new tip one at a time, each sync surfacing the
  conflicts of only that branch.
- **Protect.** ``protected_paths`` in the repo config name files whose
  conflicts brindle never asks a worker to resolve (migrations, lockfiles,
  generated schemas): those always go to the person.
"""

from __future__ import annotations

import fnmatch
import os
import re

from brindle import git
from brindle.db import Workspace

# A hunk this close to another (in base-file lines) is taken as overlapping:
# git needs a line or two of untouched context between two changes to merge
# them cleanly.
ADJACENCY = 1

Hunks = dict[str, list[tuple[int, int]]]


def _base_ref(ws: Workspace) -> str | None:
    if not ws.base_branch:
        return None
    try:
        return git.merge_base(ws.path, git.base_ref(ws.path, ws.base_branch))
    except git.GitError:
        return None


def changed_files(ws: Workspace) -> list[str]:
    """Files ``ws``'s branch has touched relative to its base: committed,
    uncommitted and untracked. Cheap (a couple of git calls), and [] when
    the worktree or its base can't be read."""
    mb = _base_ref(ws)
    if mb is None:
        return []
    try:
        names = git.paths(["diff", "--name-only", mb], ws.path)
        untracked = git.paths(["ls-files", "--others", "--exclude-standard"], ws.path)
    except git.GitError:
        return []
    seen: dict[str, None] = {}
    for f in (*names, *untracked):
        if f:
            seen[os.path.normpath(f)] = None
    return list(seen)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OCTAL = re.compile(r"\\([0-7]{3})")
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "a": "\a", "b": "\b", "f": "\f",
            "r": "\r", "v": "\v"}


def _unquote(name: str) -> str:
    """A path as git prints it in a diff header, with its C-style quoting
    undone (``"caf\\303\\251 x.py"`` -> ``café x.py``), so it matches the
    same path listed elsewhere."""
    if len(name) < 2 or not (name.startswith('"') and name.endswith('"')):
        return name
    inner = name[1:-1]
    raw = bytearray()
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch != "\\" or i + 1 >= len(inner):
            raw += ch.encode("utf-8")
            i += 1
            continue
        m = _OCTAL.match(inner, i)
        if m:
            raw.append(int(m.group(1), 8))
            i = m.end()
            continue
        raw += _ESCAPES.get(inner[i + 1], inner[i + 1]).encode("utf-8")
        i += 2
    return raw.decode("utf-8", errors="surrogateescape")


def _header_path(name: str, prefix: str) -> str:
    return os.path.normpath(_unquote(name).removeprefix(prefix))


def parse_hunks(patch: str) -> Hunks:
    """The hunks of a unified diff, per file, as (first, last) line ranges of
    the *old* side: the lines of the base the hunk replaces (a pure
    insertion covers the line it's inserted after). Two branches' hunks are
    comparable on that side, since both diff against the same base."""
    hunks: Hunks = {}
    current: str | None = None
    # The ``---``/``+++`` file headers sit between a file's ``diff --git``
    # line and its first hunk. Inside a hunk, a removed line that itself
    # starts with ``-- `` (or an added one with ``++ ``) prints the same way,
    # and must not be taken for a header.
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current, in_hunk = None, False
            continue
        if not in_hunk and line.startswith("--- "):
            name = line[4:].split("\t", 1)[0].strip()
            if name != "/dev/null":   # a deleted file keeps its old name
                current = _header_path(name, "a/")
            continue
        if not in_hunk and line.startswith("+++ "):
            name = line[4:].split("\t", 1)[0].strip()
            if name != "/dev/null":
                current = _header_path(name, "b/")
            continue
        m = _HUNK.match(line)
        if m and current is not None:
            in_hunk = True
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            end = start + count - 1 if count else start
            hunks.setdefault(current, []).append((start, end))
    return hunks


def hunks(ws: Workspace) -> Hunks:
    """``ws``'s committed hunks against its base (see ``parse_hunks``), or {}
    when they can't be read."""
    mb = _base_ref(ws)
    if mb is None:
        return {}
    try:
        return parse_hunks(git.out(["diff", "-U0", "--no-color", mb, "HEAD"], ws.path))
    except git.GitError:
        return {}


def overlapping_hunks(a: Hunks, b: Hunks) -> int:
    """How many hunks of ``a`` overlap (or nearly touch) a hunk of ``b`` in the
    same file. Order-independent in which pairs count; the number is a
    conflict-risk score, not an exact conflict count."""
    n = 0
    for path, ranges in a.items():
        others = b.get(path)
        if not others:
            continue
        for s1, e1 in ranges:
            for s2, e2 in others:
                if s1 <= e2 + ADJACENCY and s2 <= e1 + ADJACENCY:
                    n += 1
                    break
    return n


def shared_files(a: list[str], b: list[str]) -> list[str]:
    """Files in both lists, in ``a``'s order."""
    theirs = set(b)
    return [f for f in a if f in theirs]


def merge_order(candidates: list[tuple[Workspace, Hunks]]) -> list[Workspace]:
    """The order to merge ``candidates`` in: repeatedly the branch whose hunks
    overlap the remaining others' least (ties: the order given, usually
    oldest first). A branch that overlaps nothing merges cleanly whatever
    the order; one that overlaps everything goes last, where only it has
    conflicts to resolve, against a tip that already holds the rest."""
    remaining = list(candidates)
    ordered: list[Workspace] = []
    while remaining:
        best_i, best_score = 0, None
        for i, (ws, mine) in enumerate(remaining):
            score = sum(overlapping_hunks(mine, theirs) + overlapping_hunks(theirs, mine)
                        for j, (_w, theirs) in enumerate(remaining) if j != i)
            if best_score is None or score < best_score:
                best_i, best_score = i, score
        ordered.append(remaining.pop(best_i)[0])
    return ordered


def is_protected(path: str, patterns: list[str]) -> bool:
    """Whether ``path`` matches a ``protected_paths`` entry: an exact path, a
    glob (``*.lock``, ``migrations/**``), or a directory prefix
    (``migrations/``, ``migrations``)."""
    path = os.path.normpath(path)
    for raw in patterns:
        pat = os.path.normpath(raw.strip()) if raw.strip() else ""
        if not pat:
            continue
        if path == pat or fnmatch.fnmatch(path, pat) or path.startswith(pat.rstrip("/") + "/"):
            return True
        if "**" in pat:
            from pathlib import PurePath

            try:
                if PurePath(path).match(pat):
                    return True
            except ValueError:
                pass
    return False


def protected(files: list[str], patterns: list[str]) -> list[str]:
    return [f for f in files if is_protected(f, patterns)]
