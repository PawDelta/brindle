"""What an org's policy lets this person do with routing. ``GET /v1/routing``
shows it and ``PUT /v1/routing`` enforces it, by the tests a delegation is
judged by (``team_policy.access``, as the picker asks it, and the managed
models' ``refusal``), so the app never offers a profile a worker would then
be refused. Models are named as the org policy and the website name them
(the model id check_assign matches), agents by their product names."""

from __future__ import annotations

from dataclasses import dataclass, field

from brindle.pro import managed_models, team_policy
from brindle.service.dto import RoutingLockDTO, RoutingPolicyDTO


def provider_name(provider: str | None) -> str | None:
    return team_policy.CLI_NAMES.get(provider, provider) if provider else None


@dataclass
class RoutingPolicy:
    access: team_policy.Access
    managed: managed_models.Managed | None
    repo_root: str
    _runs: dict = field(default_factory=dict)

    @classmethod
    def load(cls, repo_root: str) -> RoutingPolicy:
        acc = team_policy.access(repo_root)
        managed = None
        if acc.org_id is not None and acc.error is None:
            try:
                managed = managed_models.current(repo_root)
            except managed_models.ManagedUnavailable as e:
                acc = team_policy.Access(org_id=acc.org_id, error=str(e))
        return cls(acc, managed, repo_root)

    @property
    def applies(self) -> bool:
        return self.access.org_id is not None or self.access.error is not None

    def _fields(self, profile: str) -> tuple[str | None, str | None]:
        """(provider, model) of ``profile`` as check_assign is given them (an alias kept)."""
        if profile not in self._runs:
            from brindle.policy import _profile_fields

            self._runs[profile] = _profile_fields(profile, self.repo_root)
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
            return (f"profile {profile.name!r} runs {model or 'no declared model'}; org {acc.org_id} "
                    f"allows only {', '.join(acc.allowed_models or ()) or 'no models'}")
        if acc.allowed_profiles is not None and profile.name not in acc.allowed_profiles:
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
