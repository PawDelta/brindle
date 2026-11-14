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

from brindle import spawn

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
    autopilot: bool = True             # deprecated; the goal driver is always on
    checks: list[str] = field(default_factory=list)  # must pass in a branch before it merges
    scoped_checks: str | None = None   # "off" or "infer"; None: not chosen (acts as "infer", see brindle.check_scope.inferred)
    scoped_checks_max_files: int = 40  # a worker branch whose scoped tests exceed this runs only its own and namesake tests (0: no cap)
    review: bool | None = None         # require a reviewer's approval (None: only under autopilot)
    reviewer: str = "reviewer"         # agent profile that reviews branches
    review_profile: str | None = None  # force request_review's profile (skips its automatic cross-model pick)
    review_sizing: bool = True        # scoped context and cheaper profiles for small or docs/test-only reviews
    review_small_max_files: int = 5
    review_small_max_lines: int = 200  # additions + deletions; binary changes keep the usual reviewer
    review_risky_paths: list[str] = field(default_factory=lambda: [
        "*auth*", "*permission*", "*daemon*", "*concurr*", "*thread*", "*lock*", "*async*",
    ])
    # Weight -> reviewer profile for a second, independent review of work of that weight;
    # the pipeline merges only when both approve. Empty: one review (see brindle.pipeline).
    second_review: dict[str, str] = field(default_factory=dict)
    pre_commit: bool = True            # run pre-commit (the framework) over a branch before merging
    max_agents: int = 5                # workers running at once per session; 0 means no cap
    check_timeout: int = 900           # seconds allowed for each check command
    wait_for_room: bool = True         # a new worker or check run waits while the machine is busy; false starts it at once
    room_wait_max: int = 300           # seconds a worker waits for room before its supervisor is told once (it never starts early)
    check_concurrency: int = 4         # check runs at once on this machine, across branches; the rest queue (0: no cap)
    usage_limit: int = 90              # autopilot stops pushing on at this % of the Claude usage limit
    limit_headroom: float = 10         # percentage points reserved for a heavy task; medium uses half, light a quarter
    limit_cooldown_minutes: int | None = None  # how long a provider that hit its limit counts as unavailable (default 300 for Antigravity)
    graphify: bool | None = None       # point agents at graphify-out/graph.json (None: if it's there)
    graphify_auto_refresh: bool = True  # rebuild untracked code maps automatically; queries work when false
    stale_after: int = 30              # minutes before an idle, reported worker is closed; 0: never
    stall_alert_after: int = 10        # minutes a piped branch may sit with nothing working on it before the supervisor is told; 0: never
    long_command_alert_after: int = 1200  # seconds one shell command may run in a worker's pane before its supervisor is told (once); 0: never
    idle_supervisor_days: float = 3   # days without input before an idle supervisor is stopped (resumable); 0: never
    superseded_after: float = 6        # hours without activity before a worker whose work already landed is stopped; 0: never
    pipeline: bool = True              # brindle reviews and merges reported branches itself (brindle.pipeline)
    review_start_delay: int = 60       # seconds before recovering a reported branch with no review in progress
    goal_audit: bool = True            # an adversarial reviewer checks a finished goal against the original request before it counts as reached
    # A small request goes straight to one light/medium worker: no goal, milestones
    # or completion audit; one light review and the checks, then the merge (assign(fast_path=true)).
    fast_path: bool = True
    # The supervisor works from a compact goal state brindle keeps (brindle.supervisor_state):
    # once its context passes supervisor_restart_tokens, it restarts fresh, seeded with that state.
    supervisor_state: bool = True
    supervisor_restart_tokens: int = 0     # off by default: restarts hurt long sessions
    # Cheaper agent start: a worker's or reviewer's stable brief (role prompt, rule packs,
    # repo brief) leads its prompt and the per-task text follows; reviewers and light
    # workers get only the brindle tools their role uses and the 5-minute prompt cache.
    prompt_cache_prefix: bool = True
    review_rounds: int = 2            # suggested fix-round cap; notify the supervisor once, then keep fixing
    review_stall_stop: bool = True     # past the cap, hand repeated findings to the supervisor
    # Retry a failed heavy task once on developer-heavy (Fable) before asking the supervisor.
    # None: on, except where the worker runs on a cloud account (Bedrock, Vertex, Foundry),
    # which may not have Fable; true or false forces it (see brindle.pipeline.fable_enabled).
    fable_escalation: bool | None = None
    merge_into: str | None = None      # branch worker branches are cut from and merge into (None: the supervisor's / default branch)
    auto_merge_default_branch: bool = False  # let the pipeline merge into the repo's default branch on its own
    auto_merge_session_prs: bool = True  # merge open session PRs once the forge reports green CI and no conflicts
    delegation: str = "balanced"      # how readily a supervisor hands work to workers: "conservative" (save tokens), "balanced" or "fast" (save time)
    max_weight: str | None = None      # ceiling for weight routing: "light", "medium", "heavy", "advanced", or None (unlimited)
    plan_first: bool = False           # workers propose a plan and wait for approval before editing
    # "on": brindle answers agents' permission requests by its rules (see
    # brindle.permissions). Unset, see _default_permission_policy.
    permission_policy: str = "off"
    # Where that came from: "repo" (either of its files), "user" (the person's
    # own file) or "default" (nobody set it, so nobody chose it).
    permission_policy_source: str = "default"
    # Seconds brindle's permission hook holds a request left to the person for
    # the app's answer (brindle.agents._hold_for_app): a day, so in practice
    # until the person answers it, sends it to the terminal, or the agent is
    # closed. By provider:
    #   Claude Code  PermissionRequest hook; timeout written as this + 30s (docs:
    #                600s default, no maximum). Its own prompt shows meanwhile
    #                (seen in 2.1.304), except for an Agent-tool subagent's request,
    #                where it comes once the hook returns nothing (seen in 2.1.318);
    #                on timeout that prompt is what's left.
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
    # Seconds of that hold a worker's ask (one no person-only rule makes) waits on
    # its supervisor's answer before the person sees it (brindle.permission_routing).
    permission_supervisor_hold_seconds: int = 120
    # "brindle": agy agents run with --dangerously-skip-permissions and
    # brindle's hook allows or denies every call (see brindle.antigravity).
    # "prompt": agy asks as usual. None (unset): "brindle" for agents brindle
    # runs and for an interactive one with the policy on and the Autonomous
    # preset, "prompt" for any other interactive one, where a person answers.
    agy_approvals: str | None = None
    # A task whose files overlap a running one: "block", "warn" or "queue" (wait
    # for it to merge). None: block within a session, queue behind another session's.
    overlap: str | None = None
    # What counts as an overlap: "hunk" (default: a shared file only when the
    # regions the tasks declare, "path:120-180", or the hunks the running one
    # has changed, meet) or "file" (any shared file).
    overlap_granularity: str | None = "hunk"
    coordinator: bool | None = None    # None: on when the plan includes it; False: off, including after coordinator stop
    # Paths/globs whose merge conflicts brindle never hands to a worker to
    # resolve (migrations, lockfiles, generated code): they go to the person.
    protected_paths: list[str] = field(default_factory=list)
    # Names of processes (as Linux shows them, or argv[0]'s basename) whose
    # folder may be unreadable but which never run in a project, so they don't
    # keep a worktree from removal (see brindle.procs.cwds).
    live_check_ignore: list[str] = field(default_factory=list)
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
    shared_cargo_target: bool = True   # worktrees share one CARGO_TARGET_DIR per repo (see brindle.build_caches)
    delete_pushed_branches: bool = False  # delete the origin branch brindle pushed once its PR merged
    min_free_disk_gb: float = 10.0     # a new worker or check run waits for disk below this much free (0: never)
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
    fable_for_design_only: bool = True
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
    # Environment variables (names or globs) an agent's pane may inherit on top of
    # brindle's built-in allowlist (secrets.env_allowed), from the person's file,
    # the repo's and config.local.json, all kept. A secret-looking name never passes.
    agent_env: list[str] = field(default_factory=list)
    # "report": a pane keeps the variables off the allowlist, and each launch logs
    # their names (see agent_env_report, `brindle doctor`); "enforce": they're dropped.
    # Secret-looking names are dropped in either mode.
    agent_env_mode: str = "report"

    def routing_allows(self, name: str, design_heavy: bool = False) -> bool:
        """Explicit profile choices bypass this automatic-routing restriction."""
        return not self.fable_for_design_only or name != "developer-fable" or design_heavy


def _merge_commands(shared: list[str], local: object) -> list[str]:
    if isinstance(local, list):
        return [str(c) for c in local]
    if isinstance(local, dict):
        return [*local.get("before", []), *shared, *local.get("after", [])]
    return shared


def _read_json(path: Path, text: str | None = None) -> dict:
    if text is None and not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") if text is None else text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _read_shared(root: Path, branch: str | None) -> dict:
    """The shared config as committed on ``branch`` (the branch a session's
    workers are cut from and merge into), not whatever the checkout at
    ``root`` has checked out; the working tree's file when there's no
    branch or it isn't committed there."""
    path = root / CONFIG_DIR / CONFIG_FILE
    if branch:
        spec = f"{branch}:{CONFIG_DIR}/{CONFIG_FILE}"
        text = _committed_text(root, branch)
        if text is not None:
            return _read_json(Path(spec), text)
    return _read_json(path)


# The committed config text per (repo, commit sha): a commit's content never
# changes, so a new commit on the branch is simply a new key. Bounded LRU,
# process-local, locked. None records "no config committed there".
_COMMITTED: dict[tuple[str, str], str | None] = {}
_COMMITTED_MAX = 256
_COMMITTED_LOCK = threading.Lock()


def _branch_sha(root: Path, branch: str) -> str | None:
    """The commit ``branch`` names, read from the git files as git would
    resolve the name (``status_cache._short_ref``), with no git run: the
    cull reads a session's config every minute. None when it can't be read
    that way (a tag, which may need peeling; a sha; reftable), then git says."""
    from brindle import status_cache

    try:
        private = status_cache._worktree_gitdir(root)
        found = status_cache._short_ref(private, status_cache._common_gitdir(private), branch)
    except (OSError, ValueError, IndexError):
        return None
    if found is None or not found[0].startswith(("refs/heads/", "refs/remotes/")):
        return None
    return found[1]


def _committed_text(root: Path, branch: str) -> str | None:
    sha = _branch_sha(root, branch)
    if sha is None:
        try:
            rev = spawn.read(["git", "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"], cwd=root)
        except OSError:
            return None
        sha = rev.stdout.strip()
        if rev.returncode != 0 or not sha:
            return None
    key = (str(root), sha)
    with _COMMITTED_LOCK:
        if key in _COMMITTED:
            text = _COMMITTED.pop(key)
            _COMMITTED[key] = text    # most recently used last
            return text
    try:
        proc = spawn.read(["git", "show", f"{sha}:{CONFIG_DIR}/{CONFIG_FILE}"], cwd=root)
    except OSError:
        return None
    text = proc.stdout if proc.returncode == 0 else None
    with _COMMITTED_LOCK:
        _COMMITTED[key] = text
        while len(_COMMITTED) > _COMMITTED_MAX:
            _COMMITTED.pop(next(iter(_COMMITTED)))
    return text


def workspace_config(ws, *, session: bool = False) -> RepoConfig:
    """``load_repo_config`` for workspace ``ws``, reading the shared file
    from its base branch (see ``_read_shared``). A session's own workspace
    (``session``) reads its own branch; its workers' base is that branch."""
    return load_repo_config(ws.repo_root, branch=ws.branch if session and ws.kind == "worktree" else ws.base_branch)


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


# ~/.brindle/config.json: taking the recommended preset records the policy here
# rather than as ``permission_policy``, because it isn't the person's choice of
# the switch: an explicit value always beats it, and only an explicit one makes
# brindle refuse a Codex launch it can't enforce (providers.codex_sandbox_base).
POLICY_DEFAULT_KEY = "permission_policy_default"


def _default_permission_policy(user: dict) -> str:
    """The permission policy when nobody set ``permission_policy``: "on", on
    every plan and whatever the license says, for new installs and earlier ones
    alike, so no one's agents are left to ask about everything. Reading no license
    means an air-gapped or broken one gets the same rules: they're checked here,
    from local files, and only ever add denies and asks to what would run anyway.
    The test suite's homes opt out, so a test says when it wants the policy."""
    if user.get(POLICY_DEFAULT_KEY) == "on":
        return "on"
    return "off" if os.environ.get("BRINDLE_POLICY_DEFAULT_OFF") else "on"


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


def load_repo_config(repo_root: str | Path, *, user_only: bool = False, branch: str | None = None) -> RepoConfig:
    """The settings for ``repo_root``: its local file over its shared one over
    the person's ~/.brindle/config.json. ``user_only``: the person's file
    alone, as every repo starts from it (the app's Agents tab). ``branch``:
    read the shared file as committed there (see ``_read_shared``)."""
    base = config_root(repo_root) / CONFIG_DIR
    shared = {} if user_only else _read_shared(base.parent, branch)
    local = {} if user_only else _read_json(base / LOCAL_CONFIG_FILE)
    user = user_settings()

    cfg = RepoConfig()
    for key in ("setup", "teardown", "copy", "checks", "add_dirs", "worker_extra_read_dirs", "protected_paths",
                "live_check_ignore"):
        merged = _merge_commands(list(shared.get(key, [])), local.get(key))
        setattr(cfg, key, merged)
    for key in ("base_branch", "branch_prefix", "worktree_root", "default_agent", "fetch", "auto_update", "review",
                "reviewer", "review_profile", "pre_commit", "max_agents", "check_timeout",
                "review_sizing", "review_small_max_files", "review_small_max_lines", "review_risky_paths",
                "check_concurrency", "wait_for_room", "room_wait_max", "min_free_disk_gb", "delete_pushed_branches",
                "usage_limit", "limit_headroom", "pool_size", "graphify", "graphify_auto_refresh", "stale_after", "stall_alert_after", "pipeline",
                "long_command_alert_after", "idle_supervisor_days", "superseded_after",
                "review_rounds", "review_stall_stop", "review_start_delay", "fable_escalation", "fable_for_design_only", "goal_audit", "fast_path",
                "supervisor_state", "supervisor_restart_tokens", "prompt_cache_prefix", "overlap",
                "overlap_granularity", "coordinator", "local_models", "merge_into",
                "auto_merge_default_branch", "auto_merge_session_prs", "delete_merged_branches", "shared_cargo_target", "delegation", "max_weight", "sidebar", "pr_footer", "plan_first", "learning",
                "learning_candidates", "limit_cooldown_minutes", "message_delivery", "permission_policy",
                "permission_hold_seconds", "permission_supervisor_hold_seconds", "agy_approvals",
                "learned_rules_repeats", "learned_rules_profile", "exclude_providers",
                "reviewer_fallbacks", "supervisor_profile", "supervisor_fallbacks", "scoped_checks",
                "scoped_checks_max_files", "agent_env_mode"):
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
    cfg.agent_env = []
    for source in (user, shared, local):
        found = source.get("agent_env")
        for name in found if isinstance(found, list) else []:
            if isinstance(name, str) and name.strip() and name.strip() not in cfg.agent_env:
                cfg.agent_env.append(name.strip())
    if not any("check_concurrency" in source for source in (local, shared, user)):
        cfg.check_concurrency = host_check_concurrency()
    if "permission_policy" in local or "permission_policy" in shared:
        cfg.permission_policy_source = "repo"
    elif "permission_policy" in user:
        cfg.permission_policy_source = "user"
    else:
        cfg.permission_policy = _default_permission_policy(user)
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

    # The models picked on the picker's workers screen (the person's own profiles, for user_only).
    worker_models.apply(cfg, user, str(repo_root), user_library=user_only)
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


def visuals_setting() -> str:
    """Whether brindle may offer a visual; publication always needs a request."""
    return "on" if user_settings().get("visuals") == "on" else "off"


def visuals_enabled_setting() -> bool:
    """The visuals capability as a person setting: on unless set to "off"."""
    return user_settings().get("visuals_enabled") != "off"


def login_shell_env_setting() -> bool:
    """Whether the provider CLIs the daemon launches get the proxy and certificate
    settings of the person's login shell (brindle.login_shell_env): on unless
    ``login_shell_env`` is "off"."""
    return user_settings().get("login_shell_env") != "off"


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


OPENCODE_PERMISSION_MODES = ("ask", "auto", "autonomous")


def opencode_permission_mode() -> str:
    """How OpenCode sessions whose profile sets no ``permission_mode`` ask
    (brindle.opencode): "ask" (the default) keeps OpenCode's own prompts for
    edits and commands; "auto" lets edits run; "autonomous" asks nothing and
    brindle's plugin decides each call, failing closed. Anything else is "ask"."""
    mode = user_settings().get("opencode_permission_mode")
    return mode if mode in OPENCODE_PERMISSION_MODES else "ask"


_USER_WRITE = threading.Lock()


def set_user(key: str, value: object) -> Path:
    """Set one key in ``~/.brindle/config.json`` (other keys are kept)."""
    return update_user(key, lambda _: value)


def update_user(key: str, change) -> Path:
    """Set one key in ``~/.brindle/config.json`` to ``change(its value now)``,
    read and written under the one writer lock, so a change to part of a
    nested setting never loses another made at the same time."""
    path = user_config_path()
    if os.environ.get("BRINDLE_AGENT_ID"):
        # A brindle agent's CLI call must not change the person's own settings.
        print("brindle: running as an agent, so ~/.brindle/config.json was not changed", file=sys.stderr)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _USER_WRITE:   # daemon actions run in threads: one read-modify-replace at a time
        data = _read_json(path)
        data[key] = change(data.get(key))
        _replace_json(path, data)
    _sync_setting(key)
    return path


def _replace_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` through a unique temp file in the same
    directory, replaced into place: a reader (every MCP server looks at the
    user config every few seconds) never sees half a file, and a failed
    write leaves the old one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def forget_user(key: str) -> bool:
    """Remove one key from ``~/.brindle/config.json``; True once it's gone.
    Only for a setting an org applied that its fresh setup no longer has
    (``company_login.clear_env(stale=True)``): removing it only stops it
    being applied, so unlike :func:`set_user` this also runs in an agent's
    CLI call (``brindle doctor`` from a session)."""
    path = user_config_path()
    with _USER_WRITE:
        data = _read_json(path)
        if key not in data:
            return False
        del data[key]
        _replace_json(path, data)
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


_REPO_WRITE = threading.Lock()


def _change_json(path: Path, change) -> None:
    """Read ``path``, apply ``change`` to its dict and replace the file, one at a
    time (daemon actions run in threads) through a unique temp file in the same
    directory, so a reader never sees half a file and no write is lost."""
    with _REPO_WRITE:
        data = _read_json(path)
        change(data)
        _replace_json(path, data)


def set_local(repo_root: str | Path, key: str, value: object) -> Path:
    """Set one key in ``.brindle/config.local.json`` (gitignored: this person's
    own settings; other keys are kept)."""
    base = config_root(repo_root) / CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / LOCAL_CONFIG_FILE
    _change_json(path, lambda data: data.__setitem__(key, value))
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCAL_CONFIG_FILE}\n", encoding="utf-8")
    return path


def set_repo(repo_root: str | Path, key: str, value: object) -> Path:
    """Set one key in the repo's shared ``.brindle/config.json`` (committed:
    everyone's settings; other keys are kept)."""
    path = scope_paths(repo_root)["repo"]
    path.parent.mkdir(parents=True, exist_ok=True)
    _change_json(path, lambda data: data.__setitem__(key, value))
    return path


def unset_local(repo_root: str | Path, key: str) -> Path:
    """Remove one key from ``.brindle/config.local.json`` (other keys are kept),
    so the repo's shared file or the person's own value reads through again."""
    path = scope_paths(repo_root)["local"]
    if path.exists():
        _change_json(path, lambda data: data.pop(key, None))
    return path


def unset_repo(repo_root: str | Path, key: str) -> Path:
    """Remove one key from the repo's committed ``.brindle/config.json``."""
    path = scope_paths(repo_root)["repo"]
    if path.exists():
        _change_json(path, lambda data: data.pop(key, None))
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


def local_values(repo_root: str | Path, keys: tuple[str, ...]) -> dict:
    """What the repo's local file itself sets for ``keys``: the effective
    config unions ``rules`` across scopes, so editing them needs the file's own list."""
    local = _read_json(scope_paths(repo_root)["local"])
    return {k: local[k] for k in keys if k in local}


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
