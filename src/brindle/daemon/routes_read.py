"""The daemon's read-only routes. The wrapper opens and closes each request's
``DB`` on its worker thread. Routes answer with ``service.dto`` shapes; engine errors become
``ServiceError`` JSON. Secrets leave only as key names and their last 4 characters."""

from __future__ import annotations

import dataclasses
import json
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
from brindle.daemon.routes_actions import (
    doc_switches, docs_api, docs_everywhere, docs_org_api, docs_scopes, read_edit_target,
)
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
    RoutingKindDTO,
    RoutingProfileDTO,
    RoutingSettingDTO,
    RoutingTierDTO,
    RoutingTierModelDTO,
    RuleDTO,
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
            "added_at", "docs", "quarantined", "library_state", "library_pending", "library_removing")
    from brindle.pro import analysis_imports

    return [{k: s[k] for k in keep if k in s}
            for s in kb.sources(db, knowledge.repo_key(_need_repo(request)),
                                analysis_imports.current_identity())]


@_json
def kb_search(db: DB, request: Request) -> Any:
    """The best chunks for ``q`` among the repo's indexed, non-quarantined
    documents (kb.search): heading path, text, source link and date. No
    query, no hits."""
    return kb.search(db, knowledge.repo_key(_need_repo(request)), request.query_params.get("q", ""))


@_json
def list_profiles(db: DB, request: Request) -> Any:
    repo = _repo(request)
    out = []
    for p in profiles.list_profiles(repo):
        errors, warnings = profiles.lint_profile(p.name, repo)
        out.append(ProfileDTO.from_profile(p, lint_errors=errors, lint_warnings=warnings).to_json())
    return out


@_json
def list_rules(db: DB, request: Request) -> Any:
    """Every rule pack an agent can be given, with its one-line description and where it comes from."""
    repo = _need_repo(request)
    out = []
    for name in profiles.list_rule_packs(repo):
        try:
            pack = profiles.load_rule_pack(name, repo)
        except (KeyError, profiles.ProfileError):
            continue    # listed but unreadable: `brindle profile lint` reports it
        out.append(RuleDTO(name=name, description=pack.description, source=pack.source).to_json())
    return out


@_json
def get_profile(db: DB, request: Request) -> Any:
    """One profile as ``brindle profile show`` resolves it (its fields, where
    it is read from, the extends chain, its rule packs and the full prompt;
    never env, whose values can be secrets) with ``brindle profile lint``'s
    findings for it: ``{**ProfileDTO, source, extends, rules, prompt, lint}``."""
    repo, name = _repo(request), request.path_params["name"]
    errors, warnings = profiles.lint_profile(name, repo)   # the lint `brindle profile lint` runs
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
    return {**ProfileDTO.from_profile(p, errors, warnings).to_json(),
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
def get_debug(db: DB, request: Request) -> Any:
    """``{enabled, staff}`` for debug logs (only a brindle staff account may turn them on)."""
    from brindle import debuglog

    return debuglog.checked()


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
        errors, warnings = profiles.lint_profile(p.name, repo)
        rows.append(RoutingProfileDTO(
            lint_errors=errors, lint_warnings=warnings,
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
        profiles=rows, policy=policy.to_dto(every), kinds=_routing_kinds(db, cfg, repo)).to_json()


def _routing_kinds(db: DB, cfg: config.RepoConfig, repo: str) -> dict[str, RoutingKindDTO]:
    """Per kind of work: the owner's pick, its ordered fallbacks, whether it
    is locked, and the different profile hosted learning runs for it now.
    A locked kind is never overridden, so it never reports one."""
    from brindle import agents, autopilot

    def learned(kind: str, run) -> str | None:
        if cfg.routing_locked.get(kind):
            return None
        try:
            return run()
        except Exception:  # noqa: BLE001 - a read never fails on the learner
            log.exception("brindle: couldn't ask the learner about %s", kind)
            return None

    def tuned(kind: str) -> dict[str, str | None]:
        return {k: cfg.routing_overrides.get(kind, {}).get(k) for k in config.KIND_OVERRIDE_FIELDS}

    kinds = {}
    def weight_pick(t: str) -> str | None:
        # The dispatch's own resolution, so an excluded or unavailable candidate is never reported.
        name, learned_pick, _ = autopilot._route_by_weight(db, cfg, repo, t, None, None)
        return name if learned_pick else None

    for t in config.WEIGHTS:
        names = cfg.routing[t]
        kinds[t] = RoutingKindDTO(model=names[0] if names else None, fallbacks=names[1:],
                                  locked=bool(cfg.routing_locked.get(t)),
                                  learned=learned(t, lambda t=t: weight_pick(t)), **tuned(t))

    def reviewer_pick() -> str | None:
        if cfg.review_profile:      # the owner's explicit choice, not a learned one
            return None
        pick = agents.default_review_profile(cfg, None, db, repo)
        return pick if pick != cfg.reviewer else None

    kinds["reviewer"] = RoutingKindDTO(model=cfg.review_profile or cfg.reviewer,
                                       fallbacks=cfg.reviewer_fallbacks,
                                       locked=bool(cfg.routing_locked.get("reviewer")),
                                       learned=learned("reviewer", reviewer_pick), **tuned("reviewer"))
    kinds["supervisor"] = RoutingKindDTO(model=cfg.supervisor_profile, fallbacks=cfg.supervisor_fallbacks,
                                         locked=bool(cfg.routing_locked.get("supervisor")), **tuned("supervisor"))
    return kinds


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
    """Each provider CLI as a terminal would find it (PATH, then the makers'
    standard places): ``state`` "installed", "missing" or "off" (turned off
    in brindle), the path brindle runs and its version, whether it is signed
    in where that is cheap to know (else null), every install found, and
    brindle's config home for it (null for Antigravity, which shares ~/.gemini)."""
    from brindle import providers_install

    return providers_install.statuses()


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


def _all_docs(request: Request, query: dict[str, str]) -> list[dict]:
    """The docs of every scope of docs_scopes, each doc once, newest first."""
    seen: dict[str, dict] = {}
    for got in docs_everywhere("/analysis-docs", _repo(request), query):
        for d in got.get("docs") or []:
            if isinstance(d, dict) and isinstance(d.get("doc_id"), str):
                seen.setdefault(d["doc_id"], d)
    return sorted(seen.values(), key=lambda d: -float(d.get("created_at") or 0))


@_json
def analysis_docs(db: DB, request: Request) -> Any:
    """The library's docs (``{"docs": [...]}``, each as the server lists it),
    filtered by ``scope`` (user, team:<id> or org:<id>; none: every scope the
    person reads, see docs_scopes), ``topic``, ``kind`` and ``session``."""
    q = request.query_params
    query = {k: q[k] for k in ("topic", "kind", "session") if q.get(k)}
    if q.get("scope"):
        return docs_api("GET", "/analysis-docs", repo=_repo(request), scope=q["scope"], query=query)
    return {"docs": _all_docs(request, query)}


@_json
def analysis_doc_session_summary(db: DB, request: Request) -> Any:
    """Session-local counts; never consults the library's aggregate usage."""
    from brindle.pro import analysis_brief, analysis_docs as docs

    session = request.query_params.get("session")
    if not session:
        raise ServiceError("invalid", "session is required", 400)
    drafts = docs.drafts(db, session)
    writing = {doc_id for (_, doc_id), item in drafts.items() if item["state"] == "open"}
    cited = {doc_id for kind, doc_id, _ in knowledge.receipt(db, session)
             if kind == analysis_brief.SESSION_DOC_ITEM}
    updated = {doc_id for kind, doc_id, _ in knowledge.receipt(db, session) if kind == docs.UPDATED_ITEM}
    return {"writing": len(writing), "updated": len(updated), "using": len(cited),
            "total": len(writing | updated | cited)}


@_json
def analysis_doc_map(db: DB, request: Request) -> Any:
    """Proxy one path neighbourhood without storing its response locally."""
    q = request.query_params
    try:
        paths = json.loads(q.get("paths", "[]"))
    except (ValueError, TypeError) as e:
        raise ServiceError("bad_request", "paths must be a JSON array", 400) from e
    if (not isinstance(paths, list) or not paths
            or any(not isinstance(p, str) or not p.strip() for p in paths)
            or q.get("depth", "1") != "1"
            or q.get("scope_kind") not in ("user", "group", "org") or not q.get("scope_id")):
        raise ServiceError("bad_request", "choose a scope and code paths at depth 1", 400)
    return docs_api("GET", "/analysis-docs/map", repo=_repo(request),
                    query={"scope_kind": q["scope_kind"], "scope_id": q["scope_id"],
                           "paths": ",".join(paths), "depth": "1"})    # the server's list is comma-separated


@_json
def analysis_doc_topics(db: DB, request: Request) -> Any:
    """``{"topics": [{topic, count}, ...]}`` in one scope, or over every doc
    of :func:`_all_docs` without one."""
    scope = request.query_params.get("scope")
    if scope:
        return docs_api("GET", "/analysis-docs/topics", repo=_repo(request), scope=scope)
    counts: dict[str, int] = {}
    for d in _all_docs(request, {}):
        for t in d.get("topics") or []:
            counts[t] = counts.get(t, 0) + 1
    return {"topics": [{"topic": t, "count": n} for t, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]}


@_json
def analysis_doc_scopes(db: DB, request: Request) -> Any:
    return {"scopes": docs_scopes(_repo(request))}


@_json
def analysis_doc_switches(db: DB, request: Request) -> Any:
    """The person's own switches, and which their org has locked (see routes_actions.doc_switches)."""
    return doc_switches(_repo(request))


@_json
def analysis_doc_policy(db: DB, request: Request) -> Any:
    """The org's doc policy, including ``export_destinations`` (a subset of download, google_docs, sharepoint)."""
    return docs_org_api("GET", "policy", repo=_repo(request))


@_json
def analysis_doc_export_config(db: DB, request: Request) -> Any:
    """``{google_client_id, microsoft_client_id}`` for the app's in-app sign-in (either may be null)."""
    return docs_api("GET", "/analysis-docs/export-config", repo=_repo(request))


@_json
def analysis_doc_destination(db: DB, request: Request) -> Any:
    """The team's export destination: ``{kind, target_name, connected_at}``, kind null when none."""
    return docs_org_api("GET", "destination", repo=_repo(request))


@_json
def analysis_doc_results(db: DB, request: Request) -> Any:
    """How tasks with a doc brief went against similar ones without, from
    this machine's records (analysis_brief.results); nothing is sent."""
    from brindle.pro import analysis_brief

    days = int(request.query_params.get("days") or analysis_brief.RESULTS_DAYS)
    return analysis_brief.results(db, _repo(request), days)


@_json
def analysis_doc(db: DB, request: Request) -> Any:
    """One doc: its files, what the server read from it and how often briefs used it."""
    from urllib.parse import quote

    return docs_api("GET", f"/analysis-docs/{quote(request.path_params['id'], safe='')}",
                    repo=_repo(request))


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
    Route("/v1/analysis-docs", analysis_docs),
    Route("/v1/analysis-docs/session-summary", analysis_doc_session_summary),
    # Before /analysis-docs/{id}.
    Route("/v1/analysis-docs/topics", analysis_doc_topics),
    Route("/v1/analysis-docs/map", analysis_doc_map),
    Route("/v1/analysis-docs/scopes", analysis_doc_scopes),
    Route("/v1/analysis-docs/results", analysis_doc_results),
    Route("/v1/analysis-docs/policy", analysis_doc_policy),
    Route("/v1/analysis-docs/switches", analysis_doc_switches),
    Route("/v1/analysis-docs/export-config", analysis_doc_export_config),
    Route("/v1/analysis-docs/destination", analysis_doc_destination),
    Route("/v1/analysis-docs/{id}", analysis_doc),
    Route("/v1/profiles", list_profiles),
    Route("/v1/profiles/{name}", get_profile),
    Route("/v1/rules", list_rules),
    Route("/v1/config", get_config),
    Route("/v1/user-settings", get_user_settings),
    Route("/v1/routing", get_routing),
    Route("/v1/debug", get_debug),
    Route("/v1/doctor", get_doctor),
    # Before the action routes' DELETE /v1/providers/{provider}.
    Route("/v1/providers/installed", providers_installed),
    Route("/v1/providers/{provider}/preflight", provider_preflight),
    Route("/v1/account", account),
    Route("/v1/cost", get_cost),
    Route("/v1/usage", get_usage),
]
