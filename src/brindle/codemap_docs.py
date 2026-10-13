"""Doc refresh for the code map: ask the supervisor to delegate graphify update on doc changes.

Code files are refreshed without an LLM after merges (codemap.refresh_later).
Docs (.md/.yaml/.yml) require an LLM for graphify's semantic update; brindle
does not call an LLM itself, so when a merge touches docs, brindle suggests
that the supervisor assign a light worker for just those files.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from brindle import codemap, git

if TYPE_CHECKING:
    from brindle.db import Workspace

DOC_EXTENSIONS = (".md", ".yaml", ".yml")


def doc_files(files: list[str]) -> list[str]:
    """Filter for doc files (.md, .yaml, .yml)."""
    seen: dict[str, None] = {}
    for f in files:
        if f and f.lower().endswith(DOC_EXTENSIONS):
            seen[f] = None
    return list(seen)


def changed_files(ws: Workspace, target: str | Path | None = None) -> list[str]:
    """Files changed in the merged workspace."""
    target_path = target or codemap.base_checkout(ws)
    if target_path:
        try:
            return git.paths(["diff", "--name-only", "HEAD~1", "HEAD"], target_path)
        except git.GitError:
            pass
    try:
        from brindle import conflicts

        return conflicts.changed_files(ws)
    except Exception:
        return []


def docs_refresh_note(ws: Workspace, target: str | Path | None = None) -> str | None:
    """If the session has a code map and the merge touched doc files, return
    a short note for the supervisor to assign a light-weight worker to run
    graphify's semantic update for just those files."""
    target_path = target or codemap.base_checkout(ws)
    if codemap.graph_path(target_path) is None:
        return None
    docs = doc_files(changed_files(ws, target_path))
    if not docs:
        return None
    shown = ", ".join(docs)
    return (
        f"Docs changed ({shown}): assign a light-weight worker to run "
        f"graphify's semantic update for just those files."
    )


refresh_note = docs_refresh_note
note = docs_refresh_note
