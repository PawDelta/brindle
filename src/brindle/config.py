"""Paths and per-repo configuration.

Repo config lives in ``<repo>/.brindle/config.json`` (committed, shared with the
team) and ``<repo>/.brindle/config.local.json`` (gitignored, personal). Local
keys override shared ones; for command lists, local may instead give
``{"before": [...], "after": [...]}`` to wrap the team's commands.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = ".brindle"
CONFIG_FILE = "config.json"
LOCAL_CONFIG_FILE = "config.local.json"

PORT_RANGE_START = 20000
PORT_BLOCK_SIZE = 10


def brindle_home() -> Path:
    return Path(os.environ.get("BRINDLE_HOME", Path.home() / ".brindle"))


def db_path() -> Path:
    return brindle_home() / "brindle.db"


def worktrees_dir() -> Path:
    return brindle_home() / "worktrees"


def user_profiles_dir() -> Path:
    return brindle_home() / "agents"


def user_config_path() -> Path:
    """``~/.brindle/config.json``: this person's defaults for every repo. A
    repo's own .brindle/config.json and config.local.json override it."""
    return brindle_home() / CONFIG_FILE


WEIGHTS = ("light", "medium", "heavy")
# The first profile of each tier is its baseline; the rest are candidates
# brindle Pro's learner may pick instead when its evidence says they do better.
# Nothing here prefers another provider for its own sake: all-Claude or
# all-Codex may well be best, and that's the learner's call.
DEFAULT_ROUTING = {
    "light": ["developer", "developer-codex", "developer-local"],
    "medium": ["developer", "developer-codex", "developer-heavy"],
    "heavy": ["developer-heavy", "developer", "developer-codex"],
}


@dataclass
class RepoConfig:
    setup: list[str] = field(default_factory=list)
    teardown: list[str] = field(default_factory=list)
    copy: list[str] = field(default_factory=list)
    base_branch: str | None = None
    branch_prefix: str = ""
    default_agent: str = "developer"
    fetch: bool = True
    # Autopilot and merge gates.
    autopilot: bool = True             # `brindle` starts the supervisor with autopilot on
    checks: list[str] = field(default_factory=list)  # must pass in a branch before it merges
    review: bool | None = None         # require a reviewer's approval (None: only under autopilot)
    reviewer: str = "reviewer"         # agent profile that reviews branches
    review_profile: str | None = None  # force request_review's profile (skips its automatic cross-model pick)
    pre_commit: bool = True            # run pre-commit (the framework) over a branch before merging
    max_agents: int = 4                # workers running at once per session; 0 means no cap
    check_timeout: int = 900           # seconds allowed for each check command
    check_concurrency: int = 2         # check runs at once on this machine, across branches; the rest queue (0: no cap)
    usage_limit: int = 90              # autopilot stops pushing on at this % of the Claude usage limit
    limit_cooldown_minutes: int | None = None  # how long a provider that hit its limit counts as unavailable (default 300 for Antigravity)
    graphify: bool | None = None       # point agents at graphify-out/graph.json (None: if it's there)
    stale_after: int = 30              # minutes before an idle, reported worker is closed; 0: never
    pipeline: bool = True              # brindle reviews and merges reported branches itself (brindle.pipeline)
    goal_audit: bool = True            # an adversarial reviewer checks a finished goal against the original request before it counts as reached
    review_rounds: int = 2             # fix-and-re-review rounds the pipeline runs before asking the supervisor
    merge_into: str | None = None      # branch worker branches are cut from and merge into (None: the supervisor's / default branch)
    auto_merge_default_branch: bool = False  # let the pipeline merge into the repo's default branch on its own
    delegation: str = "balanced"      # how readily a supervisor hands work to workers: "conservative" (save tokens), "balanced" or "fast" (save time)
    plan_first: bool = False           # workers propose a plan and wait for approval before editing
    permission_policy: str = "off"     # "on": brindle answers workers' permission requests by its rules (see brindle.permissions)
    overlap: str = "block"           # a task whose files overlap a running one: "block" or "warn"
    # Paths/globs whose merge conflicts brindle never hands to a worker to
    # resolve (migrations, lockfiles, generated code): they go to the person.
    protected_paths: list[str] = field(default_factory=list)
    # Worktree pool: pre-built worktrees (checked out, files copied, setup run)
    # that `create` claims instead of doing that work live. None here means
    # "not set"; load_repo_config resolves it to 1 if the repo has `setup`
    # commands (worth pre-building) or 0 otherwise (a bare `worktree add` is
    # already fast). 0 disables the pool.
    pool_size: int | None = None
    add_dirs: list[str] = field(default_factory=list)
    # Per-worktree Docker services (brindle Pro): [{"name", "preset"?, "image"?, "port"?, "env"?}]
    # -- see brindle.services.
    services: list[dict] = field(default_factory=list)
    # Start Ollama in the background when a native profile points at it on
    # this machine and it isn't running, and preload the models (see
    # brindle.native.serve). On by default, and only ever does anything for a
    # native profile pointing at Ollama on a loopback address; the server
    # brindle started is stopped again when the last session closes. Set
    # false to never start or preload one.
    local_models: bool = True
    delete_merged_branches: bool = True  # removing a worktree deletes its branch once fully merged into its base
    pr_footer: bool = True             # `brindle pr` ends the PR body with one "built with brindle" line
    sidebar: str = "left"              # where the dashboard sits: "left" of the chat or "bottom"
    # Standing rules for the supervisor ("fix review findings without asking", ...):
    # ~/.brindle/config.json's, then the repo's, then config.local.json's, all kept.
    rules: list[str] = field(default_factory=list)
    learning: str = "auto"             # "auto" (brindle Pro's cloud learner when entitled, else off), "cloud" or "off"; hosted only, anything else is off (see brindle.learning)
    learning_candidates: list[str] = field(default_factory=list)  # profiles the learner may pick from
    # Learned rules (brindle Pro; see brindle.learned_rules): how many tasks a review
    # finding must recur in before a rule is suggested, and the profile that groups
    # findings (None: a local model that answers, else the cheapest Claude model).
    learned_rules_repeats: int = 3
    learned_rules_profile: str | None = None
    # Which installed plugin to use per group ("events", "policy", "account"), or "off";
    # unset: the only one installed, if exactly one (see brindle.plugins).
    plugins: dict[str, str] = field(default_factory=dict)
    message_delivery: str = "pull"     # agent messages to an interactive supervisor: "pull" (a notice, then read_messages) or "push" (the text itself)
    # Air-gap mode (brindle Enterprise; see brindle.airgap): no outbound traffic, local models only.
    airgap: bool = False
    # Weight routing: task weight -> profiles to try, in order (see autopilot.choose_profile).
    routing: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_ROUTING.items()})
    # Model prices in $/MTok that add to or override brindle's built-in ones (see brindle.pricing):
    # {"model": {"input", "output", "cache_write"?, "cache_read"?}}, merged per model: user, then repo, then config.local.json.
    pricing: dict[str, dict] = field(default_factory=dict)
    # Dollar budgets (brindle Pro; see brindle.budget): {"task_usd", "goal_usd", "month_usd", "stop"?},
    # merged per key: user, then repo, then config.local.json.
    budget: dict = field(default_factory=dict)


def _merge_commands(shared: list[str], local: object) -> list[str]:
    if isinstance(local, list):
        return [str(c) for c in local]
    if isinstance(local, dict):
        return [*local.get("before", []), *shared, *local.get("after", [])]
    return shared


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def config_root(path: str | Path) -> Path:
    """The directory whose ``.brindle`` applies to ``path``: ``path`` itself when
    it has one, else (for a linked git worktree, where the git-ignored config
    isn't checked out) the main worktree found via ``git rev-parse
    --git-common-dir`` (read from the ``.git`` file, so no git process runs)."""
    path = Path(path)
    if (path / CONFIG_DIR).is_dir():
        return path
    try:
        gitdir = Path((path / ".git").read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except (OSError, IndexError):  # not a linked worktree: nothing to discover
        return path
    main = common.parent
    return main if common.name == ".git" and (main / CONFIG_DIR).is_dir() else path


def load_repo_config(repo_root: str | Path) -> RepoConfig:
    base = config_root(repo_root) / CONFIG_DIR
    shared = _read_json(base / CONFIG_FILE)
    local = _read_json(base / LOCAL_CONFIG_FILE)
    user = user_settings()

    cfg = RepoConfig()
    for key in ("setup", "teardown", "copy", "checks", "add_dirs", "protected_paths"):
        merged = _merge_commands(list(shared.get(key, [])), local.get(key))
        setattr(cfg, key, merged)
    for key in ("base_branch", "branch_prefix", "default_agent", "fetch", "autopilot", "review",
                "reviewer", "review_profile", "pre_commit", "max_agents", "check_timeout",
                "check_concurrency",
                "usage_limit", "pool_size", "graphify", "stale_after", "pipeline",
                "review_rounds", "goal_audit", "overlap", "local_models", "merge_into",
                "auto_merge_default_branch", "delete_merged_branches", "delegation", "sidebar", "pr_footer", "plan_first", "learning",
                "learning_candidates", "limit_cooldown_minutes", "message_delivery", "permission_policy",
                "learned_rules_repeats", "learned_rules_profile"):
        for source in (local, shared, user):
            if key in source:
                setattr(cfg, key, source[key])
                break
    rules: list[str] = []
    for source in (user, shared, local):
        found = source.get("rules")
        for rule in found if isinstance(found, list) else []:
            if isinstance(rule, str) and rule.strip() and rule.strip() not in rules:
                rules.append(rule.strip())
    cfg.rules = rules
    services = local["services"] if "services" in local else shared.get("services")
    if isinstance(services, list):
        cfg.services = [s for s in services if isinstance(s, dict)]
    for source in (shared, local):  # per group, so a repo can override one and keep the rest
        plugins = source.get("plugins")
        if isinstance(plugins, dict):
            for group, name in plugins.items():
                if isinstance(group, str) and isinstance(name, str):
                    cfg.plugins[group] = name
    for source in (shared, local):  # per tier, so a repo can override one and keep the rest
        routing = source.get("routing")
        if isinstance(routing, dict):
            for tier, names in routing.items():
                if tier in WEIGHTS and isinstance(names, list):
                    cfg.routing[tier] = [n for n in names if isinstance(n, str)]
    for source in (user, shared, local):  # per model, so a repo can override one and keep the rest
        pricing = source.get("pricing")
        if isinstance(pricing, dict):
            for model, price in pricing.items():
                if isinstance(model, str) and isinstance(price, dict):
                    cfg.pricing[model] = price
    for source in (user, shared, local):  # per key, so a repo can override one and keep the rest
        budget = source.get("budget")
        if isinstance(budget, dict):
            cfg.budget.update({k: v for k, v in budget.items() if isinstance(k, str)})
    if cfg.pool_size is None:
        cfg.pool_size = 1 if cfg.setup else 0
    # Air-gap mode: either file may turn it on, and neither may turn it off.
    cfg.airgap = bool(shared.get("airgap", False)) or bool(local.get("airgap", False))
    if cfg.airgap:
        from brindle import airgap

        airgap.arm()
    return cfg


TEMPLATE = {
    "setup": [],
    "teardown": [],
    "copy": [],
    "base_branch": None,
    "branch_prefix": "",
    "default_agent": "developer",
    "fetch": True,
    "checks": [],
}


def user_settings() -> dict:
    return _read_json(user_config_path())


def set_user(key: str, value: object) -> Path:
    """Set one key in ``~/.brindle/config.json`` (other keys are kept)."""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_json(path)
    data[key] = value
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _sync_setting(key)
    return path


def _sync_setting(key: str) -> None:
    """Push a changed user-wide setting to brindle Pro (settings sync) when
    entitled; never raises, never waits more than a few seconds."""
    try:
        from brindle.pro import settings_sync

        if key in settings_sync.SYNCED_KEYS:
            settings_sync.push_soon()
    except Exception:  # noqa: BLE001 - sync is a convenience
        pass


def set_local(repo_root: str | Path, key: str, value: object) -> Path:
    """Set one key in ``.brindle/config.local.json`` (gitignored: this person's
    own settings; other keys are kept)."""
    base = config_root(repo_root) / CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / LOCAL_CONFIG_FILE
    data = _read_json(path)
    data[key] = value
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCAL_CONFIG_FILE}\n", encoding="utf-8")
    return path


def write_template(repo_root: str | Path, values: dict | None = None) -> Path:
    """Write ``.brindle/config.json`` (the template, with ``values`` over it)
    unless it already exists, and the ``.gitignore`` for the local file."""
    base = Path(repo_root) / CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / CONFIG_FILE
    if not path.exists():
        path.write_text(json.dumps({**TEMPLATE, **(values or {})}, indent=2) + "\n", encoding="utf-8")
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCAL_CONFIG_FILE}\n", encoding="utf-8")
    return path
