"""The permission policy for the app: the store (preset, rules, saved
actions, prompt counts) and the pending asks, grouped, as DTOs."""

from __future__ import annotations

import time

from brindle import permission_grants as grants
from brindle import permissions
from brindle.db import DB
from brindle.service.dto import (
    AllowAllGrantDTO,
    AllowAllPlanDTO,
    AskDecisionDTO,
    AskDTO,
    AskGroupDTO,
    AskResultDTO,
    AskScopeDTO,
    PermissionExceptionDTO,
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
        prompts=(prompts or {}).get(rule.id, 0))


def saved_dto(action: permissions.SavedAction) -> SavedActionDTO:
    return SavedActionDTO(**{**action.to_dict(), "request": action.request, "expired": action.expired()})


def policy_dto(store: permissions.Store | None = None) -> PermissionPolicyDTO:
    store = store or permissions.load_store()
    counts = permissions.prompt_counts(store)
    return PermissionPolicyDTO(
        preset=store.preset, unmatched=store.unmatched,
        presets=[PermissionPresetDTO(name=name, description=desc, unmatched=unmatched, current=name == store.preset)
                 for name, (_, unmatched, desc) in permissions.PRESETS.items()],
        rules=[rule_dto(r, counts) for r in store.rules],
        saved=[saved_dto(s) for s in store.saved], prompt_counts=counts)


def scope_dto(scope: dict) -> AskScopeDTO:
    return AskScopeDTO(value=scope["value"], pattern=scope["pattern"], label=scope["label"], level=scope.get("level"))


def ask_dto(a: grants.PendingAsk) -> AskDTO:
    return AskDTO(agent_id=a.agent_id, root_id=a.root_id, request_id=a.request_id, digest=a.digest,
                  repo=a.repo_root, kind=a.ask.kind, tool=a.request.tool, exact=a.ask.exact,
                  summary=a.request.summary(), waiting_since=a.created_at, worker=a.worker, blocked=a.blocked,
                  rule=a.rule.id if a.rule else None, limits=limits_dto(a.rule.limits if a.rule else None),
                  reason=a.policy.reason)


def _title(g: grants.Group) -> str:
    general = g.rep.ask.general
    if g.kind == "push":
        dest = general["pattern"].split(permissions.PUSH_ARROW, 1)[-1]
        return f"Allow pushes into {dest}?"
    if g.kind in permissions.PATH_KINDS:
        verb = {"read": "reads", "write": "writes", "edit": "edits"}[g.kind]
        return f"Allow {verb} under {general['pattern']}?"
    if g.kind == "bash":
        return f"Allow commands starting with {general['pattern'].removesuffix(' *')}?"
    return f"Allow {g.rep.request.tool}?"


def _modes(g: grants.Group) -> list[str]:
    if g.policy.decision == "deny":
        return []
    limits = (g.policy.rule.limits if g.policy.rule else None) or {}
    life = limits.get("life")
    out = ["once", "deny", "always-deny"]
    if life == "time":
        out.append("timed")
    elif life == "session":
        out.append("session")
    else:
        out += ["session", "timed", "forever"]
    return out


def group_dto(g: grants.Group) -> AskGroupDTO:
    rule = g.policy.rule
    return AskGroupDTO(key=g.key, repo=g.repo_root, kind=g.kind, title=_title(g), general=scope_dto(g.rep.ask.general),
                       scopes=[scope_dto(s) for s in g.rep.ask.scopes], items=[ask_dto(a) for a in g.items],
                       blocked=g.policy.decision == "deny", rule=rule.id if rule else None,
                       limits=limits_dto(rule.limits if rule else None), modes=_modes(g))


def asks(db: DB, repo_root: str | None) -> list[AskGroupDTO]:
    return [group_dto(g) for g in grants.groups(grants.pending_asks(db, repo_root))]


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
