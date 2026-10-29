"""Who may approve a worker's plan under the org's plan rules."""

from dataclasses import dataclass

from brindle.plan_policy import LOW_RISK_WEIGHTS, PLAN_PATHS, OrgPlanRules, PlanTask, touches_plan_paths
from brindle.policy import Decision, allow, deny


@dataclass(frozen=True)
class Approver:
    id: str
    reviewer: bool = False      # a reviewer agent rather than a person


def is_low_risk(rules: OrgPlanRules, plan: PlanTask) -> bool:
    """Light or medium work that touches no org plan path or built-in sensitive path."""
    return ((plan.weight or "").lower() in LOW_RISK_WEIGHTS and not plan.cross_repo
            and not touches_plan_paths(plan, (*PLAN_PATHS, *rules.paths)))


def eligible(rules: OrgPlanRules | None, approver: Approver, plan: PlanTask) -> Decision:
    """Whether ``approver`` may approve ``plan`` at all, whatever the others did.
    ``approver.id`` is the verified member id, also when a reviewer agent acts for them."""
    if rules is None:
        return allow()
    if rules.approvers and approver.id not in rules.approvers:
        return deny(f"{approver.id} may not approve plans; " + _who(rules))
    if approver.reviewer:
        if rules.two_person:
            return deny("your org requires two different people to approve a plan; "
                        "a reviewer agent doesn't count as one. " + _who(rules))
        if not (rules.reviewer_low_risk and is_low_risk(rules, plan)):
            return deny("a reviewer agent may approve only low-risk plans (light or medium weight, "
                        "no plan paths) and only if your org allows it; " + _who(rules))
    return allow()


def may_approve(rules: OrgPlanRules | None, approver: Approver, plan: PlanTask,
                approvals_so_far: tuple[str, ...] | list[str] = ()) -> Decision:
    """Whether ``approver`` approving ``plan`` completes the org's rules, given
    the member ids of people who already approved it. A denial names who can
    approve; with ``plan_two_person`` an eligible first approval is still
    denied until a second, different person approves. No rules: allowed."""
    d = eligible(rules, approver, plan)
    if not d.allowed or rules is None:
        return d
    if rules.two_person and len({*approvals_so_far, approver.id}) < 2:
        return deny("your org requires two different people to approve a plan; "
                    "this approval is recorded, a second person must approve too. " + _who(rules))
    return allow()


def _who(rules: OrgPlanRules) -> str:
    return ("only " + ", ".join(rules.approvers) + " can approve") if rules.approvers else "any member can approve"
