"""Context assembler: fit prompt blocks under a token cap.

Blocks are kept whole, in priority order; when the total exceeds the cap the
lowest-priority blocks are dropped. With no cap every non-empty block is kept.
"""
from __future__ import annotations

from dataclasses import dataclass

# Lower number = more important.
TASK_AND_RULES, DECISIONS, OTHER_SESSIONS, DOCUMENT_CHUNKS = range(4)


@dataclass(frozen=True)
class Block:
    priority: int
    text: str


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def assemble(blocks: list[Block], cap: int | None = None) -> str:
    """The kept blocks' text joined by blank lines, in the order given.

    Blocks are admitted by priority (ties keep the given order); one that
    would push the total over ``cap`` tokens is dropped whole, along with
    every lower-priority block after it."""
    live = [b for b in blocks if b.text]
    if cap is not None:
        used = 0
        cut = None
        for b in sorted(live, key=lambda b: b.priority):
            used += estimate_tokens(b.text)
            if used > cap:
                cut = b.priority
                break
        if cut is not None:
            # A priority tier is the unit of dropping; the top tier always stays.
            top = min(b.priority for b in live)
            live = [b for b in live if b.priority < cut or b.priority == top]
    return "\n\n".join(b.text for b in live)
