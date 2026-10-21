"""JSON shapes for the desktop app. Each model is built from what
``view.snapshot`` / ``view.agent_entry`` or a db row already holds, so the
field names are the engine's own and the drift test keeps them in step."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from brindle.db import Agent, Autopilot, Milestone
from brindle.profiles import Profile


class _DTO(BaseModel):
    # Extra keys from the engine are ignored, never sent on.
    model_config = ConfigDict(extra="ignore", frozen=True)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class NativeSubagentDTO(_DTO):
    id: str
    agent_type: str
    started_at: float
    ended_at: float | None = None


class AgentDTO(_DTO):
    id: str
    profile: str
    provider: str
    status: str
    mode: str
    # Set (instead of the rest) when another identity holds the session.
    locked: str | None = None
    tokens: str | None = None
    unread: int | None = None
    hooks_off: str | None = None
    parent_id: str | None = None
    status_since: float | None = None
    pending: int | None = None
    reported: bool | None = None
    window: str | None = None
    tmux_server: str | None = None
    headless: bool | None = None
    subagents: list[NativeSubagentDTO] = []


class WorkspaceDTO(_DTO):
    id: str
    name: str
    branch: str
    base_branch: str | None = None
    path: str
    ahead: int | None = None
    behind: int | None = None
    dirty: int | None = None
    agents: list[AgentDTO] = []
    review: str | None = None
    review_state: str | None = None
    autopilot: bool = False
    asking: str | None = None


class SessionDTO(_DTO):
    id: str
    workspace_id: str
    profile: str
    status: str
    mode: str
    created_at: float
    status_since: float | None = None
    goal: str | None = None

    @classmethod
    def from_agent(cls, a: Agent, goal: str | None = None) -> SessionDTO:
        return cls(id=a.id, workspace_id=a.workspace_id, profile=a.profile, status=a.status,
                   mode=a.mode, created_at=a.created_at, status_since=a.status_since, goal=goal)


class PermissionChoiceDTO(_DTO):
    choice: str                  # allow_once | allow_always | deny
    label: str
    rule: str | None = None      # allow_always: the rule it writes, and whose


class PermissionDTO(_DTO):
    """A tool-permission request waiting on the person. ``via``: "hook"
    (brindle's hook holds the request: POST /inbox/{agent_id}/permission
    {request: request_id, digest, choice} answers it until ``hold_until``,
    refused for any request but the one shown, exactly as shown; choice
    "terminal" ends the hold so the CLI's own prompt asks instead) or
    "prompt" (it is at the CLI's own prompt: ``terminal`` says so, and the
    app's "Go to it", with the item's root_id and agent_id, is the way to
    answer it; no ``choices``)."""
    via: str
    request_id: str | None = None
    digest: str | None = None         # of the exact request and suggested rule shown
    hold_until: float | None = None   # when the hook stops holding it (epoch seconds)
    hold_note: str | None = None      # the same for the person: "waiting for you" while over an hour is left
    # Whether the CLI's own prompt shows in its terminal meanwhile too (None: not known).
    terminal_prompt_visible: bool | None = None
    terminal: str | None = None
    tool: str | None = None
    kind: str | None = None      # brindle.permissions.KINDS
    target: str | None = None    # the command, path or URL, complete
    wording: str | None = None   # the prompt as the provider words it
    choices: list[PermissionChoiceDTO] = []


class InboxActionDTO(_DTO):
    """A route the app can call to act on the item itself."""
    method: str
    path: str


class InboxItemDTO(_DTO):
    kind: str
    reason: str
    # An org notice belongs to no agent or workspace.
    agent_id: str | None = None
    workspace_id: str | None = None
    root_id: str | None = None     # the agent's session: with agent_id, where the app links to
    detail: str | None = None
    notice_id: str | None = None   # an org notice's: POST /inbox/{notice_id}/read
    fix: str | None = None         # a setup item's: what the app's fix button repairs (codex_hook_trust)
    permission: PermissionDTO | None = None
    action: InboxActionDTO | None = None


class MilestoneDTO(_DTO):
    id: int
    position: int
    title: str
    status: str
    detail: str | None = None
    check_cmd: str | None = None
    profile: str | None = None
    repo: str | None = None
    checked_at: float | None = None
    output: str | None = None
    state: str | None = None       # passed | not_done | regressed | failed (couldn't run); None: never checked
    state_why: str | None = None
    label: str | None = None       # plain words for state: "not done yet", "regressed", "couldn't run: <why>"

    @classmethod
    def from_row(cls, m: Milestone) -> MilestoneDTO:
        from brindle.autopilot import state_text

        return cls.model_validate({**vars(m), "label": state_text(m)})


class GoalDTO(_DTO):
    root_id: str
    enabled: bool
    goal: str | None = None
    detail: str | None = None
    state: str
    note: str | None = None
    progress: int
    usage_resets_at: float | None = None
    milestones: list[MilestoneDTO] = []

    @classmethod
    def from_autopilot(cls, ap: Autopilot, milestones: list[Milestone]) -> GoalDTO:
        return cls(root_id=ap.root_id, enabled=bool(ap.enabled), goal=ap.goal, detail=ap.detail,
                   state=ap.state, note=ap.note, progress=ap.progress,
                   usage_resets_at=ap.usage_resets_at,
                   milestones=[MilestoneDTO.from_row(m) for m in milestones])


class DecisionDTO(_DTO):
    id: str
    repo_key: str
    topic: str
    body: str
    rationale: str
    scope: list[str]
    status: str
    created_at: float

    @classmethod
    def from_row(cls, d: dict[str, Any]) -> DecisionDTO:
        return cls.model_validate(d)


class ProfileDTO(_DTO):
    name: str
    description: str
    provider: str
    model: str | None = None
    permission_mode: str | None = None
    effort: str | None = None
    headless: bool = False
    local: bool = False
    fallback: list[str] = []

    @classmethod
    def from_profile(cls, p: Profile) -> ProfileDTO:
        # env is left out: its values can be secrets.
        return cls(name=p.name, description=p.description, provider=p.provider, model=p.model,
                   permission_mode=p.permission_mode, effort=p.effort, headless=p.headless,
                   local=p.local, fallback=list(p.fallback))


class RoutingSettingDTO(_DTO):
    values: list[str]
    source: str              # "default", "user", "repo" or "local"


class RoutingTierModelDTO(_DTO):
    """One of a tier's profiles in model terms, as the website names them:
    the model id the org's policy is checked against and the agent's name."""
    profile: str
    provider: str | None = None
    provider_name: str | None = None     # "Claude Code", "Codex", ...
    model: str | None = None
    allowed: bool = True


class RoutingTierDTO(RoutingSettingDTO):
    models: list[RoutingTierModelDTO] = []


class RoutingProfileDTO(_DTO):
    name: str
    provider: str
    provider_name: str | None = None
    model: str | None = None
    installed: bool
    signed_in: bool
    excluded: bool
    cost_tier: str           # "low", "medium" or "high"
    allowed: bool = True     # whether the org's policy lets a worker run on it
    refusal: str | None = None


class RoutingLockDTO(_DTO):
    kind: str                # "provider" (not allowed), "route" (managed by the org) or "keys"
    name: str
    reason: str


class RoutingPolicyDTO(_DTO):
    """The org policy routing answers to. ``applies``: a Team or Enterprise
    org's policy covers this person; ``editable``: they may change routing
    at all (not when the policy applies but can't be read: ``reason``).
    ``models``: each model the org allows (every model the profiles run, with
    no list) -> the local profiles that run it."""
    applies: bool = False
    org_id: str | None = None
    editable: bool = True
    reason: str | None = None
    allowed_providers: list[str] | None = None
    allowed_models: list[str] | None = None
    allowed_profiles: list[str] | None = None
    deny_personal_keys: bool = False
    models: dict[str, list[str]] = {}
    locked: list[RoutingLockDTO] = []


class RoutingDTO(_DTO):
    tiers: dict[str, RoutingTierDTO]
    reviewer: RoutingSettingDTO
    exclude_providers: RoutingSettingDTO
    profiles: list[RoutingProfileDTO]
    policy: RoutingPolicyDTO = RoutingPolicyDTO()


class AccountOrgDTO(_DTO):
    id: str
    name: str
    role: str
    plan: str
    personal: bool
    current: bool            # the org this account is using now


class AccountDTO(_DTO):
    signed_in: bool
    name: str | None = None
    plan: str | None = None
    orgs: list[AccountOrgDTO] = []


class PreflightActionDTO(_DTO):
    name: str                # login | set_key | trust | consent | open_provider_once
    method: str
    path: str
    body: dict[str, Any] = {}


class PreflightItemDTO(_DTO):
    """One thing a provider's CLI would stop on before a session (brindle.preflight)."""
    kind: str                # signin | key | trust | consent | setup
    id: str
    provider: str
    title: str
    detail: str
    action: PreflightActionDTO | None = None
    link: str | None = None
