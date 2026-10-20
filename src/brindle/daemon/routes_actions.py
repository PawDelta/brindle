"""The daemon's action routes: what the app can do, each a thin call into the
service layer or the engine function the CLI or MCP tool already uses.

Every route takes a JSON body (or none) and answers JSON. A service
``Conflict`` (a choice the person has to make first) answers 409 with the
choices; any other refusal is a ``ServiceError`` with its own status. A long
action (a merge, a PR, a rewind, milestone checks, a sign-in) answers 202
with a job id at once and runs on its own: ``job.done`` or ``job.failed``
(carrying that id) arrives on the bus when it ends, ``job.progress`` while
it runs. Milestone checks run in the engine's existing detached helper, so
they finish even if the daemon exits first."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import io
import json
import logging
import secrets
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from brindle import agents, autopilot, bus, git, identity_lock, knowledge, pipeline, tmux, workspaces
from brindle import sessions as sessions_mod
from brindle.daemon import terminal
from brindle.db import DB, Agent, Workspace
from brindle.service import sessions as svc
from brindle.service.dto import SessionDTO
from brindle.service.errors import ServiceError, from_exception

log = logging.getLogger(__name__)

Body = dict[str, Any]
Action = Callable[[DB, dict[str, str], Body], Any]


@dataclass(frozen=True)
class Job:
    id: str
    kind: str

    def to_json(self) -> dict[str, Any]:
        return {"job": self.id, "kind": self.kind}


def _spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()


def start_job(kind: str, work: Callable[[DB, Job], Any], **fields) -> Job:
    """Run ``work`` on its own thread (with its own DB connection) and
    publish how it ended. ``fields``: the event's ids (workspace_id, root_id ...)."""
    job = Job(secrets.token_hex(8), kind)

    def run() -> None:
        db = DB()
        try:
            result = work(db, job)
        except Exception as e:  # noqa: BLE001 - every failure is the job's result
            err = from_exception(e)
            if err.code == "internal":
                log.exception("daemon job %s (%s) failed", job.id, kind)
            bus.publish(db, "job.failed", job=job.id, job_kind=kind, error=err.to_json()["error"],
                        **fields)
        else:
            bus.publish(db, "job.done", job=job.id, job_kind=kind, result=result, **fields)
        finally:
            db.conn.close()

    _spawn(run)
    return job


def _run_helper(argv: list[str]) -> dict[str, int]:
    """Run a brindle helper in its own session (so it outlives the daemon)
    and wait for it."""
    proc = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"exit": proc.wait()}


class _Progress(io.TextIOBase):
    """A text stream whose lines go out as ``job.progress`` events."""

    def __init__(self, db: DB, job: Job) -> None:
        self.db, self.job = db, job

    def write(self, s: str) -> int:
        for line in s.splitlines():
            if line.strip():
                bus.publish(self.db, "job.progress", job=self.job.id, text=line)
        return len(s)


# -- plumbing --------------------------------------------------------------------


def _call(fn: Action, params: dict[str, str], body: Body) -> Any:
    db = DB()
    try:
        return fn(db, params, body)
    finally:
        db.conn.close()


def _endpoint(fn: Action):
    async def endpoint(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            body = await request.json() if raw.strip() else {}
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(ServiceError("invalid", "the body must be a JSON object"))
        try:
            got = await asyncio.to_thread(_call, fn, dict(request.path_params), body)
        except Exception as e:  # noqa: BLE001 - mapped to a status below
            err = from_exception(e)
            if err.code == "internal":
                log.exception("daemon action %s failed", request.url.path)
            return _error(err)
        if isinstance(got, svc.Conflict):
            return JSONResponse(got.to_json(), 409)
        if isinstance(got, Job):
            return JSONResponse(got.to_json(), 202)
        return JSONResponse(got)

    endpoint.__name__ = fn.__name__
    return endpoint


def _error(err: ServiceError) -> JSONResponse:
    return JSONResponse(err.to_json(), err.http_status)


def _get(body: Body, key: str, kind: type | tuple[type, ...] = str, default: Any = None,
         required: bool = False) -> Any:
    value = body.get(key, default)
    if value is None:
        if required:
            raise ServiceError("invalid", f"{key} is required")
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ServiceError("invalid", f"{key} has the wrong type")
    return value


def _agent(db: DB, agent_id: str) -> Agent:
    a = db.get_agent(agent_id)
    if a is None:
        raise ServiceError("not_found", f"no agent {agent_id}", 404)
    return a


def _workspace(db: DB, ws_id: str) -> Workspace:
    ws = db.get_workspace(ws_id)
    if ws is None:
        raise ServiceError("not_found", f"no workspace {ws_id}", 404)
    return ws


def _root(db: DB, root_id: str) -> tuple[Agent, Workspace]:
    root = _agent(db, root_id)
    return root, _workspace(db, root.workspace_id)


def _notes(notes: list[svc.Note]) -> list[dict[str, str]]:
    return [dataclasses.asdict(n) for n in notes]


# -- sessions --------------------------------------------------------------------


def start_session(db: DB, params: dict, body: Body) -> Any:
    got = svc.start(db, _get(body, "repo", required=True), provider=_get(body, "provider"),
                    prompt=_get(body, "prompt"), watch=_get(body, "watch", bool, True),
                    autopilot=_get(body, "autopilot", bool), branch=_get(body, "branch"),
                    worktree=_get(body, "worktree"), choice=_get(body, "choice"),
                    session_id=_get(body, "session_id"))
    if isinstance(got, svc.Opened):
        return {"opened": SessionDTO.from_agent(got.root).to_json()}
    if isinstance(got, svc.Started):
        return {**got.to_json(), "notes": _notes(got.notes)}
    return got


def continue_session(db: DB, params: dict, body: Body) -> Any:
    _, ws = _root(db, params["root"])
    got = svc.continue_(db, svc.continue_workspace(db, ws.path), params["root"])
    return got if isinstance(got, svc.Conflict) else {**got.to_json(), "notes": _notes(got.notes)}


def pause_session(db: DB, params: dict, body: Body) -> Any:
    root, _ = _root(db, params["root"])
    return {"paused": [a.id for a in agents.pause(db, root.id)]}


def handover_session(db: DB, params: dict, body: Body) -> Any:
    root, ws = _root(db, params["root"])
    dest = workspaces.checkout_for_target(db, ws.path, _get(body, "to", required=True))
    new = sessions_mod.handover(db, root.id, dest, _get(body, "note"))
    svc.cull_detached(dest.repo_root)
    return SessionDTO.from_agent(new).to_json()


# -- inbox -----------------------------------------------------------------------


def inbox_action(db: DB, params: dict, body: Body) -> Any:
    """``id`` is the item's agent (an org notice's ``notice_id`` for "read")."""
    item, action = params["id"], params["action"]
    if action == "read":
        from brindle.pro import status

        return {"read": status.mark_read([item])}
    a = _agent(db, item)
    if action in ("approve", "reject"):
        # The person decides as the worker's supervisor would.
        text = agents.approve_plan(db, a.parent_id, a.id, _get(body, "feedback", default=""),
                                   action == "approve")
        return {"message": text}
    if action in ("accept", "decline"):
        ap = db.get_autopilot(a.id)
        if ap is None or not autopilot.proposing(ap):
            raise ServiceError("conflict", f"{a.id} has no checks waiting on an answer", 409)
        checks = json.loads(ap.proposal).get("checks") or []
        db.update_autopilot(a.id, proposal=None)
        if action == "decline":
            return {"message": "nothing was written"}
        return {"message": autopilot.add_checks(_workspace(db, a.workspace_id).repo_root, checks,
                                                _get(body, "allow_commands", bool, False))}
    if action == "answer":
        return {"status": svc.send(db, a.id, _get(body, "text", required=True))}
    if action == "review":
        return review_workspace(db, {"id": a.workspace_id}, body)
    if action == "merge":
        return merge_workspace(db, {"id": a.workspace_id}, body)
    raise ServiceError("invalid", f"unknown inbox action {action!r}")


# -- agents ----------------------------------------------------------------------


def message_agent(db: DB, params: dict, body: Body) -> Any:
    return {"status": svc.send(db, _agent(db, params["id"]).id, _get(body, "text", required=True))}


def close_agent(db: DB, params: dict, body: Body) -> Any:
    a, stopped = svc.close(db, _agent(db, params["id"]).id)
    return {"closed": a.id, "stopped": stopped}


def kill_agent(db: DB, params: dict, body: Body) -> Any:
    a = _agent(db, params["id"])
    identity_lock.ensure(a)
    agents.kill(db, a.id)
    return {"killed": a.id}


def peek_agent(db: DB, params: dict, body: Body) -> Any:
    a = _agent(db, params["id"])
    identity_lock.ensure(a)
    if not a.tmux_window:
        raise ServiceError("conflict", f"{a.id} has no terminal", 409)
    server = {"server": a.tmux_server} if a.tmux_server else {}
    return {"screen": tmux.capture(a.tmux_window, lines=_get(body, "lines", int, 40), **server).rstrip()}


def rewind_agent(db: DB, params: dict, body: Body) -> Any:
    from brindle import rewind

    a = _agent(db, params["id"])
    identity_lock.ensure(a)
    to = _get(body, "to", int, required=True)
    profile, note = _get(body, "profile"), _get(body, "note")

    def work(db: DB, job: Job) -> Any:
        fresh = rewind.rewind(db, a.id, to, profile=profile, note=note)
        return {"agent": fresh.id, "workspace_id": fresh.workspace_id}

    return start_job("rewind", work, agent_id=a.id)


# -- workspaces ------------------------------------------------------------------


def review_workspace(db: DB, params: dict, body: Body) -> Any:
    from brindle.config import load_repo_config

    ws = _workspace(db, params["id"])
    if ws.kind != "worktree":
        raise ServiceError("invalid", "only a worker's workspace (its own branch and worktree) can be reviewed")
    # With checks, a detached helper runs them first; review.submitted ends it.
    reviewer = agents.start_review(db, None, ws, _get(body, "profile"), _get(body, "focus"),
                                   load_repo_config(ws.repo_root))
    return {"reviewer": reviewer.id if reviewer else None, "checks_running": reviewer is None}


def merge_workspace(db: DB, params: dict, body: Body) -> Any:
    ws = _workspace(db, params["id"])
    identity_lock.ensure_workspace(db, ws)
    squash = _get(body, "squash", bool, False)

    def work(db: DB, job: Job) -> Any:
        text = pipeline.merge(db, None, ws, squash=squash)
        return {"merged": text.startswith(("Merged", "Already merged")), "message": text}

    return start_job("merge", work, workspace_id=ws.id, repo_root=ws.repo_root)


def sync_workspace(db: DB, params: dict, body: Body) -> Any:
    ws = _workspace(db, params["id"])
    if git.dirty_files(ws.path):
        raise ServiceError("conflict", "uncommitted changes; commit or stash first", 409)
    strategy = "merge" if _get(body, "merge", bool, False) else "rebase"

    def work(db: DB, job: Job) -> Any:
        return {"ref": git.sync(ws.path, workspaces.require_base(ws), strategy)}

    return start_job("sync", work, workspace_id=ws.id, repo_root=ws.repo_root)


def pr_workspace(db: DB, params: dict, body: Body) -> Any:
    ws = _workspace(db, params["id"])
    title, draft = _get(body, "title"), _get(body, "draft", bool, False)

    def work(db: DB, job: Job) -> Any:
        url = workspaces.pull_request(ws, title, draft)
        # The CLI's offer to turn on GitHub's delete-on-merge: the app asks.
        return {"url": url, "keeps_merged_branches": workspaces.keeps_merged_branches(ws.repo_root)}

    return start_job("pr", work, workspace_id=ws.id, repo_root=ws.repo_root)


def remove_workspace(db: DB, params: dict, body: Body) -> Any:
    ws = _workspace(db, params["id"])
    force, delete_branch = _get(body, "force", bool, False), _get(body, "delete_branch", bool)
    note = svc.unpushed_note(ws, delete_branch)
    removed = svc.remove(db, ws, force=force, delete_branch=delete_branch)
    teardown = removed.teardown
    return {"removed": ws.id, "branch_note": removed.branch_note, "note": note,
            "teardown_failed": teardown.log if teardown and not teardown.ok else None}


# -- goal ------------------------------------------------------------------------


def _autopilot_root(db: DB, body: Body) -> tuple[Agent, Workspace]:
    root, ws = _root(db, _get(body, "root", required=True))
    if db.get_autopilot(root.id) is None:
        raise ServiceError("conflict", "autopilot isn't set up for this session", 409)
    return root, ws


def set_goal(db: DB, params: dict, body: Body) -> Any:
    root, _ = _autopilot_root(db, body)
    items = autopilot.milestone_items(db, root.id, _get(body, "milestones", list, required=True))
    autopilot.set_goal(db, root.id, _get(body, "goal", required=True), items, _get(body, "detail"))
    return {"progress": autopilot.progress(db, root.id)}


def check_goal(db: DB, params: dict, body: Body) -> Any:
    root, ws = _autopilot_root(db, body)
    started: list[Job] = []

    def detach(argv: list[str]) -> None:
        started.append(start_job("goal.check", lambda db, job: _run_helper(argv), root_id=root.id))

    # A check already running is left alone: no second job, its result still arrives.
    text = autopilot.check_in_background(db, root.id, ws, _get(body, "position", int), detach=detach)
    return started[0] if started else {"running": True, "message": text}


def enable_goal(db: DB, params: dict, body: Body) -> Any:
    root, _ = _root(db, _get(body, "root", required=True))
    on = _get(body, "enabled", bool, required=True)
    autopilot.set_enabled(db, root.id, on)
    if agents.is_alive(root):
        note = ("Autopilot is on again: keep driving toward the goal (get_progress shows where it stands)."
                if on else "Autopilot is now off: stop driving and wait for the user's instructions.")
        try:
            agents.send_message(db, root.id, f"[brindle autopilot] {note}")
        except agents.AgentError:
            pass
    return {"enabled": on}


# -- decisions, coordinator, config, permissions -----------------------------------


def record_decision(db: DB, params: dict, body: Body) -> Any:
    repo = _get(body, "repo", required=True)
    made, existing = knowledge.record_decision(
        db, knowledge.repo_key(repo), _get(body, "topic", required=True),
        _get(body, "decision", required=True), _get(body, "rationale", default=""),
        _get(body, "paths", list), supersedes=_get(body, "supersedes"))
    if made is None:
        raise ServiceError("conflict", "an accepted decision on this topic already covers these "
                           f"paths ({existing['id'][:8]}); pass supersedes to replace it", 409)
    return {"decision": made, "told": knowledge.notify_decision(db, repo, made, None)}


def coordinator_undo(db: DB, params: dict, body: Body) -> Any:
    from brindle import coordinator

    key = knowledge.repo_key(_get(body, "repo", required=True))
    return {"message": coordinator.undo(db, key, _get(body, "event", required=True))}


def _fits(value: Any, default: Any) -> bool:
    if value is None or default is None:
        return True
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(default, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def patch_config(db: DB, params: dict, body: Body) -> Any:
    """``values`` into ~/.brindle/config.json (scope "user") or the repo's
    .brindle/config.local.json (scope "repo"). Only the repo config's own
    keys, each with a value of its default's type (or null)."""
    from brindle import config

    scope = _get(body, "scope", default="user")
    if scope not in ("user", "repo"):
        raise ServiceError("invalid", "scope must be user or repo")
    repo = _get(body, "repo", required=scope == "repo")
    values = _get(body, "values", dict, required=True)
    defaults = config.RepoConfig()
    known = {f.name for f in dataclasses.fields(config.RepoConfig)}
    if unknown := sorted(set(values) - known):
        raise ServiceError("invalid", f"unknown setting(s): {', '.join(unknown)}")
    for key, value in values.items():
        if not _fits(value, getattr(defaults, key)):
            raise ServiceError("invalid", f"{key} must be a {type(getattr(defaults, key)).__name__}")
    for key, value in values.items():
        if scope == "user":
            path = config.set_user(key, value)
        else:
            path = config.set_local(repo, key, value)
    return {"updated": sorted(values), "path": str(path)}


def put_routing(db: DB, params: dict, body: Body) -> Any:
    """``tiers`` (tier -> profiles to try, in order), ``reviewer`` and
    ``exclude_providers`` into ~/.brindle/config.json (scope "user"), the
    repo's .brindle/config.local.json ("local") or its shared
    .brindle/config.json ("repo"). Tiers not given keep their value there.
    A profile the org's policy doesn't allow, or any change while the policy
    applies but can't be read, is refused (403)."""
    from brindle import config, profiles, providers
    from brindle.service.routing import RoutingPolicy

    repo = _get(body, "repo", required=True)
    scope = _get(body, "scope", required=True)
    setters = {"user": config.set_user, "local": functools.partial(config.set_local, repo),
               "repo": functools.partial(config.set_repo, repo)}
    if scope not in setters:
        raise ServiceError("invalid", "scope must be user, local or repo")
    tiers, reviewer = _get(body, "tiers", dict), _get(body, "reviewer")
    excluded = _get(body, "exclude_providers", list)
    by_name = {p.name: p for p in profiles.list_profiles(repo)}
    known = set(by_name)
    named = [reviewer] if reviewer is not None else []
    for tier, names in (tiers or {}).items():
        if config.normalize_weight(tier) not in config.WEIGHTS:
            raise ServiceError("invalid", f"tier must be one of {', '.join(config.WEIGHTS)}, not {tier!r}")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ServiceError("invalid", f"{tier} must be a non-empty list of profile names")
        named.extend(names)
    if unknown := sorted(set(named) - known):
        raise ServiceError("invalid", f"no such profile(s): {', '.join(unknown)}")
    if excluded is not None and (bad := sorted(set(map(str, excluded)) - set(providers.PROVIDERS))):
        raise ServiceError("invalid", f"no such provider(s): {', '.join(bad)}")
    policy = RoutingPolicy.load(repo)
    if policy.applies and policy.access.error:
        raise ServiceError("forbidden", f"routing can't be changed now: {policy.access.error}", 403)
    for name in dict.fromkeys(named):
        if why := policy.refusal(by_name[name]):
            raise ServiceError("forbidden", why, 403)
    write, updated = setters[scope], []
    if tiers:
        routing = config._read_json(config.scope_paths(repo)[scope]).get("routing")
        routing = dict(routing) if isinstance(routing, dict) else {}
        for tier, names in tiers.items():
            canon = config.normalize_weight(tier)
            # An alias ("reasoning") left in place would still override the canonical key.
            routing = {k: v for k, v in routing.items() if config.normalize_weight(k) != canon}
            routing[canon] = names
            if scope == "user":
                config.forget_user(f"routing_{canon}")   # the picker's pick would win
        path = write("routing", routing)
        updated.append("routing")
    if reviewer is not None:
        path = write("reviewer", reviewer)
        updated.append("reviewer")
    if excluded is not None:
        path = write("exclude_providers", excluded)
        updated.append("exclude_providers")
    if not updated:
        raise ServiceError("invalid", "give tiers, reviewer or exclude_providers")
    return {"updated": updated, "path": str(path)}


def change_permissions(db: DB, params: dict, body: Body) -> Any:
    """``op``: allow or deny (``kind``, ``match``, ``match_type``), forget or
    accept (``id``), or reset."""
    from brindle import antigravity
    from brindle import permissions as perms

    op = _get(body, "op", required=True)
    if op in ("allow", "deny"):
        rule = perms.add_rule(_get(body, "kind", required=True), _get(body, "match", required=True),
                              op, _get(body, "match_type", default="exact"))
        reply: dict[str, Any] = {"rule": rule.id, "describe": f"{op} {rule.describe()}"}
    elif op in ("forget", "accept"):
        rule_id = _get(body, "id", required=True)
        got = perms.forget(rule_id) if op == "forget" else perms.accept(rule_id)
        if got is None:
            raise ServiceError("not_found", f"no rule or suggestion {rule_id} of yours", 404)
        reply = {"rule": rule_id, "describe": got if isinstance(got, str) else f"allow {got.describe()}"}
    elif op == "reset":
        perms.reset()
        reply = {"reset": True}
    else:
        raise ServiceError("invalid", "op must be allow, deny, forget, accept or reset")
    antigravity.resync_permissions()
    return reply


# -- account ---------------------------------------------------------------------


def _pro_account(out=None):
    from brindle.pro.account import ProAccount

    return ProAccount(out=out or io.StringIO())


def _said(account) -> list[str]:
    return [line for line in account.out.getvalue().splitlines() if line.strip()]


def account_login(db: DB, params: dict, body: Body) -> Any:
    device = _get(body, "device", bool, False)

    def work(db: DB, job: Job) -> Any:
        def open_url(url: str) -> bool:
            # The app opens it in the system browser; the daemon waits for the callback.
            bus.publish(db, "job.progress", job=job.id, url=url)
            return True

        _pro_account(_Progress(db, job)).cmd_login(None, device=device, open_url=open_url)
        return {"signed_in": True}

    return start_job("account.login", work)


def account_logout(db: DB, params: dict, body: Body) -> Any:
    account = _pro_account()
    account.cmd_logout(None)
    return {"messages": _said(account)}


def account_switch_org(db: DB, params: dict, body: Body) -> Any:
    account = _pro_account()
    account.cmd_org_use(None, _get(body, "org", required=True))
    return {"messages": _said(account)}


# -- terminals: the real command in a terminal the app shows -------------------------
# Each answers {"terminal": id, "started": bool} for WS /v1/terminal/{id}; while
# the same action is still running, a second press gets its terminal (started
# False). The person signs in or types the secret there; the daemon never
# automates a sign-in. The app may instead send a key the person typed to
# PUT /v1/keys/{name} (write-only), or a code they pasted to a terminal's input.


def provider_login(db: DB, params: dict, body: Body) -> Any:
    """The provider CLI's own sign-in (``claude auth login``, ``codex login``, ...).
    ``method``: how to sign in, where the CLI has a flag for it (see
    picker.LOGIN_METHODS); its own default otherwise."""
    from brindle import picker
    from brindle.pro.team_policy import CLI_NAMES

    provider = params["provider"]
    if provider not in CLI_NAMES:
        raise ServiceError("invalid", f"provider must be one of {', '.join(CLI_NAMES)}")
    method = _get(body, "method")
    methods = picker.LOGIN_METHODS.get(provider, {})
    if method is not None and method not in methods:
        raise ServiceError("invalid", f"{provider} sign-in methods: {', '.join(methods) or 'none'}")
    browser = picker._wsl_browser_env()      # WSL with no Linux browser: open it in Windows'
    env = {"BROWSER": browser["BROWSER"]} if browser else {}
    return terminal.open_action(f"login {provider}", picker._login_command(provider, method), env=env)


def terminal_input(db: DB, params: dict, body: Body) -> Any:
    """Type ``text``, one line the person typed or pasted in the app (a
    sign-in's code), into the action terminal ``id``, then Enter unless
    ``submit`` is false. User-typed input only: nothing brindle or an agent
    makes up goes in here, and agent callers are refused like everywhere."""
    text = _get(body, "text", required=True)
    if not text or len(text) > 4096 or any(ord(c) < 0x20 or ord(c) == 0x7f for c in text):
        raise ServiceError("invalid", "text must be one line of printable characters")
    if not terminal.send_input(params["id"], text, _get(body, "submit", bool, True)):
        raise ServiceError("not_found", f"no running terminal {params['id']}", 404)
    return {"terminal": params["id"], "sent": True}


def put_key(db: DB, params: dict, body: Body) -> Any:
    """Store ``value`` as key ``name``, as ``brindle keys set NAME`` does.
    Write-only: the reply carries the last four characters, never the value,
    and neither the value nor the body is logged or put on the bus."""
    from brindle import keys_cmds, keystore

    name, repo = params["name"], _get(body, "repo")
    value = body.pop("value", None)
    if not isinstance(value, str) or not value.strip():
        raise ServiceError("invalid", "value must be a non-empty string")
    try:
        keys_cmds.store(name, value, repo)
    except keystore.KeyNameError as e:
        raise ServiceError("invalid", str(e)) from None
    except keys_cmds.PolicyDenied as e:
        raise ServiceError("forbidden", str(e), 403) from None
    except keystore.CredentialError as e:
        raise ServiceError("not_stored", str(e), 422) from None
    except Exception:  # noqa: BLE001 - no traceback: its frames held the value
        raise ServiceError("internal", "couldn't store the key", 500) from None
    return {"name": name, "last4": value.strip()[-4:], "set": True}


def set_key(db: DB, params: dict, body: Body) -> Any:
    """``brindle keys set NAME``: the value goes into the terminal's hidden prompt."""
    from brindle import keystore, providers

    name, repo = params["name"], _get(body, "repo")
    try:
        keystore.check_name(name, repo)
    except keystore.CredentialError as e:
        raise ServiceError("invalid", str(e)) from e
    return terminal.open_action(f"key {name}", [*providers.brindle_invocation(), "keys", "set", name], cwd=repo)


def upgrade_engine(db: DB, params: dict, body: Body) -> Any:
    return terminal.open_action("upgrade", ["uv", "tool", "upgrade", "brindle"])


def _post(path: str, fn: Action) -> Route:
    return Route(f"/v1{path}", _endpoint(fn), methods=["POST"])


ROUTES = [
    _post("/sessions", start_session),
    _post("/sessions/{root}/continue", continue_session),
    _post("/sessions/{root}/pause", pause_session),
    _post("/sessions/{root}/handover", handover_session),
    _post("/inbox/{id}/{action}", inbox_action),
    _post("/agents/{id}/message", message_agent),
    _post("/agents/{id}/close", close_agent),
    _post("/agents/{id}/kill", kill_agent),
    _post("/agents/{id}/peek", peek_agent),
    _post("/agents/{id}/rewind", rewind_agent),
    # Workspace ids are ``slug/name``, so the id spans path segments.
    _post("/workspaces/{id:path}/review", review_workspace),
    _post("/workspaces/{id:path}/merge", merge_workspace),
    _post("/workspaces/{id:path}/sync", sync_workspace),
    _post("/workspaces/{id:path}/pr", pr_workspace),
    _post("/workspaces/{id:path}/remove", remove_workspace),
    _post("/goal", set_goal),
    _post("/goal/check", check_goal),
    _post("/goal/enabled", enable_goal),
    _post("/decisions", record_decision),
    _post("/coordinator/undo", coordinator_undo),
    Route("/v1/config", _endpoint(patch_config), methods=["PATCH"]),
    # Agents never get here: the guard refuses them (see brindle.daemon.auth).
    Route("/v1/routing", _endpoint(put_routing), methods=["PUT"]),
    _post("/permissions", change_permissions),
    _post("/account/login", account_login),
    _post("/account/logout", account_logout),
    _post("/account/switch-org", account_switch_org),
    # Like every route, refused to agent callers by the guard.
    _post("/providers/{provider}/login", provider_login),
    _post("/keys/{name}/set", set_key),
    Route("/v1/keys/{name}", _endpoint(put_key), methods=["PUT"]),
    _post("/terminals/{id}/input", terminal_input),
    _post("/engine/upgrade", upgrade_engine),
]
