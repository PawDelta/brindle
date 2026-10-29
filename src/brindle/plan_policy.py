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


def _matches(path: str, pattern: str) -> bool:
    """Whether the declared ``path`` (a file or a glob) can name a file ``pattern`` covers."""
    path = path.removeprefix("./")
    # fnmatch's "*" crosses "/", so "x/**" also covers "x/a/b"; a leading "**/" may match nothing.
    forms = (pattern, pattern.removeprefix("**/"))
    if any(fnmatch(path, p) for p in forms):
        return True
    if not any(c in path for c in "*?["):
        return False
    # A glob declaration: does it overlap the pattern? Compare the pattern against the
    # glob itself, and against the glob's literal directory with the pattern placed under it.
    parts = path.split("/")
    base = "/".join(parts[:next((i for i, s in enumerate(parts) if any(c in s for c in "*?[")), len(parts))])
    return any(fnmatch(p, path) or fnmatch(f"{base}/{p}" if base else p, path)
               or fnmatch(f"{base}/x" if base else "x", p) for p in forms)


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
