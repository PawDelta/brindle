"""What an org's policy lets this person do with routing. ``GET /v1/routing``
shows it and ``PUT /v1/routing`` enforces it, by the tests a delegation is
judged by (``team_policy.access``, as the picker asks it, and the managed
models' ``refusal``), so the app never offers a profile a worker would then
be refused. Models are named as the org policy and the website name them
(the model id check_assign matches), agents by their product names."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from brindle import config, profiles
from brindle.db import Workspace
from brindle.pro import managed_models, team_policy
from brindle.service.dto import RoutingLockDTO, RoutingModelOrderDTO, RoutingPolicyDTO
from brindle.service.errors import ServiceError


def provider_name(provider: str | None) -> str | None:
    return team_policy.CLI_NAMES.get(provider, provider) if provider else None


def legacy_medium_override(cfg: config.RepoConfig, name: str, *, pinned: bool) -> list[str] | None:
    """The destination's explicit Medium order when the effective row uses it."""
    if name == "reviewer" and (pinned or cfg.reviewer_models_inherited):
        return None
    return cfg.agent_models.get(name, {}).get("medium")


def shadows_legacy_medium(settings: dict, name: str, *, inherits_medium: bool) -> bool:
    models = settings.get("agent_models")
    sizes = models.get(name) if isinstance(models, dict) else None
    # An empty map resets all inherited overrides for this role.
    return isinstance(sizes, dict) and ((inherits_medium and not sizes) or any(
        config.normalize_weight(size) == "medium" and isinstance(order, list) for size, order in sizes.items()))


def agent_model_orders(cfg: config.RepoConfig, name: str, legacy: RoutingModelOrderDTO | None
                       ) -> dict[str, RoutingModelOrderDTO]:
    if name == "reviewer" and cfg.review_profile and legacy:
        order = RoutingModelOrderDTO(model=legacy.model, fallbacks=legacy.fallbacks, locked=legacy.locked)
        return dict.fromkeys(config.WEIGHTS, order)
    pools = cfg.agent_models.get(name)
    if pools is None and name == "supervisor" and legacy and (legacy.model or legacy.fallbacks):
        return {size: RoutingModelOrderDTO(model=legacy.model, fallbacks=legacy.fallbacks, locked=legacy.locked)
                for size in config.WEIGHTS}
    inherited = legacy if name == "reviewer" and cfg.reviewer_models_inherited else None
    return {size: RoutingModelOrderDTO(
        model=names[0] if names else None, fallbacks=names[1:], locked=inherited.locked if inherited else True,
        learned=inherited.learned if inherited and size != "light" else None,
    ) for size, names in (pools or {}).items()}


def parse_agent_model_changes(agents: dict, known: set[str], library: str | None
                              ) -> dict[str, dict[str, list[str] | None]]:
    changes = {}
    for name, spec in agents.items():
        try:
            is_agent = name in known and profiles.kind_of(name, library) == "agent"
        except (KeyError, profiles.ProfileError):
            is_agent = False
        if not is_agent:
            raise ServiceError("invalid", f"no routable agent named {name!r}")
        models = spec.get("models") if isinstance(spec, dict) else None
        if not isinstance(models, dict):
            raise ServiceError("invalid", f"{name}: models must map sizes to overrides")
        for size, order in models.items():
            if size not in config.WEIGHTS:
                raise ServiceError("invalid", f"{name}: size must be one of {', '.join(config.WEIGHTS)}")
            names = None
            if order is not None:
                model = order.get("model") if isinstance(order, dict) else None
                if not isinstance(model, str) or not model:
                    raise ServiceError("invalid", f"{name} {size}: model must be a profile name")
                fallbacks = order.get("fallbacks", [])
                if not isinstance(fallbacks, list) or not all(isinstance(n, str) and n for n in fallbacks):
                    raise ServiceError("invalid", f"{name} {size}: fallbacks must be a list of profile names")
                names = list(dict.fromkeys([model, *fallbacks]))
            changes.setdefault(name, {})[size] = names
    return changes


def merge_agent_model_changes(value, changes: dict[str, dict[str, list[str] | None]]) -> dict:
    stored = dict(value) if isinstance(value, dict) else {}
    for agent, sizes in changes.items():
        old = stored.pop(agent, None)
        pools = dict(old) if isinstance(old, dict) else {}
        for size, names in sizes.items():
            pools = {k: v for k, v in pools.items() if config.normalize_weight(k) != size}
            if names is not None:
                pools[size] = names
        if pools:
            stored[agent] = pools
    return stored


@dataclass
class RoutingPolicy:
    access: team_policy.Access
    managed: managed_models.Managed | None
    repo_root: str
    _runs: dict = field(default_factory=dict)
    user_library: bool = False      # profile names are the person's, never the repo's own
    _model_fields: Callable[[str], tuple[str, str | None] | None] | None = None

    @classmethod
    def load(cls, repo_root: str, *, user_library: bool = False, workspace: Workspace | None = None) -> RoutingPolicy:
        """The policy for ``repo_root``; ``user_library``: judging the
        person's own profiles (scope "user"), under the same policy."""
        acc = team_policy.access(repo_root)
        managed = None
        if acc.org_id is not None and acc.error is None:
            try:
                managed = managed_models.current(repo_root)
            except managed_models.ManagedUnavailable as e:
                acc = team_policy.Access(org_id=acc.org_id, error=str(e))
        from brindle.policy import workspace_models

        model_fields = workspace_models(workspace, user_library=user_library) if workspace else None
        return cls(acc, managed, repo_root, user_library=user_library, _model_fields=model_fields)

    @property
    def applies(self) -> bool:
        return self.access.org_id is not None or self.access.error is not None

    def _fields(self, profile: str) -> tuple[str | None, str | None]:
        """(provider, model) of ``profile`` as check_assign is given them (an alias kept)."""
        if profile not in self._runs:
            from brindle.policy import _profile_fields

            resolved = self._model_fields(profile) if self._model_fields else None
            self._runs[profile] = resolved or _profile_fields(profile, self.repo_root, user_library=self.user_library)
        return self._runs[profile]

    def runs(self, profile: str) -> tuple[str | None, str | None]:
        """(provider, model) a worker on ``profile`` runs: an alias the org's
        Claude route pins is named by its route id, as the policy names it."""
        provider, model = self._fields(profile)
        return provider, self.access.route_model(provider, model)

    def refusal(self, profile) -> str | None:
        """Why the org won't let a worker run on ``profile`` (a Profile), or None."""
        if not self.applies:
            return None
        acc = self.access
        if acc.error:
            return acc.error
        provider, model = self._fields(profile.name)
        if not provider:
            return f"brindle can't tell which agent profile {profile.name!r} runs"
        if why := acc.refusal(provider):
            return why
        if not acc.allows_run(provider, model):
            model = acc.route_model(provider, model)
            return team_policy.model_refusal(acc.org_id, acc.allowed_models, model or "no declared model",
                                             who=f"profile {profile.name!r}")
        if acc.allowed_profiles is not None and not team_policy._listed(
                profile.name, acc.allowed_profiles, None if self.user_library else self.repo_root):
            return (f"org {acc.org_id} allows only the profiles "
                    f"{', '.join(acc.allowed_profiles) or '(none)'}, not {profile.name!r}")
        return managed_models.refusal(self.managed, profile) if self.managed is not None else None

    def to_dto(self, profiles: list) -> RoutingPolicyDTO:
        """The policy block; ``profiles``: every profile, to map the allowed models onto."""
        if not self.applies:
            return RoutingPolicyDTO()
        acc = self.access
        if acc.error:
            return RoutingPolicyDTO(applies=True, org_id=acc.org_id, editable=False, reason=acc.error)
        models: dict[str, list[str]] = {m: [] for m in acc.allowed_models or ()}
        for p in profiles:
            provider, model = self._fields(p.name)
            if not model or self.refusal(p):
                continue
            if acc.allowed_models is None:
                models.setdefault(acc.route_model(provider, model), []).append(p.name)
                continue
            # By check_assign's test: an entry may name the alias itself or the id it is pinned to.
            pinned = acc.pinned(provider, model)
            for m in acc.allowed_models:
                if team_policy.model_allowed((m,), model, pinned):
                    models[m].append(p.name)
        locked = [RoutingLockDTO(kind="provider", name=cli, reason=why)
                  for cli in team_policy.CLI_NAMES if (why := acc.refusal(cli))]
        m = self.managed
        if m is not None:
            # An enforced agent_setup routes each agent; an older server's provider_config is one route.
            routed = [c for c in ("claude", "codex", "antigravity") if getattr(m, c) is not None]
            if m.config is not None:
                routed.append(m.config.provider)
            locked.extend(RoutingLockDTO(kind="route", name=r,
                                         reason=f"{m.describe()}; only an org admin can change these routes")
                          for r in routed)
        if acc.deny_personal_keys:
            locked.append(RoutingLockDTO(kind="keys", name="personal_api_keys",
                                         reason=f"org {acc.org_id} doesn't allow personal API keys"))
        return RoutingPolicyDTO(
            applies=True, org_id=acc.org_id,
            allowed_providers=_list(acc.allowed_providers), allowed_models=_list(acc.allowed_models),
            allowed_profiles=_list(acc.allowed_profiles), deny_personal_keys=acc.deny_personal_keys,
            models=models, locked=locked)


def _list(v):
    return None if v is None else list(v)
