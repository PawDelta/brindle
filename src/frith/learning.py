"""Hosted learning: which profiles fit which tasks, learned by frith Pro's API.

Nothing is learned on this machine, and there is no plugin interface for it:
no installed package can act as a learner. frith reports what happened to each
worker task (a review verdict, an escalation to the supervisor, a merge, the
worktree removed unmerged) to the hosted learner (``frith.pro.learning``), and
asks it to pick a profile when ``assign``/``handoff`` get none and no
milestone names one.

The repo config's ``learning`` key is ``"auto"`` (the default: hosted learning
when the frith Pro entitlement includes it, else off), ``"cloud"`` or
``"off"``; any other value means off.

Every call into the learner is guarded: an unreachable or failing API never
fails the review, merge or delegation it was told about.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from frith import plugins
from frith.config import RepoConfig
from frith.db import DB, Agent, Workspace

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskInfo:
    """A task as frith describes it to the learner. ``agent_id`` and the
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


@dataclass(frozen=True)
class Outcome:
    """One event in a worker task's life. ``event`` is "review" (with
    ``approved``), "escalated" (the supervisor was asked to step in),
    "merged" or "removed_unmerged" (the task is over)."""
    event: str
    approved: bool | None = None
    checks_passed: bool = False
    tokens: int = 0
    wall_seconds: float = 0.0
    at: float = field(default_factory=time.time)


class LearningPlugin(ABC):
    """What a learner implements (the hosted learner is the only one)."""

    # why the last ``suggest`` overrode ``default``; None when it kept it
    last_reason: str | None = None
    # whether the last ``suggest``'s evidence included other orgs' shared results
    last_prior: bool = False

    @abstractmethod
    def record(self, task: TaskInfo, outcome: Outcome) -> None:
        """Take note of ``outcome`` for ``task``."""

    @abstractmethod
    def suggest(self, task: TaskInfo, candidates: list[str],
                default: str | None = None) -> str | None:
        """One of ``candidates`` for ``task``, or None to leave it to frith.
        ``default`` is what frith would use without learning; returning it
        means the learner kept it. Sets ``last_reason`` when it overrides."""

    def report(self) -> str:
        """What ``frith learning`` prints."""
        return "this learner has nothing to report"


_learners: dict[str, LearningPlugin] = {}


def plugin(cfg: RepoConfig, repo_root: str) -> LearningPlugin | None:
    """The hosted learner (frith Pro's ``CloudLearner``) when the repo's
    ``learning`` setting resolves to ``"cloud"``, else None. Never loads an
    installed package."""
    if plugins.learning_name(cfg) != plugins.CLOUD:
        return None
    if repo_root not in _learners:
        from frith.pro.learning import CloudLearner

        _learners[repo_root] = CloudLearner(repo_root)
    return _learners[repo_root]


def reset() -> None:
    """Forget the learners made so far (tests use it)."""
    _learners.clear()


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
            return files, t.weight
    return (), None


def _tokens(db: DB, worker: Agent) -> int:
    try:
        from frith.usage import agent_usage

        u = agent_usage(db, worker)
        return int(u.total) if u else 0
    except Exception:
        return 0


def _task_info(db: DB, worker: Agent, ws: Workspace) -> TaskInfo:
    from frith.profiles import load_profile

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


def note(db: DB, cfg: RepoConfig, worker: Agent | None, ws: Workspace, *,
         approved: bool | None = None, escalated: bool = False,
         merged: bool | None = None, checks_passed: bool = False) -> None:
    """Tell the learner about one event in ``worker``'s task. Does nothing
    without a learner, and never raises."""
    try:
        p = plugin(cfg, ws.repo_root)
        if p is None or worker is None:
            return
        info = _task_info(db, worker, ws)
        if approved is not None:
            p.record(info, Outcome("review", approved=approved))
        if escalated:
            p.record(info, Outcome("escalated"))
        if merged is not None:
            p.record(info, Outcome(
                "merged" if merged else "removed_unmerged", checks_passed=checks_passed,
                tokens=_tokens(db, worker), wall_seconds=max(0.0, time.time() - worker.created_at),
            ))
    except Exception:
        log.exception("frith: the learner failed to record an outcome")


def choose_why(db: DB, cfg: RepoConfig, repo_root: str, task: str | None = None,
               files: list[str] | None = None, candidates: list[str] | None = None,
               weight: str | None = None,
               default: str | None = None) -> tuple[str | None, str | None]:
    """(override, reason): the learner's pick among ``candidates`` (default:
    the repo's ``learning_candidates``) when it overrides ``default`` (what
    frith would use: the first candidate unless given), else (None, None).
    The learner is not asked with fewer than two candidates."""
    names = [c for c in (candidates if candidates is not None else cfg.learning_candidates)
             if isinstance(c, str)]
    if not names:
        return None, None
    default = default or names[0]
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
        pick = p.suggest(TaskInfo(repo_root=repo_root, task=task or "",
                                  files=tuple(files or ()), weight=weight), names, default)
        if pick not in names or pick == default:
            return None, None
        return pick, p.last_reason
    except Exception:
        log.exception("frith: the learner failed to suggest a profile")
        return None, None


def choose(db: DB, cfg: RepoConfig, repo_root: str, task: str | None = None,
           files: list[str] | None = None, candidates: list[str] | None = None,
           weight: str | None = None, default: str | None = None) -> str | None:
    """The learner's override of ``default`` among ``candidates``, or None
    (no learner, nothing to choose between, or it kept the default)."""
    return choose_why(db, cfg, repo_root, task, files, candidates, weight, default)[0]


def used_prior(cfg: RepoConfig, repo_root: str) -> bool:
    """Whether the learner's last override (see ``choose_why``) drew on other
    orgs' shared results. False without a learner or when it didn't say."""
    try:
        p = plugin(cfg, repo_root)
        return p is not None and p.last_prior is True
    except Exception:
        return False


__all__ = ["LearningPlugin", "Outcome", "TaskInfo", "choose", "choose_why", "note", "plugin", "reset",
           "used_prior"]
