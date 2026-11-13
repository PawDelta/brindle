"""Three-way text edits shared by reconnecting writers and live supervisors.

Offsets are Python string offsets, never substring searches or CRDT positions.
Unresolved edits leave the remote text in place. Compare boxes contain whole
paragraphs, including any independent edits already applied in those paragraphs.
Results are immutable; resolve against the latest returned result so that later
collision and suggestion offsets follow earlier resolutions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import re
from typing import Literal


@dataclass(frozen=True)
class Hunk:
    """Replace the half-open base range with text; start == end inserts."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Collision:
    id: int
    start: int
    end: int
    base: str
    mine: str
    theirs: str


@dataclass(frozen=True)
class Suggestion:
    start: int
    end: int
    before: str
    after: str


@dataclass(frozen=True)
class RebaseResult:
    text: str
    collisions: tuple[Collision, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()


def _hunks(base: str, text: str) -> list[Hunk]:
    # Pin equal edges first: repeated paragraphs must not steal an unchanged
    # prefix/suffix merely because a longer match exists elsewhere in the doc.
    start = 0
    limit = min(len(base), len(text))
    while start < limit and base[start] == text[start]:
        start += 1
    end, other_end = len(base), len(text)
    while end > start and other_end > start and base[end - 1] == text[other_end - 1]:
        end -= 1
        other_end -= 1
    matcher = SequenceMatcher(None, base[start:end], text[start:other_end], autojunk=False)
    return [Hunk(start + a, start + b, text[start + c:start + d])
            for tag, a, b, c, d in matcher.get_opcodes() if tag != "equal"]


def _touches(a: Hunk, b: Hunk) -> bool:
    if a.start == a.end and b.start == b.end:
        return False
    if a.start == a.end or b.start == b.end:
        return a.start <= b.end and b.start <= a.end
    return a.start < b.end and b.start < a.end


def _render(base: str, start: int, end: int, edits: list[Hunk]) -> str:
    parts = []
    cursor = start
    for edit in sorted(edits, key=lambda h: (h.start, h.end)):
        parts.extend((base[cursor:edit.start], edit.text))
        cursor = edit.end
    parts.append(base[cursor:end])
    return "".join(parts)


def rebase(base: str, local: str, remote: str) -> RebaseResult:
    """Apply independent local hunks to remote, holding overlaps for a choice.

    Changes are compared in base coordinates. Concurrent insertions at the same
    point keep remote first. An insertion on a replacement/deletion edge needs a choice.
    Identical changes on both sides are already applied. Multiple collisions in
    one paragraph share a compare box; nonoverlapping hunks still apply now.
    """
    theirs = _hunks(base, remote)
    mine = [h for h in _hunks(base, local) if h not in theirs]
    held = [h for h in mine if any(_touches(h, t) for t in theirs)]
    safe = [h for h in mine if h not in held]
    edits = sorted(theirs + safe, key=lambda h: (h.start, h.end))
    boundaries = [0, *(m.end() for m in re.finditer(r"\r?\n[ \t]*\r?\n", base)), len(base)]

    def paragraph(start: int, end: int) -> tuple[int, int]:
        left = max(p for p in boundaries if p <= start)
        right = min((p for p in boundaries if p >= end and p > start), default=len(base))
        return left, right

    spans = []
    for h in held:
        related = [h, *(t for t in theirs if _touches(h, t))]
        left, right = paragraph(min(e.start for e in related), max(e.end for e in related))
        # A hunk may itself span paragraphs. Never split it at a compare edge.
        while True:
            crossing = [e for e in edits + held if e.start < right and e.end > left]
            expanded = paragraph(min([left] + [e.start for e in crossing]),
                                 max([right] + [e.end for e in crossing]))
            if expanded == (left, right):
                break
            left, right = expanded
        spans.append((left, right))
    groups: list[tuple[int, int]] = []
    for left, right in sorted(spans):
        if groups and left < groups[-1][1]:
            groups[-1] = (groups[-1][0], max(right, groups[-1][1]))
        elif (left, right) not in groups:
            groups.append((left, right))

    collisions = []
    for ident, (left, right) in enumerate(groups):
        def inside(h: Hunk) -> bool:
            return left <= h.start and h.end <= right and (h.start < right or right == len(base))

        local_held = [h for h in held if inside(h)]
        current = [h for h in edits if inside(h)]
        chosen = [h for h in current
                  if h in safe or not any(_touches(h, m) for m in local_held)] + local_held
        before = _render(base, left, right, current)
        offset = left + sum(len(h.text) - (h.end - h.start) for h in edits
                            if h.end <= left and not inside(h))
        collisions.append(Collision(ident, offset, offset + len(before), base[left:right],
                                    _render(base, left, right, chosen), before))
    return RebaseResult(_render(base, 0, len(base), edits), tuple(collisions))


def apply_resolution(
    result: RebaseResult,
    collision_id: int,
    choice: Literal["theirs", "mine", "merged", "suggestion"],
    text: str | None = None,
) -> RebaseResult:
    """Resolve a compare box, returning updated text and remaining positions.

    ``merged`` requires the entire replacement paragraph(s). ``suggestion``
    keeps the current text and returns a positional suggestion for the caller
    to persist. Unknown/already resolved IDs and invalid choices are refused.
    """
    collision = next((c for c in result.collisions if c.id == collision_id), None)
    if collision is None:
        raise ValueError("Unknown collision")
    if choice not in ("theirs", "mine", "merged", "suggestion"):
        raise ValueError("Unknown resolution")
    if choice == "merged" and text is None:
        raise ValueError("Merged resolution requires text")
    replacement = (text if choice == "merged" else collision.mine if choice == "mine"
                   else collision.theirs)
    assert replacement is not None
    delta = len(replacement) - (collision.end - collision.start)

    def move(item):
        if item.start >= collision.end:
            return replace(item, start=item.start + delta, end=item.end + delta)
        return item

    suggestions = tuple(move(s) for s in result.suggestions)
    if choice == "suggestion":
        suggestions += (Suggestion(collision.start, collision.end, collision.theirs, collision.mine),)
    return RebaseResult(
        result.text[:collision.start] + replacement + result.text[collision.end:],
        tuple(move(c) for c in result.collisions if c.id != collision_id),
        suggestions,
    )
