"""Brief-quality learner (brindle Pro): which traits of a task brief tend to go
badly in this repo.

Every finished task (a ``tasks`` row joined to its ``routing_decisions``
outcome) is a sample: it went well when it merged within one review round, and
badly when it needed more rounds or was removed unmerged. For each trait a
brief can have (no ``done_when``, no ``files`` globs, many globs, a weight, a
very short or very long text, waiting on other tasks) ``rates`` counts how
often briefs with that trait went well here. ``warning`` names the worst trait
of a new brief whose rate is clearly below the repo's overall rate, with what
to change.

Everything is computed from the local database; nothing is sent anywhere.
Fails closed: without the Pro entitlement, or with fewer than
``MIN_FINISHED`` finished tasks, there is no warning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from brindle.db import DB

log = logging.getLogger(__name__)

FEATURE = "learned_rules"       # the Pro learning entitlement, as brindle.learned_rules checks it
MIN_FINISHED = 20               # finished tasks before anything is said
MIN_TRAIT_SAMPLES = 5           # finished tasks with a trait before its rate counts
MARGIN = 0.2                    # how far below the overall rate a trait's rate must be
MANY_FILES = 4                  # this many globs or more is "many"
SHORT_BRIEF = 200               # characters; shorter is "short"
LONG_BRIEF = 3000               # characters; longer is "long"

# trait key -> (what the brief is called, what to do about it)
TRAITS = {
    "no_done_when": ("with no done_when", "add one"),
    "no_files": ("with no files globs", "list the files it should touch"),
    "many_files": (f"with {MANY_FILES} or more files globs", "split the task"),
    "short": (f"under {SHORT_BRIEF} characters", "say more about what done looks like"),
    "long": (f"over {LONG_BRIEF} characters", "split the task"),
    "no_weight": ("with no weight", "set one"),
    "weight:light": ("sized light", "check the task isn't bigger than that"),
    "weight:medium": ("sized medium", "check the task isn't bigger than that"),
    "weight:heavy": ("sized heavy", "split the task"),
    "has_deps": ("that wait on other tasks", "check the dependencies are needed"),
}


def entitled() -> bool:
    """Whether the plan includes it. Fails closed: no license, an unreadable
    one, or any error means no."""
    from brindle.pro import license

    try:
        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 -- an unreadable license is "not entitled"
        return False


def _count(raw) -> int:
    """The length of a JSON list (the ``files`` and ``depends_on`` columns)."""
    if isinstance(raw, (list, tuple)):
        return len(raw)
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return 0
    return len(value) if isinstance(value, list) else 0


def traits(task_text: str | None, done_when: str | None, files, weight: str | None,
           depends_on=None) -> list[str]:
    """The trait keys of a brief (see ``TRAITS``)."""
    out = []
    if not (done_when or "").strip():
        out.append("no_done_when")
    n = _count(files)
    if n == 0:
        out.append("no_files")
    elif n >= MANY_FILES:
        out.append("many_files")
    length = len((task_text or "").strip())
    if length < SHORT_BRIEF:
        out.append("short")
    elif length > LONG_BRIEF:
        out.append("long")
    out.append(f"weight:{weight}" if weight in ("light", "medium", "heavy") else "no_weight")
    if _count(depends_on):
        out.append("has_deps")
    return out


@dataclass(frozen=True)
class Rate:
    good: int       # merged within one review round
    total: int      # finished tasks

    @property
    def value(self) -> float:
        return self.good / self.total if self.total else 0.0


@dataclass(frozen=True)
class Rates:
    overall: Rate
    by_trait: dict[str, Rate]


def finished(db: DB, repo_root: str) -> list[tuple[list[str], bool]]:
    """One ``(traits, went_well)`` per finished task of the repo. A task with
    several routing rows counts once, by its latest."""
    rows = db.conn.execute(
        "SELECT t.task_text, t.done_when, t.files, t.weight, t.depends_on, "
        "       r.outcome, r.review_rounds "
        "FROM tasks t JOIN routing_decisions r ON r.task_id = t.id "
        "WHERE t.repo_root = ? AND r.outcome IN ('merged', 'removed_unmerged') "
        "  AND r.id = (SELECT MAX(id) FROM routing_decisions WHERE task_id = t.id)",
        (repo_root,)).fetchall()
    return [(traits(r["task_text"], r["done_when"], r["files"], r["weight"], r["depends_on"]),
             r["outcome"] == "merged" and (r["review_rounds"] or 0) <= 1)
            for r in rows]


def rates(db: DB, repo_root: str) -> Rates:
    """How often briefs went well here, overall and per trait."""
    samples = finished(db, repo_root)
    counts: dict[str, list[int]] = {}
    for keys, good in samples:
        for k in keys:
            c = counts.setdefault(k, [0, 0])
            c[0] += good
            c[1] += 1
    return Rates(Rate(sum(g for _, g in samples), len(samples)),
                 {k: Rate(g, n) for k, (g, n) in counts.items()})


def warning(db: DB, repo_root: str, task_text: str | None, done_when: str | None,
            files, weight: str | None) -> str | None:
    """A one-line warning for the brief's worst trait here, or None: without
    the entitlement, below ``MIN_FINISHED`` finished tasks, or when no trait of
    the brief is clearly worse than the repo's overall rate. Never raises."""
    try:
        if not entitled():
            return None
        r = rates(db, repo_root)
        if r.overall.total < MIN_FINISHED:
            return None
        worst: tuple[float, str] | None = None
        for key in traits(task_text, done_when, files, weight):
            rate = r.by_trait.get(key)
            if rate is None or rate.total < MIN_TRAIT_SAMPLES:
                continue
            gap = r.overall.value - rate.value
            if gap >= MARGIN and (worst is None or gap > worst[0]):
                worst = (gap, key)
        if worst is None:
            return None
        rate, (label, advice) = r.by_trait[worst[1]], TRAITS[worst[1]]
        return f"Briefs {label} merged cleanly {rate.good} of {rate.total} times here; {advice}."
    except Exception:  # noqa: BLE001 -- a hint must never fail the delegation
        log.exception("brindle: couldn't compute the brief warning")
        return None
