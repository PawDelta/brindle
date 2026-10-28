"""JSON shapes for the desktop app. Each model is built from what
``view.snapshot`` / ``view.agent_entry`` or a db row already holds, so the
field names are the engine's own and the drift test keeps them in step."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from brindle.db import Agent, Autopilot, Milestone
from brindle.profiles import Profile


class _DTO(BaseModel):
    # Extra keys from the engine are ignored, never sent on.
    model_config = ConfigDict(extra="ignore", frozen=True)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class OrgIdentityDTO(_DTO):
    sub: str
    org_id: str
    role: str | None
    policy_role: str | None


class OrgRoleDTO(_DTO):
    name: str
    builtin: bool
    permissions: list[str]
    holders: list[str] = []
    okta_group: str | None = None
    team_weight: float = 1


class OrgMemberDTO(_DTO):
    sub: str
    email: str
    role: str
    policy_role: str | None = None


class OrgGrantPolicyDTO(_DTO):
    """Account GrantPolicy (account-api 8fd09374); a complete replacement block."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    allow: StrictBool
    kinds: list[Literal["read", "write", "edit", "bash", "fetch", "mcp", "other"]]
    overridable: list[Literal["git-push", "env"]]
    max_minutes: Annotated[StrictInt, Field(ge=1, le=1440)]
    max_uses: Annotated[StrictInt, Field(ge=1, le=1000)]
    deny: list[str] = []
    allow_persistent: StrictBool = False
    lifetimes: list[Literal["session", "time", "forever"]] = ["session", "time"]
    scopes: list[Literal["branch", "remote", "repo", "all"]] = ["branch"]


class OrgApprovalPolicyDTO(_DTO):
    version: int
    permission_grants: OrgGrantPolicyDTO | None
    roles: dict[str, OrgGrantPolicyDTO | None]


class OrgPermissionsDTO(_DTO):
    identity: OrgIdentityDTO
    roles: list[OrgRoleDTO]
    members: list[OrgMemberDTO]
    inventory_error: str | None = None
    policy: OrgApprovalPolicyDTO
    can_edit_policy: bool
    policy_readonly_reason: str | None


class OrgAssignmentRequestDTO(_DTO):
    model_config = ConfigDict(extra="forbid", frozen=True)
    org_id: str
    actor_sub: str
    sub: Annotated[str, Field(min_length=1, max_length=128)]
    role: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class OrgAssignmentDTO(_DTO):
    org_id: str
    sub: str
    role: str


class OrgPolicyRequestDTO(_DTO):
    model_config = ConfigDict(extra="forbid", frozen=True)
    org_id: str
    actor_sub: str
    if_version: Annotated[StrictInt, Field(ge=0)]
    role: str | None
    permission_grants: OrgGrantPolicyDTO | None


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
    name: str | None = None      # the person's name for the session; shown instead of the goal

    @classmethod
    def from_agent(cls, a: Agent, goal: str | None = None) -> SessionDTO:
        return cls(id=a.id, workspace_id=a.workspace_id, profile=a.profile, status=a.status,
                   mode=a.mode, created_at=a.created_at, status_since=a.status_since, goal=goal,
                   name=a.name)


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
    phase: str = "in_progress"     # done | in_review | in_progress (autopilot.milestone_phases)

    @classmethod
    def from_row(cls, m: Milestone, phase: str = "in_progress") -> MilestoneDTO:
        from brindle.autopilot import state_text

        return cls.model_validate({**vars(m), "label": state_text(m), "phase": phase})


class GoalDTO(_DTO):
    root_id: str
    enabled: bool
    goal: str | None = None
    detail: str | None = None
    state: str
    note: str | None = None
    progress: int
    usage_resets_at: float | None = None
    goal_phase: str = "in_progress"  # in_progress | in_review | ready_for_pr | pr_open | done (session_review.goal_phase)
    pr_url: str | None = None        # the session's PR, from pr_open on
    milestones: list[MilestoneDTO] = []

    @classmethod
    def from_autopilot(cls, ap: Autopilot, milestones: list[Milestone],
                       phases: dict[int, str] | None = None,
                       goal_phase: tuple[str, str | None] = ("in_progress", None)) -> GoalDTO:
        phases = phases or {}
        return cls(root_id=ap.root_id, enabled=bool(ap.enabled), goal=ap.goal, detail=ap.detail,
                   state=ap.state, note=ap.note, progress=ap.progress,
                   usage_resets_at=ap.usage_resets_at,
                   goal_phase=goal_phase[0], pr_url=goal_phase[1],
                   milestones=[MilestoneDTO.from_row(m, phases.get(m.id, "in_progress"))
                               for m in milestones])


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
    lint_errors: list[str] = []     # the profile's automatic check (`brindle profile lint`)
    lint_warnings: list[str] = []

    @classmethod
    def from_profile(cls, p: Profile, lint_errors: list[str] = (), lint_warnings: list[str] = ()) -> ProfileDTO:
        # env is left out: its values can be secrets.
        return cls(name=p.name, description=p.description, provider=p.provider, model=p.model,
                   permission_mode=p.permission_mode, effort=p.effort, headless=p.headless,
                   local=p.local, fallback=list(p.fallback),
                   lint_errors=list(lint_errors), lint_warnings=list(lint_warnings))


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
    lint_errors: list[str] = []     # the profile's automatic check (`brindle profile lint`)
    lint_warnings: list[str] = []


class RoutingKindDTO(_DTO):
    """One kind of work (light, medium, heavy, advanced, reviewer, supervisor):
    the owner's pick and its ordered fallbacks, whether it is locked against
    hosted learning, and the different profile learning is running for it now
    (``learned``; None when it keeps the owner's pick)."""
    model: str | None = None
    fallbacks: list[str] = []
    locked: bool = False
    learned: str | None = None
    effort: str | None = None          # the owner's effort for whatever runs this kind (None: its profile's own)
    instructions: str | None = None    # text added to the prompt of whatever runs this kind


class RuleDTO(_DTO):
    """A rule pack that can be given to an agent."""
    name: str
    description: str
    source: str


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
    kinds: dict[str, RoutingKindDTO] = {}


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


class ActionTerminalDTO(_DTO):
    """An action terminal (a sign-in, ``brindle keys set``; see
    brindle.daemon.terminal.open_action). The app's tmux client attaches to
    the pane ``target`` on ``server`` (``tmux -L server``; None: the default
    server); ``terminal`` is the id POST /terminals/{id}/input takes.
    ``started`` is False when a press found one still running."""
    terminal: str
    started: bool
    target: str
    server: str | None = None


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


# -- how each provider connects (brindle.connections) -------------------------------------------


class CloudLoginDTO(_DTO):
    """A cloud login's state (brindle.cloud_login.status): ``valid`` now,
    ``expires_at`` (epoch seconds, None when unknown), ``expired`` when only
    ``renew_command`` (the person) can renew it."""
    valid: bool
    expired: bool
    expires_at: float | None = None
    renew_command: str = ""
    note: str = ""


class ProviderChoiceDTO(_DTO):
    """The person's saved choice (PUT /providers/{p}/connection)."""
    method: str
    cloud: str | None = None
    settings: dict[str, str] = {}


class ProviderConnectionDTO(_DTO):
    """How a provider connects now: ``method`` auto | sign_in | api_key |
    cloud | org (the org decides, with no cloud route here), and for cloud the
    route (``cloud``: bedrock, vertex, foundry, azure), its non-secret
    ``settings`` (variable -> value) and the ``login`` it uses. An org's route
    wins over ``choice``, the person's saved choice; ``managed`` says why.
    ``clouds``: the routes the person can choose for this provider."""
    provider: str
    method: str
    choice: ProviderChoiceDTO | None = None
    cloud: str | None = None
    settings: dict[str, str] = {}
    login: CloudLoginDTO | None = None
    managed: str | None = None
    clouds: list[str] = []


class AwsProfileDTO(_DTO):
    name: str
    sso: bool
    region: str | None = None


class GcloudOptionsDTO(_DTO):
    adc: bool
    project: str | None = None
    region: str | None = None


class AzureOptionsDTO(_DTO):
    tenant_id: str
    subscription_id: str | None = None
    subscription_name: str | None = None


class CloudOptionsDTO(_DTO):
    """What this machine's cloud CLIs are set up with, read from their files
    (never a secret): AWS profiles, gcloud's application-default login and
    default project, the Azure CLI's signed-in tenant (None when signed out),
    and the usual ``regions`` per route."""
    aws_profiles: list[AwsProfileDTO]
    gcloud: GcloudOptionsDTO
    azure: AzureOptionsDTO | None = None
    regions: dict[str, list[str]] = {}


# -- permissions (brindle.permissions, brindle.permission_grants) ------------------------------


class PermissionLimitsDTO(_DTO):
    """An ask rule's limits on saved approvals: how far (``max_scope``, a
    LEVELS entry for pushes or a pattern) and how long (``life`` session |
    time | forever, with ``minutes`` for time)."""
    max_scope: str | None = None
    life: str | None = None
    minutes: float | None = None


class PermissionExceptionDTO(_DTO):
    """Requests a rule doesn't apply to (a deny's exception): matched like a rule."""
    kind: str
    match: str
    match_type: str
    describe: str


class PermissionRuleDTO(_DTO):
    """One rule of the person's store (GET /permissions). ``decision`` allow |
    deny | ask; ``source`` preset | user | learned; ``match_type`` exact |
    prefix | glob or a named matcher (permissions.BUILTIN_MATCHERS; a
    ``push`` rule's match is "<source> → <destination>"). ``repo_root``: the
    one repo it applies in (None: every repo). ``prompts``: asks it caused
    in the last day."""
    id: str
    kind: str
    match: str
    match_type: str
    decision: str
    source: str
    describe: str
    created: float = 0.0
    label: str = ""
    limits: PermissionLimitsDTO | None = None
    expires: float | None = None
    expired: bool = False
    exclusions: list[PermissionExceptionDTO] = []
    repo_root: str | None = None
    prompts: int = 0


class PermissionPresetDTO(_DTO):
    name: str                # autonomous | permissive | standard | secure
    description: str
    unmatched: str           # what the preset does with a request no rule covers
    current: bool


class SavedActionDTO(_DTO):
    """A decision saved from the Inbox (permissions.SavedAction). ``kind``: a
    permissions.KINDS entry or ``push``; ``scope`` the ladder rung (exact,
    general, repository, ...) and ``pattern`` what it covers; ``request`` the
    exact request it was saved from; ``life`` session | time | forever."""
    id: str
    kind: str
    pattern: str
    decision: str            # allow | deny
    scope: str
    level: str | None = None
    request: dict[str, Any] = {}
    life: str = "forever"
    minutes: float | None = None
    expires: float | None = None
    expired: bool = False
    session: str | None = None
    repo: str | None = None
    created: float = 0.0
    note: str = ""


class PermissionGroupDTO(_DTO):
    """A capability group (brindle.permission_groups): what it answers for a
    request no rule covers, unless one of its subcategories says otherwise."""
    name: str                # files | shell | git | network | mcp
    baseline: str            # allow | ask | deny
    subcategories: dict[str, str] = {}   # subcategory -> inherit | allow | ask | deny


class PermissionPolicyDTO(_DTO):
    """GET /permissions: the chosen preset, the unmatched setting, every rule
    (editable, the preset's included), the saved actions, per-rule prompt
    counts ("unmatched" for asks no rule caused) and the capability groups."""
    preset: str | None
    unmatched: str
    presets: list[PermissionPresetDTO]
    rules: list[PermissionRuleDTO]
    saved: list[SavedActionDTO]
    prompt_counts: dict[str, int] = {}
    groups: list[PermissionGroupDTO] = []


class AskScopeDTO(_DTO):
    """One rung of an ask's scope ladder: ``value`` exact | general |
    repository | remote | server | all, ``pattern`` what a grant of it covers."""
    value: str
    pattern: str
    label: str
    level: str | None = None


class AskResultDTO(_DTO):
    agent_id: str
    request_id: str
    exact: str
    why: str | None = None


class AskDTO(_DTO):
    """One pending request, generalized. ``blocked``: a deny rule covers it
    (its ``rule``); else ``rule`` is the ask rule that asked, with its
    ``limits`` (None: no rule covers it, and unmatched actions ask)."""
    agent_id: str
    root_id: str
    request_id: str
    digest: str
    repo: str
    kind: str
    tool: str
    exact: str
    summary: str
    waiting_since: float
    worker: str = ""
    blocked: bool = False
    rule: str | None = None
    limits: PermissionLimitsDTO | None = None
    reason: str = ""
    # Why the worker's supervisor, which had it first, left it to the person
    # ("Supervisor escalated: ...", "Supervisor didn't answer in 2 min"); "" when it never had it.
    escalation: str = ""


class AskGroupDTO(_DTO):
    """GET /permissions/asks: one Inbox item per repo, kind and general form,
    generalized from its oldest request. ``policy``: the tightest limits
    among its requests (a deny blocks the whole item). ``modes``: what
    POST /permissions/asks/decide accepts for it."""
    key: str
    repo: str
    kind: str
    title: str
    general: AskScopeDTO
    scopes: list[AskScopeDTO]
    items: list[AskDTO]
    blocked: bool = False
    rule: str | None = None
    limits: PermissionLimitsDTO | None = None
    modes: list[str] = []


class AskDecisionDTO(_DTO):
    """POST /permissions/asks/decide: what one decision did."""
    mode: str
    scope: AskScopeDTO
    grant: SavedActionDTO | None = None
    resumed: list[AskResultDTO] = []
    stopped: list[AskResultDTO] = []
    skipped: list[AskResultDTO] = []
    failed: list[AskResultDTO] = []


class AllowAllGrantDTO(_DTO):
    group: str
    kind: str
    scope: AskScopeDTO
    mode: str
    minutes: float | None = None
    requests: list[str] = []


class AllowAllPlanDTO(_DTO):
    """GET /permissions/asks/plan (what allowing everything waiting would
    grant) and the result of POST /permissions/asks/allow-all and
    /unblock (``saved``, ``resumed``, ``failed`` filled in; ``excepted``,
    ``added``, ``locked`` for unblock)."""
    grants: list[AllowAllGrantDTO]
    resumes: int
    skipped: list[AskResultDTO] = []
    saved: list[SavedActionDTO] = []
    resumed: list[AskResultDTO] = []
    failed: list[AskResultDTO] = []
    excepted: int | None = None
    added: int | None = None
    locked: list[AskResultDTO] = []
