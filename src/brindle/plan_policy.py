"""Whether a worker must get its plan approved before it edits (``plan`` config)."""

from dataclasses import dataclass
from fnmatch import fnmatch

# Files that always need a plan in "auto" mode: CI, migrations, auth and deploy.
PLAN_PATHS = (
    ".github/workflows/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile", "azure-pipelines.yml",
    "**/migrations/**", "**/alembic/**",
    "**/auth/**", "**/auth.py",
    "deploy/**", "**/deploy/**", "Dockerfile", "**/Dockerfile", "**/*.tf",
)
PLAN_WEIGHTS = ("heavy", "advanced")
LOW_RISK_WEIGHTS = ("light", "medium")


@dataclass
class PlanTask:
    weight: str | None = None
    files: list[str] | None = None
    cross_repo: bool = False    # a repo other than the session's own, or a dependency in another repo


def _has_glob(s: str) -> bool:
    return any(c in s for c in "*?[")


def _segments_meet(x: str, y: str) -> bool:
    """Whether path segments ``x`` and ``y`` (either may be a glob) can name the same
    thing. Two globs are compared by their literal prefixes and suffixes, so it can
    only err towards "yes"; a segment with a character class, ``?`` or braces is not
    analysed and meets anything."""
    if any(c in s for s in (x, y) for c in "[]?{}"):
        return True
    if not _has_glob(x):
        return fnmatch(x, y)
    if not _has_glob(y):
        return fnmatch(y, x)

    def ends(s: str) -> tuple[str, str]:
        idx = [i for i, c in enumerate(s) if c in "*?["]
        return s[:idx[0]], s[idx[-1] + 1:]

    (px, sx), (py, sy) = ends(x), ends(y)
    return (px.startswith(py) or py.startswith(px)) and (sx.endswith(sy) or sy.endswith(sx))


def _overlap(a: list[str], b: list[str]) -> bool:
    """Whether some path can match both segment lists ("**" matches any number of segments)."""
    if not a and not b:
        return True
    if a and a[0] == "**":
        return _overlap(a[1:], b) or (bool(b) and _overlap(a, b[1:]))
    if b and b[0] == "**":
        return _overlap(a, b[1:]) or (bool(a) and _overlap(a[1:], b))
    return bool(a) and bool(b) and _segments_meet(a[0], b[0]) and _overlap(a[1:], b[1:])


def _matches(path: str, pattern: str) -> bool:
    """Whether the declared ``path`` (a file or a glob) can name a file ``pattern`` covers."""
    return _overlap(path.removeprefix("./").split("/"), pattern.split("/"))


@dataclass(frozen=True)
class OrgPlanRules:
    """The org's plan rules, from its policy (``plan``, ``plan_paths``,
    ``plan_approvers``, ``plan_two_person``, ``plan_reviewer_may_approve_low_risk``)."""
    plan: str = "auto"                  # auto | always | never
    paths: tuple[str, ...] = ()
    approvers: tuple[str, ...] = ()     # member ids; empty: any member may approve
    two_person: bool = False
    reviewer_low_risk: bool = False

    @classmethod
    def from_policy(cls, policy: dict | None) -> "OrgPlanRules":
        if callable(getattr(policy, "plan_rules", None)):
            policy = policy.plan_rules()
        p = policy if isinstance(policy, dict) else {}
        plan = p.get("plan")
        return cls(
            plan=plan if plan in ("auto", "always", "never") else "auto",
            paths=tuple(s for s in p.get("plan_paths") or () if isinstance(s, str)),
            approvers=tuple(s for s in p.get("plan_approvers") or () if isinstance(s, str)),
            two_person=p.get("plan_two_person") is True,
            reviewer_low_risk=p.get("plan_reviewer_may_approve_low_risk") is True,
        )


class OrgRulesUnavailable(Exception):
    """The person is in a team org but its policy can't be read: plans can't be judged."""


def load_org_rules(repo_root: str | None) -> OrgPlanRules | None:
    """The org's plan rules for ``repo_root``; None without a team org.
    Raises :class:`OrgRulesUnavailable` when there is a team org whose policy can't be read."""
    from brindle.pro import org_context

    ctx = org_context.active() or org_context.OrgContext.resolve(repo_root=repo_root)
    return ctx.plan_rules


def org_requires_plan(task: "PlanTask", rules: OrgPlanRules | None) -> bool:
    """Whether the org's rules alone make ``task`` need a plan (no local setting can waive it)."""
    return rules is not None and (rules.plan == "always" or touches_plan_paths(task, rules.paths))


def current_member(repo_root: str | None) -> str | None:
    """This person's member id in the team org, or None."""
    try:
        from brindle.pro import org_context

        ctx = org_context.active() or org_context.OrgContext.resolve(repo_root=repo_root)
        ent = ctx._team_entitlement()
        return ent.sub if ent else None
    except Exception:  # noqa: BLE001
        return None


def touches_plan_paths(task: PlanTask, paths) -> bool:
    return any(_matches(f, p) for f in task.files or () for p in paths)


def needs_plan(task: PlanTask, *, config, org_rules: OrgPlanRules | None = None) -> bool:
    """True when ``task`` must submit a plan first. ``config.plan`` "always" /
    "never" decide alone; "auto" plans for heavy work, cross-repo work and
    ``PLAN_PATHS``. ``org_rules`` layers the org's on top: its ``plan`` "always"
    or "never" replaces the local auto heuristics, its ``plan_paths`` always
    need a plan, and a local "always" still holds (the stricter answer wins)."""
    if org_rules is not None:
        if org_rules.plan == "always" or touches_plan_paths(task, org_rules.paths):
            return True
        if org_rules.plan == "never":
            return config.plan == "always"
    if config.plan == "always":
        return True
    if config.plan == "never":
        return False
    if (task.weight or "").lower() in PLAN_WEIGHTS or task.cross_repo:
        return True
    return touches_plan_paths(task, PLAN_PATHS)
