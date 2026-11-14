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


class RepoDTO(_DTO):
    """A repo brindle has worked in. ``demo``: brindle's practice repo (under the
    demo folder), which the app does not offer as a project to start in."""

    repo_root: str
    name: str
    demo: bool = False


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


class OrgControlDTO(_DTO):
    """team_policy.OrgControl: Block (deny) or Ask for one grouping
    (permission_grants.ORG_GROUPINGS) or one pattern ("<kind> <match_type> <match>")."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Literal["deny", "ask"]
    reason: Annotated[str, Field(max_length=300)] = ""
    group: str | None = None
    rule: str | None = None


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
    controls: list[OrgControlDTO] = []
    org_name: Annotated[str, Field(max_length=100)] = ""


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
    accepts_files: bool = False
    status: str
    mode: str
    dismissed_at: float | None = None
    # Set (instead of the rest) when another identity holds the session.
    locked: str | None = None
    tokens: str | None = None
    # The model its transcript last ran on (the usage meter names it); absent until it has run.
    model: str | None = None
    # Title of the milestone current when the worker was assigned; absent for a supervisor or when none.
    milestone: str | None = None
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
    request: str | None = None   # full startup prompt, before the goal is derived
    name: str | None = None      # the person's name for the session; shown instead of the goal

    @classmethod
    def from_agent(cls, a: Agent, goal: str | None = None) -> SessionDTO:
        return cls(id=a.id, workspace_id=a.workspace_id, profile=a.profile, status=a.status,
                   mode=a.mode, created_at=a.created_at, status_since=a.status_since, goal=goal,
                   name=a.name, request=a.task)


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


class PlanOfferMilestoneDTO(_DTO):
    title: str
    verified: bool


class PlanOfferDTO(_DTO):
    """Current session context for deciding whether to plan the goal first."""
    session_name: str
    goal: str
    detail: str | None = None
    milestones: list[PlanOfferMilestoneDTO]
    workers: int
    repos: int
    reasons: list[str]


class InboxItemDTO(_DTO):
    kind: str
    reason: str
    # An org notice belongs to no agent or workspace.
    agent_id: str | None = None
    workspace_id: str | None = None
    root_id: str | None = None     # the agent's session: with agent_id, where the app links to
    detail: str | None = None
    notice_id: str | None = None   # an org notice's: POST /inbox/{notice_id}/read
    request_id: str | None = None  # a doc request's: POST /inbox/{request_id}/dismiss
    doc: int | str | None = None   # integer kb_docs id or opaque analysis-doc library id
    section: str | None = None
    fix: str | None = None         # a setup item's: what the app's fix button repairs (codex_hook_trust)
    permission: PermissionDTO | None = None
    action: InboxActionDTO | None = None
    visual_id: str | None = None   # a visual's open question: GET /v1/sessions/{root_id}/visuals
    plan_offer: PlanOfferDTO | None = None
    url: str | None = None         # a pr_opened item's: the open PR on GitHub


class VisualOptionDTO(_DTO):
    id: str
    label: str
    detail: str | None = None


class VisualAnswerDTO(_DTO):
    option: str | None
    note: str | None = None
    at: float
    attachments: list[str] = []


class VisualDTO(_DTO):
    """A visual in a session (brindle.visuals). ``rev``: its current revision,
    served at /v1/visuals/{id}/r/{rev}; an answer names the revision it read.
    ``unread``: a revision the person hasn't opened yet."""

    id: str
    session: str
    answer_agent_id: str
    accepts_files: bool
    title: str
    by: str
    kind: Literal["visual", "plan"]
    rev: int
    question: str | None = None
    options: list[VisualOptionDTO] | None = None
    answer: VisualAnswerDTO | None = None
    kept: bool
    unread: bool
    created_at: float


class PlanSectionDTO(_DTO):
    """One section of a plan's revision: what a comment anchors to.
    ``kind``: markdown, or a diagram (``markdown`` then holds its mermaid or
    SVG source, and ``caption`` its figure caption)."""

    id: str
    title: str
    kind: Literal["markdown", "mermaid", "svg"] = "markdown"
    markdown: str
    caption: str | None = None


class PlanOptionDTO(VisualOptionDTO):
    """One alternative of a plan's decision, as its card shows it: trade-offs
    as + (``pros``) and − (``cons``) lines, and the files it ``touches``."""

    pros: list[str] = []
    cons: list[str] = []
    touches: list[str] = []


class PlanDecisionDTO(_DTO):
    """A decision that shapes the plan: its alternatives and the supervisor's pick."""

    id: str
    title: str
    options: list[PlanOptionDTO]
    mine: str | None = None
    challenge: str | None = None
    # Who voted for which option ({option: [voter]}), and the person's own
    # confirmed pick, which their verdict starts from.
    votes: dict[str, list[str]] = {}
    confirmed: str | None = None


class PlanWorkerDTO(_DTO):
    """One worker of the proposed split: the files it expects to change and
    the one test that proves its task done."""

    id: str
    title: str
    after: list[str] = []
    files: list[str] = []
    test: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    state: Literal["queued", "working", "in review", "merged", "cancelled"] = "queued"
    test_state: Literal["not run", "failing", "passing"] = "not run"


class PlanCommentDTO(_DTO):
    section: str
    text: str


class PlanVerdictDTO(_DTO):
    approved: bool
    picks: dict[str, str] = {}
    comments: list[PlanCommentDTO] = []
    note: str | None = None
    at: float
    rev: int
    attachments: list[str] = []


class PlanAnchorDTO(_DTO):
    """Where a thread is anchored (brindle.plan_docs): a text range of a
    section (``text``: ``quote`` between ``start`` and ``end``), a diagram
    section, a decision (optionally one option) or a whole section, the
    ask, the lede or the split. ``state``: where it is in the current
    revision: ``current``, ``moved`` (found elsewhere) or ``detached`` (gone)."""

    kind: Literal["text", "diagram", "decision", "section"]
    section: str | None = None
    start: int | None = None
    end: int | None = None
    quote: str | None = None
    decision: str | None = None
    option: str | None = None
    state: Literal["current", "moved", "detached"] = "current"


class PlanReplyDTO(_DTO):
    id: int
    author: str
    text: str
    attachments: list[str] = []
    at: float
    sent: bool = False
    from_supervisor: bool = False


class PlanThreadDTO(_DTO):
    """A comment thread, or a suggestion (``before`` becomes ``after`` when
    the author accepts it). ``sent``: a verdict carried it to the supervisor."""

    id: int
    kind: Literal["comment", "suggestion"]
    rev: int
    author: str
    text: str
    anchor: PlanAnchorDTO
    before: str | None = None
    after: str | None = None
    attachments: list[str] = []
    status: Literal["open", "resolved", "accepted", "rejected"]
    sent: bool = False
    at: float
    replies: list[PlanReplyDTO] = []
    question: bool = False


class PlanRevisionDTO(_DTO):
    """One revision of a plan: the supervisor's proposal, the person's edit
    or accepted suggestion, or a teammate's text from the Library (``author``
    is teammate:<sub>); ``changed``, the sections it changed."""

    rev: int
    author: str
    source: Literal["proposal", "edit", "suggestion"]
    changed: list[str] = []
    at: float


class PlanRevisionDetailDTO(PlanRevisionDTO):
    """An earlier revision's content: GET /v1/plans/{id}/revisions/{n}."""

    lede: str
    sections: list[PlanSectionDTO]
    markdown: str
    decisions: list[PlanDecisionDTO]
    split: list[PlanWorkerDTO]
    ask: str | None = None


class PlanGateRowDTO(_DTO):
    """One row of a plan's build gate: a reviewer (``sub``) or an org review
    rule no one on the list meets (``name``). ``meets``: the rules this
    reviewer's sign-off also counts for. ``met_by``: who met a rule.
    ``changes_by``: the rule's signers who have since asked for changes."""

    sub: str | None = None
    name: str | None = None
    required: bool
    status: Literal["signed_off", "changes_requested", "waiting", "waived"]
    met_by: list[str] = []
    changes_by: list[str] = []
    meets: list[str] = []
    you: bool = False


class PlanPolicyWaiverDTO(_DTO):
    """An org admin's waiver of a review rule, for the revision on screen."""

    by: str
    at: float | None = None
    reason: str


class PlanPolicyStandInDTO(_DTO):
    """Someone an org admin named to sign for a review rule until ``until``."""

    stand_in: str
    until: float
    by: str | None = None
    at: float | None = None


class PlanPolicyOpenDTO(_DTO):
    """Someone a review rule counts who has read the doc, and when they last did."""

    sub: str
    at: float | None = None


class PlanGatePolicyDTO(_DTO):
    """An org review rule covering a shared plan, which nobody on the doc can
    remove. ``when``: paths, label or any; ``matched``: what in this revision
    set it off; ``needs_kind``: team, role or person; ``eligible``: how many
    people can meet it today; ``asked_at``: the last Ask again; ``opened_by``:
    who the rule counts has read the doc."""

    org_id: str
    policy: str
    name: str
    note: str | None = None
    when: str | None = None
    label: str | None = None
    paths: list[str] = []
    matched: list[str] = []
    needs_kind: str | None = None
    needs_name: str | None = None
    approvals: int
    eligible: int | None = None
    status: Literal["signed_off", "changes_requested", "waiting", "waived"]
    met_by: list[str] = []
    changes_by: list[str] = []
    waiver: PlanPolicyWaiverDTO | None = None
    stand_ins: list[PlanPolicyStandInDTO] = []
    asked_at: float | None = None
    opened_by: list[PlanPolicyOpenDTO] = []
    set_by: str | None = None
    set_at: float | None = None


class PlanUnconfirmedDTO(_DTO):
    """Why the Library couldn't confirm a shared plan's review when the person
    tried to approve it. ``approval_held``: their Approve applies by itself
    once it can; ``can_stop_sharing``: they may make it a personal doc instead."""

    reason: Literal["offline", "sign_in", "key_unavailable", "library_error", "other_account"]
    message: str
    approval_held: bool = False
    can_stop_sharing: bool = False


class PlanGateDTO(_DTO):
    """A shared plan's build gate (brindle.plans.gate). ``reason``: why it
    can't build now; ``signed`` of ``required`` sign-offs are in. ``policies``:
    every org review rule covering it, including those a listed reviewer meets.
    ``approval_refused``: why an Approve held for the Library wasn't applied."""

    state: Literal["waiting", "changes_requested", "conflict", "ready", "closed"]
    reason: str | None = None
    required: int
    signed: int
    waiting_on: list[str] = []
    reviewers: list[PlanGateRowDTO] = []
    policies: list[PlanGatePolicyDTO] = []
    unconfirmed: PlanUnconfirmedDTO | None = None
    approval_refused: str | None = None


class LibraryFolderDTO(_DTO):
    """A Library folder (GET /v1/library/folders): the server's, plus ``repo``,
    the repo it was made for as this machine recorded it (None: not known here)."""
    id: str
    name: str
    visibility: Literal["private", "shared"] = "private"
    repo: str | None = None
    created_at: float | None = None


class DesignTaskDTO(_DTO):
    """One task of a design's plan (GET /v1/analysis-docs/{id} ``tasks``), in
    its board state: queued, working, in review, merged or cancelled."""
    id: str
    title: str
    state: str


class PlanDTO(_DTO):
    """A planning doc (brindle.plans): markdown brindle renders itself, split
    into its ``##`` sections. ``state``: open (the person hasn't decided),
    redirected (their comments went back; a new revision is coming) or
    approved. While open or redirected, the session's assign calls queue."""

    id: str
    session: str
    title: str
    rev: int
    state: Literal["open", "redirected", "approved"]
    # The person's request as the supervisor restated it; a comment on it
    # names the section "ask".
    ask: str | None = None
    markdown: str
    lede: str
    sections: list[PlanSectionDTO]
    decisions: list[PlanDecisionDTO]
    split: list[PlanWorkerDTO]
    verdict: PlanVerdictDTO | None = None
    queued: int
    created_at: float
    revisions: list[PlanRevisionDTO] = []
    threads: list[PlanThreadDTO] = []
    # The person may change the text now (direct edits, accepting suggestions): only while open.
    can_edit: bool = False
    # Its reference from Settings › Doc format's naming (DES-0042), shown beside the
    # title; null for a plan written without one (brindle.doc_templates).
    reference: str | None = None
    # Set once the plan is shared for team review (brindle.pro.shared_docs).
    gate: PlanGateDTO | None = None
    # The Team plan it was shared under has ended: a personal doc, with no gate, until it comes back.
    team_ended: bool = False


class SharedDesignDTO(PlanDTO):
    """A design someone else shared, as the person reviewing it reads it
    (brindle.pro.shared_reader): ``id`` is its Library doc id and it has no
    plan on this machine. ``can_*``: what the Library lets this account do,
    ``can_sign_off`` false only when it can't be one of the signers.
    ``author``: who wrote it, ``person`` if that is this account."""

    me: str
    author: str | None = None
    can_comment: bool = False
    can_suggest: bool = False
    can_sign_off: bool = False
    signed_off: bool = False


class KeptVisualDTO(_DTO):
    """A kept visual: outlives its session. Its page: /v1/kept-visuals/{id}/html."""

    id: str
    title: str
    from_session_name: str
    repo: str
    answer: VisualAnswerDTO | None = None
    kept_at: float


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
    phase_name: str | None = None  # the goal's phase it is grouped under (goals.md `phase:`); None: no phases

    @classmethod
    def from_row(cls, m: Milestone, phase: str = "in_progress") -> MilestoneDTO:
        from brindle.autopilot import state_text

        return cls.model_validate({**vars(m), "label": state_text(m), "phase": phase})


class PrStateDTO(_DTO):
    """The sidebar's PR line (session_review.session_pr_state): state is
    ci | ready | failed | unknown; ``done`` of ``total`` checks have finished."""
    number: int
    state: str
    done: int = 0
    total: int = 0
    unpushed: int = 0
    failed_checks: list[str] = []


class GoalDTO(_DTO):
    root_id: str
    enabled: bool
    plan_offer: str | None = None   # id of brindle's open offer to plan this goal first
    goal: str | None = None
    summary: str | None = None
    detail: str | None = None
    state: str
    note: str | None = None
    progress: int
    usage_resets_at: float | None = None
    goal_phase: str = "in_progress"  # in_progress | in_review | ready_for_pr | pr_open | done (session_review.goal_phase)
    pr_url: str | None = None        # the session's PR, from pr_open on
    pr_state: PrStateDTO | None = None   # where its checks stand; None: no open PR
    drifting: bool = False
    # What runs now (autopilot.goal_activity): the active step's stage, working | checks | review |
    # merging (None: nothing runs), the session's commits, and seconds left when history allows.
    stage: str | None = None
    commits_last_hour: int = 0
    last_commit_at: float | None = None
    eta_seconds: float | None = None
    checking: bool = False           # milestone checks run now
    auditing: bool = False           # the completion audit runs now
    milestones: list[MilestoneDTO] = []

    @classmethod
    def from_autopilot(cls, ap: Autopilot, milestones: list[Milestone],
                       phases: dict[int, str] | None = None,
                       goal_phase: tuple[str, str | None] = ("in_progress", None),
                       activity: dict | None = None, pr_state: dict | None = None,
                       drifting: bool = False) -> GoalDTO:
        from brindle.autopilot import person_note
        from brindle.plans import open_offer_id

        phases = phases or {}
        return cls(**(activity or {}), root_id=ap.root_id, enabled=bool(ap.enabled),
                   plan_offer=open_offer_id(ap.plan_offer),
                   goal=ap.goal, summary=ap.summary, detail=ap.detail,
                   state=ap.state, note=person_note(ap), progress=ap.progress,
                   usage_resets_at=ap.usage_resets_at,
                   goal_phase=goal_phase[0], pr_url=goal_phase[1], pr_state=pr_state, drifting=drifting,
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
    author_session: str | None = None

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
    email: str | None = None
    plan: str | None = None
    orgs: list[AccountOrgDTO] = []


class GitHubConnectionDTO(_DTO):
    """brindle's own GitHub sign-in (brindle.github_connect), as every
    /v1/github/connection route answers. ``route``: how the person connected (a
    copy of the GitHub CLI's sign-in, or a token they pasted). ``needs_reconnect``:
    GitHub no longer takes it. ``gh_available``: a copy of gh's sign-in can be
    offered. Never the token."""
    connected: bool
    login: str | None = None
    route: Literal["gh", "token"] | None = None
    storage: Literal["keychain", "file"] | None = None
    expires_at: float | None = None
    needs_reconnect: bool = False
    gh_available: bool = False


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


class ConnectionReadyDTO(_DTO):
    """Whether an agent of the provider would launch with its method now
    (connections.ready); if not, ``reason`` and the ``action`` the app can
    offer: install | sign_in | sign_out | renew | set_key."""
    ok: bool
    reason: str | None = None
    action: Literal["install", "sign_in", "sign_out", "renew", "set_key"] | None = None


class StoppedAgentDTO(_DTO):
    """An agent stopped because the provider's credentials were refused (failed_why auth)."""
    id: str
    name: str
    profile: str


class ProviderConnectionDTO(_DTO):
    """How a provider connects now: ``method`` auto | sign_in | api_key |
    cloud | org (the org decides, with no cloud route here), and for cloud the
    route (``cloud``: bedrock, vertex, foundry, azure), its non-secret
    ``settings`` (variable -> value) and the ``login`` it uses. An org's route
    wins over ``choice``, the person's saved choice; ``managed`` says why.
    ``clouds``: the routes the person can choose for this provider.
    ``blocked_by`` "codex_login": Codex's ChatGPT login keeps an API key from
    being used until POST /providers/codex/sign-out."""
    provider: str
    method: str
    choice: ProviderChoiceDTO | None = None
    cloud: str | None = None
    settings: dict[str, str] = {}
    login: CloudLoginDTO | None = None
    managed: str | None = None
    clouds: list[str] = []
    ready: ConnectionReadyDTO
    blocked_by: Literal["codex_login"] | None = None
    stopped_agents: list[StoppedAgentDTO] = []


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
    # What the person's org sets over all of it (locked rows; GET /permissions?repo=).
    org_controls: list[OrgControlViewDTO] = []
    # Whether brindle answers from any of this at all (permission_policy): for
    # that repo with ?repo=, else as the person's own settings have it. Off, no
    # preset is ``current``. policy_source: repo, user, or default (nobody set it).
    policy_on: bool = True
    policy_source: str = "default"


class AskScopeDTO(_DTO):
    """One rung of an ask's scope ladder: ``value`` exact | general |
    repository | remote | server | all, ``pattern`` what a grant of it covers.
    ``modes`` lists the decisions this request accepts at this scope."""
    value: str
    pattern: str
    label: str
    level: str | None = None
    modes: list[str] | None = None


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


class OrgControlViewDTO(_DTO):
    """An org control as a member sees it (permission_grants.org_control_view):
    the org's name, what it covers in plain words, Block (deny) or Ask, and why."""
    name: str
    what: str
    decision: str = "ask"
    reason: str = ""


class AskGroupDTO(_DTO):
    """GET /permissions/asks: one Inbox item per repo, kind and general form,
    generalized from its oldest request. ``policy``: the tightest limits
    among its requests (a deny blocks the whole item). ``modes``: what
    POST /permissions/asks/decide accepts for it. ``org``: the org control
    that asks for (or blocks) it, when one does."""
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
    # Why the card offers no saved approval, when its own request cannot take one.
    save_reason: str | None = None
    org: OrgControlViewDTO | None = None


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
