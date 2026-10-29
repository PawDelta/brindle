"""The ``pro`` policy plugin (``brindle.policy`` group): enforce a team org's
policy on delegations and merges.

* No team entitlement (not logged in, no ``team`` feature): everything is
  allowed; this plugin only enforces what an org has set.
* With one, the org's policy is used at the entitlement's
  ``policy_version``: from the on-disk cache (``$BRINDLE_HOME/pro/
  policy-v2-<org>.json``, 0600) when it is that version or newer, else fetched
  from ``GET /orgs/{org_id}/policy`` and cached. If the fetch fails, the last
  good copy is used; if there has never been one, everything is denied with
  a message saying how to fix it.
* The answer may carry per-role overrides and ``effective``: the flat policy
  for the calling member after theirs. That one is enforced when present,
  else the base policy. A role change bumps no version, so the cached copy
  is also keyed by the entitlement's ``role`` and ``policy_role``.
* ``check_assign`` denies a provider, model or profile outside the allowed
  lists (and an undeclared one when a list is set), and a new worker once the repo
  already has ``max_parallel_workers`` at work (or when brindle couldn't
  count them). ``check_merge`` denies a merge that
  wasn't asked for by the user (the pipeline's auto-merge, a supervisor or
  any other agent) when ``require_human_review`` is set.

brindle itself fails closed if this plugin raises.
"""

from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from urllib.parse import urlsplit

from brindle.policy import AssignInfo, Decision, MergeInfo, PolicyPlugin, allow, deny

from brindle.pro._files import private_dir, read_private, write_private
from brindle.pro.status import clean

log = logging.getLogger(__name__)

FEATURE = "team"
FETCH_TIMEOUT = 5.0
MAX_CACHE = 64 * 1024
ORG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
BUDGETS_FEATURE = "org_budgets"
MANAGED_FEATURE = "managed_models"
MAX_PROTECTED_PATHS = 64
PROVIDERS = ("bedrock", "vertex", "azure", "openai-compatible", "anthropic")
KEY_PROVIDERS = ("anthropic", "openai-compatible")    # the providers an org-owned key can be set for
KEY_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class PolicyUnavailable(Exception):
    pass


DEFAULT_VISIBILITY = {"own_dollars": True, "team_spend": True, "org_spend": True}
LISTS = ("allowed_providers", "allowed_models", "allowed_profiles")
RULES = LISTS + ("require_human_review", "max_parallel_workers")


@dataclass(frozen=True)
class ProviderConfig:
    """Enterprise ``managed_models``: the endpoint the org's workers run on
    (non-secret; the credentials come from the machine's own cloud sign-in)."""
    provider: str
    region: str | None = None
    model_ids: tuple[str, ...] = ()
    endpoint: str | None = None
    project: str | None = None     # vertex: the GCP project id
    # anthropic / openai-compatible: where the org's own key comes from. Never
    # the key itself: ``key_env`` names an org variable on the person's machine,
    # ``key_helper`` is a command (argv, no shell) run there at pane start.
    key_env: str | None = None
    key_helper: tuple[str, ...] | None = None

    def to_json(self) -> dict:
        return {"provider": self.provider, "region": self.region,
                "model_ids": list(self.model_ids), "endpoint": self.endpoint,
                "project": self.project, "key_env": self.key_env,
                "key_helper": list(self.key_helper) if self.key_helper else None}


AGENT_CLAUDE_ROUTES = ("subscription", "console", "bedrock", "vertex", "foundry", "gateway")
AGENT_CODEX_ROUTES = ("chatgpt", "api_key", "azure", "gateway")
AGENT_ANTIGRAVITY_ROUTES = ("gemini", "gateway")
# The sub-block a Claude route needs; the others are pruned from the parsed block.
CLAUDE_ROUTE_BLOCK = {"bedrock": "aws", "vertex": "gcp", "foundry": "azure", "gateway": "gateway"}
CLAUDE_ORG_ROUTES = ("subscription", "console")     # the routes that sign in to a Claude org
# The Claude routes that point Claude Code at a cloud or an endpoint of the org's
# (the rest sign in to a Claude org), and the Codex routes that point Codex at
# an endpoint (``base_url``): the routes an enforced policy manages as
# ``provider_config`` always did (see brindle.pro.managed_models).
CLAUDE_CLOUD_ROUTES = ("bedrock", "vertex", "foundry", "gateway")
CODEX_ENDPOINT_ROUTES = ("azure", "gateway")
ANTIGRAVITY_ENDPOINT_ROUTES = ("gateway",)
CLAUDE_MODEL_ROUTES = ("console", "bedrock", "vertex", "foundry", "gateway")   # model_ids kept
FEDERATED_ROUTES = ("bedrock", "vertex", "foundry")  # the routes ``auth: federated`` applies to
DEFAULT_MAX_SESSION_AGE = 86400
AGENT_ENFORCE_FEATURE = MANAGED_FEATURE             # ``enforce`` needs this Enterprise entitlement
MAX_MODEL_IDS = 32


@dataclass(frozen=True)
class AwsFederation:
    """``auth: federated`` on Bedrock: the role PawDelta's ID token assumes."""
    role_arn: str
    max_session_age: int = DEFAULT_MAX_SESSION_AGE


@dataclass(frozen=True)
class GcpFederation:
    project_number: str
    pool_id: str
    provider_id: str
    service_account: str
    max_session_age: int = DEFAULT_MAX_SESSION_AGE


@dataclass(frozen=True)
class AzureFederation:
    tenant_id: str
    client_id: str
    max_session_age: int = DEFAULT_MAX_SESSION_AGE


@dataclass(frozen=True)
class AwsSetup:
    """The AWS account Bedrock is called in. The SSO group (``sso_start_url``,
    ``sso_region``, ``account_id``, ``role_name``) comes all four or none:
    without it, the machine's own AWS sign-in applies (no profile is written
    and no account is matched)."""
    region: str
    sso_start_url: str | None = None
    sso_region: str | None = None
    account_id: str | None = None
    role_name: str | None = None

    @property
    def sso(self) -> bool:
        return self.sso_start_url is not None
    federation: AwsFederation | None = None     # no sso_* when federated


@dataclass(frozen=True)
class GcpSetup:
    project: str
    region: str
    federation: GcpFederation | None = None


@dataclass(frozen=True)
class AzureSetup:
    """A Foundry resource (``resource``, with its ``subscription_id``) or the
    endpoint itself (``base_url``); at least one of the two."""
    subscription_id: str | None = None
    resource: str | None = None
    base_url: str | None = None
    federation: AzureFederation | None = None


@dataclass(frozen=True)
class GatewaySetup:
    base_url: str


@dataclass(frozen=True)
class ModelsSetup:
    opus: str | None = None
    sonnet: str | None = None
    haiku: str | None = None


@dataclass(frozen=True)
class ClaudeSetup:
    route: str
    org_id: str | None = None
    aws: AwsSetup | None = None
    gcp: GcpSetup | None = None
    azure: AzureSetup | None = None
    gateway: GatewaySetup | None = None
    models: ModelsSetup | None = None
    # The model ids the route serves (Bedrock ids or ARNs, Vertex ids, ...) and
    # the one panes start on (``ANTHROPIC_MODEL``; it is one of model_ids when
    # those are set). Kept for every route but subscription.
    model_ids: tuple[str, ...] = ()
    default_model: str | None = None
    auth: str = "own_login"         # "federated": short-lived cloud credentials through PawDelta

    @property
    def federated(self) -> bool:
        return self.auth == "federated"


@dataclass(frozen=True)
class CodexSetup:
    route: str
    base_url: str | None = None     # azure, gateway: the endpoint (``OPENAI_BASE_URL``)


@dataclass(frozen=True)
class AntigravitySetup:
    route: str
    base_url: str | None = None     # gateway: the endpoint (``GOOGLE_GEMINI_BASE_URL``)


@dataclass(frozen=True)
class AgentSetup:
    """Org policy ``agent_setup``: how the org's workers sign in to Claude and
    Codex. Names only, never a secret. ``enforce`` is False unless the org has
    the Enterprise entitlement. ``deny_personal_keys`` (Enterprise too) is
    None when the block doesn't set it (an older server): the policy's
    top-level field applies then (see brindle.pro.managed_models).
    ``exclusive`` (with ``enforce``): an agent the block gives no route is
    refused, as ``provider_config`` refused every agent it couldn't serve;
    the server sets it when it converts a ``provider_config``. None when the
    block doesn't carry the key at all: a server older than routing in
    ``agent_setup``, whose ``provider_config`` stays the managed source."""
    claude: ClaudeSetup | None = None
    codex: CodexSetup | None = None
    antigravity: AntigravitySetup | None = None
    enforce: bool = False
    deny_personal_keys: bool | None = None
    exclusive: bool | None = None

    @property
    def routing(self) -> bool:
        """Whether this block is the org's routing (a phase-1 server, which
        always sends ``exclusive``), rather than login help next to a
        ``provider_config`` that is still the managed source."""
        return self.exclusive is not None

    @property
    def routed(self) -> bool:
        """Whether an agent's route points at a cloud or an endpoint of the
        org's: what ``enforce`` manages as ``provider_config`` always did."""
        return bool((self.claude is not None and self.claude.route in CLAUDE_CLOUD_ROUTES)
                    or (self.codex is not None and self.codex.route in CODEX_ENDPOINT_ROUTES
                        and self.codex.base_url)
                    or (self.antigravity is not None and self.antigravity.route in ANTIGRAVITY_ENDPOINT_ROUTES
                        and self.antigravity.base_url))


@dataclass(frozen=True)
class OrgControl:
    """One org control over the members' permission defaults: ``decision``
    deny (Block) or ask (Ask) for a whole grouping (``group``, a
    brindle.permission_grants.ORG_GROUPINGS id: a command family, network
    fetches, deploys, package installs, unchecked commands) or for one
    pattern (``rule``: "<kind> <match_type> <match>", a rule kind and a
    match type or named matcher). ``reason`` is shown to the member and the
    agent, in plain words."""
    decision: str
    reason: str = ""
    group: str | None = None
    rule: str | None = None


@dataclass(frozen=True)
class GrantPolicy:
    """Org policy ``permission_grants``: which one-time and time-limited
    permission grants a member may give their workers from the app
    (brindle.permission_grants), and the org's own permission denies, which
    nothing a member does lifts. A role's grant block replaces the whole
    inherited block, including its denies."""
    allow: bool
    kinds: tuple[str, ...]               # brindle.permissions.KINDS a grant may cover
    overridable: tuple[str, ...]         # GRANT_ACTIONS: built-in denies a person may lift once
    max_minutes: int
    max_uses: int
    deny: tuple[str, ...] = ()           # "<kind> <match_type> <match>", as a profile's permission_denies
    allow_persistent: bool = False
    lifetimes: tuple[str, ...] = ("session", "time")
    scopes: tuple[str, ...] = ("branch",)
    # Block or Ask over whole groupings and patterns, above every member's
    # preset, rules and saved approvals (permission_grants.org_decision).
    controls: tuple[OrgControl, ...] = ()
    org_name: str = ""                   # what members see the org called ("" : "your organization")


GRANT_KEYS = ("allow", "kinds", "overridable", "max_minutes", "max_uses", "deny", "allow_persistent",
              "lifetimes", "scopes", "controls", "org_name")
CONTROL_DECISIONS = ("deny", "ask")
MAX_CONTROLS = 64
MAX_CONTROL_REASON = 300
GRANT_ACTIONS = ("git-push", "env")
GRANT_LIFETIMES = ("session", "time", "forever")
GRANT_SCOPES = ("branch", "remote", "repo", "all")
MAX_GRANT_MINUTES = 1440
MAX_GRANT_USES = 1000
MAX_GRANT_DENIES = 64


@dataclass(frozen=True)
class OrgPolicy:
    org_id: str
    version: int
    allowed_providers: tuple[str, ...] | None = None
    allowed_models: tuple[str, ...] | None = None
    require_human_review: bool = False
    max_parallel_workers: int | None = None
    fetched_at: float = 0.0
    allowed_profiles: tuple[str, ...] | None = None
    roles: dict = field(default_factory=dict)      # role -> the rules it overrides
    effective: OrgPolicy | None = None             # the calling member's policy, overrides applied
    role: str | None = None                        # the caller's role and policy role,
    policy_role: str | None = None                 # as the backend answered them
    # The entitlement's (role, policy_role) this copy was fetched for: the
    # cache key next to ``version``, since a role change bumps no version.
    cached_for: tuple[str | None, str | None] | None = None
    # Team "org_budgets": USD per seat per UTC month and per goal (None: no limit),
    # globs no branch may change, and the caller's own spend this month as the
    # backend counts it (``spend``, next to the policy).
    budget_seat_month_usd: float | None = None
    budget_goal_usd: float | None = None
    budget_task_usd: float | None = None      # per task (a missing one: no limit, old servers)
    protected_paths: tuple[str, ...] = ()
    # Per-member budgets: the caller's resolved ``paused`` throttle, where each budget
    # field came from ("everyone" | "role" | "custom_role" | "member"), and, for
    # admins only, the per-member overrides.
    paused: bool = False
    paused_reason: str | None = None
    budget_sources: dict = field(default_factory=dict)
    member_overrides: dict = field(default_factory=dict)
    spend_seat_usd: float | None = None
    spend_month: str | None = None
    # Who may see dollar amounts, resolved for the caller (owners/admins: all true). Only
    # ``own_dollars`` is honored here; a missing field (older servers) means all true.
    visibility: dict = field(default_factory=lambda: dict(DEFAULT_VISIBILITY))
    # Enterprise "managed_rollout" (see brindle.pro.rollout): org-wide, so the
    # base policy's values are used for the member's effective policy too.
    min_version: str | None = None
    required_profiles: tuple[str, ...] = ()
    required_rule_packs: tuple[str, ...] = ()
    kill_switch: bool = False
    # Enterprise "managed_models": the provider the org's workers must use, and
    # whether personal API keys are stripped from their panes.
    provider_config: ProviderConfig | None = None
    deny_personal_keys: bool = False
    # The org's ``agent_setup`` block (None: absent, or malformed and ignored).
    agent_setup: AgentSetup | None = None
    # The org's ``permission_grants`` block (None: absent, so no grants at all).
    permission_grants: GrantPolicy | None = None
    # Plan rules (the server's PLAN_RULE_FIELDS, org-wide): whether workers
    # plan before editing ("auto" | "always" | "never"), globs whose changes
    # always need a plan, the members (by sub) who may approve one, and the
    # two approval switches. A policy stored before them is served at these
    # defaults, so they are optional here too.
    plan: str = "auto"
    plan_paths: tuple[str, ...] = ()
    plan_approvers: tuple[str, ...] = ()
    plan_two_person: bool = False
    plan_reviewer_may_approve_low_risk: bool = False
    # Who may approve a plan, for enforcement: one group per org whose policy
    # applies, each that org's approver list, and an approval needs an eligible
    # person from EVERY group. One org's policy is one group (none when its
    # list is empty); merging two policies keeps both groups, so neither org's
    # approvers are widened by the other's (see brindle.pro.managed.stricter).
    plan_approver_groups: tuple[tuple[str, ...], ...] = ()
    # Keep the server answer separate from the interpreted policy: newer
    # servers may send fields this client cannot model yet. Do not serialize
    # the interpreted policy back over those fields when caching it.
    raw_body: dict | None = field(default=None, repr=False, compare=False)

    @property
    def enforced(self) -> OrgPolicy:
        """The policy to enforce: the member's effective one, else the base."""
        return self.effective or self

    def rules(self) -> dict:
        return {k: _list(getattr(self, k)) if k in LISTS else getattr(self, k) for k in RULES}

    def budgets(self) -> dict:
        return {"budget": {"seat_month_usd": self.budget_seat_month_usd,
                           "goal_usd": self.budget_goal_usd,
                           "task_usd": self.budget_task_usd},
                "protected_paths": list(self.protected_paths),
                "paused": self.paused, "paused_reason": self.paused_reason,
                "budget_sources": dict(self.budget_sources),
                "member_overrides": {k: dict(v) for k, v in self.member_overrides.items()}}

    def rollout(self) -> dict:
        return {"min_version": self.min_version,
                "required_profiles": list(self.required_profiles),
                "required_rule_packs": list(self.required_rule_packs),
                "kill_switch": self.kill_switch}

    def managed(self) -> dict:
        return {"provider_config": self.provider_config.to_json() if self.provider_config else None,
                "deny_personal_keys": self.deny_personal_keys}

    def grants(self) -> dict:
        g = self.permission_grants
        return {"permission_grants": None if g is None else {
            k: list(v) if isinstance(v, tuple) else v for k, v in asdict(g).items()}}

    def plan_rules(self) -> dict:
        return {"plan": self.plan, "plan_paths": list(self.plan_paths),
                "plan_approvers": list(self.plan_approvers), "plan_two_person": self.plan_two_person,
                "plan_reviewer_may_approve_low_risk": self.plan_reviewer_may_approve_low_risk}

    def to_json(self) -> dict:
        out = {"org_id": self.org_id, "version": self.version, "fetched_at": self.fetched_at,
               "policy": {**self.rules(), **self.budgets(), **self.rollout(), **self.managed(),
                          **self.grants(), **self.plan_rules(),
                          "agent_setup": asdict(self.agent_setup) if self.agent_setup else None,
                          "roles": {r: {k: _list(v) if k in LISTS else v
                                        for k, v in o.items()}
                                    for r, o in self.roles.items()}},
               "role": self.role, "policy_role": self.policy_role}
        if self.effective is not None:
            out["effective"] = {**self.effective.rules(), **self.effective.budgets(),
                                **self.effective.rollout(), **self.effective.managed(),
                                **self.effective.grants()}
        out["visibility"] = dict(self.visibility)
        if self.cached_for is not None:
            out["cached_for"] = list(self.cached_for)
        if self.spend_seat_usd is not None:
            out["spend"] = {"month": self.spend_month, "seat_usd": self.spend_seat_usd}
        return out


def _list(v):
    return None if v is None else list(v)


def _names(v, what: str) -> tuple[str, ...] | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v) or len(v) > 256:
        raise PolicyUnavailable(f"malformed {what}")
    return tuple(v)


def _rule(name: str, v):
    if name in LISTS:
        return _names(v, name)
    if name == "require_human_review":
        if not isinstance(v, bool):
            raise PolicyUnavailable("malformed policy")
        return v
    if not (v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 1)):
        raise PolicyUnavailable("malformed policy")
    return v


def _rules(p: dict) -> dict:
    return {k: _rule(k, p.get(k, False if k == "require_human_review" else None)) for k in RULES}


def _usd(v, what: str) -> float | None:
    if v is None:
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v < float("inf"):
        raise PolicyUnavailable(f"malformed {what}")
    return float(v)


def _budgets(p: dict) -> dict:
    """The ``org_budgets`` fields of a policy (or effective policy) dict, as
    ``OrgPolicy`` keyword arguments. Malformed ones make the policy unusable."""
    budget = p.get("budget")
    if budget is None:
        budget = {}
    if not isinstance(budget, dict):
        raise PolicyUnavailable("malformed budget")
    paths = p.get("protected_paths")
    if paths is None:
        paths = []
    if (not isinstance(paths, list) or len(paths) > MAX_PROTECTED_PATHS
            or not all(isinstance(g, str) and g.strip() for g in paths)):
        raise PolicyUnavailable("malformed protected_paths")
    paused = p.get("paused", False)
    if paused is None:
        paused = False
    if not isinstance(paused, bool):
        raise PolicyUnavailable("malformed paused")
    reason = p.get("paused_reason")
    sources = p.get("budget_sources")
    overrides = p.get("member_overrides")
    if not (sources is None or isinstance(sources, dict)) or not (
            overrides is None or isinstance(overrides, dict)):
        raise PolicyUnavailable("malformed budget sources")
    return {"budget_seat_month_usd": _usd(budget.get("seat_month_usd"), "budget"),
            "budget_goal_usd": _usd(budget.get("goal_usd"), "budget"),
            "budget_task_usd": _usd(budget.get("task_usd"), "budget"),
            "protected_paths": tuple(paths), "paused": paused,
            "paused_reason": (clean(reason)[:200] or None) if isinstance(reason, str) else None,
            "budget_sources": {k: v for k, v in (sources or {}).items()
                               if isinstance(k, str) and isinstance(v, str)},
            "member_overrides": {k: v for k, v in list((overrides or {}).items())[:4096]
                                 if isinstance(k, str) and isinstance(v, dict)}}


PLAN_MODES = ("auto", "always", "never")
MAX_PLAN_APPROVERS = 64


def _plan_rules(p: dict) -> dict:
    """The plan-rule fields of a policy dict, as ``OrgPolicy`` keyword
    arguments: each at its default when absent (a policy stored before them).
    Malformed ones make the policy unusable."""
    mode = p.get("plan", "auto")
    if mode is None:
        mode = "auto"
    if mode not in PLAN_MODES:
        raise PolicyUnavailable("malformed plan")
    paths = p.get("plan_paths")
    if paths is None:
        paths = []
    if (not isinstance(paths, list) or len(paths) > MAX_PROTECTED_PATHS
            or not all(isinstance(g, str) and g.strip() for g in paths)):
        raise PolicyUnavailable("malformed plan_paths")
    approvers = p.get("plan_approvers")
    if approvers is None:
        approvers = []
    if (not isinstance(approvers, list) or len(approvers) > MAX_PLAN_APPROVERS
            or not all(isinstance(a, str) and a.strip() for a in approvers)):
        raise PolicyUnavailable("malformed plan_approvers")
    flags = {}
    for name in ("plan_two_person", "plan_reviewer_may_approve_low_risk"):
        v = p.get(name, False)
        if v is None:
            v = False
        if not isinstance(v, bool):
            raise PolicyUnavailable(f"malformed {name}")
        flags[name] = v
    return {"plan": mode, "plan_paths": tuple(paths), "plan_approvers": tuple(approvers),
            "plan_approver_groups": (tuple(approvers),) if approvers else (), **flags}


def _rollout(p: dict) -> dict:
    """The ``managed_rollout`` fields of a policy dict, as ``OrgPolicy``
    keyword arguments. Malformed ones make the policy unusable."""
    from brindle.pro import rollout

    try:
        return rollout.parse_fields(p)
    except ValueError as e:
        raise PolicyUnavailable(str(e)) from e


def _text(v, what: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > 512 or not v.isprintable():
        raise PolicyUnavailable(f"malformed {what}")
    return v


def _managed(p: dict) -> dict:
    """The ``managed_models`` fields of a policy dict, as ``OrgPolicy`` keyword
    arguments. Malformed ones make the policy unusable (fail closed)."""
    deny = p.get("deny_personal_keys", False)
    if not isinstance(deny, bool):
        raise PolicyUnavailable("malformed deny_personal_keys")
    pc = p.get("provider_config")
    if pc is None:
        return {"provider_config": None, "deny_personal_keys": deny}
    if not isinstance(pc, dict) or pc.get("provider") not in PROVIDERS:
        raise PolicyUnavailable("malformed provider_config")
    ids = pc.get("model_ids")
    if ids is None:
        ids = []
    if (not isinstance(ids, list) or len(ids) > 32
            or not all(isinstance(i, str) and i.strip() and i.isprintable() for i in ids)):
        raise PolicyUnavailable("malformed provider_config model_ids")
    key_env, key_helper = _org_key(pc)
    return {"provider_config": ProviderConfig(
        provider=pc["provider"], region=_text(pc.get("region"), "provider_config region"),
        model_ids=tuple(ids), endpoint=_text(pc.get("endpoint"), "provider_config endpoint"),
        project=_text(pc.get("project"), "provider_config project"),
        key_env=key_env, key_helper=key_helper),
        "deny_personal_keys": deny}


def _grants(p: dict) -> dict:
    """The ``permission_grants`` field of a policy dict, as an ``OrgPolicy``
    keyword argument: None when absent. Strict: an unknown key, a missing
    one, or any malformed value makes the policy unusable (fail closed)."""
    from brindle.permissions import KINDS, MATCH_TYPES

    g = p.get("permission_grants")
    if g is None:
        return {"permission_grants": None}
    required = set(GRANT_KEYS) - {"deny", "allow_persistent", "lifetimes", "scopes", "controls", "org_name"}
    if not isinstance(g, dict) or not required <= set(g) or not set(g) <= set(GRANT_KEYS):
        raise PolicyUnavailable("malformed permission_grants")

    def names(key: str, allowed: tuple[str, ...], default=None) -> tuple[str, ...]:
        v = g.get(key, default)
        if not isinstance(v, list) or not all(isinstance(x, str) and x in allowed for x in v):
            raise PolicyUnavailable(f"malformed permission_grants.{key}")
        return tuple(dict.fromkeys(v))

    def count(key: str, most: int) -> int:
        v = g[key]
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= most:
            raise PolicyUnavailable(f"malformed permission_grants.{key}")
        return v

    deny = g.get("deny", [])
    if not isinstance(deny, list) or len(deny) > MAX_GRANT_DENIES:
        raise PolicyUnavailable("malformed permission_grants.deny")
    for spec in deny:
        parts = spec.split(None, 2) if isinstance(spec, str) else []
        if len(parts) != 3 or parts[0] not in KINDS or parts[1] not in MATCH_TYPES:
            raise PolicyUnavailable("malformed permission_grants.deny")
    if not isinstance(g["allow"], bool):
        raise PolicyUnavailable("malformed permission_grants.allow")
    if not isinstance(g.get("allow_persistent", False), bool):
        raise PolicyUnavailable("malformed permission_grants.allow_persistent")
    org_name = g.get("org_name") or ""
    if not isinstance(org_name, str) or len(org_name) > 100:
        raise PolicyUnavailable("malformed permission_grants.org_name")
    return {"permission_grants": GrantPolicy(
        allow=g["allow"], kinds=names("kinds", KINDS), overridable=names("overridable", GRANT_ACTIONS),
        max_minutes=count("max_minutes", MAX_GRANT_MINUTES), max_uses=count("max_uses", MAX_GRANT_USES),
        deny=tuple(deny), allow_persistent=g.get("allow_persistent", False),
        lifetimes=names("lifetimes", GRANT_LIFETIMES, ["session", "time"]),
        scopes=names("scopes", GRANT_SCOPES, ["branch"]), controls=_controls(g.get("controls") or []),
        org_name=org_name.strip())}


def _controls(v) -> tuple[OrgControl, ...]:
    """``permission_grants.controls``, strictly (fail closed): each names
    exactly one grouping or one pattern rule, with deny or ask."""
    from brindle.permission_grants import ORG_GROUPINGS
    from brindle.permissions import BUILTIN_MATCHERS, KINDS, MATCH_TYPES, push_pattern

    if not isinstance(v, list) or len(v) > MAX_CONTROLS:
        raise PolicyUnavailable("malformed permission_grants.controls")
    out = []
    for c in v:
        if not isinstance(c, dict) or not set(c) <= {"decision", "reason", "group", "rule"}:
            raise PolicyUnavailable("malformed permission_grants.controls")
        group, rule, reason = c.get("group"), c.get("rule"), c.get("reason") or ""
        if (c.get("decision") not in CONTROL_DECISIONS or (group is None) == (rule is None)
                or not isinstance(reason, str) or len(reason) > MAX_CONTROL_REASON
                or group is not None and group not in ORG_GROUPINGS):
            raise PolicyUnavailable("malformed permission_grants.controls")
        if rule is not None:
            parts = rule.split(None, 2) if isinstance(rule, str) else []
            if (len(parts) != 3 or parts[0] not in KINDS or parts[1] not in (*MATCH_TYPES, *BUILTIN_MATCHERS)
                    or parts[1] == "push" and push_pattern(parts[2]) is None):
                raise PolicyUnavailable("malformed permission_grants.controls")
        out.append(OrgControl(c["decision"], reason.strip(), group, rule))
    return tuple(out)


def _org_key(pc: dict) -> tuple[str | None, tuple[str, ...] | None]:
    """``key_env`` and ``key_helper`` of a ``provider_config``. A ``key_env``
    naming one of the person's own keys (secrets.PERSONAL_KEYS) or a brindle
    secret is rejected: the org's key must come from a variable of its own."""
    from brindle import secrets

    env, helper = pc.get("key_env"), pc.get("key_helper")
    if env is None and helper is None:
        return None, None
    if pc["provider"] not in KEY_PROVIDERS:
        raise PolicyUnavailable(f"provider_config key_env/key_helper isn't supported for {pc['provider']}")
    if env is not None and helper is not None:
        raise PolicyUnavailable("provider_config sets both key_env and key_helper")
    if env is not None:
        if not isinstance(env, str) or not KEY_ENV_RE.match(env):
            raise PolicyUnavailable("malformed provider_config key_env")
        if env in secrets.PERSONAL_KEYS or secrets.is_job_secret(env):
            raise PolicyUnavailable(f"provider_config key_env {env} is a personal or brindle key variable; "
                                    "name an org variable instead")
        return env, None
    if (not isinstance(helper, list) or not 1 <= len(helper) <= 32
            or not all(isinstance(a, str) and a and len(a) <= 1024 and "\0" not in a for a in helper)):
        raise PolicyUnavailable("malformed provider_config key_helper")
    return None, tuple(helper)


AWS_SSO_KEYS = ("sso_start_url", "sso_region", "account_id", "role_name")
AWS_KEYS = AWS_SSO_KEYS + ("region", "federation")
GCP_KEYS = ("project", "region", "federation")
AZURE_KEYS = ("subscription_id", "resource", "base_url", "federation")
AWS_FEDERATION_KEYS = ("role_arn", "max_session_age")
GCP_FEDERATION_KEYS = ("project_number", "pool_id", "provider_id", "service_account", "max_session_age")
AZURE_FEDERATION_KEYS = ("tenant_id", "client_id", "max_session_age")
GATEWAY_KEYS = ("base_url",)
MODEL_KEYS = ("opus", "sonnet", "haiku")
CLAUDE_KEYS = ("route", "org_id", "aws", "gcp", "azure", "gateway", "models", "model_ids",
               "default_model", "auth")
AUTH_MODES = ("own_login", "federated")
MIN_SESSION_AGE, MAX_SESSION_AGE = 3600, 604800
IAM_ROLE_ARN_RE = re.compile(r"arn:aws(?:-[a-z]+)?:iam::(\d{12}):role/[A-Za-z0-9+=,.@_/-]{1,512}")
GCP_NUMBER_RE = re.compile(r"\d{1,20}")
GCP_POOL_RE = re.compile(r"[a-z0-9-]{4,32}")
GCP_SA_RE = re.compile(r"[a-z][a-z0-9-]{5,29}@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com")
CODEX_KEYS = ("route", "base_url")
ANTIGRAVITY_KEYS = ("route", "base_url")
AGENT_KEYS = ("claude", "codex", "antigravity", "enforce", "deny_personal_keys", "exclusive")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
AWS_ACCOUNT_RE = re.compile(r"\d{12}")
IAM_ROLE_RE = re.compile(r"[A-Za-z0-9+=,.@_-]{1,64}")
AWS_REGION_RE = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-\d{1,2}")
GCP_REGION_RE = re.compile(r"[a-z]+-[a-z]+\d{1,2}")
GCP_PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
AZURE_RESOURCE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?")   # a name: no dots, no URL
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")      # @: Vertex ids (model@version)
HTTPS_URL_RE = re.compile(r"https://[^\s/?#@]+(?:/[^\s?#]*)?")
# Secret-shaped values: none belongs in a policy. Matched on any string field.
SECRET_RE = re.compile(r"(?<![A-Za-z0-9])sk-|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|gh[pousr]_|"
                       r"xox[abprs]-|AIza[0-9A-Za-z_-]{35}|-----BEGIN|"
                       r"(?i:password|secret|token|api[_-]?key)\s*[=:]")


class _Malformed(ValueError):
    pass


def _obj(v, keys: tuple[str, ...], what: str) -> dict:
    """``v`` as an object with only ``keys`` in it (anything else is malformed)."""
    if not isinstance(v, dict):
        raise _Malformed(f"{what} isn't an object")
    extra = sorted(set(v) - set(keys), key=str)
    if extra:
        # A newer server may send fields this client doesn't know: ignore them
        # (but never a secret-shaped one), and say so once at debug level.
        if any(_secret_shaped(k) or _secret_shaped(v[k]) for k in extra):
            raise _Malformed(f"{what} has a secret-shaped unknown field")
        log.debug("ignoring unknown %s field(s): %s", what, ", ".join(str(k) for k in extra))
        v = {k: x for k, x in v.items() if k in keys}
    return v


def _secret_shaped(v) -> bool:
    if isinstance(v, str):
        return bool(SECRET_RE.search(v))
    if isinstance(v, dict):
        return any(_secret_shaped(k) or _secret_shaped(x) for k, x in v.items())
    if isinstance(v, list):
        return any(_secret_shaped(x) for x in v)
    return False


def _field(d: dict, key: str, pattern: re.Pattern, *, required: bool = True,
           max_len: int = 128) -> str | None:
    v = d.get(key)
    if v is None:
        if required:
            raise _Malformed(f"missing {key}")
        return None
    if not isinstance(v, str) or len(v) > max_len or not pattern.fullmatch(v) or SECRET_RE.search(v):
        raise _Malformed(f"invalid {key}")
    return v


def _https(d: dict, key: str) -> str:
    v = _field(d, key, HTTPS_URL_RE, max_len=512)
    u = urlsplit(v)
    if u.scheme != "https" or not u.hostname or "@" in u.netloc:
        raise _Malformed(f"invalid {key}")
    return v


def _session_age(d: dict) -> int:
    age = d.get("max_session_age")
    if age is None:
        return DEFAULT_MAX_SESSION_AGE
    if isinstance(age, bool) or not isinstance(age, int) or not MIN_SESSION_AGE <= age <= MAX_SESSION_AGE:
        raise _Malformed("invalid max_session_age")
    return age


def _aws_federation(v) -> AwsFederation:
    f = _obj(v, AWS_FEDERATION_KEYS, "aws federation")
    return AwsFederation(role_arn=_field(f, "role_arn", IAM_ROLE_ARN_RE, max_len=640),
                         max_session_age=_session_age(f))


def _gcp_federation(v) -> GcpFederation:
    f = _obj(v, GCP_FEDERATION_KEYS, "gcp federation")
    return GcpFederation(project_number=_field(f, "project_number", GCP_NUMBER_RE, max_len=20),
                         pool_id=_field(f, "pool_id", GCP_POOL_RE, max_len=32),
                         provider_id=_field(f, "provider_id", GCP_POOL_RE, max_len=32),
                         service_account=_field(f, "service_account", GCP_SA_RE, max_len=100),
                         max_session_age=_session_age(f))


def _azure_federation(v) -> AzureFederation:
    f = _obj(v, AZURE_FEDERATION_KEYS, "azure federation")
    return AzureFederation(tenant_id=_field(f, "tenant_id", UUID_RE), client_id=_field(f, "client_id", UUID_RE),
                           max_session_age=_session_age(f))


def _aws(v, federated: bool = False) -> AwsSetup:
    a = _obj(v, AWS_KEYS, "aws")
    url = a.get("sso_start_url")
    if isinstance(url, str) and url.endswith("#"):
        # AWS shows its start URL as ".../start/#"; the trailing # isn't part of it.
        a = {**a, "sso_start_url": url[:-1]}
    region = _field(a, "region", AWS_REGION_RE)
    if federated:
        # The SSO group is unused: only the region (STS endpoint) and the role matter.
        fed = _aws_federation(a.get("federation"))
        account = _field(a, "account_id", AWS_ACCOUNT_RE, required=False)
        arn_account = IAM_ROLE_ARN_RE.fullmatch(fed.role_arn).group(1)
        if account is not None and account != arn_account:
            raise _Malformed("federation role_arn is in another account than account_id")
        return AwsSetup(account_id=arn_account, role_name=fed.role_arn.rsplit("/", 1)[1],
                        region=region, federation=fed)
    given = [k for k in AWS_SSO_KEYS if a.get(k) is not None]
    if not given:
        return AwsSetup(region=region)      # the machine's own AWS sign-in applies
    if len(given) != len(AWS_SSO_KEYS):
        raise _Malformed("aws SSO fields come all four or none (missing "
                         + ", ".join(k for k in AWS_SSO_KEYS if k not in given) + ")")
    return AwsSetup(region=region, sso_start_url=_https(a, "sso_start_url"),
                    sso_region=_field(a, "sso_region", AWS_REGION_RE),
                    account_id=_field(a, "account_id", AWS_ACCOUNT_RE),
                    role_name=_field(a, "role_name", IAM_ROLE_RE, max_len=64))


def _gcp(v, federated: bool = False) -> GcpSetup:
    g = _obj(v, GCP_KEYS, "gcp")
    return GcpSetup(project=_field(g, "project", GCP_PROJECT_RE),
                    region=_field(g, "region", GCP_REGION_RE),
                    federation=_gcp_federation(g.get("federation")) if federated else None)


def _azure(v, federated: bool = False) -> AzureSetup:
    z = _obj(v, AZURE_KEYS, "azure")
    if federated and z.get("subscription_id") == "":     # as the policy cache writes it back
        z = {**z, "subscription_id": None}
    sub = _field(z, "subscription_id", UUID_RE, required=False)
    resource = _field(z, "resource", AZURE_RESOURCE_RE, required=False, max_len=64)
    base_url = _https(z, "base_url") if z.get("base_url") is not None else None
    if resource is None and base_url is None:
        raise _Malformed("azure needs a resource or a base_url")
    if resource is not None and sub is None and not federated:
        raise _Malformed("azure resource needs its subscription_id")
    return AzureSetup(subscription_id=sub, resource=resource, base_url=base_url,
                      federation=_azure_federation(z.get("federation")) if federated else None)


def _model_ids(c: dict) -> tuple[tuple[str, ...], str | None]:
    """``model_ids`` (de-duplicated, in order) and ``default_model`` of a
    claude block; the default is one of the ids when there are any."""
    ids = c.get("model_ids")
    if ids is None:
        ids = []
    if not isinstance(ids, (list, tuple)) or len(ids) > MAX_MODEL_IDS:   # a tuple: the cache's asdict
        raise _Malformed("invalid model_ids")
    seen: dict[str, None] = {}
    for raw in ids:
        seen[_field({"model_ids": raw}, "model_ids", MODEL_ID_RE)] = None
    default = _field(c, "default_model", MODEL_ID_RE, required=False)
    if default is not None and seen and default not in seen:
        raise _Malformed("default_model isn't one of model_ids")
    return tuple(seen), default


def _gateway(v) -> GatewaySetup:
    g = _obj(v, GATEWAY_KEYS, "gateway")
    return GatewaySetup(base_url=_https(g, "base_url"))


def _models(v) -> ModelsSetup:
    m = _obj(v, MODEL_KEYS, "models")
    return ModelsSetup(**{k: _field(m, k, MODEL_ID_RE, required=False, max_len=128) for k in MODEL_KEYS})


def _claude(v) -> ClaudeSetup:
    c = _obj(v, CLAUDE_KEYS, "claude")
    route = c.get("route")
    if route not in AGENT_CLAUDE_ROUTES:
        raise _Malformed("invalid claude route")
    org = _field(c, "org_id", UUID_RE, required=route in CLAUDE_ORG_ROUTES)
    auth = c.get("auth")
    if auth is None:
        auth = "own_login"
    if auth not in AUTH_MODES:
        raise _Malformed("invalid claude auth")
    # Only the cloud routes can be federated; elsewhere the setting means nothing.
    federated = auth == "federated" and route in FEDERATED_ROUTES
    auth = "federated" if federated else "own_login"
    # Every sub-block present is validated, then only the one the route needs is kept.
    parsed = {"aws": _aws, "gcp": _gcp, "azure": _azure}
    blocks = {k: fn(c[k], federated and CLAUDE_ROUTE_BLOCK[route] == k) if c.get(k) is not None else None
              for k, fn in parsed.items()}
    blocks["gateway"] = _gateway(c["gateway"]) if c.get("gateway") is not None else None
    need = CLAUDE_ROUTE_BLOCK.get(route)
    if need and blocks[need] is None:
        raise _Malformed(f"claude route {route} needs {need}")
    models = _models(c["models"]) if c.get("models") is not None else None
    ids, default = _model_ids(c)        # validated for every route, kept where they apply
    if route not in CLAUDE_MODEL_ROUTES:
        ids, default = (), None
    return ClaudeSetup(route=route, org_id=org if route in CLAUDE_ORG_ROUTES else None,
                       aws=blocks["aws"] if need == "aws" else None,
                       gcp=blocks["gcp"] if need == "gcp" else None,
                       azure=blocks["azure"] if need == "azure" else None,
                       gateway=blocks["gateway"] if need == "gateway" else None,
                       models=models, model_ids=ids, default_model=default, auth=auth)


def _codex(v) -> CodexSetup:
    c = _obj(v, CODEX_KEYS, "codex")
    route = c.get("route")
    if route not in AGENT_CODEX_ROUTES:
        raise _Malformed("invalid codex route")
    # gateway needs its endpoint; azure may have one (policies older than the
    # field have none); the login routes have no use for it.
    base_url = _https(c, "base_url") if (route == "gateway" or c.get("base_url") is not None) else None
    return CodexSetup(route=route, base_url=base_url if route in CODEX_ENDPOINT_ROUTES else None)


def _antigravity(v) -> AntigravitySetup:
    a = _obj(v, ANTIGRAVITY_KEYS, "antigravity")
    route = a.get("route")
    if route not in AGENT_ANTIGRAVITY_ROUTES:
        raise _Malformed("invalid antigravity route")
    base_url = _https(a, "base_url") if (route == "gateway" or a.get("base_url") is not None) else None
    return AntigravitySetup(route=route, base_url=base_url if route in ANTIGRAVITY_ENDPOINT_ROUTES else None)


def _enforce_entitled() -> bool:
    from brindle.pro import license

    try:
        return license.has(AGENT_ENFORCE_FEATURE)
    except Exception:  # noqa: BLE001 - no verified entitlement: not enforced
        return False


def _parse_agent_setup(raw) -> AgentSetup:
    a = _obj(raw, AGENT_KEYS, "agent_setup")
    enforce = a.get("enforce", False)
    if enforce is None:
        enforce = False
    if not isinstance(enforce, bool):
        raise _Malformed("invalid enforce")
    deny = a.get("deny_personal_keys")
    if not (deny is None or isinstance(deny, bool)):
        raise _Malformed("invalid deny_personal_keys")
    # Carried (a phase-1 server stores its default explicitly) or absent (an
    # older server): the difference decides what the managed source is.
    exclusive = a.get("exclusive")
    if not (exclusive is None or isinstance(exclusive, bool)):
        raise _Malformed("invalid exclusive")
    claude = _claude(a["claude"]) if a.get("claude") is not None else None
    codex = _codex(a["codex"]) if a.get("codex") is not None else None
    antigravity = _antigravity(a["antigravity"]) if a.get("antigravity") is not None else None
    return AgentSetup(claude=claude, codex=codex, antigravity=antigravity,
                      enforce=enforce and _enforce_entitled(),
                      deny_personal_keys=deny, exclusive=exclusive)


def _agent_setup(p: dict) -> AgentSetup | None:
    """The policy's ``agent_setup`` block. A malformed one is ignored with one
    warning, and never raises: the rest of the policy still applies."""
    raw = p.get("agent_setup")
    if raw is None:
        return None
    try:
        return _parse_agent_setup(raw)
    except Exception as e:  # noqa: BLE001 - a bad block must not break the policy
        log.warning("ignoring the org policy's agent_setup block (%s)", e)
        return None


def _spend(v) -> dict:
    if not isinstance(v, dict):
        return {}
    usd, month = v.get("seat_usd"), v.get("month")
    if not isinstance(usd, (int, float)) or isinstance(usd, bool) or not 0 <= usd < float("inf"):
        return {}
    return {"spend_seat_usd": float(usd), "spend_month": month if isinstance(month, str) else None}


def _visibility(v) -> dict:
    """The caller's ``visibility`` flags. A missing field or key is true (older
    servers); anything present but not exactly ``true`` hides (fail closed)."""
    if v is None:
        return dict(DEFAULT_VISIBILITY)
    if not isinstance(v, dict):
        return {k: False for k in DEFAULT_VISIBILITY}
    return {k: v.get(k, True) is True for k in DEFAULT_VISIBILITY}


def _roles(v) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict) or len(v) > 256:
        raise PolicyUnavailable("malformed roles")
    out = {}
    for role, over in v.items():
        if not (isinstance(role, str) and ROLE_RE.match(role) and isinstance(over, dict)):
            raise PolicyUnavailable("malformed roles")
        # A null field is an override left unset (the base value applies).
        out[role] = {k: _rule(k, over[k]) for k in RULES if over.get(k) is not None}
        if over.get("permission_grants") is not None:
            _grants(over)  # Validate even overrides that do not belong to the current member.
            out[role]["permission_grants"] = deepcopy(over["permission_grants"])
    return out


def _role_name(v) -> str | None:
    return v if isinstance(v, str) and ROLE_RE.match(v) else None


def parse_policy(org_id: str, body: dict, fetched_at: float | None = None) -> OrgPolicy:
    """An :class:`OrgPolicy` from the backend's answer: either flat
    ``{allowed_providers, ..., version}`` or ``{version, policy: {...}}``,
    with the calling member's ``effective`` policy next to it when the
    backend gives one."""
    if not isinstance(body, dict):
        raise PolicyUnavailable("malformed policy")
    p = body.get("policy") if isinstance(body.get("policy"), dict) else body
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PolicyUnavailable("malformed policy version")
    if body.get("org_id") not in (None, org_id):
        raise PolicyUnavailable("policy is for another org")
    at = float(body.get("fetched_at") or fetched_at or time.time())
    eff = body.get("effective")
    if not (eff is None or isinstance(eff, dict)):
        raise PolicyUnavailable("malformed effective policy")
    key = body.get("cached_for")
    ok_key = isinstance(key, list) and len(key) == 2 and all(k is None or isinstance(k, str)
                                                             for k in key)
    spend = {**_spend(body.get("spend")),
             "visibility": _visibility(body.get("visibility", p.get("visibility")))}
    roll = _rollout(p)
    plan = _plan_rules(p)         # org-wide too
    managed = _managed(p)
    setup = _agent_setup(p)       # org-wide, like the rollout: the effective policy carries it too
    grants = _grants(p)
    return OrgPolicy(org_id=org_id, version=version, fetched_at=at, **_rules(p), **_budgets(p),
                     **roll, **plan, **managed, **grants, roles=_roles(p.get("roles")),
                     effective=None if eff is None else OrgPolicy(
                         org_id=org_id, version=version, fetched_at=at, **_rules(eff),
                         **_budgets(eff), **roll, **plan, **(_managed(eff) if (
                             "provider_config" in eff or "deny_personal_keys" in eff) else managed),
                         agent_setup=setup,
                         **(_grants(eff) if "permission_grants" in eff else grants), **spend),
                     role=_role_name(body.get("role")),
                     policy_role=_role_name(body.get("policy_role")),
                     cached_for=tuple(key) if ok_key else None,
                     agent_setup=setup, raw_body=deepcopy(body), **spend)


def cache_path(org_id: str):
    if not ORG_RE.match(org_id):
        raise PolicyUnavailable("invalid org id")
    # Pre-fix processes keep writing the legacy name with a lossy serializer.
    # Never read or migrate that cache: it may already have lost policy fields.
    return private_dir() / f"policy-v2-{org_id}.json"


def load_cached(org_id: str) -> OrgPolicy | None:
    try:
        raw = read_private(cache_path(org_id), MAX_CACHE)
        return parse_policy(org_id, json.loads(raw)) if raw else None
    except Exception as e:  # noqa: BLE001 - an unreadable cache is no cache
        log.warning("ignoring the cached team policy (%s)", e)
        return None


def save_cached(p: OrgPolicy) -> None:
    """Cache the server answer, retaining fields unknown to this client.

    Locally constructed policies have no server answer; those can still use
    the model's JSON representation. Cache metadata always comes from ``p``
    so the entitlement key and fetch time survive a save/load cycle.
    """
    body = dict(p.raw_body) if p.raw_body is not None else p.to_json()
    body["org_id"] = p.org_id
    body["fetched_at"] = p.fetched_at
    body["cached_for"] = list(p.cached_for) if p.cached_for is not None else None
    write_private(cache_path(p.org_id), json.dumps(body).encode())


def fetch_policy(org_id: str, client=None, store=None, cached_for=None) -> OrgPolicy:
    """Fetch and cache the org's policy. ``cached_for`` is the entitlement's
    ``(role, policy_role)`` the answer is for (see :func:`current_policy`)."""
    from brindle.pro import auth, credentials

    store = store or credentials.default_store()
    if client is None:
        creds = store.load() or {}
        client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=FETCH_TIMEOUT))
    if not ORG_RE.match(org_id):
        raise PolicyUnavailable("invalid org id")
    try:
        status, body = auth.authed(client, store, "GET", f"/orgs/{org_id}/policy")
    except auth.AuthError as e:
        raise PolicyUnavailable(e.code) from e
    if status != 200:
        raise PolicyUnavailable(f"HTTP {status}")
    p = replace(parse_policy(org_id, body, time.time()), cached_for=cached_for)
    save_cached(p)
    return p


def with_role_overrides(p: OrgPolicy, role: str | None, policy_role: str | None) -> OrgPolicy:
    """``p`` with its ``effective`` policy filled in from its per-role
    overrides, by the backend's rule (orgs.effective_policy): the base, then
    the built-in role's overrides, then the custom policy role's, each only
    where set. Unchanged when ``p`` already has one or has no overrides."""
    if p.effective is not None or not p.roles:
        return p
    merged = p.rules()
    grants = p.permission_grants
    for name in (role, policy_role):
        override = p.roles.get(name) or {}
        for k, v in override.items():
            if v is not None and k in RULES:
                merged[k] = list(v) if isinstance(v, tuple) else v
        if override.get("permission_grants") is not None:
            grants = _grants(override)["permission_grants"]
    return replace(p, effective=OrgPolicy(org_id=p.org_id, version=p.version,
                                          fetched_at=p.fetched_at, **_rules(merged),
                                          **_budgets(p.budgets()), **_rollout(p.rollout()),
                                          **_plan_rules(p.plan_rules()),
                                          provider_config=p.provider_config,
                                          deny_personal_keys=p.deny_personal_keys,
                                          agent_setup=p.agent_setup,
                                          permission_grants=grants,
                                          spend_seat_usd=p.spend_seat_usd,
                                          spend_month=p.spend_month,
                                          visibility=p.visibility))


def load_offline(repo_root: str | None, org_id: str) -> OrgPolicy:
    """The offline policy file (``.brindle/policy.json``) for air-gap mode,
    for ``org_id`` (the signed-in org, or the managed device's). A signed
    offline policy export (``{"token", "body"}``) counts only once its token
    verifies as an offline-policy document for ``org_id`` that is unexpired
    and covers ``body``; a plain policy (the org policy's schema, unsigned)
    still loads, with a warning. Raises :class:`PolicyUnavailable` when the
    file is missing, unreadable, malformed, for another org, or an export
    that doesn't verify."""
    from brindle import airgap
    from brindle.pro import signed

    path = airgap.policy_path(repo_root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise PolicyUnavailable(f"no offline policy file at {path}") from None
    except OSError as e:
        raise PolicyUnavailable(f"cannot read {path}: {e}") from e
    if len(raw) > MAX_CACHE:
        raise PolicyUnavailable(f"{path} is too large")
    try:
        body = json.loads(raw)
    except ValueError as e:
        raise PolicyUnavailable(f"{path} is not valid JSON") from e
    airgap.check_local_policy(signed=isinstance(body, dict) and "token" in body)
    export = body if isinstance(body, dict) and "token" in body else None
    if export is not None:
        token, body = body.get("token"), body.get("body")
        if not isinstance(token, str) or not isinstance(body, dict):
            raise PolicyUnavailable(f"{path} is a malformed offline policy export")
        try:
            from brindle.pro.org_context import active

            ctx = active()
            signed.verify(token, "offline-policy", {"org_id": org_id}, body=body,
                          now=ctx.now if ctx is not None else None)
        except signed.SignedError as e:
            raise PolicyUnavailable(f"the offline policy export at {path} doesn't verify: {e}") from e
    else:
        log.warning("the offline policy at %s is unsigned; `brindle account org policy --export` "
                    "writes a signed one", path)
    p = parse_policy(org_id, body, time.time())
    return replace(p, raw_body=export) if export is not None else p


def current_policy(ent, client=None, store=None, repo_root: str | None = None, cached=None) -> OrgPolicy:
    """The policy for ``ent``'s org at (at least) ``ent.policy_version`` and
    for ``ent``'s role and policy role (changing those bumps no version, and
    the member's effective policy depends on them), else the last good copy;
    raises :class:`PolicyUnavailable` if there is none. ``cached`` reads the
    cached copy (default :func:`load_cached`).
    In air-gap mode, the offline policy file (:func:`load_offline`) and
    nothing else: nothing is fetched and the cache is not consulted."""
    from brindle import airgap

    if airgap.enabled():
        return with_role_overrides(load_offline(repo_root, ent.org_id), ent.role,
                                   getattr(ent, "policy_role", None))
    cached = (cached or load_cached)(ent.org_id)
    want = (ent.role, ent.policy_role)
    if (cached is not None and cached.version >= ent.policy_version
            and cached.cached_for == want):
        return cached
    try:
        return fetch_policy(ent.org_id, client, store, cached_for=want)
    except Exception as e:  # noqa: BLE001
        # A stale version of this member's own policy is a fair fallback; a
        # copy fetched for another role is not: after a demotion it would
        # keep the old, looser rules while the backend is unreachable.
        if cached is not None and cached.cached_for == want:
            log.warning("team policy v%s unavailable (%s); using cached v%s",
                        ent.policy_version, e, cached.version)
            return cached
        raise PolicyUnavailable(str(e)) from e


class ProPolicy(PolicyPlugin):
    def __init__(self, repo_root: str, *, store=None, client=None, entitlement=None) -> None:
        self.repo_root = repo_root
        self._store, self._client = store, client
        self._entitlement = entitlement

    def policy(self) -> OrgPolicy | Decision:
        """The policy to enforce (this member's effective one when the backend
        gives it, else the org's base policy), ``allow()`` with no team
        entitlement, or a denial when a policy is required but has never
        been fetched (brindle.pro.org_context.OrgContext.read_policy, with
        the org context being resolved, if any)."""
        from brindle.pro import org_context

        ctx = org_context.active() or org_context.OrgContext.resolve(repo_root=self.repo_root)
        with ctx.activated():
            return ctx.read_policy(self.repo_root, store=self._store, client=self._client,
                                   entitlement=self._entitlement)

    def org_budgets(self) -> OrgPolicy | Decision | None:
        """The org policy whose budgets and protected paths apply here: None
        when the org_budgets feature isn't entitled (:func:`license.has`,
        fail closed) or there is no team org, a denial when it is but the
        policy can't be had (the caller must not go on without it)."""
        from brindle.pro.org_context import OrgContext, active

        try:
            entitled = (active() or OrgContext.resolve()).entitled(BUDGETS_FEATURE)
        except Exception:  # noqa: BLE001 - no verified entitlement: no org budgets
            entitled = False
        if not entitled:
            return None
        p = self.policy()
        if isinstance(p, Decision):
            return None if p.allowed else p      # allowed: no team entitlement, no org
        return p

    def managed_models(self) -> OrgPolicy | Decision | None:
        """The org policy whose managed routes (an enforced ``agent_setup``,
        else ``provider_config``) and ``deny_personal_keys`` apply here
        (brindle.pro.managed_models): None when managed_models isn't entitled
        (:func:`license.has`, fail closed) or there is no team org, a denial
        when it is but the policy can't be had."""
        from brindle.pro.org_context import OrgContext, active

        try:
            entitled = (active() or OrgContext.resolve()).entitled(MANAGED_FEATURE)
        except Exception:  # noqa: BLE001 - no verified entitlement: nothing managed
            entitled = False
        if not entitled:
            return None
        p = self.policy()
        if isinstance(p, Decision):
            return None if p.allowed else p
        return p

    def check_assign(self, info: AssignInfo) -> Decision:
        from brindle.pro.org_context import OrgContext

        ctx = OrgContext.resolve(repo_root=self.repo_root)
        with ctx.activated():
            p = self.policy()
        if isinstance(p, Decision):
            return p
        from brindle.pro import rollout

        why = rollout.refusal(p) if ctx.entitled(rollout.FEATURE) else None
        if why:
            return deny(why)
        why = ctx.blocked_reason(p)       # paused, or shut down remotely
        if why:
            return deny(f"org {p.org_id}: {why}")
        who = f"profile {info.profile!r}" if info.profile else "this delegation"
        if p.allowed_providers is not None and info.provider not in p.allowed_providers:
            got = f"provider {info.provider!r}" if info.provider else "an undeclared provider"
            return deny(f"{who} uses {got}; org {p.org_id} allows only "
                        f"{', '.join(p.allowed_providers) or 'no providers'}")
        if p.allowed_models is not None:
            route = claude_route(p.org_id, p.agent_setup, info.provider)
            if not model_allowed(p.allowed_models, info.model, pinned_model(route, info.model)):
                got = f"model {info.model!r}" if info.model else "no declared model"
                fix = (f"it is the default model of org {p.org_id}'s Claude route; an org admin must change "
                       "agent_setup" if route and info.model and info.model == route.default_model
                       else "set `model` in the profile")
                return deny(f"{who} uses {got}; org {p.org_id} allows only "
                            f"{', '.join(p.allowed_models) or 'no models'} ({fix})")
        if p.allowed_profiles is not None and info.profile not in p.allowed_profiles:
            got = who if info.profile else "a delegation with no profile"
            return deny(f"{got} is not allowed; org {p.org_id} allows only "
                        f"{', '.join(p.allowed_profiles) or 'no profiles'} for you")
        cap = p.max_parallel_workers
        if cap is not None and (info.running_workers is None or info.running_workers >= cap):
            now = "an unknown number" if info.running_workers is None else str(info.running_workers)
            return deny(f"org {p.org_id} allows at most {cap} parallel worker(s) per repo and "
                        f"{now} are at work; wait for one to finish (or cancel one) and try again")
        return allow()

    def check_merge(self, info: MergeInfo) -> Decision:
        p = self.policy()
        if isinstance(p, Decision):
            return p
        if p.require_human_review and info.actor != "user":
            return deny(f"org {p.org_id} requires a human review before merging; "
                        "a person must run this merge (not the pipeline or an agent)")
        return allow()


CLI_NAMES = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Google Antigravity", "opencode": "OpenCode"}


def claude_route(org_id: str | None, setup: AgentSetup | None, provider: str | None) -> ClaudeSetup | None:
    """The org's Claude route a ``provider`` pane runs on, or None: only a
    route a Claude pane gets (see company_login.claude_model)."""
    if not (setup and setup.claude and provider == "claude"):
        return None
    from brindle import company_login, providers

    if providers.claude_org_managed() or not company_login.route_in_use(org_id, setup):
        return None
    return setup.claude


def pinned_model(route: ClaudeSetup | None, model: str | None) -> str | None:
    """The route id an alias (opus, sonnet, haiku) runs as when ``route`` pins it, or None."""
    if route is None or route.models is None or model not in ("opus", "sonnet", "haiku"):
        return None
    return getattr(route.models, model, None)


def model_allowed(allowed: tuple[str, ...] | None, model: str | None, pinned: str | None = None) -> bool:
    """Whether ``allowed`` (an org's allowed_models; None: any) lets a worker
    run ``model``: the model itself, matched as pricing normalizes it, or the
    route id its alias is ``pinned`` to, matched exactly."""
    if allowed is None:
        return True
    from brindle import pricing

    if model and (model in allowed or pricing.normalize(model) in {pricing.normalize(m) for m in allowed}):
        return True
    return bool(pinned) and pinned in allowed


@dataclass(frozen=True)
class Access:
    """The CLIs, models and profiles this person may use here: the org's
    ``allowed_providers``, ``allowed_models`` and ``allowed_profiles`` (the member's
    effective policy, so narrowed by role and member), minus the CLIs an exclusive
    managed routing gives no route, and whether personal API keys are denied.
    ``error`` (the policy applies but can't be read) allows nothing. The
    supervisor picker, the workers screen and ``-s`` all ask this, and only
    this, what to offer and what to refuse."""
    org_id: str | None = None
    allowed_providers: tuple[str, ...] | None = None    # None: any CLI
    allowed_models: tuple[str, ...] | None = None       # None: any model
    allowed_profiles: tuple[str, ...] | None = None     # None: any profile
    deny_personal_keys: bool = False
    unrouted: dict = field(default_factory=dict)        # CLI -> why the managed routing refuses it
    error: str | None = None
    agent_setup: AgentSetup | None = None               # its Claude route may pin an alias to an allowed id

    def pinned(self, provider: str | None, model: str | None) -> str | None:
        """The route id the org's Claude route pins ``model`` (an alias) to, or None."""
        return pinned_model(claude_route(self.org_id, self.agent_setup, provider), model)

    def route_model(self, provider: str | None, model: str | None) -> str | None:
        """The model a ``provider`` worker on ``model`` runs as: its pinned route id, else ``model``."""
        return self.pinned(provider, model) or model

    def allows_run(self, provider: str | None, model: str | None) -> bool:
        """Whether ``model`` may be used by ``provider``: check_assign's test (:func:`model_allowed`)."""
        return not self.error and model_allowed(self.allowed_models, model, self.pinned(provider, model))

    def refusal(self, provider: str) -> str | None:
        """Why this person may not run ``provider``'s CLI, or None."""
        if self.error:
            return self.error
        if self.allowed_providers is not None and provider not in self.allowed_providers:
            return (f"org {self.org_id} doesn't allow {CLI_NAMES.get(provider, provider)}; it allows "
                    f"{', '.join(self.allowed_providers) or 'no CLIs'}")
        return self.unrouted.get(provider)

    def allows(self, provider: str) -> bool:
        return self.refusal(provider) is None

    def allows_model(self, model: str | None) -> bool:
        """Whether ``model`` may be used, by check_assign's matching."""
        return not self.error and model_allowed(self.allowed_models, model)

    def allows_profile(self, profile: str, provider: str | None, model: str | None) -> bool:
        """Whether a worker on ``profile`` (which runs ``provider``'s ``model``,
        as ``policy._profile_fields`` resolves them) may start: check_assign's
        provider, model and profile tests."""
        return (bool(provider) and self.allows(provider) and self.allows_run(provider, model)
                and (self.allowed_profiles is None or profile in self.allowed_profiles))

    def clis(self) -> list[str]:
        """The CLIs to offer: every supervisor CLI when nothing narrows the
        choice, else each one the org allows (installed or not: the picker
        sets it up)."""
        if self.error:
            return []
        return [p for p in CLI_NAMES if self.allows(p)]


def access(repo_root: str | None) -> Access:
    """:class:`Access` for this person in ``repo_root``: everything when
    there is no team org."""
    from types import SimpleNamespace

    from brindle.pro import managed_models

    p = ProPolicy(repo_root or "").policy()
    if isinstance(p, Decision):
        return Access() if p.allowed else Access(error=p.reason)
    try:
        m = managed_models.current(repo_root)
    except managed_models.ManagedUnavailable as e:
        return Access(org_id=p.org_id, error=str(e))
    unrouted = {}
    if m is not None:
        for cli, label in CLI_NAMES.items():
            # A bare CLI sets no key and no endpoint: only its route is judged.
            bare = SimpleNamespace(name=cli, provider=cli, env={}, api_key_env=None, base_url=None)
            if managed_models.refusal(m, bare):
                unrouted[cli] = f"org {m.org_id} manages its agents' routes and gives {label} none"
    deny_keys = m.deny_personal_keys if m is not None else p.deny_personal_keys
    return Access(org_id=p.org_id, allowed_providers=p.allowed_providers,
                  allowed_models=p.allowed_models, allowed_profiles=p.allowed_profiles,
                  deny_personal_keys=deny_keys, unrouted=unrouted, agent_setup=p.agent_setup)


def make(repo_root: str) -> ProPolicy:
    return ProPolicy(repo_root)


def managed_models(repo_root: str) -> OrgPolicy | Decision | None:
    """:meth:`ProPolicy.managed_models` for ``repo_root``."""
    return ProPolicy(repo_root).managed_models()


def org_budgets(repo_root: str) -> OrgPolicy | Decision | None:
    """:meth:`ProPolicy.org_budgets` for ``repo_root``."""
    return ProPolicy(repo_root).org_budgets()
