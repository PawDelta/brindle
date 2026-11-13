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


def _split(base: str, text: str) -> tuple[int, int]:
    """Bisect a shortest insertion/deletion path using Myers' linear-space diff.

    Longest matching blocks can move repeated paragraphs to save one large
    match. Minimizing edits instead keeps small replacements at their positions.
    Frontiers walk from both ends until they meet on a shortest path.
    """
    n, m = len(base), len(text)
    distance = (n + m + 1) // 2
    offset = distance + 1
    forward = [-1] * (2 * distance + 3)
    backward = forward.copy()
    forward[offset + 1] = backward[offset + 1] = 0
    delta = n - m
    for d in range(distance + 1):
        for reverse, frontier, other in ((False, forward, backward), (True, backward, forward)):
            for k in range(-d, d + 1, 2):
                index = offset + k
                if k == -d or (k != d and frontier[index - 1] < frontier[index + 1]):
                    x = frontier[index + 1]
                else:
                    x = frontier[index - 1] + 1
                y = x - k
                if x < 0 or y < 0 or x > n or y > m:
                    continue
                while x < n and y < m:
                    a = base[n - x - 1] if reverse else base[x]
                    b = text[m - y - 1] if reverse else text[y]
                    if a != b:
                        break
                    x += 1
                    y += 1
                frontier[index] = x
                other_k = delta - k
                reach = d if reverse else d - 1
                if bool(delta % 2) != reverse and -reach <= other_k <= reach:
                    other_x = other[offset + other_k]
                    if other_x >= 0 and x + other_x >= n:
                        return (n - x, m - y) if reverse else (x, y)
    return n, m


def _hunks(base: str, text: str) -> list[Hunk]:
    # Trim equal edges at every split, keeping offsets in the original base.
    start = 0
    limit = min(len(base), len(text))
    while start < limit and base[start] == text[start]:
        start += 1
    end, other_end = len(base), len(text)
    while end > start and other_end > start and base[end - 1] == text[other_end - 1]:
        end -= 1
        other_end -= 1
    before, after = base[start:end], text[start:other_end]
    if not before and not after:
        return []
    if not before or not after or set(before).isdisjoint(after):
        return [Hunk(start, end, after)]
    a, b = _split(before, after)
    if (a, b) in ((0, 0), (len(before), len(after))):
        return [Hunk(start, end, after)]
    edits = [Hunk(h.start + start, h.end + start, h.text)
             for h in _hunks(before[:a], after[:b])]
    edits += [Hunk(h.start + start + a, h.end + start + a, h.text)
              for h in _hunks(before[a:], after[b:])]
    merged: list[Hunk] = []
    for h in edits:
        if merged and merged[-1].end == h.start:
            previous = merged.pop()
            h = Hunk(previous.start, h.end, previous.text + h.text)
        merged.append(h)
    return merged


def _touches(a: Hunk, b: Hunk) -> bool:
    if a.start == a.end and b.start == b.end:
        return False
    if a.start == a.end or b.start == b.end:
        return a.start <= b.end and b.start <= a.end
    return a.start < b.end and b.start < a.end


def _changes(base: str, text: str) -> list[Hunk]:
    """Align paragraphs before diffing their characters.

    Similar replacements cost less than removing and reinserting a paragraph.
    Exact duplicates are therefore paired in order around small edits instead
    of moving a long matching block. Paragraph boundaries also prevent a
    deletion from matching stray letters in an unrelated appended paragraph.
    """
    pattern = r".+?(?:\r?\n[ \t]*\r?\n|\Z)"
    before = re.findall(pattern, base, re.DOTALL)
    after = re.findall(pattern, text, re.DOTALL)
    previous = list(range(len(after) + 1))
    directions = []
    for i, a in enumerate(before, 1):
        current = [i]
        row = bytearray(len(after))
        for j, b in enumerate(after, 1):
            cost = 2 * (1 - SequenceMatcher(None, a, b, autojunk=False).ratio())
            options = (previous[j - 1] + cost, previous[j] + 1, current[j - 1] + 1)
            direction = min(range(3), key=options.__getitem__)
            current.append(options[direction])
            row[j - 1] = direction
        directions.append(row)
        previous = current
    pairs = []
    i, j = len(before), len(after)
    while i or j:
        direction = directions[i - 1][j - 1] if i and j else 1 if i else 2
        if direction == 0:
            pairs.append((before[i - 1], after[j - 1]))
            i -= 1
            j -= 1
        elif direction == 1:
            pairs.append((before[i - 1], ""))
            i -= 1
        else:
            pairs.append(("", after[j - 1]))
            j -= 1
    edits = []
    offset = 0
    for a, b in reversed(pairs):
        edits.extend(Hunk(h.start + offset, h.end + offset, h.text) for h in _hunks(a, b))
        offset += len(a)
    return edits


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
    theirs = _changes(base, remote)
    mine = [h for h in _changes(base, local) if h not in theirs]
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
        # An endpoint insertion belongs to the paragraph starting there, even
        # when the deletion it touches ends in the preceding paragraph.
        if any(e.start == e.end == right for e in related) and right < len(base):
            right = paragraph(right, right)[1]
        # A hunk may itself span paragraphs. Never split it at a compare edge.
        while True:
            crossing = [e for e in edits + held if e.start < right and e.end > left]
            if right < len(base) and any(e.start == e.end == right for e in held):
                right = paragraph(right, right)[1]
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
