"""The daemon's action routes: what the app can do, each a thin call into the
service layer or the engine function the CLI or MCP tool already uses.

Every route takes a JSON body (or none) and answers JSON. A service
``Conflict`` (a choice the person has to make first) answers 409 with the
choices, and a launch its provider's CLI would stop on answers 409 with
``preflight`` items (brindle.preflight); any other refusal is a
``ServiceError`` with its own status. A long action (a merge, a PR, a
rewind, milestone checks, a sign-in, an engine upgrade) answers 202
with a job id at once and runs on its own: ``job.done`` or ``job.failed``
(carrying that id) arrives on the bus when it ends, ``job.progress`` while
it runs. Milestone checks run in the engine's existing detached helper, so
they finish even if the daemon exits first."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import io
import json
import logging
import os
import secrets
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from brindle import agents, autopilot, bus, gates, git, identity_lock, knowledge, pipeline, preflight, scratch, tmux, workspaces
from brindle import sessions as sessions_mod
from brindle.daemon import session_review as review
from brindle.daemon import terminal
from brindle.db import DB, Agent, Workspace
from brindle.service import inbox
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


def start_job(kind: str, work: Callable[[DB, Job], Any], job: Job | None = None, **fields) -> Job:
    """Run ``work`` on its own thread (with its own DB connection) and
    publish how it ended. ``fields``: the event's ids (workspace_id, root_id ...)."""
    job = job or Job(secrets.token_hex(8), kind)

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
            # The query (?path= on a file route) is the params' too; the path's own win.
            params = {**request.query_params, **request.path_params}
            got = await asyncio.to_thread(_call, fn, params, body)
        except agents.PreflightBlocked as e:      # a launch on the way: as if asked first
            return JSONResponse(preflight.Blocked(e.items).to_json(), 409)
        except Exception as e:  # noqa: BLE001 - mapped to a status below
            err = from_exception(e)
            if err.code == "internal":
                log.exception("daemon action %s failed", request.url.path)
            return _error(err)
        if isinstance(got, (svc.Conflict, preflight.Blocked)):
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


def _blocked(launches: list[tuple[Agent, Workspace]], keys_only: bool = False) -> preflight.Blocked | None:
    """What would stop any of these launches (agent, where) before its first
    turn, each item once, judged with the identity each runs as."""
    items: dict[str, preflight.Item] = {}
    for a, ws in launches:
        for item in agents.launch_blockers(a, ws, keys_only=keys_only):
            items.setdefault(item.id, item)
    return preflight.Blocked(list(items.values())) if items else None


def session_blockers(db: DB, repo: str, provider: str | None, *, read_only: bool = False) -> preflight.Blocked | None:
    """What would stop a new supervisor chat in ``repo`` (on ``provider``, else
    its profile's) before its first turn. ``read_only`` (a GET): the checkout
    isn't registered, and the sign-in check sees only credentials (see
    agents.launch_blockers)."""
    from brindle.profiles import load_profile

    if read_only:
        top = git.toplevel(repo)
        ws = db.workspace_by_path(top) or Workspace("preflight", git.main_repo_root(repo), "main", "main",
                                                    "HEAD", None, top, None, "", 0.0)
    else:
        ws = workspaces.adopt_root(db, repo)
    provider = (svc.supervisor_provider(ws.repo_root, provider)
                or load_profile(agents.SUPERVISOR, ws.repo_root).provider)
    chat = Agent("", ws.id, agents.SUPERVISOR, provider, None, "interactive", "starting", "", None, 0.0)
    return _blocked([(chat, ws)], keys_only=read_only)


def start_session(db: DB, params: dict, body: Body) -> Any:
    """A 409 ``{"preflight": [...]}`` instead of a chat opening on its CLI's
    sign-in or first-run screen (see brindle.preflight). Only opening a
    session that is running now launches nothing, so only that isn't checked."""
    repo, choice, session_id = _get(body, "repo", required=True), _get(body, "choice"), _get(body, "session_id")
    name = _session_name(_get(body, "name"))     # checked with the rest, before anything launches
    opening = (choice == "o" and not _get(body, "branch") and not _get(body, "worktree")
               and any(s.root.id == session_id
                       for s in sessions_mod.live(db, workspaces.adopt_root(db, repo).repo_root)))
    if not opening:
        blocked = session_blockers(db, repo, _get(body, "provider"))
        if blocked:
            return blocked
    got = svc.start(db, repo, provider=_get(body, "provider"),
                    prompt=_get(body, "prompt"), watch=_get(body, "watch", bool, True),
                    autopilot=_get(body, "autopilot", bool), branch=_get(body, "branch"),
                    worktree=_get(body, "worktree"), choice=_get(body, "choice"),
                    session_id=_get(body, "session_id"))
    if isinstance(got, svc.Opened):
        return {"opened": SessionDTO.from_agent(got.root).to_json()}
    if isinstance(got, svc.Started):
        got.agent.name = name
        if name:
            db.update_agent(got.agent.id, name=name)
        return {**got.to_json(), "notes": _notes(got.notes)}
    return got


def continue_session(db: DB, params: dict, body: Body) -> Any:
    _, ws = _root(db, params["root"])
    blocked = _blocked([(a, db.get_workspace(a.workspace_id) or ws)
                        for a in agents.tree(db, params["root"]) if a.status != "done"])
    if blocked:
        return blocked
    got = svc.continue_(db, svc.continue_workspace(db, ws.path), params["root"])
    return got if isinstance(got, svc.Conflict) else {**got.to_json(), "notes": _notes(got.notes)}


def pause_session(db: DB, params: dict, body: Body) -> Any:
    root, _ = _root(db, params["root"])
    return {"paused": [a.id for a in agents.pause(db, root.id)]}


def close_session(db: DB, params: dict, body: Body) -> Any:
    root, _ = _root(db, params["root"])
    closed = svc.close_session(db, root.id)
    return {"closed": [a.id for a, _ in closed], "stopped": [a.id for a, was in closed if was]}


def _session_name(raw: str | None) -> str | None:
    """A session's name as kept: whitespace collapsed, at most 80 characters, None when empty."""
    return " ".join((raw or "").split())[:80] or None


def rename_session(db: DB, params: dict, body: Body) -> Any:
    """``name``: the session's new name; empty clears it (listed by its goal again)."""
    root, _ = _root(db, params["root"])
    name = _session_name(_get(body, "name"))
    db.update_agent(root.id, name=name)
    return {"id": root.id, "name": name}


def handover_session(db: DB, params: dict, body: Body) -> Any:
    root, ws = _root(db, params["root"])
    dest = workspaces.checkout_for_target(db, ws.path, _get(body, "to", required=True))
    new = sessions_mod.handover(db, root.id, dest, _get(body, "note"))
    svc.cull_detached(dest.repo_root)
    return SessionDTO.from_agent(new).to_json()


def transfer_session(db: DB, params: dict, body: Body) -> Any:
    root, ws = _root(db, params["root"])
    identity_lock.ensure(root)
    target, branch = _get(body, "target_path", required=True), _get(body, "branch")
    if not scratch.is_scratch(ws.path):       # refused at once, not as a failed job
        raise ServiceError("invalid", "This isn't a scratch session; the work is already in a real repository.")

    job = Job(secrets.token_hex(8), "session.transfer")
    with _TRANSFER_LOCK:
        running = _TRANSFERRING.get(ws.id)
        if running:
            if running[1] != (target, branch):
                raise ServiceError("conflict", "this session is already being transferred elsewhere", 409)
            return running[0]                 # pressed again: the one already running
        _TRANSFERRING[ws.id] = (job, (target, branch))

    def work(db: DB, job: Job) -> Any:
        try:
            t = svc.transfer_to_repo(db, ws, target, branch)
        finally:
            with _TRANSFER_LOCK:
                _TRANSFERRING.pop(ws.id, None)
        return {"workspace_id": t.workspace.id, "branch": t.workspace.branch,
                "repo_root": t.workspace.repo_root, "commits": t.commits}

    try:
        return start_job("session.transfer", work, job=job, root_id=root.id, repo_root=ws.repo_root)
    except BaseException:
        with _TRANSFER_LOCK:
            _TRANSFERRING.pop(ws.id, None)
        raise


_TRANSFER_LOCK = threading.Lock()
_TRANSFERRING: dict[str, tuple[Job, tuple[str, str | None]]] = {}     # scratch workspace -> its transfer


# -- inbox -----------------------------------------------------------------------


def inbox_action(db: DB, params: dict, body: Body) -> Any:
    """``id`` is the item's agent (an org notice's ``notice_id`` for "read")."""
    item, action = params["id"], params["action"]
    if action == "read":
        from brindle.pro import status

        return {"read": status.mark_read([item])}
    a = _agent(db, item)
    if action in ("approve", "reject"):
        from brindle import plan_policy

        member = plan_policy.current_member(_workspace(db, a.workspace_id).repo_root)
        if not member:
            raise ServiceError("forbidden", "Sign in to approve or revise a plan in the Inbox", 403)
        text = agents.approve_plan(db, None, a.id, _get(body, "feedback", default=""),
                                   action == "approve", human_member=member)
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
    if action == "permission":
        # The person's answer to a's permission request (agent callers never get here).
        return inbox.answer_permission(db, a.id, _get(body, "request"), _get(body, "digest"),
                                       _get(body, "choice", required=True))
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
    ws = _workspace(db, a.workspace_id)
    fresh = a
    if profile:
        from brindle.profiles import load_profile

        fresh = dataclasses.replace(a, profile=profile, provider=load_profile(profile, ws.repo_root).provider)
    blocked = _blocked([(fresh, ws)])
    if blocked:
        return blocked

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
                                   load_repo_config(ws.repo_root, branch=ws.base_branch))
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


# An agent mid-turn (or stopped at a prompt mid-turn) may be writing the same file.
BUSY = ("starting", "processing", "waiting")
MAX_EDIT_BYTES = 1 << 20       # a file the review sheet can edit inline


def _edit_dir(ws: Workspace, rel: str, made: list[str] | None = None) -> tuple[str, int, str]:
    """``rel`` normalized, an open descriptor of its directory, and the
    file's own name in it: a relative path with no ``..`` and nothing in
    .git, its directories opened one at a time from the worktree without
    following a symlink, so nothing swapped in later can redirect a write.
    With ``made``, missing directories are created and their paths appended."""
    parts = PurePosixPath(rel).parts
    if (not parts or PurePosixPath(rel).is_absolute() or "\\" in rel
            or any(p == ".." or p.lower() == ".git" for p in parts)):
        raise ServiceError("invalid", f"{rel!r} isn't a path inside the worktree")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(ws.path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(parts[:-1]):
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if made is None:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                made.append("/".join(parts[:i + 1]))
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as e:
        os.close(fd)
        if isinstance(e, FileNotFoundError):
            raise ServiceError("not_found", f"no file {rel} in {ws.id}", 404) from None
        raise ServiceError("invalid", f"{rel!r} isn't a file inside the worktree") from None
    return "/".join(parts), fd, parts[-1]


def _unmake(ws: Workspace, made: list[str]) -> None:
    """Remove the directories :func:`_edit_dir` made, deepest first, each
    through its parent opened as it opens them (so never outside)."""
    for path in reversed(made):
        with contextlib.suppress(OSError, ServiceError):
            _, fd, leaf = _edit_dir(ws, path)
            try:
                os.rmdir(leaf, dir_fd=fd)
            finally:
                os.close(fd)


def read_edit_target(ws: Workspace, rel: str) -> tuple[str, int, str, bytes, int]:
    """``rel`` checked as :func:`_edit_dir` does and read: its normalized
    name, an open descriptor of its directory (the caller closes it), the
    file's own name there, its bytes and its permission bits. Only a regular
    text file of at most MAX_EDIT_BYTES is editable."""
    name, dir_fd, leaf = _edit_dir(ws, rel)
    try:
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except FileNotFoundError:
            raise ServiceError("not_found", f"no file {rel} in {ws.id}", 404) from None
        except OSError:     # a symlink (ELOOP)
            raise ServiceError("invalid", f"{rel!r} isn't a file inside the worktree") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ServiceError("invalid", f"{rel!r} isn't a file inside the worktree")
            data = fh.read(MAX_EDIT_BYTES + 1)
        if len(data) > MAX_EDIT_BYTES:
            raise ServiceError("too_large", f"{rel} is over {MAX_EDIT_BYTES // 1024} KB; edit it in an editor", 413)
        try:
            text = data.decode()
        except UnicodeDecodeError:
            text = "\0"
        if "\0" in text:
            raise ServiceError("binary", f"{rel} isn't UTF-8 text; edit it in an editor", 415)
    except BaseException:
        os.close(dir_fd)
        raise
    return name, dir_fd, leaf, data, stat.S_IMODE(st.st_mode)


def _replace(dir_fd: int, leaf: str, data: bytes, mode: int) -> None:
    """Put ``data`` at ``leaf`` by writing a temp file beside it and renaming
    it over, so the file there stays whole until the rename."""
    tmp = f".{leaf}.brindle-edit-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _content(body: Body) -> bytes:
    data = _get(body, "content", required=True).encode()
    if len(data) > MAX_EDIT_BYTES:
        raise ServiceError("too_large", f"an edit is at most {MAX_EDIT_BYTES // 1024} KB", 413)
    if b"\0" in data:
        raise ServiceError("binary", "an edit is UTF-8 text; edit binary files in an editor", 415)
    return data


def commit_edit(ws: Workspace, rel: str, data: bytes | None, base: str | None,
                message: str) -> tuple[str, str | None]:
    """``data`` becomes ``rel`` in ``ws``'s checkout and is committed alone,
    as ``<message>: <path>``; None deletes it. ``base`` is the blob sha the
    app showed (abbreviated is fine): if the file has changed since, the edit
    is refused (409). None creates the file (and its directories), which
    must not exist yet. git makes the commit (its hooks and locks included);
    if it refuses, the file and its index entries go back exactly as they
    were. The caller holds the checkout's lock. Returns the normalized path
    and the commit (None when nothing needed committing)."""
    if git.current_branch(ws.path) != ws.branch:
        raise ServiceError("conflict", f"{ws.id} isn't on its branch {ws.branch}", 409)
    made: list[str] = []
    before: bytes | None = None
    try:
        if base is None:
            name, dir_fd, leaf = _edit_dir(ws, rel, made)
            mode = 0o644
            try:
                os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                os.close(dir_fd)
                raise ServiceError("stale", f"{rel} already exists; reload it", 409)
        else:
            name, dir_fd, leaf, before, mode = read_edit_target(ws, rel)
    except BaseException:
        _unmake(ws, made)
        raise
    try:
        if base is not None and (len(base) < 7 or not git.hash_blob(ws.path, before, name).startswith(base)):
            raise ServiceError("stale", f"{rel} has changed since the diff was shown; reload it", 409)
        if data is None:
            os.unlink(leaf, dir_fd=dir_fd)
        else:
            _replace(dir_fd, leaf, data, mode)
        commit = None
        # Literal: a name like ':(glob)*' is that file, not a pattern matching other work.
        lit = ["--literal-pathspecs"]
        # This path's index entries as they are (none when untracked or its deletion is staged).
        listed = git.out([*lit, "ls-files", "-s", "-z", "--", name], ws.path).split("\0")
        entries = [e for e in listed if e.partition("\t")[2] == name]
        tracked = bool(entries)
        try:
            # git reads the path itself: a directory swapped for a symlink meanwhile would commit what it points at.
            if os.path.realpath(os.path.join(ws.path, name)) != os.path.join(os.path.realpath(ws.path), name):
                raise ServiceError("invalid", f"{rel!r} isn't a file inside the worktree")
            # An untracked file deleted changes nothing on the branch.
            if tracked or data is not None:
                if not tracked:
                    git.run([*lit, "add", "--", name], ws.path)     # commit --only needs git to know a new file
                if git.ok([*lit, "diff", "--quiet", "HEAD", "--", name], ws.path):
                    # Back to HEAD's content: nothing to commit, but the index drops what was staged for it.
                    git.run([*lit, "add", "--", name], ws.path)
                else:
                    # --only commits just this path: what is staged elsewhere is left alone,
                    # and a refused commit leaves the index as it was.
                    git.run([*lit, "commit", "-q", "--only", "-m", f"{message}: {name}", "--", name], ws.path)
                    commit = git.out(["rev-parse", "HEAD"], ws.path)
        except (git.GitError, ServiceError):
            # Refused (a hook, say): the index entry and the file exactly as they were.
            try:
                git.run(["update-index", "--force-remove", "--", name], ws.path)
                if entries:
                    git.run(["update-index", "-z", "--index-info"], ws.path, input="\0".join(entries) + "\0")
            finally:
                if before is None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(leaf, dir_fd=dir_fd)
                else:
                    _replace(dir_fd, leaf, before, mode)
            raise
    except BaseException:
        os.close(dir_fd)
        _unmake(ws, made)
        raise
    os.close(dir_fd)
    return name, commit


def edit_file(db: DB, params: dict, body: Body) -> Any:
    """The person's edit from the review sheet: ``content`` becomes ``path``
    in the worker's worktree, committed on its branch. ``base`` is the blob
    sha the app showed (the diff's ``index`` line, abbreviated is fine); if
    the file has changed since, the edit is refused (409), as it is while
    any agent in the workspace is busy. git makes the commit (its hooks and
    locks included); if it refuses, the file goes back as it was."""
    ws = _workspace(db, params["id"])
    if ws.kind != "worktree":
        raise ServiceError("invalid", "only a worker's workspace (its own branch and worktree) can be edited")
    identity_lock.ensure_workspace(db, ws)
    rel, data = _get(body, "path", required=True), _content(body)
    base = _get(body, "base", required=True).strip().lower()
    with git.checkout_lock(ws.path, timeout=10):
        if busy := [a.id for a in db.list_agents(ws.id) if a.status in BUSY]:
            raise ServiceError("busy", f"{', '.join(busy)} is working in {ws.id}; edit once it has stopped", 409)
        name, commit = commit_edit(ws, rel, data, base, "edit from review")
    blob = git.hash_blob(ws.path, data, name)
    bus.publish_ws(db, "workspace.edited", ws, path=name, commit=commit)
    return {"workspace_id": ws.id, "path": name, "commit": commit, "blob": blob,
            "stat": git.diff(ws.path, workspaces.require_base(ws), stat=True)}


# -- a session's review ------------------------------------------------------------


def _edit_session(db: DB, params: dict, body: Body, data: bytes | None) -> Any:
    """The person's edit of any file on the session branch, from the session
    review: committed there as ``review edits: <path>``, with the workspace
    edit's checks. Refused while the supervisor is mid-turn or a merge into
    the session branch is under way."""
    root, ws = review.session(db, params["root"])
    identity_lock.ensure(root)
    rel = params.get("path") or _get(body, "path", required=True)
    base = _get(body, "base", required=data is None)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(pipeline.base_lock(ws.repo_root, ws.branch, wait=False))
            held.enter_context(git.checkout_lock(ws.path, timeout=1))
        except (BlockingIOError, git.GitError):
            raise ServiceError("busy", f"a merge into {ws.branch} is under way; edit once it's done", 409) from None
        if (root := _agent(db, root.id)).status in BUSY:
            raise ServiceError("busy", f"the supervisor {root.id} is working; edit once it has stopped", 409)
        before = git.out(["rev-parse", "HEAD"], ws.path)
        name, commit = commit_edit(ws, rel, data, base.strip().lower() if base else None, review.EDIT_MESSAGE)
        # Record only the one commit this edit made, directly on top of what was there: a hook that
        # added commits of its own leaves them unrecorded, so they still need an audit.
        # and touching only the edited path (a formatter hook may change its content, never other files).
        if commit and git.out(["rev-parse", f"{commit}^"], ws.path) == before and git.run(
                ["diff-tree", "--no-commit-id", "-r", "--name-only", "-z", commit], ws.path).stdout == name + "\0":
            db.conn.execute("INSERT OR IGNORE INTO review_edits (workspace_id, sha) VALUES (?, ?)", (ws.id, commit))
            db.conn.commit()
    bus.publish_agent(db, "session.edited", root.id, path=name, commit=commit, deleted=data is None)
    return {"root_id": root.id, "path": name, "commit": commit,
            "blob": git.hash_blob(ws.path, data, name) if data is not None else None}


def put_session_file(db: DB, params: dict, body: Body) -> Any:
    """``content`` becomes the file; ``base`` is the blob sha GET .../file
    gave (leave it out to create a new file)."""
    return _edit_session(db, params, body, _content(body))


def delete_session_file(db: DB, params: dict, body: Body) -> Any:
    """``base`` (required) is the blob sha GET .../file gave."""
    return _edit_session(db, params, body, None)


_SESSION_PR = "root_id = ? AND branch = ?"


def _checked(db: DB, ws: Workspace, job: Job, passed: tuple[str, list[str]] | None = None,
             checked: list[str] | None = None):
    """The PR gate on the session checkout (gates.pr_checked), each check's start as the job's progress."""
    return gates.pr_checked(db, ws, lambda t: bus.publish(db, "job.progress", job=job.id, text=t),
                            passed=passed, checked=checked)


def open_session_pr(db: DB, params: dict, body: Body) -> Any:
    """Push the session branch and open its PR (``title``, ``body``,
    ``draft``) the way ``brindle pr`` does: only ever on the person's
    request. Refused (409, with the reasons) unless the session is ready.
    The checks run first, unless brindle's preparation recorded them passing
    on this very HEAD (prepare_pr). The job's result carries the URL, which
    is also published as ``session.pr_opened`` and recorded in
    ``session_prs``. When gh can't open it, the URL is the compare page's
    and nothing is recorded."""
    root, ws = review.session(db, params["root"])
    identity_lock.ensure(root)
    review.require_ready(db, root, ws)
    title, text, draft = _get(body, "title"), _get(body, "body"), _get(body, "draft", bool, False)
    key = (root.id, ws.branch)

    def work(db: DB, job: Job) -> Any:
        identity_lock.ensure(root)      # again: the person signed in may have changed since
        # The commands brindle's preparation saw pass on its sha: any check configured since still runs.
        prep = db.conn.execute(f"SELECT sha, checks FROM session_pr_prep WHERE {_SESSION_PR} AND state = 'passed'",
                               key).fetchone()
        passed = (prep["sha"], json.loads(prep["checks"] or "[]")) if prep else None
        with _checked(db, ws, job, passed=passed) as head:
            url = workspaces.open_pull_request(ws, title, draft, body=text, sha=head)
        if url is None:     # the person opens it from the compare page: no PR to record yet
            url = workspaces.compare_url(ws)
        else:
            _record_pr(db, key, url, head)
        return {"url": url, "keeps_merged_branches": workspaces.keeps_merged_branches(ws.repo_root)}

    return start_job("session.pr", work, root_id=root.id, repo_root=ws.repo_root)


def push_session_pr(db: DB, params: dict, body: Body) -> Any:
    """Push the session's commits that aren't on its open PR yet: the
    person's Push in the review (brindle never pushes them itself), through
    the same checks as opening it. Refused (409) unless the session is
    ready and its branch has a PR."""
    root, ws = review.session(db, params["root"])
    identity_lock.ensure(root)
    review.require_ready(db, root, ws)
    if (pr := review.open_pr(db, root, ws)) is None:
        raise ServiceError("conflict", f"{ws.branch} has no PR to push to; open it first", 409)
    key = (root.id, ws.branch)

    def work(db: DB, job: Job) -> Any:
        identity_lock.ensure(root)
        with _checked(db, ws, job) as verified:
            git.push(ws.path, ws.branch, sha=verified)
        db.conn.execute(f"UPDATE session_prs SET pushed_sha = ? WHERE {_SESSION_PR}", (verified, *key))
        bus.publish_agent(db, "session.pr_pushed", root.id, url=pr["url"], commit=verified)
        return {"url": pr["url"], "commit": verified}

    return start_job("session.push", work, root_id=root.id, repo_root=ws.repo_root)


# The session branches (root id, branch) whose PR this daemon is preparing now, and
# the HEAD last tried on each and when: a preparation whose checks couldn't run (the
# checkout busy, or moved under them) is tried again after PREPARE_AGAIN_SECONDS
# instead of every sweep.
_PR_WORK: set[tuple[str, str]] = set()
_PR_TRIED: dict[tuple[str, str], tuple[str, float]] = {}
PREPARE_AGAIN_SECONDS = 60.0
_PR_LOCK = threading.Lock()


def _start_pr_work(key: tuple[str, str]) -> bool:
    with _PR_LOCK:
        if key in _PR_WORK:
            return False
        _PR_WORK.add(key)
        return True


def _end_pr_work(key: tuple[str, str]) -> None:
    with _PR_LOCK:
        _PR_WORK.discard(key)


def _record_pr(db: DB, key: tuple[str, str], url: str, pushed: str | None) -> None:
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (root_id, branch) DO UPDATE SET"
                    " url = excluded.url, pushed_sha = excluded.pushed_sha,"
                    # a different PR on the branch starts afresh; the same one keeps its merge
                    " merged_at = CASE WHEN url = excluded.url THEN merged_at END,"
                    " opened_at = CASE WHEN url = excluded.url THEN opened_at ELSE excluded.opened_at END",
                    (*key, url, pushed, time.time()))
    bus.publish_agent(db, "session.pr_opened", key[0], url=url)


def prepare_pr(db: DB, root_id: str) -> Job | None:
    """Prepare the ready session ``root_id``'s PR for the person's approval:
    draft its title, run the full pre-PR checks on its HEAD (gates.pr_checked,
    pushing nothing) and record how they went in ``session_pr_prep``, which
    the Inbox shows as "PR ready for your approval" or "PR checks failed" (a
    failure is also told to the supervisor). Once per HEAD. Nothing once its
    branch has a PR (new commits wait for the person's Push), while the
    session belongs to another identity, or while this daemon is already
    preparing it."""
    root, ws = review.session(db, root_id)
    key = (root.id, ws.branch)
    if identity_lock.locked_by(root.id) or review.open_pr(db, root, ws) or not _start_pr_work(key):
        return None
    try:
        job = _prepare_pr(db, root, ws, key)
    except BaseException:
        _end_pr_work(key)
        raise
    if job is None:
        _end_pr_work(key)
    return job


def _prepare_pr(db: DB, root: Agent, ws: Workspace, key: tuple[str, str]) -> Job | None:
    head = git.out(["rev-parse", "HEAD"], ws.path)
    row = db.conn.execute(f"SELECT sha, state FROM session_pr_prep WHERE {_SESSION_PR}", key).fetchone()
    # A row still checking with nobody at work on it was left by a daemon that stopped half way.
    if row and row["sha"] == head and row["state"] != "checking":
        return None
    tried, at = _PR_TRIED.get(key, (None, 0.0))
    if tried == head and time.monotonic() - at < PREPARE_AGAIN_SECONDS:
        return None
    db.conn.execute("INSERT INTO session_pr_prep (root_id, branch, sha, state, title, prepared_at)"
                    " VALUES (?, ?, ?, 'checking', ?, ?) ON CONFLICT (root_id, branch) DO UPDATE SET"
                    " sha = excluded.sha, state = excluded.state, title = excluded.title, checks = NULL,"
                    " check_cmd = NULL, output = NULL, prepared_at = excluded.prepared_at",
                    (*key, head, review.draft(db, root, ws)["title"], time.time()))

    def settle(db: DB, state: str, sha: str, cmd: str | None = None, output: str | None = None,
               checks: list[str] | None = None) -> None:
        # Only the row this preparation wrote: one the sweep dropped meanwhile (not ready) stays gone.
        db.conn.execute(f"UPDATE session_pr_prep SET state = ?, sha = ?, check_cmd = ?, output = ?, checks = ?"
                        f" WHERE {_SESSION_PR} AND sha = ?",
                        (state, sha, cmd, output, json.dumps(checks) if checks is not None else None, *key, head))

    def work(db: DB, job: Job) -> Any:
        checked: list[str] = []
        try:
            identity_lock.ensure(root)
            with _checked(db, ws, job, checked=checked) as sha:
                settle(db, "passed", sha, checks=checked)
        except gates.ChecksFailed as e:
            settle(db, "failed", head, e.cmd, str(e))
            gates.tell(db, root.id, f"[brindle PR] The pre-PR checks failed, so the session's PR waits: {e}\n"
                       "Fix the problem; brindle checks again once the session is ready.")
            bus.publish_agent(db, "session.pr_prepared", root.id, passed=False, check=e.cmd)
            return {"passed": False, "check": e.cmd}
        except BaseException:   # the checks couldn't run: tried again later, its row gone meanwhile
            db.conn.execute(f"DELETE FROM session_pr_prep WHERE {_SESSION_PR} AND sha = ? AND state = 'checking'",
                            (*key, head))
            _PR_TRIED[key] = (head, time.monotonic())
            raise
        finally:
            _end_pr_work(key)
        bus.publish_agent(db, "session.pr_prepared", root.id, passed=True, commit=sha)
        return {"passed": True, "commit": sha}

    return start_job("session.pr_prepare", work, root_id=root.id, repo_root=ws.repo_root)


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
    keys, each with a value of its default's type (or null). Scope "shared"
    is the repo's committed .brindle/config.json. ``unset`` (scope "repo" or
    "shared") lists keys to remove from that file, clearing a project's
    override."""
    from brindle import config

    scope = _get(body, "scope", default="user")
    if scope not in ("user", "repo", "shared"):
        raise ServiceError("invalid", "scope must be user, repo or shared")
    repo = _get(body, "repo", required=scope != "user")
    values = _get(body, "values", dict, default={})
    unset = _get(body, "unset", list, default=[])
    if unset and scope == "user":
        raise ServiceError("invalid", "unset needs scope repo or shared")
    if not values and not unset:
        raise ServiceError("invalid", "values is required")
    path = None
    defaults = config.RepoConfig()
    known = {f.name for f in dataclasses.fields(config.RepoConfig)}
    if unknown := sorted((set(values) | set(unset)) - known):
        raise ServiceError("invalid", f"unknown setting(s): {', '.join(unknown)}")
    for key, value in values.items():
        if not _fits(value, getattr(defaults, key)):
            raise ServiceError("invalid", f"{key} must be a {type(getattr(defaults, key)).__name__}")
    for key, value in values.items():
        if scope == "user":
            path = config.set_user(key, value)
        elif scope == "shared":
            path = config.set_repo(repo, key, value)
        else:
            path = config.set_local(repo, key, value)
    for key in unset:
        path = config.unset_repo(repo, key) if scope == "shared" else config.unset_local(repo, key)
    return {"updated": sorted({*values, *unset}), "path": str(path)}


# -- knowledge sources (brindle kb add / sync / remove) -----------------------------


def _kb_source(db: DB, params: dict, ref: str) -> tuple[str, str, str]:
    """(repo, repo key, source id) for a source named by id, prefix or folder."""
    from brindle import kb

    repo = params.get("repo")
    if not repo:
        raise ServiceError("invalid", "repo is required")
    key = knowledge.repo_key(repo)
    try:
        return repo, key, kb.find_source(db, key, ref)
    except KeyError:
        raise ServiceError("not_found", f"no such source: {ref}", 404) from None


_kb_syncing: dict[str, Job] = {}
_kb_sync_lock = threading.Lock()


def _kb_sync_job(kind: str, repo: str, source_id: str) -> Job:
    """The source's indexing job; one already running for it is returned
    instead of a second, so two indexers never overlap."""
    from brindle import codemap, kb

    with _kb_sync_lock:
        if source_id in _kb_syncing:
            return _kb_syncing[source_id]
        _kb_syncing[source_id] = job = Job(secrets.token_hex(8), kind)

    def work(db: DB, job: Job) -> Any:
        try:
            return kb.sync(db, source_id, codemap.graph_path(repo))
        except ValueError as e:
            raise ServiceError("invalid", str(e)) from None
        finally:
            with _kb_sync_lock:
                _kb_syncing.pop(source_id, None)

    return start_job(kind, work, job=job, source=source_id, repo_root=repo)


def add_kb_source(db: DB, params: dict, body: Body) -> Any:
    """Add a folder as a source and index it in the background."""
    from pathlib import Path

    from brindle import kb

    repo = params.get("repo")
    if not repo:
        raise ServiceError("invalid", "repo is required")
    path = _get(body, "path", required=True)
    if not Path(path).expanduser().is_dir():
        raise ServiceError("invalid", f"not a folder: {path}")
    private = _get(body, "private", bool, default=False)
    source_id = kb.add_source(db, knowledge.repo_key(repo), kb.FOLDER, path,
                              visibility="personal" if private else "repo")
    return _kb_sync_job("kb.add", repo, source_id)


def sync_kb_source(db: DB, params: dict, body: Body) -> Any:
    repo, _, source_id = _kb_source(db, params, params["id"])
    return _kb_sync_job("kb.sync", repo, source_id)


def remove_kb_source(db: DB, params: dict, body: Body) -> Any:
    from brindle import kb

    _, _, source_id = _kb_source(db, params, params["id"])
    kb.remove_source(db, source_id)
    return {"removed": source_id}


PERMISSION_MODES = ("default", "auto")


def patch_user_settings(db: DB, params: dict, body: Body) -> Any:
    """``claude_permission_mode`` ("default" or "auto") into ~/.brindle/config.json;
    its other keys are kept."""
    from brindle import config

    mode = _get(body, "claude_permission_mode", required=True)
    if mode not in PERMISSION_MODES:
        raise ServiceError("invalid", f"claude_permission_mode must be one of: {', '.join(PERMISSION_MODES)}")
    config.set_user("claude_permission_mode", mode)
    return {"claude_permission_mode": config.claude_permission_mode()}


def put_debug(db: DB, params: dict, body: Body) -> Any:
    """``enabled``: turn debug logs on (403 unless the account is brindle staff) or off."""
    from brindle import debuglog

    on = _get(body, "enabled", bool, required=True)
    try:
        return debuglog.set_enabled(on)
    except PermissionError as e:
        raise ServiceError("forbidden", str(e), 403) from None


def post_debug_log(db: DB, params: dict, body: Body) -> Any:
    """One of the app's own events into the debug log (written only while it is on);
    ``app_version`` (optional) is the app's version for the uploads."""
    from brindle import debuglog

    debuglog.note_app_version(_get(body, "app_version"))
    recorded = debuglog.record(_get(body, "source", required=True), _get(body, "level", default="info"),
                               _get(body, "message", required=True), body.get("detail"))
    return {"recorded": recorded}


def put_routing(db: DB, params: dict, body: Body) -> Any:
    """``tiers`` (tier -> profiles to try, in order), ``reviewer`` and
    ``exclude_providers`` into ~/.brindle/config.json (scope "user"), the
    repo's .brindle/config.local.json ("local") or its shared
    .brindle/config.json ("repo"). Tiers not given keep their value there.
    Each kind also takes ``effort`` and ``instructions`` (null clears), applied
    to whatever runs that kind at launch.
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
    kinds = _get(body, "kinds", dict) or {}
    lock_changes: dict[str, bool] = {}
    overrides: dict[str, dict[str, str | None]] = {}    # kind -> effort / instructions (None clears)
    extra: dict[str, Any] = {}      # supervisor and reviewer settings the kinds ask for
    # A user-scope change builds on the person's own lists and library, never this repo's.
    current = config.load_repo_config(repo, user_only=scope == "user").routing
    for kind, spec in kinds.items():
        if kind not in config.ROUTING_KINDS or not isinstance(spec, dict):
            raise ServiceError("invalid", f"kind must be one of {', '.join(config.ROUTING_KINDS)}, not {kind!r}")
        model, fallbacks, locked = spec.get("model"), spec.get("fallbacks"), spec.get("locked")
        for key in config.KIND_OVERRIDE_FIELDS:
            if key not in spec:
                continue
            value = spec[key]
            if value is not None and not (isinstance(value, str) and value.strip()):
                raise ServiceError("invalid", f"{kind}: {key} must be text, or null to clear it")
            if key == "effort" and value is not None and value not in config.EFFORTS:
                raise ServiceError("invalid", f"{kind}: effort must be one of {', '.join(config.EFFORTS)}")
            overrides.setdefault(kind, {})[key] = value
        if locked is not None:
            if not isinstance(locked, bool):
                raise ServiceError("invalid", f"{kind}: locked must be true or false")
            lock_changes[kind] = locked
        elif model is not None or fallbacks is not None:
            lock_changes[kind] = True   # the owner just chose it: learning must not undo that
        if model is not None and not (isinstance(model, str) and model):
            raise ServiceError("invalid", f"{kind}: model must be a name")
        if fallbacks is not None and (not isinstance(fallbacks, list) or not all(isinstance(n, str) for n in fallbacks)):
            raise ServiceError("invalid", f"{kind}: fallbacks must be a list of profile names")
        if kind in config.WEIGHTS and (model is not None or fallbacks is not None):
            old = current[kind]
            names = [model if model is not None else old[0] if old else None,
                     *(fallbacks if fallbacks is not None else old[1:])]
            tiers = {**(tiers or {}), kind: [n for n in dict.fromkeys(names) if n]}
        elif kind == "reviewer":
            reviewer = model if model is not None else reviewer
            if fallbacks is not None:
                extra["reviewer_fallbacks"] = list(dict.fromkeys(fallbacks))
        elif kind == "supervisor":
            if model is not None:
                extra["supervisor_profile"] = model
            if fallbacks is not None:
                extra["supervisor_fallbacks"] = list(dict.fromkeys(fallbacks))
    by_name = {p.name: p for p in profiles.list_profiles(None if scope == "user" else repo)}
    known = set(by_name)
    named = [reviewer] if reviewer is not None else []
    for value in extra.values():     # supervisor and reviewer choices and fallbacks are profiles too
        named.extend(value if isinstance(value, list) else [value])
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
    policy = RoutingPolicy.load(repo, user_library=scope == "user")
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
    if lock_changes:
        # Per kind, so editing one row leaves the others (and what lower scopes lock) as they are.
        mine = config.locked_kinds(config._read_json(config.scope_paths(repo)[scope]).get("routing_locked"))
        path = write("routing_locked", {**mine, **lock_changes})
        updated.append("routing_locked")
    if overrides:
        mine = config.kind_overrides(config._read_json(config.scope_paths(repo)[scope]).get("routing_overrides"))
        for kind, changes in overrides.items():
            merged = {**mine.get(kind, {}), **changes}
            mine[kind] = {k: v for k, v in merged.items() if v is not None}
        path = write("routing_overrides", {k: v for k, v in mine.items() if v})
        updated.append("routing_overrides")
    for key, value in extra.items():
        path = write(key, value)
        updated.append(key)
    if reviewer is not None and kinds.get("reviewer", {}).get("model") and config.load_repo_config(repo).review_profile:
        write("review_profile", reviewer)   # it would win over `reviewer`, so the new choice must replace it
    if not updated:
        raise ServiceError("invalid", "give tiers, reviewer, exclude_providers or kinds")
    return {"updated": updated, "path": str(path)}


def create_profile(db: DB, params: dict, body: Body) -> Any:
    """A custom agent in ~/.brindle/agents/<name>.md, written as
    `brindle profile new` writes one: ``name``, ``description``, ``provider``
    (signed in here), ``model`` (one the org's policy allows), ``effort``,
    ``instructions`` and ``rules`` (known rule packs). An existing name is
    refused (409)."""
    from brindle import config, profiles, providers
    from brindle.service.routing import RoutingPolicy

    repo = _get(body, "repo", required=True)
    name = _get(body, "name", required=True)
    provider = _get(body, "provider", required=True)
    model, effort = _get(body, "model") or None, _get(body, "effort") or None
    description, instructions = _get(body, "description") or "", _get(body, "instructions") or ""
    rules = _get(body, "rules", list) or []
    if not profiles.PROFILE_NAME.match(name):
        raise ServiceError("invalid", f"name {name!r}: use letters, digits, '-', '_' and '.'")
    if not all(isinstance(r, str) for r in rules):
        raise ServiceError("invalid", "rules must be a list of rule pack names")
    target = config.user_profiles_dir() / f"{name}.md"
    if target.exists() or name in {p.name for p in profiles.list_profiles(repo)}:
        raise ServiceError("conflict", f"an agent named {name!r} already exists", 409)
    if provider not in providers.PROVIDERS:
        raise ServiceError("invalid", f"provider must be one of {', '.join(providers.PROVIDERS)}")
    if why := providers.unusable(provider):
        raise ServiceError("invalid", f"{provider} isn't connected: {why}")
    if effort is not None and effort not in config.EFFORTS:
        raise ServiceError("invalid", f"effort must be one of {', '.join(config.EFFORTS)}")
    for pack in rules:
        try:
            profiles.load_rule_pack(pack, repo)
        except (KeyError, profiles.ProfileError) as e:
            raise ServiceError("invalid", f"rule {pack}: {e}") from e
    access = RoutingPolicy.load(repo).access
    if access.org_id is not None or access.error:
        why = access.error or access.refusal(provider)
        if not why and not access.allows_run(provider, model):
            why = (f"org {access.org_id} allows only the models "
                   f"{', '.join(access.allowed_models or ()) or '(none)'}, not {model or 'no declared model'}")
        if why:
            raise ServiceError("forbidden", why, 403)
    if model is not None and (not isinstance(model, str) or not model.isprintable()):
        raise ServiceError("invalid", "model must be one line of plain text")
    text = profiles.profile_text(name, description, instructions or "Describe the role here.",
                                 provider=provider, model=model, effort=effort, rules=rules)
    try:    # what will load is what was validated, whatever the values held
        written = profiles._parse(text, name)
    except profiles.ProfileError as e:
        raise ServiceError("invalid", str(e)) from e
    if (written.name, written.provider, written.model, written.effort, written.rules) != (
            name, provider, model, effort, rules):
        raise ServiceError("invalid", "a value can't be written to the agent file as given")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "x", encoding="utf-8") as f:    # a second create of the name loses, not overwrites
            f.write(text)
    except FileExistsError as e:
        raise ServiceError("conflict", f"an agent named {name!r} already exists", 409) from e
    return {"name": name, "path": str(target)}


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


def _policy_changed() -> None:
    from brindle import antigravity

    antigravity.resync_permissions()


def _invalid(e: ValueError) -> ServiceError:
    return ServiceError("invalid", str(e))


def set_permission_preset(db: DB, params: dict, body: Body) -> Any:
    """PUT /permissions/preset {preset}: seed the store from that preset (its
    rules replace the preset's earlier ones; the person's own and learned rules
    stay) and record the choice. Answers the whole policy."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    try:
        store = perms.apply_preset(_get(body, "preset", required=True))
    except ValueError as e:
        raise _invalid(e) from e
    _policy_changed()
    return svc.policy_dto(store).to_json()


def patch_permissions(db: DB, params: dict, body: Body) -> Any:
    """PATCH /permissions {unmatched}: what a request no rule covers does (ask, allow or deny)."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    try:
        perms.set_unmatched(_get(body, "unmatched", required=True))
    except ValueError as e:
        raise _invalid(e) from e
    _policy_changed()
    return svc.policy_dto().to_json()


def set_permission_groups(db: DB, params: dict, body: Body) -> Any:
    """PUT /permissions/groups {groups: {group: {baseline?, subcategories?}}}:
    change capability group baselines and subcategory answers (the rest stay).
    Answers the whole policy."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    try:
        store = perms.set_groups(_get(body, "groups", dict, required=True))
    except ValueError as e:
        raise _invalid(e) from e
    _policy_changed()
    return svc.policy_dto(store).to_json()


def _rule_fields(body: Body) -> dict[str, Any]:
    """The rule fields a body may set (make_rule's), validated for type."""
    out: dict[str, Any] = {}
    for key in ("kind", "match", "match_type", "decision", "label"):
        value = _get(body, key)
        if value is not None:
            out[key] = value
    for key in ("limits",):
        value = _get(body, key, dict)
        if value is not None or key in body:
            out[key] = value
    if "expires" in body:
        out["expires"] = _get(body, "expires", (int, float))
    if "minutes" in body:    # a timed rule: expires this many minutes from now
        minutes = _get(body, "minutes", (int, float))
        out["expires"] = time.time() + minutes * 60 if minutes else None
    return out


def create_permission_rule(db: DB, params: dict, body: Body) -> Any:
    """POST /permissions/rules {kind, match, match_type?, decision, limits?, minutes?|expires?, label?}."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    fields = _rule_fields(body)
    for key in ("kind", "match", "decision"):
        if key not in fields:
            raise ServiceError("invalid", f"{key} is required")
    try:
        rule = perms.add_rule(fields.pop("kind"), fields.pop("match"), fields.pop("decision"),
                              fields.pop("match_type", "exact"), **fields)
    except ValueError as e:
        raise _invalid(e) from e
    _policy_changed()
    return svc.rule_dto(rule).to_json()


def update_permission_rule(db: DB, params: dict, body: Body) -> Any:
    """PUT /permissions/rules/{id}: change any of a rule's fields (a preset's rule becomes the person's)."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    try:
        rule = perms.update_rule(params["id"], **_rule_fields(body))
    except ValueError as e:
        raise _invalid(e) from e
    if rule is None:
        raise ServiceError("not_found", f"no rule {params['id']}", 404)
    _policy_changed()
    return svc.rule_dto(rule).to_json()


def delete_permission_rule(db: DB, params: dict, body: Body) -> Any:
    from brindle import permissions as perms

    gone = perms.forget(params["id"])
    if gone is None:
        raise ServiceError("not_found", f"no rule {params['id']}", 404)
    _policy_changed()
    return {"deleted": params["id"], "describe": gone}


def add_permission_exception(db: DB, params: dict, body: Body) -> Any:
    """POST /permissions/rules/{id}/exceptions {kind, match, match_type?}: except those requests from the rule."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    try:
        rule = perms.add_exclusion(params["id"], _get(body, "kind", required=True), _get(body, "match", required=True),
                                   _get(body, "match_type", default="exact"))
    except ValueError as e:
        raise _invalid(e) from e
    if rule is None:
        raise ServiceError("not_found", f"no rule {params['id']}", 404)
    _policy_changed()
    return svc.rule_dto(rule).to_json()


def _saved_fields(body: Body) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("kind", "pattern", "decision", "scope", "level", "life", "session", "repo", "note"):
        value = _get(body, key)
        if value is not None or key in body:
            out[key] = value
    if "minutes" in body:
        out["minutes"] = _get(body, "minutes", (int, float))
    if "request" in body:
        out["request"] = _get(body, "request", dict)
    return out


def create_saved_action(db: DB, params: dict, body: Body) -> Any:
    """POST /permissions/saved {kind, pattern, decision, scope?, level?, request?, life?, minutes?, session?, repo?, note?}."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    fields = _saved_fields(body)
    for key in ("kind", "pattern", "decision"):
        if not fields.get(key):
            raise ServiceError("invalid", f"{key} is required")
    try:
        action = perms.save_action(fields.pop("kind"), fields.pop("pattern"), fields.pop("decision"),
                                   **{k: v for k, v in fields.items() if v is not None})
    except ValueError as e:
        raise _invalid(e) from e
    return svc.saved_dto(action).to_json()


def update_saved_action(db: DB, params: dict, body: Body) -> Any:
    """PUT /permissions/saved/{id}: change its scope, pattern, decision or how long it lasts (a timed one restarts)."""
    from brindle import permissions as perms
    from brindle.service import permissions as svc

    try:
        action = perms.update_saved_action(params["id"], **_saved_fields(body))
    except ValueError as e:
        raise _invalid(e) from e
    if action is None:
        raise ServiceError("not_found", f"no saved action {params['id']}", 404)
    return svc.saved_dto(action).to_json()


def delete_saved_action(db: DB, params: dict, body: Body) -> Any:
    from brindle import permissions as perms

    gone = perms.delete_saved_action(params["id"])
    if gone is None:
        raise ServiceError("not_found", f"no saved action {params['id']}", 404)
    return {"deleted": params["id"]}


def decide_asks(db: DB, params: dict, body: Body) -> Any:
    """POST /permissions/asks/decide {key, scope, mode, minutes?}: one decision
    on an Inbox item (GET /permissions/asks). ``mode``: once | session | timed |
    forever | deny | always-deny; ``scope``: one of the item's scope values."""
    from brindle import permission_grants
    from brindle.service import permissions as svc

    minutes = _get(body, "minutes", (int, float))
    result = permission_grants.decide_group(db, _get(body, "key", required=True), _get(body, "scope", required=True),
                                            _get(body, "mode", required=True), minutes)
    return svc.decision_dto(result).to_json()


def allow_all_asks(db: DB, params: dict, body: Body) -> Any:
    """POST /permissions/asks/allow-all {repo?, forever?}: the stall notice's
    automated fix: allow everything waiting that its own policy permits, one
    grant per item at the general scope where every request's policy allows
    it, an exact grant where only that is permitted."""
    from brindle import permission_grants
    from brindle.service import permissions as svc

    result = permission_grants.allow_all(db, _get(body, "repo"), _get(body, "forever", bool, False))
    return svc.plan_dto(result).to_json()


def unblock_asks(db: DB, params: dict, body: Body) -> Any:
    """POST /permissions/asks/unblock {repo?, forever?}: except the blocked
    requests from the deny rules that block them (a rule for them where
    unmatched actions are denied), then allow them."""
    from brindle import permission_grants
    from brindle.service import permissions as svc

    result = permission_grants.unblock_denied(db, _get(body, "repo"), _get(body, "forever", bool, False))
    _policy_changed()
    return svc.plan_dto(result).to_json()


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
# Each answers an ActionTerminalDTO: the pane and tmux server the app's own tmux
# client attaches to, and the id for POST /terminals/{id}/input. While the same
# action is still running, a second press gets its terminal (started False). The person signs in or types the secret there; the daemon never
# automates a sign-in. The app may instead send a key the person typed to
# PUT /v1/keys/{name} (write-only), or a code they pasted to a terminal's input.


def provider_login(db: DB, params: dict, body: Body) -> Any:
    """The provider CLI's own sign-in (``claude auth login``, ``codex login``, ...).
    ``method``: how to sign in, where the CLI has a flag for it (see
    picker.LOGIN_METHODS); its own default otherwise."""
    from brindle import cloud_login, company_login, picker, providers
    from brindle.config import brindle_home
    from brindle.pro.team_policy import CLI_NAMES

    provider = params["provider"]
    if provider not in CLI_NAMES:
        raise ServiceError("invalid", f"provider must be one of {', '.join(CLI_NAMES)}")
    method = _get(body, "method")
    methods = picker.LOGIN_METHODS.get(provider, {})
    if method is not None and method not in methods:
        raise ServiceError("invalid", f"{provider} sign-in methods: {', '.join(methods) or 'none'}")
    repo, profile = _get(body, "repo"), _get(body, "profile")
    if bool(repo) != bool(profile):
        raise ServiceError("invalid", "repo and profile must be supplied together")
    # "Try again" in the app: end the sign-in still running and start a fresh one.
    restart = _get(body, "restart", bool, False)
    identity = agents.consent_identity(db, repo, profile, provider, _get(body, "session")) if repo else (
        company_login.stored_env() if provider == "claude" else {})
    if ((provider == "claude" and not providers.claude_org_managed()) or
            (provider == "opencode" and identity.get("BRINDLE_CLOUD_ROUTE"))):
        route = cloud_login.provider_from_env({**os.environ, **identity})
        if route in ("bedrock", "vertex", "azure"):
            command = cloud_login.login_command(identity)
            if method or command is None:
                raise ServiceError("invalid", "Refresh this profile's configured cloud credentials directly")
            browser = picker._wsl_browser_env()
            full = {**os.environ, **identity}
            env = {**{k: full[k] for k in cloud_login.IDENTITY_ENV if k in full}, **identity,
                   "BRINDLE_HOME": str(brindle_home()),
                   **({"BROWSER": browser["BROWSER"]} if browser else {})}
            return terminal.open_action(f"login cloud-{cloud_login.action_key(identity)}",
                                        cloud_login.interactive_command(),
                                        env=env, restart=restart)
    from brindle import provider_homes

    browser = picker._wsl_browser_env()      # WSL with no Linux browser: open it in Windows'
    # The CLI signs in to brindle's config home for it, which brindle uses from then on.
    env = {**provider_homes.login_env(provider), **({"BROWSER": browser["BROWSER"]} if browser else {})}
    return terminal.open_action(f"login {provider}", picker._login_command(provider, method), env=env,
                                restart=restart)


def profile_preflight(db: DB, params: dict, body: Body) -> Any:
    """Explicitly check the launch identity named by a preflight action.

    Unlike the read-only GET probe, this may run the configured cloud CLI.
    The POST uses the same profile and session resolution as sign-in.
    """
    provider = params["provider"]
    if provider not in preflight.PROVIDERS:
        raise ServiceError("invalid", "unknown provider")
    repo, profile = _get(body, "repo", required=True), _get(body, "profile", required=True)
    session = _get(body, "session")
    identity = agents.consent_identity(db, repo, profile, provider, session)
    launch = {"repo": repo, "profile": profile, **({"session": session} if session else {})}
    return [i.to_json() for i in preflight.check(provider, repo, env=identity, launch=launch)]


def provider_trust(db: DB, params: dict, body: Body) -> Any:
    """The person chose in the app to trust ``repo`` in this provider's CLI
    (a preflight ``trust`` item): recorded where the CLI keeps its answer."""
    repo = _get(body, "repo", required=True)
    if not preflight.trust(params["provider"], repo):
        raise ServiceError("not_stored", f"couldn't record {params['provider']}'s trust for {repo}", 422)
    return {"provider": params["provider"], "repo": repo, "trusted": True}


def provider_consent(db: DB, params: dict, body: Body) -> Any:
    """The person accepted a preflight ``consent`` item (``id``) in the app:
    brindle records it as the CLI does. Only this route, on the person's own
    confirmation, ever records one."""
    if _get(body, "accept", bool) is not True:
        raise ServiceError("invalid", "accept must be true: only the person's confirmation records a consent")
    item, repo, profile = _get(body, "id", required=True), _get(body, "repo"), _get(body, "profile")
    env = None
    if repo and profile:    # the launch it was asked for (the item's action names it): the same identity
        env = agents.consent_identity(db, repo, profile, params["provider"], _get(body, "session"))
    try:
        preflight.record_consent(params["provider"], item, env, _get(body, "key"))
    except preflight.StaleConsent as e:
        raise ServiceError("conflict", str(e), 409) from None
    return {"provider": params["provider"], "id": item, "recorded": True}


# Provider CLIs (brindle.providers_install): found as a terminal would, installed
# on request with the maker's own installer, never uninstalled.


def _known_provider(params: dict) -> str:
    from brindle import providers_install

    provider = params["provider"]
    if provider not in providers_install.PROVIDERS:
        raise ServiceError("invalid", f"provider must be one of {', '.join(providers_install.PROVIDERS)}")
    return provider


def install_provider(db: DB, params: dict, body: Body) -> Any:
    """Run the maker's official install command (202 job): each line it prints
    is a ``job.progress`` ``text``; ``job.done`` carries the provider as GET
    /v1/providers/installed shows it afterwards. A press while one runs gets
    that job back."""
    from brindle import providers_install

    provider = _known_provider(params)
    job = Job(secrets.token_hex(8), "provider.install")
    held = providers_install.claim(provider, job)
    if held is not job:
        return held

    def work(db: DB, job: Job) -> Any:
        try:
            return providers_install.install(
                provider, lambda line: bus.publish(db, "job.progress", job=job.id, provider=provider, text=line))
        except providers_install.InstallError as e:
            raise ServiceError("install_failed", str(e), 422) from None
        finally:
            providers_install.release(provider)

    return start_job("provider.install", work, job=job, provider=provider)


def use_provider_install(db: DB, params: dict, body: Body) -> Any:
    """Run ``path``, one of the installs GET /v1/providers/installed lists,
    when there are several; turns the provider back on."""
    from brindle import providers_install

    provider = _known_provider(params)
    try:
        return providers_install.use(provider, _get(body, "path", required=True))
    except providers_install.InstallError as e:
        raise ServiceError("not_found", str(e), 404) from None


def turn_off_provider(db: DB, params: dict, body: Body) -> Any:
    """Stop using the provider in brindle. Nothing is uninstalled."""
    from brindle import providers_install

    return providers_install.turn_off(_known_provider(params))


def provider_logout(db: DB, params: dict, body: Body) -> Any:
    """Sign brindle's own copy of the provider out (Claude Code, Codex only);
    the person's own CLI login is not touched. Answers the provider's row."""
    from brindle import providers_install

    provider = _known_provider(params)
    if provider not in providers_install.SIGN_OUT:
        raise ServiceError("invalid", f"sign out is offered for {', '.join(providers_install.SIGN_OUT)}")
    try:
        return providers_install.sign_out(provider)
    except providers_install.InstallError as e:
        raise ServiceError("logout_failed", str(e), 422) from None


def put_provider_connection(db: DB, params: dict, body: Body) -> Any:
    """How the provider connects (brindle.connections), into
    ~/.brindle/config.json: ``method`` auto | sign_in | api_key | cloud, and
    for cloud ``cloud`` (the route) and ``settings`` (its non-secret
    variables, e.g. AWS_PROFILE and AWS_REGION). A choice the org's policy
    decides instead is refused (403) with the reason. Answers GET's shape."""
    from brindle import connections
    from brindle.service.dto import ProviderConnectionDTO

    provider, repo = params["provider"], _get(body, "repo")
    try:
        connections.choose(provider, _get(body, "method", required=True), _get(body, "cloud"),
                           _get(body, "settings", dict), repo)
        return ProviderConnectionDTO(**connections.view(provider, repo, db)).to_json()
    except connections.Invalid as e:
        raise ServiceError("invalid", str(e)) from None
    except connections.Refused as e:
        raise ServiceError("forbidden", str(e), 403) from None


def _streamed(db: DB, job: Job):
    """A ``run`` for a cloud CLI's sign-in: no terminal input, its output
    (the URL or device code to confirm in the browser) as ``job.progress``."""
    def run(argv: list[str], env=None, pass_fds=()) -> subprocess.CompletedProcess:
        with subprocess.Popen(argv, env=env, pass_fds=pass_fds, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              start_new_session=True) as proc:
            for line in proc.stdout:
                bus.publish(db, "job.progress", job=job.id, text=line.rstrip("\n"))
        return subprocess.CompletedProcess(argv, proc.returncode)
    return run


def provider_cloud_sign_in(db: DB, params: dict, body: Body) -> Any:
    """The cloud sign-in for the provider's saved cloud route, a first one or
    a renewal, as a job (202): the CLI's output arrives as ``job.progress``,
    and the result says what happened: "signed in", "failed", "waiting" (one
    is already running for this login) or "manual"."""
    from brindle import connections, picker

    provider = params["provider"]
    try:
        connections.sign_in_target(provider)
    except connections.Invalid as e:
        raise ServiceError("invalid", str(e)) from None
    except connections.Refused as e:
        raise ServiceError("forbidden", str(e), 403) from None
    browser = picker._wsl_browser_env()      # WSL with no Linux browser: open it in Windows'

    def work(db: DB, job: Job) -> Any:
        return connections.sign_in(provider, run=_streamed(db, job),
                                   say=lambda text: bus.publish(db, "job.progress", job=job.id, text=text),
                                   extra_env={"BROWSER": browser["BROWSER"]} if browser else None)

    return start_job("provider.cloud_sign_in", work, provider=provider)


def provider_sign_out(db: DB, params: dict, body: Body) -> Any:
    """Sign brindle's own copy of Codex out (providers_install.sign_out; the
    person's own login is not touched), as a job (202), so that an API key
    its ChatGPT login kept out (the connection's ``blocked_by``
    "codex_login") is used. The result is the connection, as GET answers it."""
    from brindle import connections, providers_install
    from brindle.service.dto import ProviderConnectionDTO

    provider, repo = params["provider"], _get(body, "repo")
    if provider != "codex":
        raise ServiceError("invalid", "sign-out is for codex, whose ChatGPT login keeps a key from being used")

    def work(db: DB, job: Job) -> Any:
        try:
            providers_install.sign_out_only(provider)
        except providers_install.InstallError as e:
            raise ServiceError("logout_failed", str(e), 422) from None
        return ProviderConnectionDTO(**connections.view(provider, repo, db)).to_json()

    return start_job("provider.sign_out", work, provider=provider)


def terminal_input(db: DB, params: dict, body: Body) -> Any:
    """Type ``text``, one line the person typed or pasted in the app (a
    sign-in's code), into the action terminal ``id``, then Enter unless
    ``submit`` is false. User-typed input only: nothing brindle or an agent
    makes up goes in here, and agent callers are refused like everywhere.
    Over --tcp the agent check can't run; the token is the only gate."""
    text = _get(body, "text", required=True)
    if not text or len(text) > 4096 or any(ord(c) < 0x20 or ord(c) == 0x7f for c in text):
        raise ServiceError("invalid", "text must be one line of printable characters")
    if not terminal.send_input(params["id"], text, _get(body, "submit", bool, True)):
        raise ServiceError("not_found", f"no running terminal {params['id']}", 404)
    return {"terminal": params["id"], "sent": True}


def shell_terminal(db: DB, params: dict, body: Body) -> Any:
    """The person's login shell (``$SHELL -l``) in ``cwd``, a new terminal each
    call. ``cwd`` must be an attached repo's root or one of brindle's
    worktrees of it. Not an agent: no hooks, no agent row."""
    cwd = os.path.realpath(_get(body, "cwd", required=True))
    allowed = {os.path.realpath(p) for w in db.find_workspaces() for p in (w.path, w.repo_root)}
    if cwd not in allowed:
        raise ServiceError("invalid", f"{cwd} isn't an attached repo or one of its worktrees")
    return terminal.open_action(terminal.SHELL_KIND, [os.environ.get("SHELL") or "/bin/sh", "-l"], cwd=cwd, reuse=False)


def close_terminal(db: DB, params: dict, body: Body) -> Any:
    if not terminal.close_action(params["id"]):
        raise ServiceError("not_found", f"no terminal {params['id']}", 404)
    return {"terminal": params["id"], "closed": True}


def put_key(db: DB, params: dict, body: Body) -> Any:
    """Store ``value`` as key ``name``, as ``brindle keys set NAME`` does.
    Write-only: the reply carries the last four characters, never the value,
    and neither the value nor the body is logged or put on the bus. Agent
    callers are refused; over --tcp that check can't run and the token is the only gate."""
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


def delete_key(db: DB, params: dict, body: Body) -> Any:
    """``brindle keys unset NAME``. GEMINI_API_KEY also gives agy back the
    modelProvider brindle replaced for it (connections.key_removed)."""
    from brindle import connections, keys_cmds, keystore

    name = params["name"]
    try:
        keys_cmds.remove(name)
    except keystore.KeyNameError as e:
        raise ServiceError("invalid", str(e)) from None
    except keystore.CredentialError as e:
        raise ServiceError("not_stored", str(e), 422) from None
    connections.key_removed(name)
    return {"name": name, "set": False}


def retest_key(db: DB, params: dict, body: Body) -> Any:
    """Re-check the stored key with its provider; the reply says how it came out."""
    from brindle import keys_cmds, keystore

    name = params["name"]
    try:
        status = keys_cmds.retest(name)
        checked_at = keystore.get_record(name).get("checked_at")
    except keystore.KeyNameError as e:
        raise ServiceError("invalid", str(e)) from None
    except keystore.CredentialError as e:
        raise ServiceError("not_found", str(e), 404) from None
    return {"name": name, "status": status, "checked_at": checked_at}


def import_key(db: DB, params: dict, body: Body) -> Any:
    """Store the daemon's environment value of ``name``, as a save would (never returned)."""
    from brindle import keys_cmds, keystore

    name, repo = params["name"], _get(body, "repo")
    try:
        last4 = keys_cmds.import_env(name, repo)
    except keystore.KeyNameError as e:
        raise ServiceError("invalid", str(e)) from None
    except keys_cmds.PolicyDenied as e:
        raise ServiceError("forbidden", str(e), 403) from None
    except keystore.CredentialError as e:
        raise ServiceError("not_stored", str(e), 422) from None
    except Exception:  # noqa: BLE001 - no traceback: its frames held the value
        raise ServiceError("internal", "couldn't store the key", 500) from None
    return {"name": name, "last4": last4, "set": True}


def set_key(db: DB, params: dict, body: Body) -> Any:
    """``brindle keys set NAME``: the value goes into the terminal's hidden prompt."""
    from brindle import keystore, providers

    name, repo = params["name"], _get(body, "repo")
    try:
        keystore.check_name(name, repo)
    except keystore.CredentialError as e:
        raise ServiceError("invalid", str(e)) from e
    return terminal.open_action(f"key {name}", [*providers.brindle_invocation(), "keys", "set", name], cwd=repo)


# -- engine ----------------------------------------------------------------------


UPGRADE_TAIL = 20
_upgrading: Job | None = None
_upgrade_lock = threading.Lock()


def _run_upgrade() -> tuple[int, str]:
    """``uv tool upgrade brindle``: its exit status and output. uv is looked
    up where its installer puts it too, since an app-started daemon's PATH
    may not have it."""
    import shutil
    from pathlib import Path

    uv = shutil.which("uv") or next((str(p) for p in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv")
                                     if p.is_file()), None)
    if uv is None:
        return 127, "uv isn't installed: see https://docs.astral.sh/uv/"
    try:
        out = subprocess.run([uv, "tool", "upgrade", "brindle"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return 124, "uv tool upgrade brindle took over 15 minutes and was stopped"
    return out.returncode, out.stdout or ""


def upgrade_engine(db: DB, params: dict, body: Body) -> Any:
    """Upgrade brindle in the background, no terminal: ``job.progress``
    (stage "upgrading") when it starts, then ``job.done`` or ``job.failed``,
    the failure carrying the last lines of uv's output (the only time any of
    it goes on the bus). A press while one runs gets that job back."""
    global _upgrading
    with _upgrade_lock:
        if _upgrading is not None:
            return _upgrading
        _upgrading = job = Job(secrets.token_hex(8), "engine.upgrade")

    def work(db: DB, job: Job) -> Any:
        global _upgrading
        try:
            bus.publish(db, "job.progress", job=job.id, stage="upgrading")
            code, output = _run_upgrade()
        finally:
            with _upgrade_lock:
                _upgrading = None
        if code != 0:
            raise ServiceError("upgrade_failed", f"uv tool upgrade brindle exited {code}", 500,
                               tail=output.splitlines()[-UPGRADE_TAIL:])
        return {"upgraded": True}

    return start_job("engine.upgrade", work, job=job)


_trusting: Job | None = None
_trust_lock = threading.Lock()


def setup_codex_hook(db: DB, params: dict, body: Body) -> Any:
    """What ``brindle permissions install-codex-hook --yes`` does, in the
    background (the inbox's ``codex_hook_trust`` fix): Codex trusts brindle's
    permission hook again. ``job.done`` or ``job.failed`` ends it. A press
    while one runs gets that job back."""
    global _trusting
    with _trust_lock:
        if _trusting is not None:
            return _trusting
        _trusting = job = Job(secrets.token_hex(8), "setup.codex_hook")

    def work(db: DB, job: Job) -> Any:
        global _trusting
        from brindle import codex_hook
        from brindle.providers import codex_binary

        try:
            binary = codex_binary()
            codex_hook.trust(binary, codex_hook.inspect(binary))
        except codex_hook.CodexHookError as e:
            raise ServiceError("codex_hook_failed", f"couldn't trust brindle's Codex hook: {e}", 422) from e
        finally:
            with _trust_lock:
                _trusting = None
        return {"trusted": True}

    return start_job("setup.codex_hook", work, job=job)


# -- projects: the app's picker ----------------------------------------------------


def clone_repo(db: DB, params: dict, body: Body) -> Any:
    """``git clone <url>`` into ``parent_dir/<repo name>`` as the person's own
    git would (their credentials and SSH keys; brindle handles none). Git's
    progress lines go out as ``job.progress`` ``text``; ``job.done`` carries
    ``{"path": ...}``, and a failure carries git's message (code ``auth_failed``
    when it was refused for credentials). An existing destination is refused (409)."""
    from brindle import clone

    url = _get(body, "url", required=True)
    parent = _get(body, "parent_dir", required=True)
    key = str(Path(parent).expanduser().resolve() / clone.repo_name(clone.validate_url(url)))
    job = Job(secrets.token_hex(8), "repo.clone")
    with _CLONE_LOCK:
        running = _CLONING.get(key)
        if running:
            if running[1] != url:
                raise ServiceError("exists", f"{key} is being cloned from another address", 409)
            return running[0]                  # pressed again: the one already running
        clone.destination(url, parent)
        _CLONING[key] = (job, url)
    dest = Path(key)

    def work(db: DB, job: Job) -> Any:
        def progress(text: str) -> None:
            bus.publish(db, "job.progress", job=job.id, text=text)

        try:
            return {"path": clone.clone(url, dest, progress)}
        finally:
            with _CLONE_LOCK:
                _CLONING.pop(key, None)

    try:
        return start_job("repo.clone", work, job=job)
    except BaseException:
        with _CLONE_LOCK:
            _CLONING.pop(key, None)
        raise


_CLONE_LOCK = threading.Lock()
_CLONING: dict[str, tuple[Job, str]] = {}      # destination -> (its clone job, the url)


def open_repo(db: DB, params: dict, body: Body) -> Any:
    """Register the folder ``path`` as session start does (workspaces.adopt_root).
    A folder that isn't a git repo is refused (409 ``not_a_repo``) unless the
    body says ``init``: then ``git init`` makes it one first."""
    path = Path(_get(body, "path", required=True)).expanduser()
    if not path.is_dir():
        raise ServiceError("invalid", f"not a folder: {path}")
    initialized = False
    if git.run(["rev-parse", "--show-toplevel"], path, check=False).returncode != 0:
        if not _get(body, "init", bool, False):
            raise ServiceError("not_a_repo", f"{path} isn't a git repository", 409)
        git.run(["init", "-q"], path)
        initialized = True
    ws = workspaces.adopt_root(db, str(path))
    return {"workspace_id": ws.id, "repo_root": ws.repo_root, "path": ws.path, "branch": ws.branch,
            "initialized": initialized}


def _post(path: str, fn: Action) -> Route:
    return Route(f"/v1{path}", _endpoint(fn), methods=["POST"])


ROUTES = [
    _post("/sessions", start_session),
    _post("/sessions/{root}/continue", continue_session),
    _post("/sessions/{root}/pause", pause_session),
    _post("/sessions/{root}/close", close_session),
    _post("/sessions/{root}/rename", rename_session),
    _post("/sessions/{root}/handover", handover_session),
    _post("/sessions/{root}/transfer", transfer_session),
    # GET on the file path reads (routes_read); agent callers are refused by the guard.
    Route("/v1/sessions/{root:path}/file", _endpoint(put_session_file), methods=["PUT"]),
    Route("/v1/sessions/{root:path}/file", _endpoint(delete_session_file), methods=["DELETE"]),
    _post("/sessions/{root:path}/pr/push", push_session_pr),
    _post("/sessions/{root:path}/pr", open_session_pr),
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
    Route("/v1/workspaces/{id:path}/file", _endpoint(edit_file), methods=["PUT"]),
    _post("/goal", set_goal),
    _post("/goal/check", check_goal),
    _post("/goal/enabled", enable_goal),
    _post("/decisions", record_decision),
    _post("/coordinator/undo", coordinator_undo),
    _post("/kb/sources", add_kb_source),
    Route("/v1/kb/sources/{id}", _endpoint(remove_kb_source), methods=["DELETE"]),
    _post("/kb/sources/{id}/sync", sync_kb_source),
    Route("/v1/config", _endpoint(patch_config), methods=["PATCH"]),
    # Agents never get here: the guard refuses them (see brindle.daemon.auth).
    Route("/v1/user-settings", _endpoint(patch_user_settings), methods=["PATCH"]),
    Route("/v1/routing", _endpoint(put_routing), methods=["PUT"]),
    Route("/v1/debug", _endpoint(put_debug), methods=["PUT"]),
    _post("/debug/log", post_debug_log),
    _post("/profiles", create_profile),
    _post("/permissions", change_permissions),
    # The permission policy (brindle.permissions, brindle.permission_grants); agents never get here.
    Route("/v1/permissions", _endpoint(patch_permissions), methods=["PATCH"]),
    Route("/v1/permissions/preset", _endpoint(set_permission_preset), methods=["PUT"]),
    Route("/v1/permissions/groups", _endpoint(set_permission_groups), methods=["PUT"]),
    _post("/permissions/rules", create_permission_rule),
    Route("/v1/permissions/rules/{id}", _endpoint(update_permission_rule), methods=["PUT"]),
    Route("/v1/permissions/rules/{id}", _endpoint(delete_permission_rule), methods=["DELETE"]),
    _post("/permissions/rules/{id}/exceptions", add_permission_exception),
    _post("/permissions/saved", create_saved_action),
    Route("/v1/permissions/saved/{id}", _endpoint(update_saved_action), methods=["PUT"]),
    Route("/v1/permissions/saved/{id}", _endpoint(delete_saved_action), methods=["DELETE"]),
    _post("/permissions/asks/decide", decide_asks),
    _post("/permissions/asks/allow-all", allow_all_asks),
    _post("/permissions/asks/unblock", unblock_asks),
    _post("/account/login", account_login),
    _post("/account/logout", account_logout),
    _post("/account/switch-org", account_switch_org),
    # Like every route, refused to agent callers by the guard.
    _post("/providers/{provider}/login", provider_login),
    _post("/providers/{provider}/preflight", profile_preflight),
    _post("/providers/{provider}/logout", provider_logout),
    _post("/providers/{provider}/trust", provider_trust),
    _post("/providers/{provider}/consent", provider_consent),
    _post("/providers/{provider}/install", install_provider),
    _post("/providers/{provider}/use-mine", use_provider_install),
    Route("/v1/providers/{provider}/connection", _endpoint(put_provider_connection), methods=["PUT"]),
    _post("/providers/{provider}/cloud-sign-in", provider_cloud_sign_in),
    _post("/providers/{provider}/sign-out", provider_sign_out),
    Route("/v1/providers/{provider}", _endpoint(turn_off_provider), methods=["DELETE"]),
    _post("/keys/{name}/set", set_key),
    Route("/v1/keys/{name}", _endpoint(put_key), methods=["PUT"]),
    Route("/v1/keys/{name}", _endpoint(delete_key), methods=["DELETE"]),
    _post("/keys/{name}/test", retest_key),
    _post("/keys/{name}/import", import_key),
    _post("/terminals/{id}/input", terminal_input),
    _post("/terminals/shell", shell_terminal),
    Route("/v1/terminals/{id}", _endpoint(close_terminal), methods=["DELETE"]),
    _post("/repos/clone", clone_repo),
    _post("/repos/open", open_repo),
    _post("/engine/upgrade", upgrade_engine),
    _post("/setup/codex-hook", setup_codex_hook),
]
