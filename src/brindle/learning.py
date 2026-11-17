"""Hosted learning: which profiles fit which tasks, learned by brindle Pro's API.

Nothing is learned on this machine, and there is no plugin interface for it:
no installed package can act as a learner. brindle reports what happened to each
worker task (its first report, a review verdict, an escalation to the
supervisor, a merge, the worktree removed unmerged) to the hosted learner
(``brindle.pro.learning``), and asks it to pick a profile when
``assign``/``handoff`` get none and no milestone names one.

The repo config's ``learning`` key is ``"auto"`` (the default: hosted learning
when the brindle Pro entitlement includes it, else off), ``"cloud"`` or
``"off"``; any other value means off.

Every call into the learner is guarded: an unreachable or failing API never
fails the review, merge or delegation it was told about.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from brindle import plugins
from brindle.config import RepoConfig
from brindle.db import DB, Agent, Workspace

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskInfo:
    """A task as brindle describes it to the learner. ``agent_id`` and the
    profile fields are empty when a profile is still being chosen."""
    repo_root: str
    task: str = ""
    files: tuple[str, ...] = ()
    weight: str | None = None
    agent_id: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    started_at: float | None = None
    # What routing aims for (brindle.routing_goal): (cost, speed, first_try), None: no goal
    weights: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class Outcome:
    """One event in a worker or reviewer run. ``event`` is "reported" (its
    first report_result), "review" (with ``approved``), "escalated" (the
    supervisor was asked to step in), "merged" or "removed_unmerged" (the
    worker task is over), or "verdict" (with ``approved``) or "no_verdict"
    (a reviewer run ended).

    On terminal events also: ``cost_usd`` (this agent's dollars at list price
    from its token usage; None when its price is unknown, never 0 for
    "unknown"), and for a worker ``review_cost_usd`` (its reviewers' summed
    dollars, None unless all are priced), ``queue_seconds`` (its task waited
    before starting), ``idle_seconds`` (it sat idle with no result at the end)
    and ``failure`` ("no_report", "idle" or "no_commits": it produced
    nothing usable; None otherwise)."""
    event: str
    approved: bool | None = None
    checks_passed: bool = False
    tokens: int = 0
    wall_seconds: float = 0.0
    at: float = field(default_factory=time.time)
    cost_usd: float | None = None
    review_cost_usd: float | None = None
    queue_seconds: float = 0.0
    idle_seconds: float = 0.0
    failure: str | None = None

    @property
    def stalled(self) -> bool:
        return self.failure is not None


class LearningPlugin(ABC):
    """What a learner implements (the hosted learner is the only one)."""

    # why the last ``suggest`` overrode ``default``; None when it kept it
    last_reason: str | None = None
    # whether the last ``suggest``'s evidence included other orgs' shared results
    last_prior: bool = False
    # why the last ``suggest`` kept ``default``, when the learner said
    last_note: str | None = None

    @abstractmethod
    def record(self, task: TaskInfo, outcome: Outcome) -> None:
        """Take note of ``outcome`` for ``task``."""

    @abstractmethod
    def suggest(self, task: TaskInfo, candidates: list[str],
                default: str | None = None) -> str | None:
        """One of ``candidates`` for ``task``, or None to leave it to brindle.
        ``default`` is what brindle would use without learning; returning it
        means the learner kept it. Sets ``last_reason`` when it overrides."""

    def report(self) -> str:
        """What ``brindle learning`` prints."""
        return "this learner has nothing to report"


_learners: dict[str, LearningPlugin] = {}
_settled: set[str] = set()
_no_verdict: set[str] = set()
# worker id -> what its unmerged ending looked like before its worktree and rows went
_captured: dict[str, dict] = {}


def plugin(cfg: RepoConfig, repo_root: str) -> LearningPlugin | None:
    """The hosted learner (brindle Pro's ``CloudLearner``) when the repo's
    ``learning`` setting resolves to ``"cloud"``, else None. Never loads an
    installed package."""
    if plugins.learning_name(cfg) != plugins.CLOUD:
        return None
    if repo_root not in _learners:
        from brindle.pro.learning import CloudLearner

        _learners[repo_root] = CloudLearner(repo_root)
    return _learners[repo_root]


def reset() -> None:
    """Forget the learners made so far (tests use it)."""
    _learners.clear()
    _settled.clear()
    _no_verdict.clear()
    _captured.clear()


def settled(agent_id: str) -> bool:
    """Whether ``agent_id``'s terminal outcome was already sent in this process."""
    return agent_id in _settled


def _task_files(db: DB, worker: Agent, repo_root: str) -> tuple[tuple[str, ...], str | None]:
    """The files and the weight declared for ``worker``'s task."""
    import json

    for t in db.list_tasks(repo_root):
        if t.agent_id == worker.id:
            files: tuple[str, ...] = ()
            if t.files:
                try:
                    files = tuple(f for f in json.loads(t.files) if isinstance(f, str))
                except ValueError:
                    pass
            return files, t.weight or getattr(worker, "weight", None)
    rd = db.routing_decision_for_agent(worker.id)
    return (), getattr(worker, "weight", None) or (rd.weight if rd else None)


def _tokens(db: DB, worker: Agent) -> int:
    try:
        from brindle.usage import agent_usage

        u = agent_usage(db, worker)
        return int(u.total) if u else 0
    except Exception:
        return 0


def _dollars(db: DB, agent: Agent, repo_root: str) -> float | None:
    """``agent``'s spend in US dollars at list price, or None when its usage
    or its model's price is unknown."""
    try:
        from brindle import pricing
        from brindle.usage import agent_usage, usage_cost

        u = agent_usage(db, agent)
        if u is None:
            return None
        extra = pricing.repo_overrides(repo_root)
        price = pricing.price_for(u.model, extra) or pricing.profile_price(agent.profile, repo_root, extra)
        return usage_cost(u, extra, fallback=price)
    except Exception:  # noqa: BLE001 - unknown rather than a failed record
        return None


def _review_dollars(db: DB, ws: Workspace) -> float | None:
    """The summed spend of ``ws``'s reviewers; 0.0 with none, None when any is
    unpriced or the workspace's rows are already gone (unknown, not free)."""
    try:
        if db.get_workspace(ws.id) is None:
            return None
        total = 0.0
        for a in db.list_agents(ws.id):
            if a.mode != "review":
                continue
            d = _dollars(db, a, ws.repo_root)
            if d is None:
                return None
            total += d
        return total
    except Exception:  # noqa: BLE001
        return None


def _queue_seconds(db: DB, worker: Agent, repo_root: str) -> float:
    """How long ``worker``'s task waited (for room or its dependencies) before starting."""
    try:
        for t in db.list_tasks(repo_root):
            if t.agent_id == worker.id and t.started_at and t.created_at:
                return max(0.0, t.started_at - t.created_at)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _failure(db: DB, worker: Agent, ws: Workspace) -> tuple[str | None, float]:
    """(failure, idle seconds) for a worker whose task ended unmerged: "idle"
    (idle with no report), "no_report" (never reported), "no_commits" (reported
    but its branch never moved from where it started), else None."""
    current = db.get_agent(worker.id) or worker
    if current.result is None:
        if current.status == "idle":
            since = current.status_since or current.created_at
            return "idle", max(0.0, time.time() - since)
        return "no_report", 0.0
    if produced_nothing(worker, ws):
        return "no_commits", 0.0
    return None, 0.0


def produced_nothing(worker: Agent | None, ws: Workspace) -> bool:
    """Whether ``worker``'s branch is still exactly where it started: its
    recorded start (rewind's baseline ref, set at session start) equals the
    branch head. False whenever that can't be shown, e.g. no recorded start:
    a branch level with its base may just have been merged by plain git."""
    if worker is None:
        return False
    try:
        from brindle import git, rewind

        ref = rewind.baseline_ref(worker.id)
        if not git.ok(["rev-parse", "--verify", "-q", ref], ws.repo_root):
            return False
        start = git.out(["rev-parse", ref], ws.repo_root)
        return bool(start) and start == git.out(["rev-parse", f"refs/heads/{ws.branch}"], ws.repo_root)
    except Exception:  # noqa: BLE001 - no evidence: don't claim it did nothing
        return False


def _ending(db: DB, worker: Agent, ws: Workspace, merged: bool = False) -> dict:
    failure, idle = (None, 0.0) if merged else _failure(db, worker, ws)
    return {"failure": failure, "idle_seconds": idle, "review_cost_usd": _review_dollars(db, ws),
            "queue_seconds": _queue_seconds(db, worker, ws.repo_root),
            "cost_usd": _dollars(db, worker, ws.repo_root)}


def capture(db: DB, ws: Workspace, worker: Agent | None) -> None:
    """Before removing ``ws`` unmerged: note how ``worker``'s task ended
    (failure, idle time, its own and its reviewers' spend) while its worktree
    and rows still exist, for the ``note(merged=False)`` that follows the
    removal. Never raises."""
    try:
        if worker is not None:
            _captured[worker.id] = _ending(db, worker, ws)
    except Exception:  # noqa: BLE001
        log.info("brindle: couldn't capture a worker's ending for the learner", exc_info=True)


def forget_capture(worker: Agent | None) -> None:
    """Drop ``worker``'s captured ending (its removal was refused)."""
    if worker is not None:
        _captured.pop(worker.id, None)


def _task_info(db: DB, worker: Agent, ws: Workspace) -> TaskInfo:
    from brindle.profiles import load_profile

    model = None
    try:
        model = load_profile(worker.profile, ws.repo_root).model
    except Exception:
        pass
    files, weight = _task_files(db, worker, ws.repo_root)
    return TaskInfo(
        repo_root=ws.repo_root, task=worker.task or "", files=files, weight=weight,
        agent_id=worker.id, profile=worker.profile, provider=worker.provider, model=model,
        started_at=worker.created_at,
    )


def _reviewer_info(db: DB, reviewer: Agent, ws: Workspace,
                   worker: Agent | None = None) -> TaskInfo:
    from brindle import agents
    from brindle.profiles import load_profile

    if worker is None:
        worker = agents.workspace_worker(db, ws)
    model = None
    try:
        model = load_profile(reviewer.profile, ws.repo_root).model
    except Exception:
        pass
    files, weight = _task_files(db, worker, ws.repo_root) if worker else ((), None)
    task = (worker.task if worker and worker.task else "") or (reviewer.task or "")
    return TaskInfo(
        repo_root=ws.repo_root, task=task, files=files, weight=weight,
        agent_id=reviewer.id, profile=reviewer.profile, provider=reviewer.provider,
        model=model, started_at=reviewer.created_at,
    )


def note(db: DB, cfg: RepoConfig, worker: Agent | None, ws: Workspace, *,
         reported: bool = False, approved: bool | None = None,
         escalated: bool = False, merged: bool | None = None,
         checks_passed: bool = False) -> None:
    """Tell the learner about one event in ``worker``'s task. Does nothing
    without a learner, and never raises."""
    try:
        captured = None
        if merged is not None and worker is not None:
            _settled.add(worker.id)
            captured = _captured.pop(worker.id, None)
        p = plugin(cfg, ws.repo_root)
        if p is None or worker is None:
            return
        info = _task_info(db, worker, ws)
        wall = max(0.0, time.time() - worker.created_at)
        if reported:
            p.record(info, Outcome("reported", wall_seconds=wall))
        if approved is not None:
            p.record(info, Outcome("review", approved=approved))
        if escalated:
            p.record(info, Outcome("escalated"))
        if merged is not None:
            ending = (captured if captured is not None and not merged
                      else _ending(db, worker, ws, merged=bool(merged)))
            p.record(info, Outcome(
                "merged" if merged else "removed_unmerged", checks_passed=checks_passed,
                tokens=_tokens(db, worker), wall_seconds=wall, **ending,
            ))
    except Exception:
        log.exception("brindle: the learner failed to record an outcome")


def note_reviewer(db: DB, cfg: RepoConfig, reviewer: Agent | None, ws: Workspace | None, *,
                  approved: bool | None = None, worker: Agent | None = None) -> None:
    """Tell the learner how ``reviewer``'s run ended ("verdict" when ``approved``
    is a bool, else "no_verdict"), at most once per reviewer."""
    try:
        if reviewer is None or reviewer.mode != "review" or ws is None or reviewer.id in _settled:
            return
        if isinstance(approved, bool):
            _settled.add(reviewer.id)
            event: str = "verdict"
        else:
            if reviewer.id in _no_verdict:
                return
            current = db.get_agent(reviewer.id)
            if (current and current.result is not None) or db.conn.execute(
                "SELECT 1 FROM reviews WHERE reviewer_id = ?", (reviewer.id,)
            ).fetchone():
                return
            _no_verdict.add(reviewer.id)
            event = "no_verdict"
        p = plugin(cfg, ws.repo_root)
        if p is None:
            return
        info = _reviewer_info(db, reviewer, ws, worker)
        wall = max(0.0, time.time() - reviewer.created_at)
        p.record(info, Outcome(event, approved=approved if isinstance(approved, bool) else None,
                               tokens=_tokens(db, reviewer), wall_seconds=wall,
                               cost_usd=_dollars(db, reviewer, ws.repo_root)))
    except Exception:
        log.exception("brindle: the learner failed to record a reviewer outcome")


def choose_why(db: DB, cfg: RepoConfig, repo_root: str, task: str | None = None,
               files: list[str] | None = None, candidates: list[str] | None = None,
               weight: str | None = None,
               default: str | None = None, *, design_heavy: bool = False,
               goal: dict | None = None) -> tuple[str | None, str | None]:
    """(override, reason): the learner's pick among ``candidates`` (default:
    the repo's ``learning_candidates``, else the medium tier's routing list)
    when it overrides ``default`` (what brindle would use: the first
    candidate unless given), else (None, None). The learner is not asked with
    fewer than two candidates. ``goal``: the routing goal's weights in force
    (brindle.routing_goal.resolve, with the session and task levels); without
    it, the personal and repo levels'."""
    _forget_note(cfg, repo_root)
    if candidates is None:
        candidates = cfg.learning_candidates or cfg.routing.get("medium") or []
    names = [c for c in candidates if isinstance(c, str) and cfg.routing_allows(c, design_heavy)]
    if not names:
        return None, None
    default = default if default and cfg.routing_allows(default, design_heavy) else names[0]
    if default not in names:
        # e.g. default_agent outside learning_candidates: it's still the baseline to beat
        names.insert(0, default)
    if len(names) < 2:
        return None, None
    try:
        p = plugin(cfg, repo_root)
        if p is None:
            return None, None
        p.last_reason = None
        p.last_prior = False
        from brindle import routing_goal

        if goal is None:
            goal = routing_goal.for_learner(repo_root)
        pick = p.suggest(TaskInfo(repo_root=repo_root, task=task or "", files=tuple(files or ()),
                                  weight=weight, weights=routing_goal.as_tuple(goal)), names, default)
        if pick not in names or pick == default:
            return None, None
        return pick, p.last_reason
    except Exception:
        log.exception("brindle: the learner failed to suggest a profile")
        return None, None


def choose(db: DB, cfg: RepoConfig, repo_root: str, task: str | None = None,
           files: list[str] | None = None, candidates: list[str] | None = None,
           weight: str | None = None, default: str | None = None, *,
           design_heavy: bool = False, goal: dict | None = None) -> str | None:
    """The learner's override of ``default`` among ``candidates``, or None
    (no learner, nothing to choose between, or it kept the default)."""
    options: dict = {"design_heavy": True} if design_heavy else {}
    if goal is not None:
        options["goal"] = goal
    return choose_why(db, cfg, repo_root, task, files, candidates, weight, default, **options)[0]


def _forget_note(cfg: RepoConfig, repo_root: str) -> None:
    try:
        p = plugin(cfg, repo_root)
        if p is not None:
            p.last_note = None
    except Exception:  # noqa: BLE001
        pass


def kept_note(cfg: RepoConfig, repo_root: str) -> str | None:
    """Why the learner kept the default in the last ``choose_why``, when it
    said (e.g. "not enough similar tasks yet: ..."); None otherwise."""
    try:
        p = plugin(cfg, repo_root)
        note = p.last_note if p is not None else None
        return note if isinstance(note, str) and note else None
    except Exception:  # noqa: BLE001
        return None


def used_prior(cfg: RepoConfig, repo_root: str) -> bool:
    """Whether the learner's last override (see ``choose_why``) drew on other
    orgs' shared results. False without a learner or when it didn't say."""
    try:
        p = plugin(cfg, repo_root)
        return p is not None and p.last_prior is True
    except Exception:
        return False


__all__ = ["LearningPlugin", "Outcome", "TaskInfo", "capture", "choose", "choose_why",
           "forget_capture", "kept_note", "note", "note_reviewer", "plugin", "reset", "settled",
           "used_prior"]
