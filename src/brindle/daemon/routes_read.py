"""The daemon's read-only routes. The wrapper opens and closes each request's
``DB`` on its worker thread. Routes answer with ``service.dto`` shapes; engine errors become
``ServiceError`` JSON. Secrets leave only as key names and their last 4 characters."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import posixpath
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, wraps
from typing import Any

import anyio
import anyio.to_thread
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
    key_check,
    keys_cmds,
    keystore,
    knowledge,
    model_names,
    profiles,
    provider_health,
    quota,
    session_sync,
    sessions,
    spawn,
    tmux,
    view,
    workspaces,
)
from brindle.daemon import session_review
from brindle.daemon.routes_actions import (
    doc_switches, docs_api, docs_everywhere, docs_org_api, docs_scopes, library_on_this_mac, own_scope,
    read_edit_target,
)
from brindle.db import DB
from brindle.pro import license
from brindle.service import inbox
from brindle.service.dto import (
    AccountDTO,
    AgentDTO,
    CloudOptionsDTO,
    DecisionDTO,
    GoalDTO,
    PreflightItemDTO,
    ProfileDTO,
    ProviderConnectionDTO,
    RepoDTO,
    RoutingAgentDTO,
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


class HeavyReadLane:
    """App-owned admission and worker limits for slow, synchronous reads.

    Admission never waits. The separate worker limiter keeps these reads
    out of Starlette's default pool, leaving its capacity for cheap routes.
    Both limits have the same size so admitted requests have a worker slot.
    """

    def __init__(self, capacity: int = 4):
        self.admission = anyio.CapacityLimiter(capacity)
        self.workers = anyio.CapacityLimiter(capacity)

    async def run(self, handler: Callable[[Request], JSONResponse], request: Request) -> JSONResponse:
        try:
            self.admission.acquire_nowait()
        except anyio.WouldBlock:
            err = ServiceError("busy", "Heavy read capacity is full", 503)
            return JSONResponse(err.to_json(), status_code=err.http_status)

        try:
            # Keep the slot until the synchronous handler has returned,
            # including its error conversion and closing its thread's DB.
            # Heavy reads are the work that waits for room (brindle.spawn).
            with spawn.waits_for_room():
                return await anyio.to_thread.run_sync(handler, request, limiter=self.workers)
        finally:
            self.admission.release()


def _json(fn: Callable[[DB, Request], Any]) -> Callable[[Request], JSONResponse]:
    """A sync handler (Starlette runs it on a thread) whose return value is the
    JSON body and whose exceptions become the service's error JSON. Owns the
    request's DB, including on error responses."""
    @wraps(fn)
    def handler(request: Request) -> JSONResponse:
        from brindle.pro import credentials

        try:
            db = DB()
            try:
                with credentials.reading():
                    return JSONResponse(fn(db, request))
            finally:
                db.conn.close()
        except Exception as e:  # noqa: BLE001 - every engine error maps to a ServiceError
            err = from_exception(e)
            if err.code == "internal":
                log.exception("brindle: daemon route %s failed", request.url.path)
            return JSONResponse(err.to_json(), status_code=err.http_status)
    return handler


def _heavy_json(fn: Callable[[DB, Request], Any]) -> Callable[[Request], Awaitable[JSONResponse]]:
    """Mark a read for bounded admission before creating its DB or worker."""
    sync_handler = _json(fn)

    @wraps(fn)
    async def handler(request: Request) -> JSONResponse:
        lane: HeavyReadLane = request.app.state.heavy_read_lane
        return await lane.run(sync_handler, request)

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


# The system's temporary folders: a repo there (a benchmark's, a test's) is
# listed only while a session runs in it.
TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
# How long a repo's "still a git repo on disk" answer is kept.
REPO_SEEN_TTL = 60.0
_repo_seen: dict[str, float] = {}


def _still_a_repo(root: str, now: float) -> bool:
    """Whether ``root`` is still a git checkout on disk (a stat, no git). A
    yes is kept a while; a no is asked again (a repo cloned back shows at once)."""
    seen = _repo_seen.get(root)
    if seen is not None and now - seen <= REPO_SEEN_TTL:
        return True
    there = os.path.exists(os.path.join(root, ".git"))
    if there:
        _repo_seen[root] = now
    return there


@_json
def repos(db: DB, request: Request) -> Any:
    """The person's projects, oldest first by when brindle first worked there: each repo brindle has
    worked in that is still a git repo on disk. A repo in a system temp folder
    and a scratch session are listed only while a session runs there: its
    chat's pane is up and still its own, as sessions.live judges it, from the
    tmux look GET /sessions shares (no git on this path, and tmux only when
    such a repo has a chat that may be running). brindle's own repos
    (practice runs) are listed as before, flagged ``demo``."""
    from brindle import scratch
    from brindle.config import brindle_home
    from brindle.demo import is_demo_repo

    now = time.monotonic()
    opened = {root: float(created_at) for root, created_at in db.conn.execute(
        "SELECT repo_root, MIN(created_at) FROM workspaces GROUP BY repo_root")}
    home = str(brindle_home()).rstrip("/") + "/"
    scratch_root = str(scratch.scratch_dir()).rstrip("/") + "/"
    look: list = []

    def running(chats: list[str]) -> bool:
        if not chats:
            return False
        if not look:
            look.extend(_shared_panes(db))
        panes, owners = look
        return any(a is not None and agents.is_alive(a, panes) and agents.owns_pane(db, a, owners)
                   for a in map(db.get_agent, chats))

    listed = []
    for root, (used, chats) in db.repo_activity().items():
        mine = root.startswith(home)      # brindle's own (practice runs, scratch), wherever its home is
        if root.startswith(scratch_root):
            keep = running(chats)
        elif not _still_a_repo(root, now):
            keep = False
        else:
            keep = mine or not root.startswith(TEMP_ROOTS) or running(chats)
        if keep:
            listed.append((used, root))
    listed.sort(key=lambda u: (is_demo_repo(u[1]), opened.get(u[1], float("inf")), u[1]))
    return [RepoDTO(repo_root=r, name=r.rstrip("/").rsplit("/", 1)[-1], demo=is_demo_repo(r)).to_json()
            for _, r in listed]


# The app reads every repo's sessions at once (GET /sessions per repo, after
# every bus event): one tmux snapshot and its pane owners answer the whole
# burst, not one each. Kept while no new event is written and for at most
# PANE_VIEW_MAX_AGE (a pane dying writes no event).
PANE_VIEW_MAX_AGE = 2.0


@dataclasses.dataclass
class _PaneView:
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    event: int = -1
    at: float = -math.inf
    panes: Any = None
    owners: dict[str, str] | None = None


_pane_view = _PaneView()


def _shared_panes(db: DB) -> tuple[Any, dict[str, str]]:
    """The tmux snapshot and pane owners for this read: the one a read just
    before took, while still current, else a fresh one. Concurrent reads wait
    for the one taking it rather than each asking tmux."""
    newest = db.conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
    view = _pane_view
    with view.lock:
        now = time.monotonic()
        if view.owners is None or view.event != newest or now - view.at >= PANE_VIEW_MAX_AGE:
            panes = tmux.list_panes()
            view.owners = agents.pane_owners(db, panes)
            view.panes, view.event, view.at = panes, newest, now
        return view.panes, view.owners


@_json
def list_sessions(db: DB, request: Request) -> Any:
    repo = _need_repo(request)
    panes, owners = _shared_panes(db)
    live = [SessionDTO.from_agent(s.root, s.goal).to_json() for s in sessions.live(db, repo, panes, owners)]
    paused = [SessionDTO.from_agent(s.root, sessions.goal_line(db, s.root)).to_json()
              for s in sessions.paused(db, repo, panes, owners) if s.root.dismissed_at is None]
    listed = {row["id"]: row for row in live + paused}
    live_ids = {row["id"] for row in live}
    recovery = getattr(request.app.state, "recovery", None)
    for root_id in recovery.resuming() if recovery else ():
        root = db.get_agent(root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        if not root or not ws or ws.repo_root != repo or root.dismissed_at is not None or root.status == "done":
            continue
        # A launch's starting root may not have a live pane yet, so neither list includes it.
        row = listed.setdefault(root_id, SessionDTO.from_agent(root, sessions.goal_line(db, root)).to_json())
        row["status"] = "resuming"
        live_ids.add(root_id)
    # Recovering roots keep the normal live-session order even without a pane.
    current = sorted((row for root_id, row in listed.items() if root_id in live_ids),
                     key=lambda row: row["created_at"], reverse=True)
    return current + [row for row in paused if row["id"] not in live_ids]


class SnapshotView:
    """Share a rendering between the app's adjacent snapshot and inbox reads.

    Events invalidate it immediately; the short age bound also catches changes
    outside the event stream (pane liveness, transcript usage, and git files).
    One app owns one entry, and concurrent readers join the same rendering.
    A reader that waited behind a rendering takes one begun after it arrived
    as its own (it shows all the reader could ask for): while sessions come
    back, events never stop, and readers piled up behind a slow rendering
    each rendered again in turn, the pane gate's read last of all.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.key: tuple[str | None, int, bool] | None = None
        self.at = 0.0
        self.began = 0.0    # when the rendering ``rows`` holds began
        self.rows: list[dict] | None = None

    def read(self, db: DB, repo: str | None, read_screens: bool = True) -> list[dict]:
        arrived = time.monotonic()
        with self.lock:
            waited = time.monotonic() - arrived
            if (self.rows is not None and self.key is not None and self.key[0] == repo
                    and self.key[2] == read_screens and self.began > arrived):
                _log_read(waited, None, read_screens)
                return self.rows
            newest = db.conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
            key = (repo, newest, read_screens)
            if self.rows is None or self.key != key or time.monotonic() - self.at >= 0.25:
                began = time.monotonic()
                self.rows = _snapshot_rows(db, repo, read_screens)
                self.key, self.at, self.began = key, time.monotonic(), began
                _log_read(waited, self.at - began, read_screens)
            else:
                _log_read(waited, None, read_screens)
            return self.rows


SLOW_SNAPSHOT_READ = 1.0    # seconds: a read sharing a rendering this slow is logged whenever it happens


def _log_read(waited: float, rendered: float | None, read_screens: bool) -> None:
    """The daemon's log (warnings only, its stderr) has each snapshot rendering:
    how long its reader queued behind another's, and how long its own took.
    A read that took one begun after it arrived (``rendered`` None) is logged
    while sessions come back (``read_screens`` False then), or when it waited
    long. Restarts are measured from it."""
    if rendered is None and read_screens and waited < SLOW_SNAPSHOT_READ:
        return
    if rendered is None:
        log.warning("brindle: snapshot read waited %.2fs, shared a rendering", waited)
    else:
        log.warning("brindle: snapshot read waited %.2fs, rendered in %.2fs", waited, rendered)


def _snapshot_rows(db: DB, repo: str | None, read_screens: bool = True) -> list[dict]:
    # Git counts only as far as they are known: the app's first read after the
    # daemon starts must not wait on a git status per workspace (with recovery
    # competing, that took half a minute). The rest is counted afterwards and
    # published (view.warm_later); the events feed's git sweep keeps them current.
    known = view.KnownGit(db)
    rows = view.snapshot(db, repo, read_screens=read_screens, known_git=known)
    view.warm_later(known.pending)
    return rows


def recovering(state) -> bool:
    """Whether the daemon's start is still bringing sessions back (lifecycle.Recovery)."""
    recovery = getattr(state, "recovery", None)
    return recovery is not None and recovery.progress() is not None


def _shared_snapshot(db: DB, request: Request) -> list[dict]:
    # While sessions come back, no agent's screen is read to correct its status: a capture per
    # running pane, each slow on a machine busy launching them, made every read a second or two,
    # and the app shows a session that's back only from a read begun after it was. Its hooks say.
    read_screens = not recovering(request.app.state)
    shared = getattr(request.app.state, "snapshot_view", None)
    return (shared.read(db, _repo(request), read_screens) if shared is not None
            else _snapshot_rows(db, _repo(request), read_screens))


@_json
def snapshot(db: DB, request: Request) -> Any:
    rows = _shared_snapshot(db, request)
    return [WorkspaceDTO.model_validate(ws).to_json() for ws in rows]


@_json
def session_view(db: DB, request: Request) -> Any:
    """A session's chat in its workspace's row (view.session_entry): what the
    app needs to show its pane the moment the session is back after a restart.
    It never waits on the shared snapshot's rendering (SnapshotView.lock),
    which is seconds per read while the other sessions come back."""
    root = db.get_agent(request.path_params["root"])
    if root is None or root.parent_id:
        raise ServiceError("not_found", "no such session", 404)
    panes, owners = _shared_panes(db)
    row = view.session_entry(db, root, panes, owners)
    if row is None:
        raise ServiceError("not_found", "the session has no checkout", 404)
    return WorkspaceDTO.model_validate(row).to_json()


@_json
def goal(db: DB, request: Request) -> Any:
    root_id = request.query_params.get("root")
    root = view.autopilot_root(db, _need_repo(request),
                              panes=None if root_id else tmux.list_panes(), root_id=root_id)
    ap = root and db.get_autopilot(root.id)
    if not ap:
        return None
    from brindle.autopilot import goal_activity, milestone_phases

    phase = session_review.goal_phase(db, ap.root_id)
    return GoalDTO.from_autopilot(ap, db.milestones(ap.root_id),
                                  milestone_phases(db, ap.root_id), phase,
                                  goal_activity(db, ap.root_id),
                                  session_review.session_pr_state(
                                      db, ap.root_id, phase[1] if phase[0] == "pr_open" else None),
                                  drifting=session_sync.drifting(db, ap.root_id)).to_json()


@_json
def inbox_(db: DB, request: Request) -> Any:
    return [i.to_json() for i in inbox.items(db, _repo(request), snapshot_rows=_shared_snapshot(db, request))]


@_json
def agent(db: DB, request: Request) -> Any:
    try:
        a = agents.get(db, request.path_params["id"])
    except agents.AgentError as e:
        raise ServiceError("not_found", str(e), 404) from e
    return AgentDTO.model_validate(view.agent_entry(db, a, detail=True, panes=tmux.list_panes())).to_json()


@_json
def agent_transcript(db: DB, request: Request) -> Any:
    from brindle import identity_lock
    from brindle.web.chat import transcript

    a = db.get_agent(request.path_params["id"])
    if a is None:
        raise ServiceError("not_found", "no such agent", 404)
    if identity_lock.locked_by(a.id):
        raise ServiceError("forbidden", "session belongs to another identity", 403)
    try:
        return transcript(a, request.query_params.get("after") or 0, db=db)
    except ValueError as e:
        raise ServiceError("invalid", "bad cursor", 400) from e


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


@_heavy_json
def session_readiness(db: DB, request: Request) -> Any:
    """Whether brindle judges the session ready for a PR; ``reasons``: what
    is missing, in plain words (none when it is ready); ``pr``: the PR the
    person opened, with how many session commits aren't pushed to it yet
    (session_review.open_pr), or None."""
    root, ws = session_review.session(db, request.path_params["root"])
    why = session_review.reasons(db, root, ws)
    return {"root_id": root.id, "ready": not why, "reasons": why, "pr": session_review.open_pr(db, root, ws)}


@_heavy_json
def session_diff(db: DB, request: Request) -> Any:
    """The session branch against its PR base, each file and hunk with its
    authors (worker ids, "supervisor", "you") and the authors for filtering.
    Readable at any time: readiness only gates opening the PR."""
    root, ws = session_review.session(db, request.path_params["root"])
    return session_review.diff(db, root, ws)


@_heavy_json
def session_tree(db: DB, request: Request) -> Any:
    """The session checkout's tracked files (paths only) and its branch."""
    root, ws = session_review.session(db, request.path_params["root"])
    return {"root_id": root.id, "branch": ws.branch,
            "files": [p for p in git.run(["ls-files", "-z"], ws.path).stdout.split("\0") if p]}


@_json
def session_pr_draft(db: DB, request: Request) -> Any:
    """Draft text from the goal; readiness is enforced when opening the PR."""
    root, ws = session_review.session(db, request.path_params["root"])
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
def kb_doc(db: DB, request: Request) -> Any:
    """One indexed document's whole visible text, by the id the map's local
    node carries (kb.document); 404 when it is not this repo's."""
    try:
        doc_id = int(request.path_params["id"])
    except ValueError:
        doc_id = -1
    found = kb.document(db, knowledge.repo_key(_need_repo(request)), doc_id)
    if found is None:
        raise ServiceError("not_found", "no such document", 404)
    return found


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
    # The file's own body, without what it extends or its rule packs: what PUT /profiles/{name} replaces.
    found = profiles._read(name, repo)
    instructions = profiles._frontmatter(found[0])[1].strip() if found else ""
    return {**ProfileDTO.from_profile(p, errors, warnings).to_json(),
            "source": profiles.profile_source(name, repo), "extends": chain,
            "rules": [{"name": k.name, "description": k.description, "source": k.source} for k in packs],
            "prompt": prompt, "instructions": instructions, "lint": {"errors": errors, "warnings": warnings}}


@_json
def get_config(db: DB, request: Request) -> Any:
    from brindle import providers

    repo = _repo(request)
    settings = {"user": config.user_settings()}
    if repo:
        # Saved model choices are checked for readiness while the config loads (worker_models.apply):
        # no sign-in probe or keychain read for that here, as for GET /routing?learned=0.
        with providers.no_sign_in_probes():
            settings["repo"] = dataclasses.asdict(config.load_repo_config(repo))
        # Settings this project's own files set (over the person's): key -> "local" or "repo".
        settings["overrides"] = {k: s for k in settings["repo"] if (s := config.setting_source(repo, k)) in ("local", "repo")}
        # The local file's own rules: the effective list unions every scope's, so the app edits this one.
        settings["local"] = config.local_values(repo, ("rules",))
    # `keys=0`: the page doesn't show the stored keys, and reading them is the slow part
    # (one keychain read per name), so it skips them.
    if request.query_params.get("keys") in ("0", "false"):
        return {**settings, "keys": []}
    return {**settings, "keys": _key_rows(repo)}


def _key_rows(repo: str | None) -> list[dict]:
    try:
        denied = key_check.personal_keys_denied(repo)
    except Exception:  # noqa: BLE001 - policy unreadable: fail closed, as storing does
        denied = True

    def row(name: str) -> dict:
        rec = keystore.get_record(name)    # the one read of this key (on macOS a `security` call)
        raw = rec.get("value")
        value = raw if isinstance(raw, str) and raw else None
        return {
            "name": name, "set": value is not None, "last4": _last4(value or ""),
            "provider": keystore.provider_of(name),
            "status": (rec.get("status") or "unchecked") if value else None,
            "checked_at": rec.get("checked_at") if value else None,
            "key_page": key_check.key_page(name),
            "bills": keys_cmds.bills_instead_of_subscription(name),
            "personal_keys_denied": denied,
            # Set in the daemon's environment but not stored; the value is never returned.
            "importable": value is None and bool(os.environ.get(name, "").strip()),
        }

    names = keystore.known_names(repo)
    with ThreadPoolExecutor(max_workers=8) as pool:    # the reads wait on subprocesses, not the CPU
        return list(pool.map(spawn.carry(row), names))


@_json
def get_user_settings(db: DB, request: Request) -> Any:
    from brindle import provider_updates
    from brindle.provider_switch import quota_switch

    return {"visuals": config.visuals_setting(), "claude_permission_mode": config.claude_permission_mode(),
            "quota_switch": quota_switch(),
            "opencode_permission_mode": config.opencode_permission_mode(),
            "visuals_enabled": "on" if config.visuals_enabled_setting() else "off",
            "login_shell_env": "on" if config.login_shell_env_setting() else "off",
            **provider_updates.setting_fields()}


@_json
def get_permissions(db: DB, request: Request) -> Any:
    """The permission policy: preset, unmatched setting, rules, saved actions, prompt counts."""
    from brindle.service import permissions as svc_permissions

    return svc_permissions.policy_dto(repo_root=_repo(request)).to_json()


@_json
def get_permission_asks(db: DB, request: Request) -> Any:
    """The requests waiting on the person, grouped by kind and general form (``repo`` narrows)."""
    from brindle.service import permissions as svc_permissions

    return [g.to_json() for g in svc_permissions.asks(db, _repo(request))]


@_json
def get_permission_report(db: DB, request: Request) -> Any:
    """The ask rate and top causes of permission asks (``days``, default 7; ``repo`` narrows)."""
    from brindle import permission_asks

    try:
        days = max(1, int(request.query_params.get("days", permission_asks.DEFAULT_DAYS)))
    except ValueError:
        days = permission_asks.DEFAULT_DAYS
    return permission_asks.report(db, days, _repo(request))


@_json
def get_permission_plan(db: DB, request: Request) -> Any:
    """What allowing everything waiting would grant (``forever=1`` for lasting grants)."""
    from brindle.service import permissions as svc_permissions

    forever = request.query_params.get("forever") in ("1", "true", "yes")
    return svc_permissions.plan(db, _repo(request), forever).to_json()


@_json
def get_debug(db: DB, request: Request) -> Any:
    """``{enabled, staff}`` for debug logs (only a brindle staff account may turn them on).
    ``?fresh=1`` asks the backend now instead of the cached answer (the app does,
    right before a staff-only update), and says not staff when it can't be asked."""
    from brindle import debuglog

    return debuglog.checked(fresh=request.query_params.get("fresh") == "1")


@_json
@profiles.reading()
@agents.reading_launch_environment()
@license.reading()
def get_routing(db: DB, request: Request) -> Any:
    """Each tier's profiles (and the model and agent each runs), the reviewer
    and the excluded providers, with the scope each comes from, every profile
    with what routing knows of it, and the org policy all of it answers to.
    A registered worktree path in ``repo`` previews its launch defaults.
    ``scope=user``: the person's own view, as every repo starts from it (their
    ~/.brindle/config.json and their library, never this repo's files); the
    repo still gives the org policy and what learning knows.
    ``include_repo_orders=1`` also lists saved repo and local orders in the
    order metadata, without changing the user view's effective routing. ``learned=0``
    answers at once, for the app to read the rest after the page is up: no
    learned picks (each kind's ``learned`` is null; working them out
    resolves the dispatch for every candidate) and no sign-in probe (each
    ``signed_in`` as an earlier probe found it, else as can't-be-told,
    true)."""
    from brindle import providers

    if request.query_params.get("learned") != "0":
        return _routing(db, request, learn=True)
    with providers.no_sign_in_probes():
        return _routing(db, request, learn=False)


def _routing(db: DB, request: Request, learn: bool) -> Any:
    from concurrent.futures import ThreadPoolExecutor

    from brindle import autopilot, model_catalog, providers
    from brindle.service.routing import (
        RoutingPolicy, agent_model_orders, legacy_medium_override, provider_name, saved_routing_orders,
    )

    repo = _need_repo(request)
    workspace = db.workspace_by_path(repo)
    if workspace:
        repo = workspace.repo_root
    user = request.query_params.get("scope") == "user"
    lib = None if user else repo        # where profiles are looked up: the repo's own first, or only the person's
    cfg = config.load_repo_config(repo, user_only=user)
    policy = RoutingPolicy.load(repo, user_library=user, **({"workspace": workspace} if workspace else {}))
    every = profiles.list_profiles(lib)
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
    # A provider with no CLI of its own (native) is always there.
    here = {p.name: p.provider not in doctor.CLI_INSTALL or p.provider in installed for p in every}
    # Each sign-in once per environment its profiles give it, all at once: a
    # probe runs the provider's CLI, and a signed-out answer isn't cached.
    # (Without probes each answer is at hand, and a pool thread wouldn't see that mode.)
    probes = list({(p.provider, tuple(sorted(p.env.items()))) for p in every if here[p.name]})

    def probe(k: tuple) -> bool:
        return providers.signed_out(k[0], dict(k[1])) is None

    if not providers.probing():
        signed = {k: probe(k) for k in probes}
    else:
        with ThreadPoolExecutor(max_workers=max(1, len(probes))) as pool:
            signed = dict(zip(probes, pool.map(spawn.carry(probe), probes)))
    rows, agent_rows = [], {}
    builtin = profiles.builtin_names()
    for p in every:
        errors, warnings = profiles.lint_profile(p.name, lib)
        try:
            kind = profiles.kind_of(p.name, lib)
        except (KeyError, profiles.ProfileError):
            kind = "pinned"  # Unresolved inheritance still has a row for its lint errors.
        role = profiles.base_role(p.name, lib)
        if not p.model and model_catalog.default_model(p.provider):
            _, model = policy.runs(p.name)
        else:
            model = autopilot.effective_profile_model(p, lib) or None
        rows.append(RoutingProfileDTO(
            lint_errors=errors, lint_warnings=warnings,
            kind=kind, description=p.description, rules=p.rules,
            name=p.name, provider=p.provider, provider_name=provider_name(p.provider),
            model=model, **model_names.describe(p, model), builtin=p.name in builtin,
            role=role, installed=here[p.name],
            signed_in=here[p.name] and signed[(p.provider, tuple(sorted(p.env.items())))],
            excluded=p.provider in cfg.exclude_providers, cost_tier=autopilot.cost_tier(p.name, cfg, lib),
            allowed=refusals[p.name] is None, refusal=refusals[p.name]))

    def source(key: str) -> str:
        if user:
            return "user" if key in config.user_settings() else "default"
        return config.setting_source(repo, key)

    # Every candidate's check reads the same stored keys: each from the store once.
    with keystore.reading():
        context = {"workspace": workspace, "model_fields": policy._model_fields} if workspace else None
        kinds = _routing_kinds(db, cfg, repo, learn=learn, **({"routing_context": context} if context else {}))
    for row in rows:
        if row.kind == "agent":
            agent_rows[row.name] = RoutingAgentDTO(description=row.description, rules=row.rules, role=row.role,
                                                  builtin=row.builtin, models=agent_model_orders(
                                                      cfg, row.name, kinds.get(row.name)))
    # The older app has one slot per role. Medium is the representative size.
    for name in ("reviewer", "supervisor"):
        if name in agent_rows and legacy_medium_override(cfg, name, pinned=bool(cfg.review_profile)) is not None:
            kinds[name] = kinds[name].model_copy(update=agent_rows[name].models["medium"].to_json())
    return RoutingDTO(
        tiers=tiers,
        reviewer=RoutingSettingDTO(values=[cfg.reviewer], source=source("reviewer")),
        exclude_providers=RoutingSettingDTO(values=cfg.exclude_providers, source=source("exclude_providers")),
        profiles=rows, policy=policy.to_dto(every), kinds=kinds, agents=agent_rows,
        orders=saved_routing_orders(repo, user_only=user and request.query_params.get("include_repo_orders") != "1"),
    ).to_json()


def _routing_kinds(db: DB, cfg: config.RepoConfig, repo: str, learn: bool = True, *,
                   routing_context: dict | None = None) -> dict[str, RoutingKindDTO]:
    """Per kind of work: the owner's pick, its ordered fallbacks, whether it
    is locked, and the different profile hosted learning runs for it now
    (``learn=False``: none asked for, each null). A locked kind is never
    overridden, so it never reports one."""
    from brindle import agents, autopilot

    def learned(kind: str, run) -> str | None:
        if not learn or cfg.routing_locked.get(kind):
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
        name, learned_pick, _ = autopilot._route_by_weight(
            db, cfg, repo, t, None, None, **({"policy": routing_context} if routing_context else {}))
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
def get_storage(db: DB, request: Request) -> Any:
    """brindle's disk use: every worktree classified merged / abandoned / active
    with its size and why it must stay (``disk.storage_report``), as last
    measured, at once: measuring walks every worktree, so it runs on a thread
    of its own (``storage_reports``), plus ``measured_at`` and ``refreshing``.
    ``fresh=1`` measures again now (unless a measurement is under way)."""
    from brindle.daemon.storage_reports import REPORTS

    return REPORTS.get(_repo(request), fresh=request.query_params.get("fresh") in ("1", "true"))


@_json
@profiles.reading()
@agents.reading_launch_environment()
@license.reading()
def get_doctor(db: DB, request: Request) -> Any:
    return [dataclasses.asdict(c) for c in doctor.checks(_repo(request), db=db)]


@_json
def provider_preflight(db: DB, request: Request) -> Any:
    """What would stop a session on this provider in ``repo`` before its first
    turn: the items POST /v1/sessions answers 409 with, for the identity its
    chat would run as (see brindle.preflight). Native, which runs no chat,
    and a request without ``repo`` get the provider's checks as brindle sees it."""
    from brindle import preflight, provider_homes
    from brindle.daemon.routes_actions import session_blockers

    provider, repo = request.path_params["provider"], _repo(request)
    provider_homes.confirm_login(provider)
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
    in according to its CLI (null if unknown), every install found, and
    brindle's config home for it (null for Antigravity, which shares ~/.gemini)."""
    from brindle import providers_install

    return providers_install.statuses()


# GET /providers/usable answers from the last full answer for the repo: telling
# whether each CLI is signed in runs the CLI (a second or more for them all),
# which the New session page must not wait on. Each ask has the answer worked
# out again on a thread of its own (the first answer is told without running
# any CLI); one that changes is published as a "providers.usable" event, and
# the page asks again.


@dataclasses.dataclass
class _UsableCache:
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    answers: dict[str, dict] = dataclasses.field(default_factory=dict)   # repo → its last answer
    refreshing: set[str] = dataclasses.field(default_factory=set)
    again: set[str] = dataclasses.field(default_factory=set)       # asked for while one ran


_usable_cache = _UsableCache()


def _usable_answer(db: DB, repo: str) -> dict:
    from brindle.service import sessions as svc

    known = db.conn.execute("SELECT 1 FROM workspaces WHERE repo_root=? LIMIT 1", (repo,)).fetchone()
    root = repo if known else git.main_repo_root(repo)   # a repo brindle knows needs no git to name it
    usable = svc.usable_providers(root)
    return {"usable": usable, "default": usable[0] if usable else None}


def _refresh_usable(repo: str) -> None:
    """Work ``repo``'s answer out again in the background. While one already
    runs (it may have begun before a sign-in this ask is about), one more
    follows it: never more than one running and one waiting."""
    with _usable_cache.lock:
        if repo in _usable_cache.refreshing:
            _usable_cache.again.add(repo)
            return
        _usable_cache.refreshing.add(repo)
    _start_usable_refresh(repo)


def _start_usable_refresh(repo: str) -> None:
    # A plain thread: a fresh context, so no request's deadline cuts the CLIs' checks short.
    threading.Thread(target=_run_usable_refresh, args=(repo,), name="brindle-usable-refresh",
                     daemon=True).start()


def _run_usable_refresh(repo: str) -> None:
    from brindle import bus

    try:
        db = DB()
        try:
            answer = _usable_answer(db, repo)
            with _usable_cache.lock:
                before = _usable_cache.answers.get(repo)
                _usable_cache.answers[repo] = answer
            if before != answer:
                bus.publish(db, "providers.usable", repo_root=repo, **answer)
        finally:
            db.conn.close()
    except Exception:  # noqa: BLE001 - the last answer stands; the next read tries again
        log.debug("brindle: refreshing the usable CLIs for %s failed", repo, exc_info=True)
    finally:
        with _usable_cache.lock:
            follow = repo in _usable_cache.again
            _usable_cache.again.discard(repo)
            if not follow:
                _usable_cache.refreshing.discard(repo)
        if follow:      # still marked running: the waiting one goes now
            _start_usable_refresh(repo)


@_json
def providers_usable(db: DB, request: Request) -> Any:
    """What a new supervisor session in ``repo`` can run on, by the rule POST
    /sessions starts by (service.sessions.supervisor_provider): ``usable``,
    each CLI that is installed, signed in, turned on and allowed by the org's
    policy, in preference order, and ``default``, the one a start with no
    provider runs on (null when none is usable: such a start is refused).
    Answered at once from the last answer, re-checked behind the reply: no CLI runs here."""
    from brindle import providers

    repo = _need_repo(request)
    with _usable_cache.lock:
        got = _usable_cache.answers.get(repo)
    if got is None:
        # The first ask: each sign-in as last told (a CLI not yet asked counts as signed in,
        # which a start's preflight still checks), while the full answer is worked out.
        with providers.no_sign_in_probes():
            answer = _usable_answer(db, repo)
        with _usable_cache.lock:
            _usable_cache.answers.setdefault(repo, answer)
        _refresh_usable(repo)
        return answer
    # Every ask has it worked out again behind the reply (one at a time per repo): a sign-in,
    # an install or a CLI turned on since tells nothing brindle watches, and the page asks only
    # when it opens or hears the answer changed. A CLI signed in stays told (providers.SIGNED_IN_TTL),
    # so the check behind a page that opens again and again is mostly free.
    _refresh_usable(repo)
    return got


@_json
def provider_connection(db: DB, request: Request) -> Any:
    """How the provider connects (brindle.connections): sign-in, API key or a
    cloud, and for a cloud its settings and login (valid, expiry, the command
    that renews it). ``managed`` says why the org decides it instead."""
    from brindle import connections

    try:
        got = connections.view(request.path_params["provider"], _repo(request), db)
    except connections.Invalid as e:
        raise ServiceError("invalid", str(e)) from None
    return ProviderConnectionDTO(**got).to_json()


@_json
def cloud_options(db: DB, request: Request) -> Any:
    """The cloud choices this machine is set up for, from the cloud CLIs' own
    files: AWS profiles (SSO or not), gcloud's application-default login and
    default project, the Azure CLI's tenant, and the usual regions. No secrets."""
    from brindle import connections

    return CloudOptionsDTO(**connections.options()).to_json()


@lru_cache(maxsize=1)
def _account_details(ent: license.Entitlement, generation: int | None, period: int) -> AccountDTO:
    """Identity and orgs share a read, including offline misses. Entitlement,
    credential generation and a bounded period keep it scoped to this login.
    Repo-specific learning state is resolved separately on every request."""
    from brindle.pro import auth
    from brindle.pro.account import ProAccount

    account = ProAccount()
    who = {}
    try:
        who = auth.me(account._client(None), account.store)
    except Exception:  # noqa: BLE001 - identity is optional while offline
        log.debug("brindle: daemon couldn't read the account's identity", exc_info=True)
    try:
        # For the app's org picker; the plan still shows when the org list can't be had.
        orgs = account.orgs(None)
    except Exception:  # noqa: BLE001 - offline, or the backend refused
        log.debug("brindle: daemon couldn't list the account's orgs", exc_info=True)
        orgs = []
    return AccountDTO(signed_in=True, name=who.get("name"), email=who.get("email"), plan=ent.plan, orgs=orgs)


license._clear_hooks.append(_account_details.cache_clear)


@_json
def account(db: DB, request: Request) -> Any:
    try:
        ent = license.current(refresh=False)
    except license.LicenseError:
        return AccountDTO(signed_in=False).to_json()
    details = _account_details(ent, license._disk_cache_generation(), int(time.monotonic() // license.CACHE_SECONDS))
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
    return {**details.to_json(),
            "subject": ent.sub, "org_id": ent.org_id, "status": ent.status, "features": sorted(ent.features),
            "claude_usage_scope": quota.claude_usage_scope(),
            "seats": ent.seats, "role": ent.role, "expires_at": ent.exp,
            "in_grace": ent.in_grace, "grace_until": ent.grace_until,
            "learning_available": available, "learning": on, "learning_note": note}


@_json
def github_connection(db: DB, request: Request) -> Any:
    """brindle's own GitHub sign-in (brindle.github_connect): whether there is one, of what
    kind, and whether gh's can be copied. Reads brindle's store and gh's config: no keychain."""
    from brindle import github_connect
    from brindle.service.dto import GitHubConnectionDTO

    return GitHubConnectionDTO(**github_connect.status()).to_json()


@_json
def get_cost(db: DB, request: Request) -> Any:
    cost.require_entitled()
    days = int(request.query_params.get("days") or COST_DAYS)
    s = cost.summary(db, _repo(request), now=time.time(), days=days)

    def bucket(b: cost.Bucket) -> dict:
        return {"dollars": b.dollars, "tokens": b.tokens, "unknown": b.unknown, "free": b.free}

    return {"days": s.days, "total": bucket(s.total),
            "by_model": {m: bucket(b) for m, b in s.by_model.items()}}


def _with_kept(request: Request, query: dict[str, str], ask: Callable[[], list[dict]]) -> list[dict]:
    """The docs kept on this Mac (local_library) with the server's ``ask()``
    answers, each doc once (a kept doc's shared server copy is the kept
    one), newest first. With the library on this Mac, a server out of reach
    leaves just the kept docs."""
    from brindle.pro import local_library

    repo = _repo(request)
    kept = local_library.records()
    seen: dict[str, dict] = {d["doc_id"]: d for d in local_library.listed(query, own_scope(repo))} if kept else {}
    held = {r["server_id"] for r in kept if r.get("server_id")}
    try:
        got = ask()
    except ServiceError:
        if not library_on_this_mac(repo):
            raise
        got = []
    for d in got:
        if isinstance(d, dict) and isinstance(d.get("doc_id"), str) and d["doc_id"] not in held:
            seen.setdefault(d["doc_id"], d)
    return sorted(seen.values(), key=lambda d: -float(d.get("created_at") or 0))


def _own_docs(request: Request, query: dict[str, str]) -> list[dict]:
    """The person's own docs: those kept on this Mac and those on the servers."""
    return _with_kept(request, query, lambda: docs_api("GET", "/analysis-docs", repo=_repo(request), scope="user",
                                                       query=query).get("docs") or [])


def _all_docs(request: Request, query: dict[str, str]) -> list[dict]:
    """The docs of every scope of docs_scopes and those kept on this Mac, each doc once, newest first."""
    return _with_kept(request, query, lambda: [d for got in docs_everywhere("/analysis-docs", _repo(request), query)
                                               for d in got.get("docs") or []])


@_json
def analysis_docs(db: DB, request: Request) -> Any:
    """The library's docs (``{"docs": [...]}``, each as the server lists it),
    filtered by ``scope`` (user, team:<id> or org:<id>; none: every scope the
    person reads, see docs_scopes), ``topic``, ``kind`` and ``session``.
    The person's own may be kept on this Mac (see _own_docs)."""
    q = request.query_params
    query = {k: q[k] for k in ("topic", "kind", "session") if q.get(k)}
    if q.get("scope") == "user":
        return {"docs": _own_docs(request, query)}
    if q.get("scope"):
        return docs_api("GET", "/analysis-docs", repo=_repo(request), scope=q["scope"], query=query)
    return {"docs": _all_docs(request, query)}


def _topic_counts(docs: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for d in docs:
        for t in d.get("topics") or []:
            counts[t] = counts.get(t, 0) + 1
    return {"topics": [{"topic": t, "count": n} for t, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]}


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
    """Build a whole-scope graph from the library list, or proxy a path map.

    Use the existing scoped list contract for pathless maps: it also includes
    unlinked docs and supplies provenance for either view. Nothing is cached.
    """
    q = request.query_params
    try:
        paths = json.loads(q.get("paths", "[]"))
    except (ValueError, TypeError) as e:
        raise ServiceError("bad_request", "paths must be a JSON array", 400) from e
    if (not isinstance(paths, list)
            or any(not isinstance(p, str) or not p.strip() for p in paths)
            or q.get("depth", "1") != "1"
            or q.get("scope_kind") not in ("user", "group", "org") or not q.get("scope_id")):
        raise ServiceError("bad_request", "choose a scope and optional code paths at depth 1", 400)
    scope = {"kind": q["scope_kind"], "id": q["scope_id"]}
    query = {"scope_kind": scope["kind"], "scope_id": scope["id"]}
    if scope["kind"] == "user":         # the person's own: those kept on this Mac too
        listed = {"docs": _with_kept(request, {}, lambda: docs_api("GET", "/analysis-docs", repo=_repo(request),
                                                                   query=query).get("docs") or [])}
    else:
        listed = docs_api("GET", "/analysis-docs", repo=_repo(request), query=query)
    docs = {d["doc_id"]: {**d, "scope": d.get("scope") or scope}
            for d in listed.get("docs") or [] if isinstance(d, dict) and isinstance(d.get("doc_id"), str)}
    # Local documents belong to the repo on this device, never a shared scope.
    local = kb.map_documents(db, _need_repo(request), paths) if scope["kind"] == "user" else []

    def doc_paths(doc: dict) -> list[str]:
        declared = doc.get("paths") or []
        mentioned = (doc.get("mentions") or {}).get("files") or []
        return list(dict.fromkeys(posixpath.normpath(p.strip()) for p in [*declared, *mentioned]
                                  if isinstance(p, str) and p.strip()))

    if paths:
        got = docs_api("GET", "/analysis-docs/map", repo=_repo(request),
                       query={**query, "paths": ",".join(paths), "depth": "1"})
        nodes = []
        for node in got.get("nodes") or []:
            if node.get("type") == "doc":
                doc_id = node.get("doc_id", node["id"])
                doc = docs.get(doc_id, {})
                nodes.append({**node, **doc, "id": node["id"], "doc_id": doc_id,
                              "kind": "doc", "title": node.get("label") or doc.get("title"),
                              "paths": doc_paths(doc)})
            elif node.get("type") == "path":
                path = posixpath.normpath(node["label"].strip())
                nodes.append({**node, "kind": "path", "title": path, "path": path})
        # Topics are not displayed by MapGraph; omit their edges as well.
        ids = {n["id"] for n in nodes}
        edges = [{"source": e["from"], "target": e["to"], "kind": e["kind"]}
                 for e in got.get("edges") or [] if e.get("from") in ids and e.get("to") in ids]
        return {"nodes": [*nodes, *local], "edges": edges}
    nodes, edges = list(local), []
    seen_paths = set()
    for doc_id, doc in docs.items():
        files = doc_paths(doc)
        nodes.append({**doc, "id": doc_id, "kind": "doc", "paths": files})
        for path in files:
            path_id = f"path:{path}"
            if path not in seen_paths:
                nodes.append({"id": path_id, "kind": "path", "path": path})
                seen_paths.add(path)
            edges.append({"source": doc_id, "target": path_id, "kind": "mentions"})
        for relation in ("related", "supersedes"):
            for target in doc.get(relation) or []:
                if target in docs:
                    edges.append({"source": doc_id, "target": target, "kind": relation})
    return {"nodes": nodes, "edges": edges}


@_json
def analysis_doc_topics(db: DB, request: Request) -> Any:
    """``{"topics": [{topic, count}, ...]}`` in one scope, or over every doc
    of :func:`_all_docs` without one."""
    scope = request.query_params.get("scope")
    if scope == "user":
        return _topic_counts(_own_docs(request, {}))
    if scope:
        return docs_api("GET", "/analysis-docs/topics", repo=_repo(request), scope=scope)
    return _topic_counts(_all_docs(request, {}))


@_json
def analysis_doc_scopes(db: DB, request: Request) -> Any:
    return {"scopes": docs_scopes(_repo(request))}


@_json
def library_folders(db: DB, request: Request) -> Any:
    """``{"scopes": [{scope: {kind, id, name}, folders: [...]}]}``: the folders
    the person can see in each scope they read (see pro/library_folders), each
    with the ``repo`` it was made for (None when this machine doesn't know)."""
    from brindle.pro import library_folders as folders

    repo = _repo(request)
    return {"scopes": folders.with_repos(db, folders.list_all(
        docs_scopes(repo), api=lambda m, p, b=None: docs_api(m, p, b, repo=repo), refusals=(ServiceError,)))}


@_json
def analysis_doc_switches(db: DB, request: Request) -> Any:
    """The person's own switches, and which their org has locked (see routes_actions.doc_switches)."""
    return doc_switches(_repo(request))


@_json
def doc_format(db: DB, request: Request) -> Any:
    """Settings › Doc format (brindle.doc_templates): what ``level`` (org, the
    default, team:<id>, or a level of the org's structure) sets and its
    version, the format in force there, where it is kept, the reference this
    machine's next new doc gets, and how many docs each template wrote."""
    from brindle import doc_templates

    level = request.query_params.get("level") or doc_templates.ORG
    return doc_templates.view(db, level)


@_json
def analysis_doc_policy(db: DB, request: Request) -> Any:
    """The org's doc policy, including ``export_destinations`` (a subset of download, google_docs, sharepoint)."""
    return docs_org_api("GET", "policy", repo=_repo(request))


@_json
def org_settings(db: DB, request: Request) -> Any:
    from brindle.daemon.org_settings import read

    return read(_repo(request))


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
    """One doc: its files, what the server read from it and how often briefs used it,
    and ``tasks`` when it is a plan on this machine (plans.doc_tasks: its split, in board state)."""
    from urllib.parse import quote

    from brindle import plans
    from brindle.pro import local_library

    doc_id = request.path_params["id"]
    repo = _repo(request)
    kept = local_library.get(doc_id)
    task_doc_id = doc_id
    if kept is None:
        got = docs_api("GET", f"/analysis-docs/{quote(doc_id, safe='')}", repo=repo)
    else:
        got = local_library.view(kept, own_scope(repo))
        if server_id := kept.get("server_id"):
            task_doc_id = server_id
            try:
                remote = docs_api("GET", f"/analysis-docs/{quote(server_id, safe='')}", repo=repo)
            except ServiceError:
                pass  # The local text and last known folder are still readable.
            else:
                design = remote.get("design")
                folder = design.get("folder") if isinstance(design, dict) else None
                if folder != got.get("design", {}).get("folder"):
                    local_library.remember_server_folder(doc_id, server_id, folder)
                if folder:
                    got["design"] = {"folder": folder}
                else:
                    got.pop("design", None)
    tasks = plans.doc_tasks(db, task_doc_id)
    return dict(got, tasks=tasks) if isinstance(got, dict) and tasks is not None else got


# One threshold for "close to the limit", every provider alike: provider_health's.
USAGE_NEAR_PERCENT = provider_health.CLOSING_PERCENT
USAGE_NEAR_RESET_SECONDS = 3600


@_json
def get_usage(db: DB, request: Request) -> Any:
    """Each provider's quota windows, and ``near``: the providers that are
    limited, at USAGE_NEAR_PERCENT of their fullest window or about to reset it within the
    hour, worst first. Providers brindle knows nothing current about come back
    with ``known: false``."""
    # Codex reads its rollouts in quota.get. Claude's desktop reading needs
    # the same throttled CLI refresh the terminal sidebar uses.
    if request.query_params.get("fresh") == "1":
        expected = request.query_params.get("claude_scope")
        if expected is None:
            quota.refresh_claude_usage()
        else:
            quota.refresh_claude_usage(expected_identity=expected)
    claude_scope = quota.claude_usage_scope()
    now = time.time()
    providers, near = [], []
    for name in quota.PROVIDERS:
        q = quota.get(name, identity_key=claude_scope) if name == "claude" and claude_scope else quota.get(name)
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
    return {"providers": providers, "near": near, "claude_usage_scope": claude_scope}


ROUTES = [
    Route("/v1/repos", repos),
    Route("/v1/sessions", list_sessions),
    Route("/v1/snapshot", snapshot),
    Route("/v1/goal", goal),
    Route("/v1/inbox", inbox_),
    Route("/v1/agents/{id}", agent),
    Route("/v1/agents/{id}/transcript", agent_transcript),
    Route("/v1/workspaces/{id:path}/diff", diff),
    # PUT on the same path is the edit (routes_actions); agent callers are refused by the guard.
    Route("/v1/workspaces/{id:path}/file", workspace_file),
    Route("/v1/sessions/{root:path}/pr/readiness", session_readiness),
    Route("/v1/sessions/{root:path}/pr/draft", session_pr_draft),
    Route("/v1/sessions/{root:path}/diff", session_diff),
    # PUT and DELETE on the same path edit (routes_actions).
    Route("/v1/sessions/{root:path}/file", session_file),
    Route("/v1/sessions/{root:path}/tree", session_tree),
    Route("/v1/sessions/{root:path}/view", session_view),
    Route("/v1/decisions", decisions),
    Route("/v1/kb/sources", kb_sources),
    Route("/v1/kb/search", kb_search),
    Route("/v1/kb/docs/{id}", kb_doc),
    Route("/v1/analysis-docs", analysis_docs),
    Route("/v1/analysis-docs/session-summary", analysis_doc_session_summary),
    # Before /analysis-docs/{id}.
    Route("/v1/analysis-docs/topics", analysis_doc_topics),
    Route("/v1/analysis-docs/map", analysis_doc_map),
    Route("/v1/analysis-docs/scopes", analysis_doc_scopes),
    Route("/v1/analysis-docs/results", analysis_doc_results),
    Route("/v1/analysis-docs/policy", analysis_doc_policy),
    Route("/v1/analysis-docs/switches", analysis_doc_switches),
    Route("/v1/org-settings", org_settings),
    Route("/v1/analysis-docs/export-config", analysis_doc_export_config),
    Route("/v1/analysis-docs/destination", analysis_doc_destination),
    Route("/v1/analysis-docs/{id}", analysis_doc),
    Route("/v1/library/folders", library_folders),
    Route("/v1/profiles", list_profiles),
    Route("/v1/profiles/{name}", get_profile),
    Route("/v1/rules", list_rules),
    Route("/v1/config", get_config),
    Route("/v1/doc-format", doc_format),
    Route("/v1/user-settings", get_user_settings),
    Route("/v1/permissions", get_permissions),
    Route("/v1/permissions/asks", get_permission_asks),
    Route("/v1/permissions/asks/plan", get_permission_plan),
    Route("/v1/permissions/report", get_permission_report),
    Route("/v1/routing", get_routing),
    Route("/v1/debug", get_debug),
    Route("/v1/doctor", get_doctor),
    Route("/v1/storage", get_storage),
    # Before the action routes' DELETE /v1/providers/{provider}.
    Route("/v1/providers/installed", providers_installed),
    Route("/v1/providers/usable", providers_usable),
    Route("/v1/providers/{provider}/preflight", provider_preflight),
    Route("/v1/providers/{provider}/connection", provider_connection),
    Route("/v1/cloud/options", cloud_options),
    Route("/v1/account", account),
    Route("/v1/github/connection", github_connection),
    Route("/v1/cost", get_cost),
    Route("/v1/usage", get_usage),
]
