"""Paths and per-repo configuration.

Repo config lives in ``<repo>/.brindle/config.json`` (committed, shared with the
team) and ``<repo>/.brindle/config.local.json`` (gitignored, personal). Local
keys override shared ones; for command lists, local may instead give
``{"before": [...], "after": [...]}`` to wrap the team's commands.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
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


WORKTREES_DIR = "worktrees"


def worktrees_dir() -> Path:
    """``~/.brindle/worktrees``, where worktrees went before they moved under
    each repo (see ``worktree_root``). Existing ones there keep working."""
    return brindle_home() / WORKTREES_DIR


def worktree_root(repo_root: str | Path, cfg: RepoConfig | None = None) -> Path:
    """Where ``repo_root``'s new worktrees go: ``<repo>/.brindle/worktrees``,
    or the repo's ``worktree_root`` (``~`` expanded; relative to the repo)."""
    configured = (cfg or load_repo_config(repo_root)).worktree_root
    if not configured:
        return Path(repo_root) / CONFIG_DIR / WORKTREES_DIR
    return Path(repo_root) / os.path.expanduser(configured)


def user_profiles_dir() -> Path:
    return brindle_home() / "agents"


def user_config_path() -> Path:
    """``~/.brindle/config.json``: this person's defaults for every repo. A
    repo's own .brindle/config.json and config.local.json override it."""
    return brindle_home() / CONFIG_FILE


WEIGHTS = ("light", "medium", "heavy", "advanced")
ROUTING_KINDS = (*WEIGHTS, "reviewer", "supervisor")   # the kinds of work the app routes and can lock
EFFORTS = ("low", "medium", "high", "xhigh", "max")
KIND_OVERRIDE_FIELDS = ("effort", "instructions")   # what the owner can set per kind, on top of its profile
WEIGHT_ALIASES = {
    "reasoning": "advanced",
    "complex": "advanced",
    "complex reasoning": "advanced",
    "deep": "advanced",
}


def normalize_weight(weight: str | None) -> str | None:
    """Normalize a weight string, resolving aliases like 'reasoning' or 'complex' to 'advanced'."""
    if not weight:
        return None
    w = weight.strip().lower()
    return WEIGHT_ALIASES.get(w, w)


# The first profile of each tier is its baseline; the rest are candidates
# brindle Pro's learner may pick instead when its evidence says they do better.
# Nothing here prefers another provider for its own sake: all-Claude, all-Codex
# or all-Gemini may well be best, and that's the learner's call.
DEFAULT_ROUTING = {
    "light": ["developer-sonnet", "developer-light", "developer", "developer-codex", "developer-local"],
    "medium": ["developer-sonnet", "developer", "developer-codex", "developer-heavy"],
    "heavy": ["developer-heavy", "developer-gemini", "developer", "developer-codex"],
    "advanced": ["developer-fable", "developer-heavy", "developer-gemini"],
}


@dataclass
class RepoConfig:
    setup: list[str] = field(default_factory=list)
    teardown: list[str] = field(default_factory=list)
    copy: list[str] = field(default_factory=list)
    base_branch: str | None = None
    branch_prefix: str = ""
    worktree_root: str | None = None   # where new worktrees go (None: <repo>/.brindle/worktrees; see worktree_root())
    default_agent: str = "developer"
    fetch: bool = True
    auto_update: bool = False          # upgrade the PyPI tool at session start
    # Autopilot and merge gates.
    autopilot: bool = True             # `brindle` starts the supervisor with autopilot on
    checks: list[str] = field(default_factory=list)  # must pass in a branch before it merges
    check_scopes: dict = field(default_factory=dict)  # command -> brindle.check_scope.CheckScope, from "check_scopes" (or "checks" objects)
    scoped_checks: str | None = None   # "off", "infer" or "explicit"; None: not chosen (acts as "off" this release, see brindle.check_scope.infers)
    review: bool | None = None         # require a reviewer's approval (None: only under autopilot)
    reviewer: str = "reviewer"         # agent profile that reviews branches
    review_profile: str | None = None  # force request_review's profile (skips its automatic cross-model pick)
    # Weight -> reviewer profile for a second, independent review of work of that weight;
    # the pipeline merges only when both approve. Empty: one review (see brindle.pipeline).
    second_review: dict[str, str] = field(default_factory=dict)
    pre_commit: bool = True            # run pre-commit (the framework) over a branch before merging
    max_agents: int = 5                # workers running at once per session; 0 means no cap
    check_timeout: int = 900           # seconds allowed for each check command
    check_concurrency: int = 4         # check runs at once on this machine, across branches; the rest queue (0: no cap)
    usage_limit: int = 90              # autopilot stops pushing on at this % of the Claude usage limit
    limit_cooldown_minutes: int | None = None  # how long a provider that hit its limit counts as unavailable (default 300 for Antigravity)
    graphify: bool | None = None       # point agents at graphify-out/graph.json (None: if it's there)
    graphify_auto_refresh: bool = True  # rebuild untracked code maps automatically; queries work when false
    stale_after: int = 30              # minutes before an idle, reported worker is closed; 0: never
    pipeline: bool = True              # brindle reviews and merges reported branches itself (brindle.pipeline)
    review_start_delay: int = 60       # seconds before recovering a reported branch with no review in progress
    goal_audit: bool = True            # an adversarial reviewer checks a finished goal against the original request before it counts as reached
    review_rounds: int = 2             # fix-and-re-review rounds the pipeline runs before asking the supervisor
    # Retry a failed heavy task once on developer-heavy (Fable) before asking the supervisor.
    # None: on, except where the worker runs on a cloud account (Bedrock, Vertex, Foundry),
    # which may not have Fable; true or false forces it (see brindle.pipeline.fable_enabled).
    fable_escalation: bool | None = None
    merge_into: str | None = None      # branch worker branches are cut from and merge into (None: the supervisor's / default branch)
    auto_merge_default_branch: bool = False  # let the pipeline merge into the repo's default branch on its own
    delegation: str = "balanced"      # how readily a supervisor hands work to workers: "conservative" (save tokens), "balanced" or "fast" (save time)
    max_weight: str | None = None      # ceiling for weight routing: "light", "medium", "heavy", "advanced", or None (unlimited)
    plan_first: bool = False           # workers propose a plan and wait for approval before editing
    # "on": brindle answers workers' permission requests by its rules (see
    # brindle.permissions). Unset, it's "on" for Pro and up and "off" on Free.
    permission_policy: str = "off"
    # Seconds brindle's permission hook holds a request left to the person for
    # the app's answer (brindle.agents._hold_for_app): a day, so in practice
    # until the person answers it, sends it to the terminal, or the agent is
    # closed. By provider:
    #   Claude Code  PermissionRequest hook; timeout written as this + 30s (docs:
    #                600s default, no maximum). Its own prompt shows meanwhile
    #                (seen in 2.1.304); on timeout that prompt is what's left.
    #   Codex        PermissionRequest hook (permission_policy on, hook trusted);
    #                timeout codex_hook.TIMEOUT, a day + 30s (Codex caps only
    #                SessionEnd hooks), so a longer hold is cut to fit. Whether
    #                its prompt shows meanwhile: not seen yet.
    #   Antigravity  PreToolUse hook under agy_approvals "brindle" only; timeout
    #                written as this + 30s (agy documents no maximum; unverified).
    #                No prompt of its own: on timeout the call is refused. Under
    #                "prompt", agy ignores a hook's allow: answered in its pane.
    #   OpenCode     its plugin can only deny, never allow: answered in its pane.
    #   Native       brindle's own loop: no timeout but this; no prompt of its
    #                own, so on timeout the call is refused.
    permission_hold_seconds: int = 86400
    # "brindle": agy agents run with --dangerously-skip-permissions and
    # brindle's hook allows or denies every call (see brindle.antigravity).
    # "prompt": agy asks as usual. None (unset): "brindle" for agents brindle
    # runs, "prompt" for an interactive one, where a person answers.
    agy_approvals: str | None = None
    # A task whose files overlap a running one: "block", "warn" or "queue" (wait
    # for it to merge). None: block within a session, queue behind another session's.
    overlap: str | None = None
    coordinator: bool = False          # wake a coordinator above this repo's sessions on events (brindle Pro; see brindle.coordinator)
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
    # Directories outside the worktree that workers may read (not write), e.g. a
    # gitignored results/ folder in the main checkout.
    worker_extra_read_dirs: list[str] = field(default_factory=list)
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
    # Open a session's PR on its own once it is ready (never where the org's policy
    # requires a human review; see brindle.daemon.session_review.auto_pr).
    auto_pr: bool = False
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
    # Effective tier's origin for doctor and routing-skip diagnostics; not a setting.
    routing_sources: dict[str, str] = field(default_factory=lambda: {k: "DEFAULT_ROUTING" for k in WEIGHTS})
    exclude_providers: list[str] = field(default_factory=list)  # providers weight routing never picks
    # Kind of work -> whether hosted learning may never override it. Per kind, so a scope can
    # unlock one a lower scope locked and a write to one kind leaves the others as they are.
    routing_locked: dict[str, bool] = field(default_factory=dict)
    # Kind of work -> {"effort", "instructions"} applied to whatever runs that kind at launch,
    # merged per kind and field: user, then repo, then config.local.json.
    routing_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    reviewer_fallbacks: list[str] = field(default_factory=list)   # reviewer profiles to try, in order, when the reviewer can't run
    supervisor_profile: str | None = None      # the profile whose provider the supervisor runs on (None: the supervisor profile's own)
    supervisor_fallbacks: list[str] = field(default_factory=list)  # tried in order when that provider can't run it
    # Model prices in $/MTok that add to or override brindle's built-in ones (see brindle.pricing):
    # {"model": {"input", "output", "cache_write"?, "cache_read"?}}, merged per model: user, then repo, then config.local.json.
    pricing: dict[str, dict] = field(default_factory=dict)
    # Dollar budgets (brindle Pro; see brindle.budget): {"task_usd", "goal_usd", "month_usd", "stop"?},
    # merged per key: user, then repo, then config.local.json.
    budget: dict = field(default_factory=dict)


def _merge_commands(shared: list[str], local: object) -> list[str]:
    if isinstance(local, list):
        return [c if isinstance(c, dict) else str(c) for c in local]   # a "checks" entry may be an object
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
    main = linked_main(path)
    return main if main is not None and (main / CONFIG_DIR).is_dir() else path


def linked_main(path: str | Path) -> Path | None:
    """The main checkout of the linked worktree at ``path``, or None when
    ``path`` isn't one. A linked worktree's ``.git`` is a file saying
    ``gitdir: <main>/.git/worktrees/<name>``, and that folder's ``commondir``
    names ``<main>/.git`` (relative to it), so no git process runs."""
    path = Path(path)
    try:
        gitdir = path / (path / ".git").read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except (OSError, IndexError):  # no .git file, or not a worktree's gitdir
        return None
    return common.parent if common.name == ".git" else None


def _default_permission_policy() -> str:
    """"on" for Pro and up (so approvals are recorded and learned), "off" on
    Free. Fails closed: no license, or any error, means "off"."""
    try:
        from brindle.pro import license

        return "on" if license.has("learned_rules") else "off"
    except Exception:  # noqa: BLE001 -- an unreadable license is Free
        return "off"


CHECK_RAM_GB = 2        # RAM budgeted per concurrent check run
CHECK_CONCURRENCY_CAP = 6


def _memory_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError):
        return 0.0


def check_xdist_workers(slots: int) -> int:
    """pytest-xdist workers each of ``slots`` concurrent checks may start, so
    that all the slots together stay within the logical cores."""
    return max(1, (os.cpu_count() or 1) // max(1, slots))


def host_check_concurrency() -> int:
    """Check runs at once when the repo doesn't say: half the logical cores and
    RAM for ``CHECK_RAM_GB`` per run, never below 2 or above the cap (#169).
    Each run's xdist workers are capped by ``check_xdist_workers``, so the
    slots share the cores instead of each taking all of them."""
    cores = os.cpu_count() or 1
    by_ram = int(_memory_gb() // CHECK_RAM_GB)
    return max(2, min(cores // 2, by_ram, CHECK_CONCURRENCY_CAP))


def locked_kinds(found: object) -> dict[str, bool]:
    """A ``routing_locked`` setting as {kind: locked}: a {kind: bool} map, or a
    list of locked kinds."""
    if isinstance(found, dict):
        return {k: v for k, v in found.items() if k in ROUTING_KINDS and isinstance(v, bool)}
    if isinstance(found, list):
        return {k: True for k in found if k in ROUTING_KINDS}
    return {}


def kind_overrides(found: object) -> dict[str, dict[str, str]]:
    """A ``routing_overrides`` setting as {kind: {effort?, instructions?}}, keeping only what is valid."""
    out: dict[str, dict[str, str]] = {}
    for kind, spec in (found.items() if isinstance(found, dict) else ()):
        if kind in ROUTING_KINDS and isinstance(spec, dict):
            kept = {k: v for k, v in spec.items() if k in KIND_OVERRIDE_FIELDS and isinstance(v, str) and v.strip()
                    and (k != "effort" or v in EFFORTS)}   # a hand-edited file can't pass anything else to the CLI
            if kept:
                out[kind] = kept
    return out


def load_repo_config(repo_root: str | Path) -> RepoConfig:
    base = config_root(repo_root) / CONFIG_DIR
    shared = _read_json(base / CONFIG_FILE)
    local = _read_json(base / LOCAL_CONFIG_FILE)
    user = user_settings()

    cfg = RepoConfig()
    for key in ("setup", "teardown", "copy", "checks", "add_dirs", "worker_extra_read_dirs", "protected_paths"):
        merged = _merge_commands(list(shared.get(key, [])), local.get(key))
        setattr(cfg, key, merged)
    from brindle import check_scope

    # Scopes by precedence: config.json's inline objects, its "check_scopes", then the same from config.local.json.
    def scope_layers(raw: dict) -> list[dict]:
        checks = raw.get("checks")
        lists =[checks] if isinstance(checks, list) else [checks.get("before"), checks.get("after")] if isinstance(checks, dict) else []
        inline: dict = {}
        for entries in lists:
            inline.update(check_scope.inline_scopes(entries))
        return [inline, raw["check_scopes"] if isinstance(raw.get("check_scopes"), dict) else {}]

    cfg.checks, cfg.check_scopes = check_scope.parse_checks(cfg.checks, scope_layers(shared) + scope_layers(local))
    for key in ("base_branch", "branch_prefix", "worktree_root", "default_agent", "fetch", "auto_update", "autopilot", "review",
                "reviewer", "review_profile", "pre_commit", "max_agents", "check_timeout",
                "check_concurrency",
                "usage_limit", "pool_size", "graphify", "graphify_auto_refresh", "stale_after", "pipeline",
                "review_rounds", "review_start_delay", "fable_escalation", "goal_audit", "overlap", "coordinator", "local_models", "merge_into",
                "auto_merge_default_branch", "delete_merged_branches", "delegation", "max_weight", "sidebar", "pr_footer", "auto_pr", "plan_first", "learning",
                "learning_candidates", "limit_cooldown_minutes", "message_delivery", "permission_policy",
                "permission_hold_seconds", "agy_approvals",
                "learned_rules_repeats", "learned_rules_profile", "exclude_providers",
                "reviewer_fallbacks", "supervisor_profile", "supervisor_fallbacks", "scoped_checks"):
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
    if not any("check_concurrency" in source for source in (local, shared, user)):
        cfg.check_concurrency = host_check_concurrency()
    if not any("permission_policy" in source for source in (local, shared, user)):
        cfg.permission_policy = _default_permission_policy()
    services = local["services"] if "services" in local else shared.get("services")
    if isinstance(services, list):
        cfg.services = [s for s in services if isinstance(s, dict)]
    for source in (shared, local):  # per group, so a repo can override one and keep the rest
        plugins = source.get("plugins")
        if isinstance(plugins, dict):
            for group, name in plugins.items():
                if isinstance(group, str) and isinstance(name, str):
                    cfg.plugins[group] = name
    for source, path in ((user, user_config_path()), (shared, base / CONFIG_FILE),
                         (local, base / LOCAL_CONFIG_FILE)):  # per tier, keeping the other tiers
        routing = source.get("routing")
        if isinstance(routing, dict):
            for tier, names in routing.items():
                norm_tier = normalize_weight(tier)
                if norm_tier in WEIGHTS and isinstance(names, list):
                    cfg.routing[norm_tier] = [n for n in names if isinstance(n, str)]
                    cfg.routing_sources[norm_tier] = str(path)
    for source in (user, shared, local):  # per kind, a later scope overriding an earlier one
        cfg.routing_locked.update(locked_kinds(source.get("routing_locked")))
    for source in (user, shared, local):
        for kind, fields in kind_overrides(source.get("routing_overrides")).items():
            cfg.routing_overrides.setdefault(kind, {}).update(fields)
    from brindle import worker_models

    worker_models.apply(cfg, user, str(repo_root))   # the models picked on the picker's workers screen
    for source in (shared, local):  # per weight, so a repo can override one and keep the rest
        second = source.get("second_review")
        if isinstance(second, dict):
            for tier, name in second.items():
                norm_tier = normalize_weight(tier)
                if norm_tier in WEIGHTS and isinstance(name, str) and name:
                    cfg.second_review[norm_tier] = name
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


SUPERVISOR_KEY = "supervisor"      # ~/.brindle/config.json: the CLI the person last picked for the supervisor


def supervisor_pick() -> str | None:
    """The supervisor CLI this person last picked (in the picker or with
    ``-s``), or None before the first pick."""
    pick = user_settings().get(SUPERVISOR_KEY)
    return pick if isinstance(pick, str) and pick else None


def claude_permission_mode() -> str:
    """The ``--permission-mode`` for Claude sessions whose profile sets none:
    "default" so held permission requests reach the app, unless the person
    opted into another mode (e.g. "auto") with ``claude_permission_mode``."""
    mode = user_settings().get("claude_permission_mode")
    return mode if isinstance(mode, str) and mode else "default"


_USER_WRITE = threading.Lock()


def set_user(key: str, value: object) -> Path:
    """Set one key in ``~/.brindle/config.json`` (other keys are kept)."""
    path = user_config_path()
    if os.environ.get("BRINDLE_AGENT_ID"):
        # A brindle agent's CLI call must not change the person's own settings.
        print("brindle: running as an agent, so ~/.brindle/config.json was not changed", file=sys.stderr)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _USER_WRITE:   # daemon actions run in threads: one read-modify-replace at a time
        data = _read_json(path)
        data[key] = value
        # A unique temp file replaced into place, so a reader never sees half a file.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    _sync_setting(key)
    return path


def forget_user(key: str) -> bool:
    """Remove one key from ``~/.brindle/config.json``; True once it's gone.
    Only for a setting an org applied that its fresh setup no longer has
    (``company_login.clear_env(stale=True)``): removing it only stops it
    being applied, so unlike :func:`set_user` this also runs in an agent's
    CLI call (``brindle doctor`` from a session)."""
    path = user_config_path()
    data = _read_json(path)
    if key not in data:
        return False
    del data[key]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _sync_setting(key)
    return key not in _read_json(path)


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


def set_repo(repo_root: str | Path, key: str, value: object) -> Path:
    """Set one key in the repo's shared ``.brindle/config.json`` (committed:
    everyone's settings; other keys are kept)."""
    path = scope_paths(repo_root)["repo"]
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_json(path)
    data[key] = value
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def scope_paths(repo_root: str | Path) -> dict[str, Path]:
    """The file of each settings scope for ``repo_root``: "user"
    (~/.brindle/config.json), "repo" (.brindle/config.json) and "local"
    (.brindle/config.local.json)."""
    base = config_root(repo_root) / CONFIG_DIR
    return {"user": user_config_path(), "repo": base / CONFIG_FILE, "local": base / LOCAL_CONFIG_FILE}


def setting_source(repo_root: str | Path, key: str) -> str:
    """The scope whose file sets ``key`` as load_repo_config reads it
    ("local", then "repo", then "user"), or "default"."""
    paths = scope_paths(repo_root)
    return next((s for s in ("local", "repo", "user") if key in _read_json(paths[s])), "default")


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
