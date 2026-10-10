"""The ``pro`` policy plugin (``brindle.policy`` group): enforce a team org's
policy on delegations and merges.

* No team entitlement (not logged in, no ``team`` feature): everything is
  allowed; this plugin only enforces what an org has set.
* With one, the org's policy is used at the entitlement's
  ``policy_version``: from the on-disk cache (``$BRINDLE_HOME/pro/
  policy-<org>.json``, 0600) when it is that version or newer, else fetched
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
AGENT_CODEX_ROUTES = ("chatgpt", "api_key", "azure")
# The sub-block a Claude route needs; the others are pruned from the parsed block.
CLAUDE_ROUTE_BLOCK = {"bedrock": "aws", "vertex": "gcp", "foundry": "azure", "gateway": "gateway"}
CLAUDE_ORG_ROUTES = ("subscription", "console")     # the routes that sign in to a Claude org
AGENT_ENFORCE_FEATURE = MANAGED_FEATURE             # ``enforce`` needs this Enterprise entitlement


@dataclass(frozen=True)
class AwsSetup:
    sso_start_url: str
    sso_region: str
    account_id: str
    role_name: str
    region: str


@dataclass(frozen=True)
class GcpSetup:
    project: str
    region: str


@dataclass(frozen=True)
class AzureSetup:
    subscription_id: str
    resource: str


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


@dataclass(frozen=True)
class CodexSetup:
    route: str


@dataclass(frozen=True)
class AgentSetup:
    """Org policy ``agent_setup``: how the org's workers sign in to Claude and
    Codex. Names only, never a secret. ``enforce`` is False unless the org has
    the Enterprise entitlement."""
    claude: ClaudeSetup | None = None
    codex: CodexSetup | None = None
    enforce: bool = False


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

    def to_json(self) -> dict:
        out = {"org_id": self.org_id, "version": self.version, "fetched_at": self.fetched_at,
               "policy": {**self.rules(), **self.budgets(), **self.rollout(), **self.managed(),
                          "agent_setup": asdict(self.agent_setup) if self.agent_setup else None,
                          "roles": {r: {k: _list(v) if k in LISTS else v
                                        for k, v in o.items()}
                                    for r, o in self.roles.items()}},
               "role": self.role, "policy_role": self.policy_role}
        if self.effective is not None:
            out["effective"] = {**self.effective.rules(), **self.effective.budgets(),
                                **self.effective.rollout(), **self.effective.managed()}
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


AWS_KEYS = ("sso_start_url", "sso_region", "account_id", "role_name", "region")
GCP_KEYS = ("project", "region")
AZURE_KEYS = ("subscription_id", "resource")
GATEWAY_KEYS = ("base_url",)
MODEL_KEYS = ("opus", "sonnet", "haiku")
CLAUDE_KEYS = ("route", "org_id", "aws", "gcp", "azure", "gateway", "models")
CODEX_KEYS = ("route",)
AGENT_KEYS = ("claude", "codex", "enforce")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
AWS_ACCOUNT_RE = re.compile(r"\d{12}")
IAM_ROLE_RE = re.compile(r"[A-Za-z0-9+=,.@_-]{1,64}")
AWS_REGION_RE = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-\d{1,2}")
GCP_REGION_RE = re.compile(r"[a-z]+-[a-z]+\d{1,2}")
GCP_PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
AZURE_RESOURCE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?")   # a name: no dots, no URL
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
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


def _aws(v) -> AwsSetup:
    a = _obj(v, AWS_KEYS, "aws")
    url = a.get("sso_start_url")
    if isinstance(url, str) and url.endswith("#"):
        # AWS shows its start URL as ".../start/#"; the trailing # isn't part of it.
        a = {**a, "sso_start_url": url[:-1]}
    return AwsSetup(sso_start_url=_https(a, "sso_start_url"),
                    sso_region=_field(a, "sso_region", AWS_REGION_RE),
                    account_id=_field(a, "account_id", AWS_ACCOUNT_RE),
                    role_name=_field(a, "role_name", IAM_ROLE_RE, max_len=64),
                    region=_field(a, "region", AWS_REGION_RE))


def _gcp(v) -> GcpSetup:
    g = _obj(v, GCP_KEYS, "gcp")
    return GcpSetup(project=_field(g, "project", GCP_PROJECT_RE),
                    region=_field(g, "region", GCP_REGION_RE))


def _azure(v) -> AzureSetup:
    z = _obj(v, AZURE_KEYS, "azure")
    return AzureSetup(subscription_id=_field(z, "subscription_id", UUID_RE),
                      resource=_field(z, "resource", AZURE_RESOURCE_RE, max_len=64))


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
    # Every sub-block present is validated, then only the one the route needs is kept.
    parsed = {"aws": _aws, "gcp": _gcp, "azure": _azure, "gateway": _gateway}
    blocks = {k: fn(c[k]) if c.get(k) is not None else None for k, fn in parsed.items()}
    need = CLAUDE_ROUTE_BLOCK.get(route)
    if need and blocks[need] is None:
        raise _Malformed(f"claude route {route} needs {need}")
    models = _models(c["models"]) if c.get("models") is not None else None
    return ClaudeSetup(route=route, org_id=org if route in CLAUDE_ORG_ROUTES else None,
                       aws=blocks["aws"] if need == "aws" else None,
                       gcp=blocks["gcp"] if need == "gcp" else None,
                       azure=blocks["azure"] if need == "azure" else None,
                       gateway=blocks["gateway"] if need == "gateway" else None,
                       models=models)


def _codex(v) -> CodexSetup:
    c = _obj(v, CODEX_KEYS, "codex")
    if c.get("route") not in AGENT_CODEX_ROUTES:
        raise _Malformed("invalid codex route")
    return CodexSetup(route=c["route"])


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
    claude = _claude(a["claude"]) if a.get("claude") is not None else None
    codex = _codex(a["codex"]) if a.get("codex") is not None else None
    return AgentSetup(claude=claude, codex=codex, enforce=enforce and _enforce_entitled())


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
    managed = _managed(p)
    return OrgPolicy(org_id=org_id, version=version, fetched_at=at, **_rules(p), **_budgets(p),
                     **roll, **managed, roles=_roles(p.get("roles")),
                     effective=None if eff is None else OrgPolicy(
                         org_id=org_id, version=version, fetched_at=at, **_rules(eff),
                         **_budgets(eff), **roll, **(_managed(eff) if (
                             "provider_config" in eff or "deny_personal_keys" in eff) else managed),
                         **spend),
                     role=_role_name(body.get("role")),
                     policy_role=_role_name(body.get("policy_role")),
                     cached_for=tuple(key) if ok_key else None,
                     agent_setup=_agent_setup(p), **spend)


def cache_path(org_id: str):
    if not ORG_RE.match(org_id):
        raise PolicyUnavailable("invalid org id")
    return private_dir() / f"policy-{org_id}.json"


def load_cached(org_id: str) -> OrgPolicy | None:
    try:
        raw = read_private(cache_path(org_id), MAX_CACHE)
        return parse_policy(org_id, json.loads(raw)) if raw else None
    except Exception as e:  # noqa: BLE001 - an unreadable cache is no cache
        log.warning("ignoring the cached team policy (%s)", e)
        return None


def save_cached(p: OrgPolicy) -> None:
    write_private(cache_path(p.org_id), json.dumps(p.to_json()).encode())


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
    for name in (role, policy_role):
        for k, v in (p.roles.get(name) or {}).items():
            if v is not None and k in RULES:
                merged[k] = list(v) if isinstance(v, tuple) else v
    return replace(p, effective=OrgPolicy(org_id=p.org_id, version=p.version,
                                          fetched_at=p.fetched_at, **_rules(merged),
                                          **_budgets(p.budgets()), **_rollout(p.rollout()),
                                          provider_config=p.provider_config,
                                          deny_personal_keys=p.deny_personal_keys,
                                          spend_seat_usd=p.spend_seat_usd,
                                          spend_month=p.spend_month,
                                          visibility=p.visibility))


def load_offline(repo_root: str | None, org_id: str) -> OrgPolicy:
    """The offline policy file (``.brindle/policy.json``, the org policy's
    schema) for air-gap mode; raises :class:`PolicyUnavailable` when it is
    missing, unreadable, malformed or for another org."""
    from brindle import airgap

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
    return parse_policy(org_id, body, time.time())


def current_policy(ent, client=None, store=None, repo_root: str | None = None) -> OrgPolicy:
    """The policy for ``ent``'s org at (at least) ``ent.policy_version`` and
    for ``ent``'s role and policy role (changing those bumps no version, and
    the member's effective policy depends on them), else the last good copy;
    raises :class:`PolicyUnavailable` if there is none.
    In air-gap mode, the offline policy file (:func:`load_offline`) and
    nothing else: nothing is fetched and the cache is not consulted."""
    from brindle import airgap

    if airgap.enabled():
        return with_role_overrides(load_offline(repo_root, ent.org_id), ent.role,
                                   getattr(ent, "policy_role", None))
    cached = load_cached(ent.org_id)
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

    def _team_entitlement(self):
        from brindle.pro import license

        try:
            ent = self._entitlement() if self._entitlement else license.current(store=self._store,
                                                                                client=self._client)
        except license.LicenseError:
            return None
        except Exception:  # noqa: BLE001 - no verified entitlement: nothing to enforce
            log.warning("brindle Pro: couldn't check the entitlement; no team policy applies",
                        exc_info=True)
            return None
        return ent if FEATURE in ent.features else None

    def policy(self) -> OrgPolicy | Decision:
        """The policy to enforce (this member's effective one when the backend
        gives it, else the org's base policy), ``allow()`` with no team
        entitlement, or a denial when a policy is required but has never
        been fetched."""
        ent = self._team_entitlement()
        if ent is None:
            return allow()
        from brindle import airgap

        try:
            from brindle.pro import status

            # A throttle from the org's remote control narrows the member's own policy.
            return status.narrow(current_policy(ent, self._client, self._store,
                                                self.repo_root).enforced)
        except PolicyUnavailable as e:
            if airgap.enabled():
                return deny(f"air-gap mode: no usable offline team policy for org {ent.org_id} "
                            f"({e}); put the org's policy (the schema `brindle account org policy` "
                            f"shows, as JSON) at .brindle/{airgap.POLICY_FILE}")
            return deny(f"brindle Pro team policy for org {ent.org_id} has never been fetched "
                        f"({e}); connect to the network and run `brindle account org policy`")

    def org_budgets(self) -> OrgPolicy | Decision | None:
        """The org policy whose budgets and protected paths apply here: None
        when the org_budgets feature isn't entitled (:func:`license.has`,
        fail closed) or there is no team org, a denial when it is but the
        policy can't be had (the caller must not go on without it)."""
        from brindle.pro import license

        try:
            entitled = license.has(BUDGETS_FEATURE)
        except Exception:  # noqa: BLE001 - no verified entitlement: no org budgets
            entitled = False
        if not entitled:
            return None
        p = self.policy()
        if isinstance(p, Decision):
            return None if p.allowed else p      # allowed: no team entitlement, no org
        return p

    def managed_models(self) -> OrgPolicy | Decision | None:
        """The org policy whose ``provider_config`` and ``deny_personal_keys``
        apply here: None when managed_models isn't entitled
        (:func:`license.has`, fail closed) or there is no team org, a denial
        when it is but the policy can't be had."""
        from brindle.pro import license

        try:
            entitled = license.has(MANAGED_FEATURE)
        except Exception:  # noqa: BLE001 - no verified entitlement: nothing managed
            entitled = False
        if not entitled:
            return None
        p = self.policy()
        if isinstance(p, Decision):
            return None if p.allowed else p
        return p

    def check_assign(self, info: AssignInfo) -> Decision:
        p = self.policy()
        if isinstance(p, Decision):
            return p
        from brindle.pro import rollout, status

        why = rollout.refusal(p) if rollout.entitled() else None
        if why:
            return deny(why)
        why = status.block_reason(p)       # paused, or shut down remotely
        if why:
            return deny(f"org {p.org_id}: {why}")
        who = f"profile {info.profile!r}" if info.profile else "this delegation"
        if p.allowed_providers is not None and info.provider not in p.allowed_providers:
            got = f"provider {info.provider!r}" if info.provider else "an undeclared provider"
            return deny(f"{who} uses {got}; org {p.org_id} allows only "
                        f"{', '.join(p.allowed_providers) or 'no providers'}")
        if p.allowed_models is not None and info.model not in p.allowed_models:
            got = f"model {info.model!r}" if info.model else "no declared model"
            return deny(f"{who} uses {got}; org {p.org_id} allows only "
                        f"{', '.join(p.allowed_models) or 'no models'} (set `model` in the profile)")
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


def make(repo_root: str) -> ProPolicy:
    return ProPolicy(repo_root)


def managed_models(repo_root: str) -> OrgPolicy | Decision | None:
    """:meth:`ProPolicy.managed_models` for ``repo_root``."""
    return ProPolicy(repo_root).managed_models()


def org_budgets(repo_root: str) -> OrgPolicy | Decision | None:
    """:meth:`ProPolicy.org_budgets` for ``repo_root``."""
    return ProPolicy(repo_root).org_budgets()
