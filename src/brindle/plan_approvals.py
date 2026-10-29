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


def may_approve(rules: OrgPlanRules | None, approver: Approver, plan: PlanTask,
                approvals_so_far: tuple[str, ...] | list[str] = ()) -> Decision:
    """Whether ``approver`` approving ``plan`` satisfies the org's rules, given
    the ids that already approved it. A denial names who can approve; with
    ``plan_two_person`` it also asks for the second person. No rules: allowed."""
    if rules is None:
        return allow()
    if approver.reviewer:
        if rules.reviewer_low_risk and is_low_risk(rules, plan):
            person = None
        else:
            return deny("a reviewer agent may approve only low-risk plans (light or medium weight, "
                        "no plan paths) and only if your org allows it; " + _who(rules))
    else:
        person = approver.id
        if rules.approvers and person not in rules.approvers:
            return deny(f"{person} may not approve plans; " + _who(rules))
    if rules.two_person:
        distinct = {*approvals_so_far, approver.id}
        if len(distinct) < 2:
            return deny("your org requires two different people to approve a plan; "
                        "this approval is recorded, a second person must approve too. " + _who(rules))
    return allow()


def _who(rules: OrgPlanRules) -> str:
    return ("only " + ", ".join(rules.approvers) + " can approve") if rules.approvers else "any member can approve"
