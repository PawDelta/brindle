"""The permission policy for the app: the store (preset, rules, saved
actions, prompt counts) and the pending asks, grouped, as DTOs."""

from __future__ import annotations

import os
import time

from brindle import permission_grants as grants
from brindle import permissions, tmux, view
from brindle.config import brindle_home, load_repo_config
from brindle.db import DB
from brindle.service.dto import (
    AllowAllGrantDTO,
    AllowAllPlanDTO,
    AskDecisionDTO,
    AskDTO,
    AskGroupDTO,
    AskResultDTO,
    AskScopeDTO,
    OrgControlViewDTO,
    PermissionExceptionDTO,
    PermissionGroupDTO,
    PermissionLimitsDTO,
    PermissionPolicyDTO,
    PermissionPresetDTO,
    PermissionRuleDTO,
    SavedActionDTO,
)


def limits_dto(limits: dict | None) -> PermissionLimitsDTO | None:
    return PermissionLimitsDTO(**limits) if limits else None


def rule_dto(rule: permissions.Rule, prompts: dict[str, int] | None = None) -> PermissionRuleDTO:
    return PermissionRuleDTO(
        id=rule.id, kind=rule.kind, match=rule.match, match_type=rule.match_type, decision=rule.decision,
        source=rule.source, describe=rule.describe(), created=rule.created, label=rule.label,
        limits=limits_dto(rule.limits), expires=rule.expires, expired=rule.expired(),
        exclusions=[PermissionExceptionDTO(kind=x.kind, match=x.match, match_type=x.match_type, describe=x.describe())
                    for x in rule.exclusions],
        repo_root=rule.repo_root, prompts=(prompts or {}).get(rule.id, 0))


def saved_dto(action: permissions.SavedAction) -> SavedActionDTO:
    return SavedActionDTO(**{**action.to_dict(), "request": action.request, "expired": action.expired()})


def org_controls(repo_root: str | None = None) -> list[OrgControlViewDTO]:
    """The org's controls as its members see them (Settings' locked rows). The
    policy is the account's; a repo only adds its offline policy files."""
    lim = grants.org_limits(repo_root or os.path.expanduser("~"))
    return [OrgControlViewDTO(name=lim.name, what=grants.control_label(c), decision=c.decision, reason=c.reason)
            for c in (lim.controls if lim else ())]


def policy_dto(store: permissions.Store | None = None, repo_root: str | None = None) -> PermissionPolicyDTO:
    store = store or permissions.load_store()
    counts = permissions.prompt_counts(store)
    cfg = load_repo_config(repo_root) if repo_root else load_repo_config(brindle_home(), user_only=True)
    policy_on = cfg.permission_policy == "on"
    return PermissionPolicyDTO(
        org_controls=org_controls(repo_root),
        policy_on=policy_on, policy_source=cfg.permission_policy_source,
        preset=store.preset, unmatched=store.unmatched,
        presets=[PermissionPresetDTO(name=name, description=desc, unmatched=unmatched,
                                     current=policy_on and name == store.preset)
                 for name, (_, unmatched, desc) in permissions.PRESETS.items()],
        rules=[rule_dto(r, counts) for r in store.rules],
        saved=[saved_dto(s) for s in store.saved], prompt_counts=counts,
        groups=[PermissionGroupDTO(name=name, baseline=g["baseline"], subcategories=g["subcategories"])
                for name, g in permissions.effective_groups(store).items()])


def scope_dto(scope: dict) -> AskScopeDTO:
    return AskScopeDTO(value=scope["value"], pattern=scope["pattern"], label=scope["label"], level=scope.get("level"),
                       modes=scope.get("modes"))


def ask_dto(a: grants.PendingAsk) -> AskDTO:
    return AskDTO(agent_id=a.agent_id, root_id=a.root_id, request_id=a.request_id, digest=a.digest,
                  repo=a.repo_root, kind=a.ask.kind, tool=a.request.tool, exact=a.ask.exact,
                  summary=a.request.summary(), waiting_since=a.created_at, worker=a.worker, blocked=a.blocked,
                  rule=a.rule.id if a.rule else None, limits=limits_dto(a.rule.limits if a.rule else None),
                  reason=a.policy.reason, escalation=a.escalation)


def _title(g: grants.Group) -> str:
    general = g.rep.ask.general
    if g.kind == "push":
        dest = general["pattern"].split(permissions.PUSH_ARROW, 1)[-1]
        return f"Allow pushes into {dest}?"
    if g.kind in permissions.PATH_KINDS:
        verb = {"read": "reads", "write": "writes", "edit": "edits"}[g.kind]
        return f"Allow {verb} under {general['pattern']}?"
    if g.kind == "bash" and general["pattern"].startswith(grants.FAMILY):
        return f"Allow {general['label'][0].lower()}{general['label'][1:]}?"
    if g.kind == "bash":
        return f"Allow commands starting with {general['pattern'].removesuffix(' *')}?"
    return f"Allow {g.rep.request.tool}?"


SAVED_LIFE = {"session": "session", "timed": "time", "forever": "forever"}


def _modes(g: grants.Group) -> list[str]:
    if g.policy.decision == "deny":
        return []
    limits = (g.policy.rule.limits if g.policy.rule else None) or {}
    life = limits.get("life")
    out = ["once", "deny", "always-deny"]
    if life == "once" or any((r.limits or {}).get("life") == "once" for a in g.items for r in a.policy.asks):
        pass    # nothing it asks for may be saved (a push into a release branch)
    elif life == "time":
        out.append("timed")
    elif life == "session":
        out.append("session")
    else:
        out += ["session", "timed", "forever"]
    return out


def _offer(g: grants.Group) -> tuple[list[dict], list[str], dict | None]:
    """What an Ask's item offers: the scopes, modes and limits a saved
    approval may take, held to the same check a decision is
    (grants.eligible): every ask rule that applies and the org's limits.
    Each scope carries its own modes; a request that cannot be remembered
    keeps only scopes at which it can be allowed once."""
    rep = g.rep
    rules = [r for a in g.items for r in (*a.policy.asks, a.rule) if r is not None]
    caps = [r.limits["minutes"] for r in rules if (r.limits or {}).get("life") == "time" and r.limits.get("minutes")]
    cap = min(caps) if caps else None
    modes = _modes(g)
    scopes = []
    for s in rep.ask.scopes:
        offered = [m for m in modes if all(
            grants.eligible(a, s, m, cap if m == "timed" else None) is None
            for a in ([rep] if s["value"] == grants.EXACT else g.items))]
        if any(m not in ("always-deny", "deny") for m in offered):
            scopes.append({**s, "modes": offered})
    if not scopes:
        scopes = [{**rep.ask.scopes[0], "modes": []}]
    if any(m in SAVED_LIFE for s in scopes for m in s["modes"]):
        scopes = [s for s in scopes if any(m in SAVED_LIFE for m in s["modes"])]
    elif len(scopes) == 1 and scopes[0]["value"] == grants.EXACT:
        scopes = [{**s, "modes": [m for m in s["modes"] if m != "always-deny"]} for s in scopes]
    modes = [m for m in modes if any(m in s["modes"] for s in scopes)]
    limits = {"life": "time", "minutes": cap} if "timed" in modes else (rep.rule.limits if rep.rule else None)
    return scopes, modes, limits


def group_dto(g: grants.Group) -> AskGroupDTO:
    rule = g.policy.rule
    lim = grants.org_limits(g.repo_root) if rule is not None and rule.source == "org" else None
    org = grants.org_control_view(lim, rule)
    scopes, modes, limits = _offer(g)
    general = next((s for s in scopes if s["value"] == g.rep.ask.general["value"]), scopes[0])
    save_reason = None
    if not g.rep.blocked and not any(m in SAVED_LIFE for m in modes):
        save_reason = grants.eligible(g.rep, general, "session", None)
    return AskGroupDTO(key=g.key, repo=g.repo_root, kind=g.kind, title=_title(g), general=scope_dto(general),
                       scopes=[scope_dto(s) for s in scopes], items=[ask_dto(a) for a in g.items],
                       blocked=g.policy.decision == "deny", rule=rule.id if rule else None,
                       limits=limits_dto(limits), modes=modes, save_reason=save_reason,
                       org=OrgControlViewDTO(**org, decision=g.policy.decision) if org else None)


def asks(db: DB, repo_root: str | None) -> list[AskGroupDTO]:
    pending = grants.pending_asks(db, repo_root)
    if pending:
        # Holds can outlive a worker's pane and its replacement. Apply the
        # same worktree boundary as the snapshot before building any groups;
        # a fresh app has never seen those hidden ids and cannot filter them.
        alive = view.live_agents(db, tmux.list_panes())
        current: set[str] = set()
        for ws in view.session_workspaces(db, repo_root):
            rows = db.list_agents(ws.id)
            shown = view.current_agents(rows, alive) if ws.kind == "worktree" else rows
            current.update(a.id for a in shown if not view.past_worker(a))
        pending = [a for a in pending if a.agent_id in current]
    return [group_dto(g) for g in grants.groups(pending)]


def _results(rows: list[dict]) -> list[AskResultDTO]:
    return [AskResultDTO(**r) for r in rows]


def decision_dto(result: dict) -> AskDecisionDTO:
    grant = result.get("grant")
    return AskDecisionDTO(mode=result["mode"], scope=scope_dto(result["scope"]),
                          grant=SavedActionDTO(**{**grant, "expired": False}) if grant else None,
                          resumed=_results(result["resumed"]), stopped=_results(result["stopped"]),
                          skipped=_results(result["skipped"]), failed=_results(result["failed"]))


def plan_dto(result: dict) -> AllowAllPlanDTO:
    now = time.time()
    return AllowAllPlanDTO(
        grants=[AllowAllGrantDTO(**{**g, "scope": scope_dto(g["scope"])}) for g in result["grants"]],
        resumes=result["resumes"], skipped=_results(result["skipped"]),
        saved=[SavedActionDTO(**{**s, "expired": (s.get("expires") or now + 1) <= now}) for s in result.get("saved", [])],
        resumed=_results(result.get("resumed", [])), failed=_results(result.get("failed", [])),
        excepted=result.get("excepted"), added=result.get("added"), locked=_results(result.get("locked", [])))


def plan(db: DB, repo_root: str | None, forever: bool) -> AllowAllPlanDTO:
    planned = grants.plan_allow_all(grants.pending_asks(db, repo_root), forever)
    return plan_dto(grants.plan_to_json(planned))
