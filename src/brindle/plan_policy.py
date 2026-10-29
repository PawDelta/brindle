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


def needs_plan(task: PlanTask, *, config, org_rules: dict | None = None) -> bool:
    """True when ``task`` must submit a plan first. ``config.plan`` "always" /
    "never" decide alone; "auto" plans for heavy work, cross-repo work and
    ``PLAN_PATHS``. ``org_rules`` (``{"paths": [...]}``) adds paths that always
    need a plan; None adds nothing."""
    if config.plan == "always":
        return True
    if config.plan == "never":
        return False
    if (task.weight or "").lower() in PLAN_WEIGHTS or task.cross_repo:
        return True
    paths = (*PLAN_PATHS, *((org_rules or {}).get("paths") or ()))
    return any(_matches(f, p) for f in task.files or () for p in paths)
