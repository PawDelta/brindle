"""The daemon's read-only routes. The wrapper opens and closes each request's
``DB`` on its worker thread. Routes answer with ``service.dto`` shapes; engine errors become
``ServiceError`` JSON. Secrets leave only as key names and their last 4 characters."""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from brindle import (
    agents,
    config,
    cost,
    doctor,
    git,
    kb,
    keystore,
    knowledge,
    profiles,
    quota,
    sessions,
    tmux,
    view,
    workspaces,
)
from brindle.daemon import session_review
from brindle.daemon.routes_actions import read_edit_target
from brindle.db import DB
from brindle.service import inbox
from brindle.service.dto import (
    AccountDTO,
    AgentDTO,
    DecisionDTO,
    GoalDTO,
    PreflightItemDTO,
    ProfileDTO,
    RoutingDTO,
    RoutingProfileDTO,
    RoutingSettingDTO,
    RoutingTierDTO,
    RoutingTierModelDTO,
    SessionDTO,
    WorkspaceDTO,
)
from brindle.service.errors import ServiceError, from_exception

log = logging.getLogger(__name__)

COST_DAYS = 30


def _json(fn: Callable[[DB, Request], Any]) -> Callable[[Request], JSONResponse]:
    """A sync handler (Starlette runs it on a thread) whose return value is the
    JSON body and whose exceptions become the service's error JSON. Owns the
    request's DB, including on error responses."""
    @wraps(fn)
    def handler(request: Request) -> JSONResponse:
        try:
            db = DB()
            try:
                return JSONResponse(fn(db, request))
            finally:
                db.conn.close()
        except Exception as e:  # noqa: BLE001 - every engine error maps to a ServiceError
            err = from_exception(e)
            if err.code == "internal":
                log.exception("brindle: daemon route %s failed", request.url.path)
            return JSONResponse(err.to_json(), status_code=err.http_status)
    return handler


def _repo(request: Request) -> str | None:
    return request.query_params.get("repo") or None


def _need_repo(request: Request) -> str:
    repo = _repo(request)
    if repo is None:
        raise ServiceError("invalid", "repo is required", 400)
    return repo


def _last4(value: str) -> str:
    return value[-4:] if len(value) > 8 else ""


@_json
def repos(db: DB, request: Request) -> Any:
    roots = sorted({ws.repo_root for ws in db.find_workspaces(None)})
    return [{"repo_root": r, "name": r.rstrip("/").rsplit("/", 1)[-1]} for r in roots]


@_json
def list_sessions(db: DB, request: Request) -> Any:
    repo = _need_repo(request)
    live = [SessionDTO.from_agent(s.root, s.goal).to_json() for s in sessions.live(db, repo)]
    paused = [SessionDTO.from_agent(s.root, sessions.goal_line(db, s.root)).to_json()
              for s in sessions.paused(db, repo) if s.root.dismissed_at is None]
    return live + paused


@_json
def snapshot(db: DB, request: Request) -> Any:
    return [WorkspaceDTO.model_validate(ws).to_json() for ws in view.snapshot(db, _repo(request))]


@_json
def goal(db: DB, request: Request) -> Any:
    entry = view.autopilot_entry(db, _need_repo(request), panes=tmux.list_panes(),
                                 root_id=request.query_params.get("root"))
    ap = entry and db.get_autopilot(entry["root_id"])
    if not ap:
        return None
    from brindle.autopilot import milestone_phases

    return GoalDTO.from_autopilot(ap, db.milestones(ap.root_id),
                                  milestone_phases(db, ap.root_id),
                                  session_review.goal_phase(db, ap.root_id)).to_json()


@_json
def inbox_(db: DB, request: Request) -> Any:
    return [i.to_json() for i in inbox.items(db, _repo(request))]


@_json
def agent(db: DB, request: Request) -> Any:
    try:
        a = agents.get(db, request.path_params["id"])
    except agents.AgentError as e:
        raise ServiceError("not_found", str(e), 404) from e
    return AgentDTO.model_validate(view.agent_entry(db, a, detail=True, panes=tmux.list_panes())).to_json()


@_json
def diff(db: DB, request: Request) -> Any:
    ws = db.get_workspace(request.path_params["id"])
    if ws is None:
        raise ServiceError("not_found", "no such workspace", 404)
    base = workspaces.require_base(ws)
    return {"workspace_id": ws.id, "base": base, "diff": git.diff(ws.path, base),
            "stat": git.diff(ws.path, base, stat=True)}


@_json
def workspace_file(db: DB, request: Request) -> Any:
    """``?path=``: a file the review sheet can edit inline, read with the
    same checks as ``PUT .../file``. ``base`` is the blob sha that PUT takes."""
    ws = db.get_workspace(request.path_params["id"])
    if ws is None:
        raise ServiceError("not_found", "no such workspace", 404)
    rel = request.query_params.get("path")
    if not rel:
        raise ServiceError("invalid", "path is required", 400)
    name, dir_fd, _, data, _ = read_edit_target(ws, rel)
    os.close(dir_fd)
    return {"path": name, "content": data.decode(), "base": git.hash_blob(ws.path, data, name)}


@_json
def session_readiness(db: DB, request: Request) -> Any:
    """Whether brindle judges the session ready for a PR; ``reasons``: what
    is missing, in plain words (none when it is ready); ``auto_pr``: whether
    brindle opens its PR on its own once it is."""
    root, ws = session_review.session(db, request.path_params["root"])
    why = session_review.reasons(db, root, ws)
    return {"root_id": root.id, "ready": not why, "reasons": why, "auto_pr": session_review.auto_pr(ws.repo_root)}


@_json
def session_diff(db: DB, request: Request) -> Any:
    """The session branch against its PR base, each file and hunk with its
    authors (worker ids, "supervisor", "you") and the authors for filtering.
    409 with ``reasons`` unless the session is ready."""
    root, ws = session_review.session(db, request.path_params["root"])
    session_review.require_ready(db, root, ws)
    return session_review.diff(db, root, ws)


@_json
def session_pr_draft(db: DB, request: Request) -> Any:
    root, ws = session_review.session(db, request.path_params["root"])
    session_review.require_ready(db, root, ws)
    return session_review.draft(db, root, ws)


@_json
def session_file(db: DB, request: Request) -> Any:
    """``?path=``: any file in the session's checkout, read with the same
    checks as ``PUT .../file``. ``base`` is the blob sha PUT and DELETE take."""
    root, ws = session_review.session(db, request.path_params["root"])
    rel = request.query_params.get("path")
    if not rel:
        raise ServiceError("invalid", "path is required", 400)
    name, dir_fd, _, data, _ = read_edit_target(ws, rel)
    os.close(dir_fd)
    return {"path": name, "content": data.decode(), "base": git.hash_blob(ws.path, data, name)}


@_json
def decisions(db: DB, request: Request) -> Any:
    return [DecisionDTO.from_row(d).to_json()
            for d in knowledge.decisions(db, knowledge.repo_key(_need_repo(request)))]


@_json
def kb_sources(db: DB, request: Request) -> Any:
    keep = ("id", "kind", "scope", "mode", "visibility", "credential_name", "retention_days",
            "added_at", "docs", "quarantined")
    return [{k: s[k] for k in keep if k in s}
            for s in kb.sources(db, knowledge.repo_key(_need_repo(request)))]


@_json
def kb_search(db: DB, request: Request) -> Any:
    """The best chunks for ``q`` among the repo's indexed, non-quarantined
    documents (kb.search): heading path, text, source link and date. No
    query, no hits."""
    return kb.search(db, knowledge.repo_key(_need_repo(request)), request.query_params.get("q", ""))


@_json
def list_profiles(db: DB, request: Request) -> Any:
    return [ProfileDTO.from_profile(p).to_json() for p in profiles.list_profiles(_repo(request))]


@_json
def get_profile(db: DB, request: Request) -> Any:
    """One profile as ``brindle profile show`` resolves it (its fields, where
    it is read from, the extends chain, its rule packs and the full prompt;
    never env, whose values can be secrets) with ``brindle profile lint``'s
    findings for it: ``{**ProfileDTO, source, extends, rules, prompt, lint}``."""
    from brindle.cli import _lint_one   # the lint `brindle profile lint` runs; one definition

    repo, name = _repo(request), request.path_params["name"]
    errors, warnings = _lint_one(name, repo)
    try:
        p = profiles.load_profile(name, repo)
    except (KeyError, profiles.ProfileError) as e:
        # Written but unresolvable (a cycle, a missing parent): as written, the lint saying why.
        p = next((x for x in profiles.list_profiles(repo) if x.name == name), None)
        if p is None:
            source = profiles.profile_source(name, repo)
            if source is None:
                raise ServiceError("not_found", f"no profile {name}", 404) from e
            # Present but unparsable: its identity and why, without resolved fields.
            return {"name": name, "source": source, "extends": [], "rules": [], "prompt": "",
                    "lint": {"errors": errors, "warnings": warnings}}
    try:
        packs = profiles.load_rule_packs(p, repo)
    except (KeyError, profiles.ProfileError):
        packs = []
    chain, parent = [], p.extends
    while parent and parent not in chain:
        chain.append(parent)
        try:
            parent = profiles.load_profile(parent, repo).extends
        except (KeyError, profiles.ProfileError):
            break
    prompt = f"{p.prompt.strip()}\n\n{profiles.rules_prompt(packs)}".strip() if packs else p.prompt.strip()
    return {**ProfileDTO.from_profile(p).to_json(),
            "source": profiles.profile_source(name, repo), "extends": chain,
            "rules": [{"name": k.name, "description": k.description, "source": k.source} for k in packs],
            "prompt": prompt, "lint": {"errors": errors, "warnings": warnings}}


@_json
def get_config(db: DB, request: Request) -> Any:
    repo = _repo(request)
    settings = {"user": config.user_settings()}
    if repo:
        settings["repo"] = dataclasses.asdict(config.load_repo_config(repo))
        # The org's policy may force auto_pr off whatever the files say: why, or None.
        settings["auto_pr_forbidden"] = session_review.auto_pr_forbidden(repo)
    keys = []
    for name in keystore.known_names(repo):
        value = keystore.get_key(name)
        keys.append({"name": name, "set": value is not None, "last4": _last4(value or "")})
    return {**settings, "keys": keys}


@_json
def get_user_settings(db: DB, request: Request) -> Any:
    return {"claude_permission_mode": config.claude_permission_mode()}


@_json
def get_routing(db: DB, request: Request) -> Any:
    """Each tier's profiles (and the model and agent each runs), the reviewer
    and the excluded providers, with the scope each comes from, every profile
    with what routing knows of it, and the org policy all of it answers to."""
    from brindle import autopilot, providers
    from brindle.service.routing import RoutingPolicy, provider_name

    repo = _need_repo(request)
    cfg = config.load_repo_config(repo)
    policy = RoutingPolicy.load(repo)
    every = profiles.list_profiles(repo)
    refusals = {p.name: policy.refusal(p) for p in every}
    scope_of = {str(path): scope for scope, path in config.scope_paths(repo).items()}

    def as_model(name: str) -> RoutingTierModelDTO:
        provider, model = policy.runs(name)
        return RoutingTierModelDTO(profile=name, provider=provider, provider_name=provider_name(provider),
                                   model=model, allowed=refusals.get(name) is None)

    tiers = {t: RoutingTierDTO(values=cfg.routing[t], source=scope_of.get(cfg.routing_sources[t], "default"),
                               models=[as_model(n) for n in cfg.routing[t]])
             for t in config.WEIGHTS}
    installed = doctor.signin_providers()
    rows = []
    for p in every:
        # A provider with no CLI of its own (native) is always there.
        here = p.provider not in doctor.CLI_INSTALL or p.provider in installed
        rows.append(RoutingProfileDTO(
            name=p.name, provider=p.provider, provider_name=provider_name(p.provider),
            model=autopilot.effective_profile_model(p, repo) or None,
            installed=here, signed_in=here and providers.signed_out(p.provider, p.env) is None,
            excluded=p.provider in cfg.exclude_providers, cost_tier=autopilot.cost_tier(p.name, cfg, repo),
            allowed=refusals[p.name] is None, refusal=refusals[p.name]))
    return RoutingDTO(
        tiers=tiers,
        reviewer=RoutingSettingDTO(values=[cfg.reviewer], source=config.setting_source(repo, "reviewer")),
        exclude_providers=RoutingSettingDTO(values=cfg.exclude_providers,
                                            source=config.setting_source(repo, "exclude_providers")),
        profiles=rows, policy=policy.to_dto(every)).to_json()


@_json
def get_doctor(db: DB, request: Request) -> Any:
    return [dataclasses.asdict(c) for c in doctor.checks(_repo(request), db=db)]


@_json
def provider_preflight(db: DB, request: Request) -> Any:
    """What would stop a session on this provider in ``repo`` before its first
    turn: the items POST /v1/sessions answers 409 with, for the identity its
    chat would run as (see brindle.preflight). Native, which runs no chat,
    and a request without ``repo`` get the provider's checks as brindle sees it."""
    from brindle import preflight
    from brindle.daemon.routes_actions import session_blockers

    provider, repo = request.path_params["provider"], _repo(request)
    if repo is None or provider == "native":
        items = preflight.check(provider, repo)
    else:
        if provider not in preflight.PROVIDERS:
            raise ServiceError("invalid", f"provider must be one of {', '.join(preflight.PROVIDERS)}")
        blocked = session_blockers(db, repo, provider, read_only=True)
        items = blocked.items if blocked else []
    return [PreflightItemDTO(**i.to_json()).to_json() for i in items]


@_json
def providers_installed(db: DB, request: Request) -> Any:
    """Per provider CLI: whether brindle installed it (``state`` "brindle",
    version, its config home), runs the person's own copy ("mine", path and
    version) or finds none ("missing"), and ``update``: the latest version
    known and whether it is newer. ``?check=1`` asks each maker's source now."""
    from brindle import providers_install

    return providers_install.statuses(check=request.query_params.get("check") in ("1", "true"))


@_json
def account(db: DB, request: Request) -> Any:
    from brindle.pro import license
    from brindle.pro.account import ProAccount

    try:
        ent = license.current(refresh=False)
    except license.LicenseError:
        return AccountDTO(signed_in=False).to_json()
    try:
        # For the app's org picker; the plan still shows when the org list can't be had.
        orgs = ProAccount().orgs(None)
    except Exception:  # noqa: BLE001 - offline, or the backend refused
        log.debug("brindle: daemon couldn't list the account's orgs", exc_info=True)
        orgs = []
    # Signed in, the DTO carries more: the org, status, features, seats, role,
    # expiry and grace; whether hosted learning is entitled, whether it is
    # effectively on for `repo` (its setting resolved as the engine does), and
    # the learner's kept note when it said anything. Never the tokens.
    from brindle import learning, plugins

    available = plugins.LEARNING_FEATURE in ent.features and not ent.in_grace
    repo = _repo(request)
    cfg = config.load_repo_config(repo) if repo else config.RepoConfig()
    on = available and plugins.learning_name(cfg) == plugins.CLOUD
    note = learning.kept_note(cfg, repo) if on and repo else None
    return {**AccountDTO(signed_in=True, plan=ent.plan, orgs=orgs).to_json(),
            "org_id": ent.org_id, "status": ent.status, "features": sorted(ent.features),
            "seats": ent.seats, "role": ent.role, "expires_at": ent.exp,
            "in_grace": ent.in_grace, "grace_until": ent.grace_until,
            "learning_available": available, "learning": on, "learning_note": note}


@_json
def get_cost(db: DB, request: Request) -> Any:
    cost.require_entitled()
    days = int(request.query_params.get("days") or COST_DAYS)
    s = cost.summary(db, _repo(request), now=time.time(), days=days)

    def bucket(b: cost.Bucket) -> dict:
        return {"dollars": b.dollars, "tokens": b.tokens, "unknown": b.unknown, "free": b.free}

    return {"days": s.days, "total": bucket(s.total),
            "by_model": {m: bucket(b) for m, b in s.by_model.items()}}


USAGE_NEAR_PERCENT = 80.0
USAGE_NEAR_RESET_SECONDS = 3600


@_json
def get_usage(db: DB, request: Request) -> Any:
    """Each provider's quota windows, and ``near``: the providers that are
    limited, past 80% of their fullest window or about to reset it within the
    hour, worst first. Providers brindle knows nothing current about come back
    with ``known: false``."""
    now = time.time()
    providers, near = [], []
    for name in quota.PROVIDERS:
        q = quota.get(name)
        if q is None:
            providers.append({"provider": name, "known": False, "limited": False, "windows": [],
                              "limited_until": None, "updated_at": None})
            continue
        limited = bool(q.limited_until and q.limited_until > now)
        providers.append({
            "provider": name, "known": True, "limited": limited, "limited_until": q.limited_until,
            "updated_at": q.updated_at,
            "windows": [{"used": w.used, "resets_at": w.resets_at, "minutes": w.minutes,
                         "name": w.name or quota.window_name(w.minutes), "short": w.name or quota.window_short(w.minutes)}
                        for w in q.windows]})
        top = quota.fullest(q)
        # Any plan window about to reset counts, not just the fullest one.
        soon = min((w for w in q.windows if w.name is None and w.resets_at
                    and w.resets_at - now <= USAGE_NEAR_RESET_SECONDS),
                   key=lambda w: w.resets_at, default=None)
        if limited:
            near.append({"provider": name, "reason": "limited", "used": top.used if top else None,
                         "resets_at": q.limited_until})
        elif top and top.used >= USAGE_NEAR_PERCENT:
            near.append({"provider": name, "reason": "high", "used": top.used, "resets_at": top.resets_at})
        elif soon:
            near.append({"provider": name, "reason": "resets_soon", "used": soon.used,
                         "resets_at": soon.resets_at})
    near.sort(key=lambda n: (n["reason"] != "limited", -(n["used"] or 0)))
    return {"providers": providers, "near": near}


ROUTES = [
    Route("/v1/repos", repos),
    Route("/v1/sessions", list_sessions),
    Route("/v1/snapshot", snapshot),
    Route("/v1/goal", goal),
    Route("/v1/inbox", inbox_),
    Route("/v1/agents/{id}", agent),
    Route("/v1/workspaces/{id:path}/diff", diff),
    # PUT on the same path is the edit (routes_actions); agent callers are refused by the guard.
    Route("/v1/workspaces/{id:path}/file", workspace_file),
    Route("/v1/sessions/{root:path}/pr/readiness", session_readiness),
    Route("/v1/sessions/{root:path}/pr/draft", session_pr_draft),
    Route("/v1/sessions/{root:path}/diff", session_diff),
    # PUT and DELETE on the same path edit (routes_actions).
    Route("/v1/sessions/{root:path}/file", session_file),
    Route("/v1/decisions", decisions),
    Route("/v1/kb/sources", kb_sources),
    Route("/v1/kb/search", kb_search),
    Route("/v1/profiles", list_profiles),
    Route("/v1/profiles/{name}", get_profile),
    Route("/v1/config", get_config),
    Route("/v1/user-settings", get_user_settings),
    Route("/v1/routing", get_routing),
    Route("/v1/doctor", get_doctor),
    # Before the action routes' DELETE /v1/providers/{provider}.
    Route("/v1/providers/installed", providers_installed),
    Route("/v1/providers/{provider}/preflight", provider_preflight),
    Route("/v1/account", account),
    Route("/v1/cost", get_cost),
    Route("/v1/usage", get_usage),
]
