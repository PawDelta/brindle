"""Review escapes: work a reviewer approved, that merged, and that later
proves broken. Each one is a ``history`` row of kind "escape" naming the
merged branch, the profile that reviewed it and the signal that caught it:

* ``milestone``: a milestone check failed at a commit after a merge, though
  it passed at the commit before (see ``autopilot.check_milestones``);
* ``audit``: a completion audit found gaps and names a merged branch (see
  ``autopilot.record_audit``).

Like all history, these are side records: ``record_safely`` swallows any
failure so they never get in the way of the check or audit they describe.
"""

from __future__ import annotations

import logging
import re

from brindle import git, history
from brindle.db import DB

log = logging.getLogger(__name__)

MERGE_SUBJECT = re.compile(r"^Merge branch '(.+?)'")
AUDIT_LOOKBACK = 500   # merge rows searched for a branch the audit names


def merged_between(repo_path: str, old_sha: str, new_sha: str) -> list[str]:
    """Branches merged by a merge commit in ``old_sha..new_sha``, oldest first."""
    out = git.run(["log", "--merges", "--reverse", "--format=%s", f"{old_sha}..{new_sha}"],
                  repo_path, check=False).stdout
    found: list[str] = []
    for line in out.splitlines():
        m = MERGE_SUBJECT.match(line)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


def reviewer_profile(db: DB, repo_root: str, branch: str) -> str | None:
    """The profile of the reviewer whose latest review row is for ``branch``."""
    for row in db.list_history(repo_root, "review", history.CAP_PER_REPO):
        if row.branch == branch:
            return row.profile
    return None


def _already(db: DB, repo_root: str, branch: str, signal: str, detail: str) -> bool:
    return any(r.branch == branch and r.task == signal and r.result == detail
               for r in db.list_history(repo_root, "escape", history.CAP_PER_REPO))


def record(db: DB, repo_root: str, branch: str, signal: str, detail: str) -> bool:
    """Append an escape row for ``branch`` unless the same one is already
    there. Returns whether a row was written."""
    try:
        if _already(db, repo_root, branch, signal, detail):
            return False
        history.record(db, repo_root, "escape", branch=branch,
                       profile=reviewer_profile(db, repo_root, branch), task=signal, result=detail)
        return True
    except Exception:
        log.exception("brindle: couldn't record an escape for %s", branch)
        return False


def milestone_regressed(db: DB, repo_root: str, checkout_path: str, title: str,
                        passed_sha: str, failed_sha: str) -> list[str]:
    """A milestone passed at ``passed_sha`` and fails at ``failed_sha``: every
    branch merged between the two escaped review. Returns those branches."""
    if passed_sha == failed_sha:
        return []
    try:
        branches = merged_between(checkout_path, passed_sha, failed_sha)
    except Exception:
        log.exception("brindle: couldn't list merges for an escape")
        return []
    detail = f"milestone {title!r} passed at {passed_sha[:8]} and failed at {failed_sha[:8]}"
    return [b for b in branches if record(db, repo_root, b, "milestone", detail)]


# -- review-depth suggestions (brindle Pro) --------------------------------------

DEPTH_DAYS = 90
ADD_RATE = 0.10        # escape rate that suggests a second review...
ADD_MERGES = 10        # ...over at least this many merges by that reviewer profile
DROP_MERGES = 20       # no escapes over at least this many merges suggests dropping one
ALT_REVIEWERS = ("reviewer-codex", "reviewer")


def depth_entitled() -> bool:
    """Review-depth suggestions are part of the ``cost`` feature. Fails closed."""
    try:
        from brindle.pro import license

        return bool(license.has("cost"))
    except Exception:  # noqa: BLE001 -- an unreadable license is "not entitled"
        return False


def _merge_weights(db: DB, repo_root: str, since: float) -> dict[str, str]:
    """Branch -> weight, for branches merged since ``since`` whose worker's task has one."""
    weight_of = {d.agent_id: d.weight for d in db.list_routing_decisions(repo_root)
                 if d.agent_id and d.weight}
    agent_of = {}
    for r in db.list_history(repo_root, "worker_result", history.CAP_PER_REPO):
        if r.branch and r.agent_id:
            agent_of[(r.repo_root, r.branch)] = r.agent_id   # newest first: the oldest wins
    out: dict[str, str] = {}
    for r in db.list_history(repo_root, "merge", history.CAP_PER_REPO):
        if r.ts < since or not r.branch or r.branch in out:
            continue
        weight = weight_of.get(agent_of.get((r.repo_root, r.branch)))
        if weight:
            out[r.branch] = weight
    return out


def review_depth_suggestions(db: DB, repo_root: str, second_review: dict[str, str],
                             now: float | None = None, days: int = DEPTH_DAYS) -> list[str]:
    """Lines to copy into ``.brindle/config.json`` (never applied), from merges and
    escapes over the last ``days``: a ``second_review`` for a weight where one reviewer
    profile let ``ADD_RATE`` of its ``ADD_MERGES``+ merges escape, or dropping the one
    set for a weight with no escapes over ``DROP_MERGES``+ merges. Empty without the
    entitlement, or when nothing qualifies."""
    if not depth_entitled():
        return []
    import time

    since = (time.time() if now is None else now) - days * 86400
    weights = _merge_weights(db, repo_root, since)
    reviewer: dict[str, str] = {}
    for r in db.list_history(repo_root, "review", history.CAP_PER_REPO):
        if r.branch and r.profile and r.branch not in reviewer:
            reviewer[r.branch] = r.profile   # newest first: the latest review's profile
    escaped = {r.branch for r in db.list_history(repo_root, "escape", history.CAP_PER_REPO)
               if r.branch and r.ts >= since}
    merges: dict[tuple[str, str], int] = {}      # (weight, reviewer profile)
    bad: dict[tuple[str, str], int] = {}
    by_weight: dict[str, list[int]] = {}         # weight -> [merges, escapes]
    for branch, weight in weights.items():
        totals = by_weight.setdefault(weight, [0, 0])
        totals[0] += 1
        totals[1] += branch in escaped
        profile = reviewer.get(branch)
        if profile:
            merges[(weight, profile)] = merges.get((weight, profile), 0) + 1
            bad[(weight, profile)] = bad.get((weight, profile), 0) + (branch in escaped)
    lines: list[str] = []
    for weight in sorted(by_weight):
        if weight in second_review:
            n, e = by_weight[weight]
            if e == 0 and n >= DROP_MERGES:
                lines.append(f"drop the second review for {weight} ({n} merges, no escapes in {days} days): "
                             f"remove \"{weight}\" from \"second_review\"")
            continue
        worst = max(((bad[k] / n, k[1], bad[k], n) for k, n in merges.items()
                     if k[0] == weight and n >= ADD_MERGES and bad[k] / n >= ADD_RATE), default=None)
        if worst:
            rate, profile, e, n = worst
            alt = next(a for a in ALT_REVIEWERS if a != profile)
            lines.append(f"add a second review for {weight} ({profile}: {e} of {n} merges escaped, "
                         f"{round(100 * rate)}%): \"second_review\": {{\"{weight}\": \"{alt}\"}}")
    return lines


def audit_gaps(db: DB, repo_root: str, summary: str) -> list[str]:
    """A completion audit found gaps: every merged branch its summary names
    escaped review. Returns those branches."""
    merged = []
    for row in db.list_history(repo_root, "merge", AUDIT_LOOKBACK):
        if row.branch and row.branch not in merged:
            merged.append(row.branch)
    named = [b for b in merged if re.search(rf"(?<![\w/.-]){re.escape(b)}(?![\w/.-])", summary)]
    detail = "completion audit found gaps naming it"
    return [b for b in named if record(db, repo_root, b, "audit", detail)]
