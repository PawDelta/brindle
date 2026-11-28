"""Bounded diffs with offsets in Python string coordinates."""

from collections.abc import Callable, Sequence
from difflib import SequenceMatcher
from itertools import accumulate, groupby
import re


Opcode = tuple[str, int, int, int, int]
_MAX_CELLS = 250_000
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


def fits(left: int, right: int) -> bool:
    """Whether a full comparison has a small enough search space."""
    return left * right <= _MAX_CELLS


def common_edges(before: Sequence[str], after: Sequence[str]) -> tuple[int, int]:
    """Lengths of the common prefix and suffix, without counting any item twice."""
    limit = min(len(before), len(after))
    start = end = 0
    while start < limit and before[start] == after[start]:
        start += 1
    while end < limit - start and before[-end - 1] == after[-end - 1]:
        end += 1
    return start, end


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _edit(a: int, b: int, x: int, y: int) -> Opcode:
    return ("insert" if a == b else "delete" if x == y else "replace", a, b, x, y)


def _with_edges(before: Sequence[str], after: Sequence[str], start: int, suffix: int,
                middle: list[Opcode]) -> list[Opcode]:
    ops = [("equal", 0, start, 0, start)] if start else []
    ops.extend((tag, a + start, b + start, x + start, y + start) for tag, a, b, x, y in middle)
    if suffix:
        a, b = len(before), len(after)
        ops.append(("equal", a - suffix, a, b - suffix, b))
    return ops


def positional_opcodes(before: Sequence[str], after: Sequence[str]) -> list[Opcode]:
    """Use equal units at the same position as anchors in equal-length sequences."""
    at = 0
    ops = []
    for equal, run in groupby(zip(before, after, strict=True), key=lambda pair: pair[0] == pair[1]):
        end = at + sum(1 for _ in run)
        ops.append(("equal" if equal else "replace", at, end, at, end))
        at = end
    return ops


def sequence_opcodes(before: Sequence[str], after: Sequence[str], *, autojunk: bool = False) -> list[Opcode]:
    """Diff lines, paragraphs, tokens or characters with a bounded matcher."""
    if before == after:
        return [("equal", 0, len(before), 0, len(after))] if before else []
    if not before or not after:
        return [_edit(0, len(before), 0, len(after))]
    if fits(len(before), len(after)):
        return SequenceMatcher(None, before, after, autojunk=autojunk).get_opcodes()
    start, suffix = common_edges(before, after)
    left, right = before[start:len(before) - suffix], after[start:len(after) - suffix]
    if fits(len(left), len(right)):
        middle = SequenceMatcher(None, left, right, autojunk=autojunk).get_opcodes()
    elif not isinstance(left, str) and len(left) == len(right):
        middle = positional_opcodes(left, right)
    else:
        middle = [_edit(0, len(left), 0, len(right))]
    return _with_edges(before, after, start, suffix, middle)


def opcodes(before: str, after: str, *, autojunk: bool = False) -> list[Opcode]:
    """Diff at character offsets, using lines then words for large regions.

    Small inputs keep SequenceMatcher's original alignment, including ties
    between repeated characters. Large inputs pin equal edges first. A region
    that still exceeds the character budget after refinement stays one edit.
    """
    return _refine(before, after, autojunk, (_lines, tokens))


def _refine(before: str, after: str, autojunk: bool,
            splitters: tuple[Callable[[str], list[str]], ...]) -> list[Opcode]:
    if before == after or fits(len(before), len(after)) or not splitters:
        return sequence_opcodes(before, after, autojunk=autojunk)
    start, suffix = common_edges(before, after)
    left, right = before[start:len(before) - suffix], after[start:len(after) - suffix]
    if fits(len(left), len(right)):
        return _with_edges(before, after, start, suffix, sequence_opcodes(left, right, autojunk=autojunk))

    split, remaining = splitters[0], splitters[1:]
    a_units, b_units = split(left), split(right)
    a_offsets = [0, *accumulate(map(len, a_units))]
    b_offsets = [0, *accumulate(map(len, b_units))]
    ops: list[Opcode] = []
    for tag, a, b, x, y in sequence_opcodes(a_units, b_units):
        a, b, x, y = a_offsets[a], a_offsets[b], b_offsets[x], b_offsets[y]
        if tag == "equal":
            ops.append((tag, a, b, x, y))
        else:
            ops.extend((kind, a + i, a + j, x + k, x + l) for kind, i, j, k, l in
                       _refine(left[a:b], right[x:y], autojunk, remaining))

    merged: list[Opcode] = []
    for tag, a, b, x, y in _with_edges(before, after, start, suffix, ops):
        if merged and (merged[-1][0] == "equal") == (tag == "equal"):
            _, a, _, x, _ = merged.pop()
            merged.append((tag, a, b, x, y) if tag == "equal" else _edit(a, b, x, y))
        else:
            merged.append((tag, a, b, x, y))
    return merged
