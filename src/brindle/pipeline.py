"""The pipeline: review and merge a reported branch without the supervisor.

Every stage between a worker's report and its merge used to wait for a
supervisor turn: report, turn, review, verdict, turn, merge, turn, remove.
With a supervisor carrying millions of tokens of context, each turn is
seconds to minutes, and it sits on the critical path of every branch.

With ``pipeline`` on (the default), brindle runs those stages itself:

1. A worker reports (``report_result``). brindle gets the branch reviewed at
   once (``agents.start_review``): with ``checks`` configured, a detached
   ``_review-after-checks`` process runs them first and then starts the
   reviewer with their results in its prompt; with none, the reviewer starts
   right away.
2. The reviewer approves: the branch is ready. brindle merges every ready
   branch of the same base (through the same gates ``merge_workspace``
   uses), the one whose hunks overlap the others' least first and the rest
   synced onto the new tip in turn, removes each worktree (not one whose
   worker took on more since its report, or with uncommitted changes), and
   sends the supervisor one message per branch: merged, with the worker's
   report and the review.
   The reviewer requests changes: brindle sends the findings straight to the
   worker, which fixes them and reports again, back to step 1; after
   ``review_rounds`` rounds it hands the findings to the supervisor instead.
3. A branch whose sync with its base conflicts goes back to its worker to
   resolve (a light worker in its worktree if that one has finished); its
   next report starts a fresh review, and the checks run again, before it
   merges. A conflict in a ``protected_paths`` file is never resolved by a
   worker: it goes to the supervisor. Anything else the pipeline can't
   settle (a failing check, no reviewer available) goes to the supervisor
   as "needs you", with the details.

While workers run, brindle also compares what each running branch has
actually changed (not just the ``files`` its task declared) and warns the
supervisor as soon as two of them touch the same file (see
``tasks.warn_predicted_conflicts``).

The supervisor still plans, assigns, and answers the user; it no longer
relays between the worker, the reviewer and the merge. ``handoff`` workers
whose caller is waiting for the result are not piped: the caller gets the
result directly, as before.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager

from brindle import (
    agents, autopilot, bus, codemap, codemap_docs, conflicts, events, gates, git, history, learning, policy,
    savings, tasks, workspaces,
)
from brindle.config import RepoConfig, load_repo_config
from brindle.db import DB, Agent, Workspace

PIPED_MODES = ("assign", "handoff_detached")
WORKER_MODES = ("handoff", "handoff_detached", "assign")


def enabled(cfg: RepoConfig, worker: Agent, ws: Workspace) -> bool:
    return bool(cfg.pipeline) and worker.mode in PIPED_MODES and ws.kind == "worktree" \
        and bool(worker.parent_id)


# -- one merge at a time ----------------------------------------------------------

ALREADY_MERGED = "Already merged"
GONE = "Nothing to merge"

_held = threading.local()


@contextmanager
def _file_lock(name: str, wait: bool = True):
    """Without ``wait``, BlockingIOError when another holds it."""
    from brindle.config import brindle_home

    lock_dir = brindle_home() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{name}.lock", "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def merge_lock(ws_id: str):
    """Hold workspace ``ws_id`` for a verdict or a merge, from the first look
    at its state to the removal of its worktree. Two reviewers of one branch,
    a reviewer calling submit_review twice, and the supervisor's
    merge_workspace are separate processes: without this they each run the
    gates and the merge, one of them in a worktree the other is removing. A
    lock file under the brindle home, so it holds across processes and is given
    back if its holder dies; re-entrant within a thread."""
    with _reentrant_lock("merge-" + hashlib.sha1(ws_id.encode()).hexdigest()[:16]):
        yield


@contextmanager
def base_lock(repo_root: str, base: str | None, wait: bool = True):
    """Hold the base branch ``base`` of ``repo_root`` for one merge into it,
    from the sync with it through the gates to the merge (#197). Branch locks
    alone let the supervisor's merge_workspace of one branch and the
    pipeline's merge of another into the same base overlap, and git then
    fails on the other's MERGE_HEAD. Taken after the branch's merge_lock,
    never before it. Without ``wait``, BlockingIOError while a merge holds it."""
    key = hashlib.sha1(f"{repo_root}\0{base or ''}".encode()).hexdigest()[:16]
    with _reentrant_lock("base-" + key, wait):
        yield


@contextmanager
def branch_start_lock(repo_root: str, branch: str):
    """Hold ``branch`` of ``repo_root`` while a worker is started on it, from
    the check of who owns its worktree to the worker's row: otherwise another
    session's delegate of the same branch sees no owner yet and moves in too.
    Never waits and isn't re-entrant: BlockingIOError while another start holds it."""
    key = hashlib.sha1(f"{repo_root}\0{branch}".encode()).hexdigest()[:16]
    with _file_lock("start-" + key, wait=False):
        yield


@contextmanager
def _reentrant_lock(name: str, wait: bool = True):
    held = getattr(_held, "ids", None)
    if held is None:
        held = _held.ids = set()
    if name in held:
        yield
        return
    with _file_lock(name, wait):
        held.add(name)
        try:
            yield
        finally:
            held.discard(name)


def review_run(ws_id: str):
    """Held by each run that checks workspace ``ws_id`` and then starts its
    reviewer (``agents.review_after_checks``), for as long as it lives. Shared:
    two such runs (a second review, a newer report) don't wait on each other.
    The lock goes with a run that dies, so ``review_running`` tells a run
    that is at work from one that is gone."""
    return gates.marker("review-run", ws_id)


def review_running(ws_id: str) -> bool:
    """Whether a run holds ``review_run`` for workspace ``ws_id`` right now."""
    return gates.marked("review-run", ws_id)


def _settled(db: DB, ws: Workspace) -> str | None:
    """Why no gate or merge may run on ``ws`` any more: it is gone (removed,
    or being removed), or its branch already merged at its current commit.
    None while it can still be merged. The reply starts with ``GONE`` or
    ``ALREADY_MERGED``."""
    if db.get_workspace(ws.id) is None or not os.path.isdir(ws.path):
        return (f"{GONE}: the worktree of {ws.branch} is gone (already merged and removed, "
                "or removed).")
    merged = db.merged_sha(ws.id)
    if not merged:
        return None
    try:
        if gates.head(ws) != merged:
            return None   # new commits since: those can merge
    except git.GitError:
        return f"{GONE}: the worktree of {ws.branch} is gone (already merged and removed)."
    return (f"{ALREADY_MERGED}: {ws.branch} is in {ws.base_branch} at {merged[:8]}; "
            "nothing more to merge.")


def _tell(db: DB, parent_id: str | None, text: str, sender_id: str | None) -> None:
    if parent_id and db.get_agent(parent_id):
        try:
            agents.send_message(db, parent_id, text, sender_id=sender_id)
        except agents.AgentError:
            pass


def _note(db: DB, ws: Workspace, worker: Agent | None = None, *,
          actor: Agent | str | None = None, **event) -> None:
    """Tell the learning and events plugins about ``ws``'s worker (a no-op
    unless the repo has them; never raises). ``event`` is one of
    ``reported=True``, ``approved=``, ``escalated=True`` or ``merged=``
    (see learning.note); the events plugin hears it as a review, escalated,
    merge or remove event from ``actor``."""
    _publish(db, ws, worker, actor, **event)
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
        worker = worker or agents.workspace_worker(db, ws)
    except Exception:
        return
    terminal = event.get("merged") is not None
    already = bool(terminal and worker and (
        learning.settled(worker.id)
        or (event.get("merged") is False
            and (d := db.routing_decision_for_agent(worker.id)) is not None
            and d.outcome is not None)
    ))
    if not already:
        try:
            learning.note(db, cfg, worker, ws, **event)
        except Exception:
            pass
        if not event.get("reported"):
            savings.note_outcome(db, worker, **event)
    if event.get("approved") is not None:
        if isinstance(actor, Agent):
            learning.note_reviewer(db, cfg, actor, ws, approved=event["approved"], worker=worker)
        events.emit(cfg, "review", ws, worker, actor=actor, approved=event["approved"])
    if event.get("escalated"):
        events.emit(cfg, "escalated", ws, worker, actor=actor)
    if event.get("merged") is True:
        events.emit(cfg, "merge", ws, worker, actor=actor)
    elif event.get("merged") is False:
        events.emit(cfg, "remove", ws, worker, actor=actor, merged=False)


def _publish(db: DB, ws: Workspace, worker: Agent | None, actor: Agent | str | None,
             **event) -> None:
    """The bus event for one of ``_note``'s steps (see its ``event``)."""
    if event.get("reported"):
        kind = "pipeline.reported"
    elif event.get("approved") is not None:
        kind = "pipeline.review"
    elif event.get("escalated"):
        kind = "pipeline.escalated"
    elif event.get("merged") is True:
        kind = "pipeline.merged"
    elif event.get("merged") is False:
        kind = "pipeline.removed"
    else:
        return
    try:
        worker = worker or agents.workspace_worker(db, ws)
    except Exception:
        worker = None
    fields = {"actor": actor.id if isinstance(actor, Agent) else actor, "branch": ws.branch,
              **{k: v for k, v in event.items() if k != "reported"}}
    if worker:
        bus.publish_agent(db, kind, worker.id, workspace_id=ws.id, repo_root=ws.repo_root, **fields)
    else:
        bus.publish_ws(db, kind, ws, **fields)


def note_reported(db: DB, ws: Workspace, worker: Agent | None = None) -> None:
    _note(db, ws, worker, reported=True)


def note_review(db: DB, ws: Workspace, approved: bool, reviewer: Agent | None = None) -> None:
    _note(db, ws, approved=approved, actor=reviewer)


def note_removed_unmerged(db: DB, ws: Workspace, actor: Agent | None = None) -> None:
    _note(db, ws, merged=False, actor=actor)


def on_workspace_cleanup(db: DB, ws: Workspace) -> None:
    """Before ``ws``'s DB rows are deleted, record any reviewer still without
    a verdict as ``no_verdict`` and any worker abandoned without a report as
    ``removed_unmerged`` (if not already sent)."""
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    except Exception:
        cfg = RepoConfig()
    merged = bool(db.merged_sha(ws.id))
    worker = agents.workspace_worker(db, ws)
    for a in db.list_agents(ws.id):
        agents.forget_files(a)   # the agent rows go with the workspace
        if a.mode == "review" and a.result is None:
            learning.note_reviewer(db, cfg, a, ws, worker=worker)
        elif (a.mode in ("handoff", "handoff_detached", "assign")
              and a.result is None and not merged and not learning.settled(a.id)):
            d = db.routing_decision_for_agent(a.id)
            if not (d and d.outcome is not None):
                learning.note(db, cfg, a, ws, merged=False)
                savings.note_outcome(db, a, merged=False)


def note_removed_merged(db: DB, ws: Workspace, actor: Agent | None = None,
                        worker: Agent | None = None) -> None:
    """A worktree whose branch had merged is being removed: an event only
    (the learner heard about the merge). Pass ``worker`` when the
    workspace's records are already gone."""
    bus.publish_ws(db, "pipeline.removed", ws, agent_id=worker.id if worker else None,
                   actor=actor.id if actor else None, branch=ws.branch, merged=True)
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
        worker = worker or agents.workspace_worker(db, ws)
    except Exception:
        return
    events.emit(cfg, "remove", ws, worker, actor=actor, merged=True)


def _escalate(db: DB, ws: Workspace, worker: Agent | None) -> None:
    _note(db, ws, worker, escalated=True, actor="brindle")


# -- stage 1: a worker reported ---------------------------------------------------


def second_profile(db: DB, cfg: RepoConfig, worker: Agent, ws: Workspace) -> str | None:
    """The reviewer profile of the second review ``cfg.second_review`` asks
    for on ``worker``'s branch, by the weight its task was sized at. None when
    it's unset, the task has no weight there, or it names the profile that
    does the first review anyway (one reviewer per profile and commit)."""
    if not cfg.second_review:
        return None
    weight = next((t.weight for t in db.list_tasks(ws.repo_root) if t.agent_id == worker.id), None)
    profile = cfg.second_review.get(weight or "")
    if not profile or profile == agents.default_review_profile(cfg, worker, db, ws.repo_root, worker.task):
        return None
    return profile


def second_review_pending(db: DB, cfg: RepoConfig, worker: Agent, ws: Workspace) -> bool:
    """Whether a second review is required for ``ws``'s current commit and
    hasn't approved it yet (the first reviewer's approval must wait for it)."""
    profile = second_profile(db, cfg, worker, ws)
    if profile is None:
        return False
    reviews = gates.standing_reviews(db, ws.id, gates.head(ws))   # as the merge gate counts them
    if not reviews or not all(r.approved for r in reviews):
        return True   # one is missing, or one asked for changes (the worker is fixing it)
    # An approval in each slot: the commit's review ("") and the second
    # review's (its profile). Two in one slot are one review.
    slots = {_review_slot(db, a) for r in reviews
             if r.reviewer_id and (a := db.get_agent(r.reviewer_id)) is not None}
    return not {"", profile} <= slots


def _review_slot(db: DB, reviewer: Agent) -> str | None:
    """The review claim slot ``reviewer`` was started in, whatever its
    profile; None for one with no slot on record (started before slots were
    recorded), which counts for neither review."""
    if reviewer.review_slot is not None:
        return reviewer.review_slot
    return db.review_slot(reviewer.id)


def _report_only(worker: Agent, ws: Workspace) -> bool:
    """A review profile's worker, or one whose branch finished with no commits
    and no tracked edits: there is nothing for the pipeline to review or merge."""
    if agents.is_review_profile(worker.profile, ws.repo_root):
        return True
    try:
        if git.ahead_behind(ws.path, workspaces.require_base(ws))[1]:
            return False
    except Exception:  # noqa: BLE001 - can't tell: let the pipeline decide as before
        return False
    return not git.dirty_files(ws.path, tracked_only=True)


def on_report(db: DB, worker: Agent, ws: Workspace, result: str) -> bool:
    """Start the review of a reported branch. Returns whether the pipeline
    took the report (so the caller doesn't forward it to the supervisor)."""
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    except ValueError:
        return False
    if not enabled(cfg, worker, ws):
        return False
    parent = db.get_agent(worker.parent_id) if worker.parent_id else None
    if parent is None:
        return False
    if _report_only(worker, ws):
        return False   # nothing to review or merge: the report goes straight to the supervisor
    dirty = git.dirty_files(ws.path, tracked_only=True)   # untracked scratch files don't travel with the branch
    if dirty:
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        shown = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
        _tell(db, parent.id,
              agents.condense(
                  f"[brindle pipeline] {worker.id} reported on `{ws.branch}` but left uncommitted "
                  f"changes ({shown}), so it can't be reviewed or merged. "
                  f"Its report: {agents.first_line(result)}", result), worker.id)
        return True
    # Before the check run starts: it reads this report's claim (see review_claim).
    db.update_agent(worker.id, pipeline="reviewing", reviewing_since=time.time(), merge_failed=None)
    _start_review(db, cfg, parent, worker, ws, result)
    return True


def review_state(db: DB, ws: Workspace, *, head_reader=None) -> str | None:
    """#186: where the branch in ``ws`` stands in review, so silence can be
    told apart: ``waiting for checks`` (a check slot), ``checks running``,
    ``reviewer running (id)``, nothing at work on a branch in review, the
    latest verdict and its commit (with the merge's own checks, once
    approved), or ``merge failed: why``. None when the branch has no worker,
    is merged, or has not been through review."""
    worker = piped_worker(db, ws)
    if worker is None:
        return None
    merged = db.merged_sha(ws.id)
    if not (worker.pipeline or worker.merge_failed or merged or db.last_review(ws.id)):
        return None   # an ordinary idle worker needs no git inspection on redraw
    try:
        sha = head_reader() if head_reader is not None else gates.head(ws)
    except git.GitError:
        return None   # a removed worktree has no current review to display
    if sha is None or merged == sha:
        return None
    if worker.merge_failed and not worker.pipeline:
        why = " ".join(worker.merge_failed.split())
        return "merge failed: " + (why if len(why) <= 200 else why[:199] + "…")
    checks = "checks running" if gates.marked("checks-running", ws.id) else "waiting for checks"
    if worker.pipeline == "ready":
        merging = f"merging, {checks}" if gates.marked("merge-run", ws.id) else "waiting to merge"
        return f"approved, {merging}"
    # Use the same standing verdicts as the merge gate, for this commit only.
    # A recorded verdict may precede the asynchronous pipeline transition.
    review = gates.verdict(db, ws.id, sha)
    if worker.pipeline == "reviewing":
        if review_running(ws.id):
            return checks
        for a in db.list_agents(ws.id):
            if a.mode == "review" and a.status not in ("paused", "done") and a.result is None \
                    and a.dismissed_at is None and a.review_sha == sha:
                return f"reviewer running ({a.id})"
        if review is None:
            return "waiting for review: nothing is checking or reviewing it"
    if review is None:
        return None
    return f"{'approved' if review.approved else 'changes requested'} at {review.sha[:8]}"


def _start_review(db: DB, cfg: RepoConfig, parent: Agent, worker: Agent, ws: Workspace,
                  result: str) -> None:
    """Start the review(s) of ``worker``'s reported branch; when no reviewer
    can start, hand the branch to the supervisor."""
    try:
        second = second_profile(db, cfg, worker, ws)
        # With a second review both calls carry a concrete profile, so the
        # reviewer dedupe compares profiles and neither can swallow the other.
        first = agents.default_review_profile(cfg, worker, db, ws.repo_root, worker.task) if second else None
        started = agents.start_review(db, parent, ws, first, None, cfg, slot="")
        if second:
            try:
                agents.start_review(db, parent, ws, second, None, cfg, second=True)
            except agents.AgentError:
                if started is not None:
                    agents.close(db, started.id)   # don't leave a lone first review running
                raise
    except agents.AgentError as e:
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent.id,
              agents.condense(
                  f"[brindle pipeline] {worker.id} reported on `{ws.branch}`, but no reviewer could "
                  f"start ({e}). Review it yourself with workspace_diff, then merge_workspace. "
                  f"Its report: {agents.first_line(result)}", result), worker.id)


def restart_review(db: DB, worker: Agent, ws: Workspace, since: float) -> bool:
    """#186/#190: ``worker`` reported and its branch has waited in review
    since ``since`` with nothing at work on it: no check run (stopped with
    its worker, or crashed), no reviewer, no verdict. The caller has seen to
    the last two. Start the review again, once per such wait, unless a check
    run for it is still at work. Returns whether it was started."""
    if worker.review_restarted == since or review_running(ws.id):
        return False
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    except ValueError:
        return False
    parent = db.get_agent(worker.parent_id) if worker.parent_id else None
    if parent is None or not enabled(cfg, worker, ws):
        return False
    with agents.review_dispatch_lock(ws.id):
        current = db.get_agent(worker.id)
        if (current is None or current.pipeline != "reviewing"
                or current.reviewing_since != worker.reviewing_since
                or current.review_restarted == since or review_running(ws.id)):
            return False
        # A short configured delay can expire before the detached helper's
        # launch grace. Reusing that claim must not consume its one recovery.
        try:
            second = second_profile(db, cfg, current, ws)
            first = agents.default_review_profile(cfg, current, db, ws.repo_root, current.task) if second else None
            profile = agents._review_profile(db, ws, first, cfg)
            sha = gates.head(ws)
            if agents._live_reviewer(db, ws, sha, profile) or (
                    cfg.checks and agents._review_pending(ws.id, profile, sha)):
                return False
        except agents.AgentError:
            pass   # _start_review reports an unavailable reviewer through the usual escalation
        db.update_agent(worker.id, review_restarted=since)
        _start_review(db, cfg, parent, current, ws, current.result or "")
        return True


def review_claim(db: DB, ws: Workspace) -> float | None:
    """Which report the review of ``ws`` now starting is for: its piped
    worker's ``reviewing_since``, or None when the branch isn't in review."""
    worker = piped_worker(db, ws)
    return worker.reviewing_since if worker is not None and worker.pipeline == "reviewing" else None


def still_claimed(db: DB, ws: Workspace, since: float | None) -> bool:
    """Whether ``ws``'s branch is still in review for the report ``since``
    (see ``review_claim``): False once a newer report took it over (that one
    has a check run of its own), its review was settled, or it isn't piped."""
    return since is not None and review_claim(db, ws) == since


def on_moved_during_checks(db: DB, ws: Workspace) -> None:
    """A reported branch kept getting new commits, with no new report, while
    its checks ran (#168): hand it to the supervisor rather than leave it in
    review with nobody reviewing it."""
    worker = piped_worker(db, ws)
    if worker is None:
        return
    db.update_agent(worker.id, pipeline=None)
    _escalate(db, ws, worker)
    _tell(db, worker.parent_id,
          f"[brindle pipeline] `{ws.branch}` kept getting new commits while its checks ran, with "
          "no new report, so it hasn't been reviewed. This needs you: once it settles, "
          f"request_review(\"{ws.id}\"), then merge_workspace(\"{ws.id}\").", worker.id)


# -- stage 2: the review came in ---------------------------------------------------


def piped_worker(db: DB, ws: Workspace) -> Agent | None:
    """The worker whose branch the pipeline is handling in ``ws``: the latest
    worker there that is in the pipeline's hands (a conflict-resolution
    worker started in a finished worker's worktree, say), else the one
    whose task produced the code."""
    workers = sorted((a for a in db.list_agents(ws.id) if a.mode in WORKER_MODES),
                     key=lambda a: a.created_at)
    piped = [a for a in workers if a.pipeline]
    return piped[-1] if piped else (workers[0] if workers else None)


def on_review(db: DB, reviewer: Agent, ws: Workspace, approved: bool, summary: str) -> bool:
    """Act on a verdict for a piped branch. Returns whether the pipeline
    handled it (so the verdict isn't forwarded to the supervisor as a
    message). A verdict that arrives after its branch merged (a second
    reviewer of the same commit, a repeated submit_review) is dropped.

    An approval marks the branch ready and then drains every ready branch
    of the same base (see ``drain``), with no workspace lock held: the
    drain takes each branch's lock itself, and a caller holding one while
    waiting for the drain would deadlock with it."""
    with merge_lock(ws.id):
        handled, ready = _on_review(db, reviewer, ws, approved, summary)
    if ready:
        drain(db, ws.repo_root, ws.base_branch, reviewer=reviewer)
    return handled


SMALL_DIFF_LINES = 40
SMALL_DIFF_FILES = 3


def _small_diff_stat(ws: Workspace) -> str | None:
    """The ``git diff --stat`` output when ``ws`` has a small, non-empty diff
    against its base (at most ``SMALL_DIFF_LINES`` changed lines across at most
    ``SMALL_DIFF_FILES`` files); None otherwise."""
    if not ws.base_branch or not os.path.isdir(ws.path):
        return None
    try:
        mb = git.merge_base(ws.path, git.base_ref(ws.path, ws.base_branch))
        numstat = git.out(["diff", "--numstat", mb], ws.path).splitlines()
        untracked = git.paths(["ls-files", "--others", "--exclude-standard"], ws.path)
        if untracked or not numstat or len(numstat) > SMALL_DIFF_FILES:
            return None
        lines = 0
        for row in numstat:
            parts = row.split("\t", 2)
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                return None
            lines += int(parts[0]) + int(parts[1])
        if lines == 0 or lines > SMALL_DIFF_LINES:
            return None
        return git.out(["diff", "--stat", mb], ws.path).strip()
    except git.GitError:
        return None


def _cached_check_summary(db: DB, ws: Workspace, cfg: RepoConfig, reviewer: Agent) -> str | None:
    """Stored check results for ``ws``'s commit without re-running any
    command: from ``reviewer.task`` if ``review_after_checks`` already put
    them there, else from ``check_cache``."""
    if reviewer.task:
        marker = "The repo's checks have already run on this commit:\n\n"
        end_marker = "\n\nTake these results into account; don't run the whole suite yourself."
        if marker in reviewer.task and end_marker in reviewer.task:
            body = reviewer.task.split(marker, 1)[1].split(end_marker, 1)[0].strip()
            if body:
                return body
    if not os.path.isdir(ws.path):
        return None
    try:
        sha = reviewer.review_sha or gates.head(ws)
    except git.GitError:
        return None
    rows = db.conn.execute(
        "SELECT command, ok, output FROM check_cache WHERE workspace_id=? AND sha=?",
        (ws.id, sha),
    ).fetchall()
    if not rows:
        return None
    by_cmd = {r["command"]: (bool(r["ok"]), r["output"] or "") for r in rows}
    order = list(cfg.checks) + [r["command"] for r in rows if r["command"] not in cfg.checks]
    lines = [
        f"PASS `{c}`" if by_cmd[c][0] else f"FAIL `{c}`\n{by_cmd[c][1]}".rstrip()
        for c in order if c in by_cmd
    ]
    return f"Checks for {ws.branch} at {sha[:8]}:\n\n" + "\n".join(lines)


def _partial_findings(reviewer: Agent) -> str | None:
    """The stalled reviewer's partial findings or last transcript/screen
    words, if any."""
    from brindle import rewind

    words = rewind.turn_words(reviewer)
    if words:
        return words
    path = agents.transcript_file(reviewer)
    if not path:
        return None
    return rewind._last_assistant_text(path)


def _repo_pins_reviewer(repo_root: str | None) -> bool:
    """Whether ``repo_root``'s own ``.brindle/config.json`` or
    ``config.local.json`` explicitly sets ``reviewer`` or ``review_profile``."""
    if not repo_root:
        return False
    from brindle.config import CONFIG_DIR, CONFIG_FILE, LOCAL_CONFIG_FILE, _read_json, config_root

    base = config_root(repo_root) / CONFIG_DIR
    for name in (LOCAL_CONFIG_FILE, CONFIG_FILE):
        try:
            raw = _read_json(base / name)
        except ValueError:
            continue
        if "review_profile" in raw or "reviewer" in raw:
            return True
    return False


def _fallback_reviewer(cfg: RepoConfig, reviewer: Agent,
                       repo_root: str | None = None) -> tuple[str, str]:
    """Which reviewer profile to run for the fallback and why: keeps an
    explicitly pinned ``cfg.review_profile`` / ``cfg.reviewer``; otherwise
    routes to the cheapest available reviewer tier (reusing weight/price
    tiers, and including ``reviewer-local`` only when configured by the repo),
    falling back to ``cfg.reviewer``."""
    if cfg.review_profile:
        return cfg.review_profile, f"using {cfg.review_profile} (pinned by review_profile)"
    default = cfg.reviewer or "reviewer"
    if default != "reviewer" or _repo_pins_reviewer(repo_root):
        return default, f"using {default} (pinned by reviewer config)"
    from brindle import autopilot, pricing, profiles

    allow_local = "reviewer-local" in (
        *cfg.learning_candidates,
        *cfg.second_review.values(),
        *cfg.routing.get("light", ()),
    )
    names = [
        p.name for p in profiles.list_profiles(repo_root)
        if (p.name == "reviewer" or p.name.startswith("reviewer-"))
        and (allow_local or p.name != "reviewer-local")
    ]
    for extra in (*cfg.routing.get("light", ()), *cfg.learning_candidates):
        if extra.startswith("reviewer") and extra not in names and (allow_local or extra != "reviewer-local"):
            names.append(extra)
    rank_of = {"light": 0, "medium": 1, "heavy": 2, "advanced": 3}
    scored: list[tuple[tuple[int, float, int], str, str]] = []
    for name in names:
        if repo_root and autopilot._unavailable(name, cfg, repo_root) is not None:
            continue
        tier = autopilot.profile_weight(name, cfg, repo_root)
        price = pricing.profile_price(name, repo_root)
        rate = (price.input + price.output) if price is not None else float("inf")
        scored.append(((rank_of.get(tier, 1), rate, 0 if name == default else 1), name, tier))
    if scored:
        scored.sort(key=lambda item: item[0])
        _, best, tier = scored[0]
        info = autopilot.model_cost_info(best, cfg, repo_root)
        return best, f"using {best}{info} (cheapest available {tier} reviewer tier)"
    tier = autopilot.profile_weight(default, cfg, repo_root)
    return default, f"using {default} ({tier} reviewer tier; no other reviewer available)"


def _supervisor_review_summary(ws: Workspace, worker: Agent, reviewer: Agent,
                               diff_stat: str | None, check_summary: str | None,
                               partial: str | None) -> str:
    parts = [
        f"[brindle pipeline] review of `{ws.branch}` never came back: reviewer {reviewer.id} "
        f"stopped without a verdict. Review it yourself with workspace_diff, then "
        f"merge_workspace(\"{ws.id}\")."
    ]
    if diff_stat:
        parts.append(f"Diff stat:\n{diff_stat}")
    if check_summary:
        parts.append(f"Check results:\n{check_summary}")
    if partial:
        parts.append(f"Reviewer {reviewer.id}'s partial findings:\n{partial}")
    if worker.result:
        parts.append(f"Worker's report:\n{worker.result}")
    return "\n\n".join(parts)


def on_reviewer_unreported(db: DB, reviewer: Agent, ws: Workspace) -> bool:
    """A reviewer of a piped branch stopped without a verdict, which would
    leave its worker in ``pipeline='reviewing'`` for good. Escalates only as
    needed:
    1. Liveness: ignore if a command is still running or its transcript was
       written to recently.
    2. Resume the same reviewer once with a short verdict prompt (messaging a
       live idle reviewer, or reopening a dead reviewer's own recorded session).
    3. If step 2 could not resume the reviewer (or the resumed turn also
       stopped without a verdict) and the diff is small, hand the supervisor a
       ready-made summary instead of spawning another reviewer.
    4. Otherwise start one fallback review on the cheapest available reviewer
       tier (unless the repo pins one), reusing the commit's cached check
       results and the first reviewer's partial findings."""
    import time

    worker = piped_worker(db, ws)
    if worker is None or worker.pipeline != "reviewing":
        return False
    if agents.command_running(db, reviewer) or (
        agents.is_alive(reviewer) and agents.transcript_active(reviewer)
    ):
        return False
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    except ValueError:
        cfg = RepoConfig()
    if not os.path.isdir(ws.path):
        learning.note_reviewer(db, cfg, reviewer, ws, worker=worker)
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, worker.parent_id,
              f"[brindle pipeline] review of `{ws.branch}` never came back: reviewer {reviewer.id} "
              f"stopped without a verdict, and the branch's worktree {ws.path} no longer exists, "
              f"so nothing is left to review or merge. remove_workspace(\"{ws.id}\") clears "
              "its record.", reviewer.id)
        return True
    tried = any(a.id != reviewer.id and a.mode == "review" and a.result is None
                and a.unreported_noted and a.review_sha == reviewer.review_sha
                for a in db.list_agents(ws.id))
    now = time.time()
    in_flight_queued = (
        reviewer.reopened_at is not None
        and agents.is_alive(reviewer)
        and db.pending_count(reviewer.id) > 0
        and now - reviewer.reopened_at < agents.TRANSCRIPT_ACTIVE_SECONDS
    )
    if not tried and (not reviewer.reopened_at or in_flight_queued):
        try:
            if agents.reopen_reviewer(db, reviewer, ws):
                # Clearing unreported_noted lets tell_parent_unreported re-enter
                # on_reviewer_unreported if this resumed turn also stops without
                # a verdict, while reopened_at prevents resuming a second time
                # (and respects a queued reminder while it is still in flight).
                db.update_agent(reviewer.id, reopened_at=reviewer.reopened_at or now,
                                unreported_noted=None)
                return True
        except agents.AgentError:
            pass
    if not reviewer.unreported_noted:
        db.update_agent(reviewer.id, unreported_noted=int(now))
    learning.note_reviewer(db, cfg, reviewer, ws, worker=worker)
    check_summary = _cached_check_summary(db, ws, cfg, reviewer)
    partial = _partial_findings(reviewer)
    diff_stat = _small_diff_stat(ws)
    if diff_stat is not None:
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        agents.close_later(reviewer.id)
        _tell(db, worker.parent_id,
              _supervisor_review_summary(ws, worker, reviewer, diff_stat, check_summary, partial),
              reviewer.id)
        return True
    parent = db.get_agent(worker.parent_id) if worker.parent_id else None
    if not tried and parent is not None:
        profile, reason = _fallback_reviewer(cfg, reviewer, ws.repo_root)
        focus_parts = [f"Fallback review ({reason})."]
        if partial:
            focus_parts.append(
                f"Earlier reviewer {reviewer.id} ({reviewer.profile}) stopped without a verdict; "
                f"its partial findings:\n{partial}"
            )
        try:
            agents.request_review(
                db, parent, ws, profile, focus="\n\n".join(focus_parts),
                cfg=cfg, check_summary=check_summary,
                second=bool(_review_slot(db, reviewer)),   # in the stopped reviewer's place
                slot="" if db.review_slot(reviewer.id) == "" else None,
            )
            agents.close_later(reviewer.id)
            return True
        except agents.AgentError:
            pass
    db.update_agent(worker.id, pipeline=None)
    _escalate(db, ws, worker)
    _tell(db, worker.parent_id,
          _supervisor_review_summary(ws, worker, reviewer, None, check_summary, partial),
          reviewer.id)
    return True


def has_supervisor_action(summary: str) -> bool:
    """Parse review summary for actionable supervisor findings.
    Distinguishes empty or negated sections (e.g. 'Needs supervisor action: None.')
    from actual items requiring escalation."""
    # A blocker the reviewer calls out of scope for the worker (a bug in code
    # its task doesn't cover) is the same escalation: another fix round
    # can't settle it. That one only counts as a finding label ("Out of
    # scope: ..." opening a line), not as a phrase in prose ("not out of
    # scope"). Each section is judged on its own: an empty one mustn't hide
    # an actionable one after it.
    starts = [m.start(1) for m in re.finditer(
        r"(?:^|[^\w])needs[-_ ]*supervisor(?:[-_ ]*action)?\s*[:\n\-*]*\s*(.*)", summary, re.IGNORECASE)]
    starts += [m.start(1) for m in re.finditer(
        r"^[ \t>*#\-]*out[-_ ]*of[-_ ]*scope\*{0,2}[ \t]*(?::|\n)[\s*]*(.*)",
        summary, re.IGNORECASE | re.MULTILINE)]
    return any(_actionable_section(summary[start:]) for start in starts)


def _actionable_section(rest: str) -> bool:
    """Whether the section text starting at ``rest`` has something for the supervisor."""
    next_sec = re.search(r"\n\s*(?:#+\s*|[A-Z][a-zA-Z\s]{1,20}:)", rest)
    section_text = rest[:next_sec.start()] if next_sec else rest
    cleaned = re.sub(r"[^\w]+", " ", section_text).strip().lower()
    negations = {
        "", "none", "n a", "na", "nil", "nothing", "no", "no action",
        "no action needed", "no action required", "none needed", "none required",
        "not needed", "false", "none identified", "none observed", "none reported",
        "n a none"
    }
    if cleaned in negations:
        return False
    first_sentence = re.split(r"[.\n;]", section_text.strip())[0]
    first_cleaned = re.sub(r"[^\w]+", " ", first_sentence).strip().lower()
    if first_cleaned and first_cleaned in negations:
        return False
    # "None — all fixable by the worker.", "No items.": a section that opens
    # with a negation has nothing for the supervisor, whatever follows it.
    if re.match(r"(?:none|no|n a|na|nil|nothing)\b", cleaned):
        return False
    return True


def _on_review(db: DB, reviewer: Agent, ws: Workspace, approved: bool,
               summary: str) -> tuple[bool, bool]:
    """(handled, ready): whether the pipeline took the verdict, and whether
    the branch is now ready to merge (so the caller drains)."""
    # Read under the lock: an earlier verdict may have merged the branch, or
    # handed it to the supervisor, while this one waited.
    if _settled(db, ws):
        worker = piped_worker(db, ws)
        if worker is not None and worker.pipeline:
            db.update_agent(worker.id, pipeline=None)
        return True, False
    worker = piped_worker(db, ws)
    if worker is None:
        return False, False
    parent_id = worker.parent_id
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    except ValueError:
        cfg = RepoConfig()
    if not worker.pipeline:
        # #217: a worker that never reported (no brindle connection, say)
        # whose branch the supervisor had reviewed itself. Its approval is
        # the pipeline's to merge like any other, once the worker is done.
        if not (approved and worker.result is None and ws.kind == "worktree" and parent_id):
            return False, False
        try:
            standing = gates.verdict(db, ws.id, gates.head(ws))
        except git.GitError:
            standing = None
        if standing is None or not standing.approved:
            return False, False   # the verdicts on record don't approve it: the supervisor's call
        if not enabled(cfg, worker, ws):
            # Not the pipeline's to merge (a handoff, or "pipeline" off): say so.
            _tell(db, parent_id,
                  agents.condense(
                      f"[brindle] `{ws.branch}` approved; {worker.id} never reported, and brindle "
                      f"doesn't merge it on its own here. This needs you: merge it with "
                      f"merge_workspace(\"{ws.id}\").",
                      f"Review ({reviewer.id}): approved.\n{summary}"), reviewer.id)
            return True, False
        if busy_worker(db, ws, None):
            _tell(db, parent_id,
                  f"[brindle pipeline] `{ws.branch}` approved, but {worker.id} never reported and "
                  f"is still running in its worktree, so it wasn't merged. This needs you: merge it with "
                  f"merge_workspace(\"{ws.id}\") once {worker.id} is done.", reviewer.id)
            return True, False
        db.update_agent(worker.id, pipeline="reviewing")
        worker = db.get_agent(worker.id)
    report = worker.result or ""
    if approved:
        if not cfg.auto_merge_default_branch and ws.base_branch \
                and ws.base_branch == git.default_branch(ws.repo_root):
            db.update_agent(worker.id, pipeline=None)
            _escalate(db, ws, worker)
            _tell(db, parent_id,
                  agents.condense(
                      f"[brindle pipeline] `{ws.branch}` approved, base `{ws.base_branch}` is the default "
                      f"branch, so it wasn't merged. This needs you: merge_workspace(\"{ws.id}\")",
                      "To merge into the default branch automatically, set \"merge_into\" to another "
                      "branch, or \"auto_merge_default_branch\": true, in .brindle/config.json.\n\n"
                      f"Worker: {agents.first_line(report)}",
                      f"Worker's report:\n{report}",
                      f"Review ({reviewer.id}): approved.\n{summary}"), worker.id)
            return True, False
        # Ready means: the review on record for the branch's current commit
        # approves it. The verdict argument alone doesn't make it so: the
        # worker may have committed since the reviewer read the branch, or
        # a later verdict may have overruled this one.
        try:
            recorded = gates.verdict(db, ws.id, gates.head(ws))
        except git.GitError:
            recorded = None
        if recorded is not None and not recorded.approved:
            # Another reviewer's standing request for changes (#216): its own
            # verdict sent the branch back to the worker, or to the supervisor.
            return True, False
        if recorded is None:
            db.update_agent(worker.id, pipeline=None)
            _escalate(db, ws, worker)
            _tell(db, parent_id,
                  agents.condense(
                      f"[brindle pipeline] `{ws.branch}` approved but not merged. This needs you: "
                      f"request_review(\"{ws.id}\") again, then merge_workspace(\"{ws.id}\")",
                      f"Why: {reviewer.id} approved, but its latest commit hasn't been reviewed."
                      f"\n\nWorker: {agents.first_line(report)}",
                      f"Worker's report:\n{report}",
                      f"Review ({reviewer.id}): approved.\n{summary}"), worker.id)
            return True, False
        if second_review_pending(db, cfg, worker, ws):
            return True, False   # the other reviewer's verdict decides; both must approve
        db.update_agent(worker.id, pipeline="ready")
        return True, True
    needs_sup = has_supervisor_action(summary)
    rounds = (worker.pipeline_rounds or 0) + 1
    if not needs_sup and rounds <= cfg.review_rounds:
        db.update_agent(worker.id, pipeline="fixing", pipeline_rounds=rounds)
        fix = (f"Review of your branch (round {rounds} of {cfg.review_rounds}) requested "
               f"changes:\n\n{summary}\n\nFix what's valid, commit, and call report_result "
               "again; brindle will have it reviewed again.")
        if agents.is_alive(worker):
            try:
                agents.send_message(db, worker.id, fix, sender_id=reviewer.id)
                return True, False
            except agents.AgentError:
                pass
        # It exited once it reported (a worker usually has by the time a
        # review comes back), or just before the message reached it: its fix
        # round starts it again on its branch.
        current = db.get_agent(worker.id) or worker
        if not agents.is_alive(current) and agents.reopen_worker(db, current, ws, fix):
            return True, False
    if not needs_sup and _retry_on_heavy(db, worker, ws, cfg,
                       f"the review still asked for changes after {rounds - 1} fix round(s): "
                       f"{summary}", report):
        return True, False
    db.update_agent(worker.id, pipeline=None)
    _escalate(db, ws, worker)
    reason = "needs supervisor action" if needs_sup else f"still has review findings after {rounds - 1} fix round(s)"
    _tell(db, parent_id,
          agents.condense(
              f"[brindle pipeline] `{ws.branch}` {reason}. This needs you: decide what to do. "
              f"Worker: {agents.first_line(report)}",
              f"Latest review ({reviewer.id}):\n{summary}",
              f"Worker's last report:\n{report}"), worker.id)
    return True, False


# -- stage 3: merging the ready branches, in order ----------------------------------


def ready_branches(db: DB, repo_root: str, base: str | None) -> list[tuple[Agent, Workspace]]:
    """Approved branches of ``base`` in ``repo_root`` that the pipeline has
    yet to merge, oldest first."""
    found = []
    for ws in db.find_workspaces(repo_root):
        if ws.kind != "worktree" or ws.base_branch != base:
            continue
        worker = piped_worker(db, ws)
        if worker is not None and worker.pipeline == "ready":
            found.append((worker, ws))
    found.sort(key=lambda pair: pair[1].created_at)
    return found


def merge_order(ready: list[tuple[Agent, Workspace]]) -> list[tuple[Agent, Workspace]]:
    """``ready`` in the order to merge them: fewest overlapping hunks first
    (see conflicts.merge_order). One branch needs no ordering."""
    if len(ready) < 2:
        return list(ready)
    by_id = {ws.id: (worker, ws) for worker, ws in ready}
    ordered = conflicts.merge_order([(ws, conflicts.hunks(ws)) for _w, ws in ready])
    return [by_id[ws.id] for ws in ordered]


def drain(db: DB, repo_root: str, base: str | None, reviewer: Agent | None = None) -> list[str]:
    """Merge every ready branch of ``base``: the one whose hunks overlap the
    others' least first, then each of the rest synced onto the new tip (so
    its conflicts, if any, are its own) and merged in turn. A sync that
    conflicts hands the branch to a worker to resolve, or to the person for
    a protected path (see ``_resolve_conflict``); the review and the checks
    run again on the resolved branch before it merges. One drain per base at
    a time, across processes; a second one finds nothing left and returns.
    ``reviewer`` is the agent running this code, whose session a removal
    must not take down. Returns one line per branch handled."""
    lines: list[str] = []
    key = hashlib.sha1(f"{repo_root}\0{base or ''}".encode()).hexdigest()[:16]
    with _file_lock("drain-" + key):
        while True:
            ready = ready_branches(db, repo_root, base)
            if not ready:
                return lines
            # Branches that become ready while these merge are picked up by
            # the next round, synced onto whatever tip the round left.
            for worker, ws in merge_order(ready):
                lines.append(_finish(db, worker, ws, reviewer))


def _finish(db: DB, worker: Agent, ws: Workspace, reviewer: Agent | None) -> str:
    """Merge one ready branch and settle what the result means for its worker
    and its supervisor. Under the branch's lock from the first look at its
    state: a branch that stopped being ready since it was listed (its worker
    reported new commits, a supervisor merged it by hand) is left alone."""
    with merge_lock(ws.id), gates.marker("merge-run", ws.id):
        current = db.get_agent(worker.id)
        if current is None or current.pipeline != "ready":
            return f"Skipped {ws.branch}: no longer ready."
        return _finish_locked(db, current, ws, reviewer)


def _finish_locked(db: DB, worker: Agent, ws: Workspace, reviewer: Agent | None) -> str:
    parent_id = worker.parent_id
    parent = db.get_agent(parent_id) if parent_id else None
    try:
        cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    except ValueError:
        cfg = RepoConfig()
    report = worker.result or ""
    try:
        review = gates.verdict(db, ws.id, gates.head(ws))
    except git.GitError:
        review = None
    reviewed_by = review.reviewer_id if review and review.reviewer_id else (reviewer.id if reviewer else "?")
    verdict = f"Review ({reviewed_by}): approved.\n{review.summary or ''}" if review else ""
    conflicting: list[str] = []
    told: list[str] = []

    def waiting_on_checks(sha: str) -> None:
        # #217: an approved branch whose merge waits on its checks mustn't look stuck.
        if told:
            return
        told.append(sha)
        wait = agents.expected_check_wait(db, ws, cfg)
        usually = f" (usually {wait / 60:.0f} min, plus any wait for a check slot)" if wait else ""
        _tell(db, parent_id,
              f"[brindle pipeline] `{ws.branch}` was approved; its merge is waiting on the repo's "
              f"checks{usually}, for {sha[:8]}. Nothing to do: brindle merges it when they pass, "
              "and tells you either way.", worker.id)

    text = _try_merge(db, parent, ws, conflicting, waiting_on_checks)
    if text.startswith("Not merged. "):
        # A gate failed on an approved branch. That has been a flaky check
        # often enough (#186) to try once more before anyone is asked.
        text = _try_merge(db, parent, ws, conflicting, waiting_on_checks)
    if text.startswith((ALREADY_MERGED, GONE)):
        if db.get_agent(worker.id):
            db.update_agent(worker.id, pipeline=None)   # reported when it merged; nothing to add
    elif text.startswith("Merged"):
        # This may run inside the approving reviewer's own process, and
        # removing its workspace must not take that process down half way:
        # record and announce everything first, then remove without killing
        # the session the reviewer is in.
        db.update_agent(worker.id, pipeline=None)
        keep = reviewer if reviewer is not None and reviewer.workspace_id == ws.id else None
        if keep is not None:
            db.set_status(keep.id, "done")
        in_use = _still_in_use(db, ws)
        if in_use:
            _tell(db, parent_id,
                  agents.condense(
                      f"[brindle pipeline] {text} Kept the worktree and its worker: {in_use}. "
                      f"Its next report merges again. Worker: {agents.first_line(report)}",
                      f"Worker's report:\n{report}", verdict), worker.id)
            return text
        _tell(db, parent_id,
              agents.condense(
                  f"[brindle pipeline] {text} Removing the worktree. Worker: "
                  f"{agents.first_line(report)}",
                  f"Worker's report:\n{report}", verdict), worker.id)
        note = _remove(db, ws, keep=keep)
        if not note.startswith("("):
            note_removed_merged(db, ws, actor=keep or reviewer, worker=worker)
        if note.startswith("("):
            _tell(db, parent_id, f"[brindle pipeline] {ws.branch}: {note}", worker.id)
    elif conflicting:
        _resolve_conflict(db, worker, ws, cfg, conflicting, report)
    else:
        latest = db.get_agent(worker.id)
        if latest is not None and latest.pipeline not in (None, "ready"):
            return text   # the worker moved on meanwhile (a new report): its new state stands
        if text.startswith("Not merged. ") and _retry_on_heavy(db, worker, ws, cfg, text, report):
            return text   # the checks failed: a stronger model gets one go at it
        db.update_agent(worker.id, pipeline=None, merge_failed=text)
        _escalate(db, ws, worker)
        # The supervisor sees the first line: it carries the action, and the
        # whole failure follows in the details rather than being cut short.
        _tell(db, parent_id,
              agents.condense(
                  f"[brindle pipeline] `{ws.branch}` was approved but couldn't be merged "
                  f"({agents.first_line(text)}). This needs you: fix it (or have the worker fix it "
                  f"with send_message), then merge_workspace(\"{ws.id}\"). "
                  f"Worker: {agents.first_line(report)}",
                  f"Why it wasn't merged:\n{text}",
                  f"Worker's report:\n{report}"), worker.id)
    return text


def _try_merge(db: DB, parent: Agent | None, ws: Workspace, conflicting: list[str],
               waiting_on_checks=None) -> str:
    try:
        return merge(db, parent, ws, conflicts_out=conflicting, waiting_on_checks=waiting_on_checks)
    except workspaces.WorkspaceError as e:   # one branch's trouble mustn't stall the others
        return f"Not merged: {e}"


def resolution_task(ws: Workspace, files: list[str], cfg: RepoConfig) -> str:
    """The brief for whoever resolves ``ws``'s conflict with its base."""
    base = ws.base_branch
    shown = ", ".join(files)
    tests = f" Then run the repo's checks ({'; '.join(cfg.checks)})." if cfg.checks else ""
    return (
        f"Merge conflict: `{base}` has moved on since `{ws.branch}` was approved, and merging "
        f"it into your branch conflicts in: {shown}.\n\n"
        f"In {ws.path}, run `git merge {base}`, resolve the conflicts in those files so that "
        f"both sides' intent survives (keep what `{base}` changed and what this branch "
        f"changed; don't drop either), make sure no conflict markers remain, run the tests "
        f"that cover those files{tests} and commit the merge. Then call report_result again: "
        "brindle will have the branch reviewed and checked again, and merge it. "
        "Don't change anything else."
    )


def _light_profile(db: DB, parent: Agent | None, ws: Workspace, cfg: RepoConfig,
                   task: str, files: list[str]) -> str:
    if parent is None:
        return cfg.default_agent
    try:
        name, _ = autopilot.choose_profile(db, parent.id, ws.repo_root, None, task, files, "light")
        return name
    except autopilot.AutopilotError:
        return cfg.default_agent


ESCALATION_PROFILE = "developer-heavy"
CLOUD_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def fable_enabled(cfg: RepoConfig, worker: Agent, ws: Workspace) -> bool:
    """Whether a failed heavy task may be retried on Fable: the repo's
    ``fable_escalation`` when set, else on unless the worker runs on a cloud
    account (its profile's env, or brindle's own, selects Bedrock, Vertex or
    Foundry), where Fable may not be enabled."""
    if cfg.fable_escalation is not None:
        return bool(cfg.fable_escalation)
    env = dict(os.environ)
    try:
        from brindle.profiles import load_profile

        env.update(load_profile(worker.profile, ws.repo_root).env or {})
    except Exception:  # noqa: BLE001 - an unreadable profile adds nothing
        pass
    return not any(env.get(k) not in (None, "", "0", "false") for k in CLOUD_ENV)


def _retry_on_heavy(db: DB, worker: Agent, ws: Workspace, cfg: RepoConfig, why: str,
                    report: str) -> bool:
    """A heavy task's worker failed (the review still asks for changes after
    its rounds, or the checks fail): start ``developer-heavy`` on the same
    branch, once, instead of handing the branch to a person. False when the
    task isn't heavy, Fable isn't enabled, this was already the retry, or no
    worker could start; the caller then escalates as before."""
    if worker.profile == ESCALATION_PROFILE or not fable_enabled(cfg, worker, ws):
        return False
    if cfg.max_weight and cfg.max_weight in ("light", "medium"):
        return False
    if ESCALATION_PROFILE not in cfg.routing.get("heavy", []):
        return False
    weight = next((t.weight for t in db.list_tasks(ws.repo_root) if t.agent_id == worker.id), None)
    if weight != "heavy":
        return False
    parent = db.get_agent(worker.parent_id) if worker.parent_id else None
    task = (f"{worker.task or ''}\n\nA first attempt by {worker.profile} did not get through: "
            f"{why}\n\nThe branch `{ws.branch}` holds its work in this worktree. Continue from it: "
            "fix what is wrong, run the tests that cover your change, commit, and call "
            "report_result. brindle reviews and checks the branch again before it merges.").strip()
    # The retry works in the failed worker's worktree: two agents must never
    # write to one, so the original goes first, and the retry waits on that.
    if agents.is_alive(worker):
        try:
            agents.close(db, worker.id)
        except agents.AgentError:
            return False
        if agents.is_alive(db.get_agent(worker.id) or worker):
            return False
    try:
        retry, _ws = agents.delegate(db, parent, ws, ESCALATION_PROFILE, task, "assign",
                                     isolate=False, done_when=worker.done_when, plan_first=False)
    except agents.AgentError:
        return False
    db.update_agent(worker.id, pipeline=None)
    db.update_agent(retry.id, pipeline="fixing", pipeline_rounds=0)
    _tell(db, worker.parent_id,
          agents.condense(
              f"[brindle pipeline] `{ws.branch}` (heavy) failed on {worker.profile}: "
              f"{agents.first_line(why)} Retrying once on {ESCALATION_PROFILE} ({retry.id}) in "
              "the same worktree; it is reviewed and checked again before it merges. Nothing to "
              "do unless that fails too.",
              f"Why: {why}", f"Worker's last report:\n{report}"), worker.id)
    return True


def _resolve_conflict(db: DB, worker: Agent, ws: Workspace, cfg: RepoConfig,
                      files: list[str], report: str) -> None:
    """``ws``'s branch conflicts with its base. A protected path goes to the
    person; otherwise the branch's own worker is asked to resolve it if it
    is still running, else a light worker is started in its worktree. Either
    way the branch leaves 'ready': its next report starts a fresh review."""
    parent_id = worker.parent_id
    base = ws.base_branch
    guarded = conflicts.protected(files, cfg.protected_paths)
    if guarded:
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent_id,
              agents.condense(
                  f"[brindle pipeline] `{ws.branch}` was approved, but merging `{base}` into it "
                  f"conflicts in protected path{'s' if len(guarded) > 1 else ''}: "
                  f"{', '.join(guarded)} (\"protected_paths\" in .brindle/config.json). brindle "
                  "never resolves those itself. This needs you: resolve the conflict yourself, or "
                  f"decide who should (in {ws.path}: `git merge {base}`, resolve, commit), then "
                  f"request_review and merge_workspace(\"{ws.id}\"). Worker: "
                  f"{agents.first_line(report)}",
                  f"All conflicting files: {', '.join(files)}",
                  f"Worker's report:\n{report}"), worker.id)
        return
    task = resolution_task(ws, files, cfg)
    if agents.is_alive(worker):
        db.update_agent(worker.id, pipeline="resolving")
        try:
            agents.send_message(db, worker.id, task, sender_id=None)
            _tell(db, parent_id,
                  f"[brindle pipeline] `{ws.branch}` was approved, but merging `{base}` into it "
                  f"conflicts in: {', '.join(files)}. Asked its worker {worker.id} to resolve the "
                  "conflict; the branch is reviewed and checked again before it merges. "
                  "Nothing to do unless that fails.", worker.id)
            return
        except agents.AgentError:
            pass
    parent = db.get_agent(parent_id) if parent_id else None
    profile = _light_profile(db, parent, ws, cfg, task, files)
    done_when = (f"`git merge {base}` is resolved and committed on `{ws.branch}` with no conflict "
                 "markers left, the tests covering the conflicting files pass, and report_result "
                 "was called")
    try:
        resolver, _ws = agents.delegate(db, parent, ws, profile, task, "assign", isolate=False,
                                        done_when=done_when, plan_first=False)
    except agents.AgentError as e:
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent_id,
              agents.condense(
                  f"[brindle pipeline] `{ws.branch}` was approved, but merging `{base}` into it "
                  f"conflicts in: {', '.join(files)}, its worker {worker.id} has finished, and no "
                  f"worker could be started to resolve it ({e}). This needs you: resolve it (in "
                  f"{ws.path}: `git merge {base}`, resolve, commit) or assign someone, then "
                  f"request_review and merge_workspace(\"{ws.id}\"). Worker: "
                  f"{agents.first_line(report)}",
                  f"Worker's report:\n{report}"),
              worker.id)
        return
    db.update_agent(worker.id, pipeline=None)
    db.update_agent(resolver.id, pipeline="resolving")
    _tell(db, parent_id,
          f"[brindle pipeline] `{ws.branch}` was approved, but merging `{base}` into it conflicts "
          f"in: {', '.join(files)}. Its worker {worker.id} has finished, so a light worker "
          f"({resolver.id}, {profile}) is resolving the conflict in its worktree; the branch is "
          "reviewed and checked again before it merges. Nothing to do unless that fails.",
          worker.id)


def _still_in_use(db: DB, ws: Workspace) -> str | None:
    """Why ``ws`` must outlive the merge of its branch (#170): a worker there
    took on more since its report (a turn that started after it, or messages
    it hasn't taken yet), or the worktree holds uncommitted changes. None
    when it can be removed."""
    for a in db.list_agents(ws.id):
        if a.mode not in WORKER_MODES or not agents.is_alive(a):
            continue
        if db.pending_count(a.id):
            return f"{a.id} has messages it hasn't taken yet"
        a = agents.reconcile(db, a, samples=1)
        if a.status in ("processing", "waiting") and a.reviewing_since is not None \
                and (a.status_since or 0) > a.reviewing_since:
            return f"{a.id} is at work again since its report"
    try:
        dirty = git.dirty_files(ws.path)
    except git.GitError:
        dirty = []
    if dirty:
        return "it has uncommitted changes (" + ", ".join(dirty[:8]) + (" ..." if len(dirty) > 8 else "") + ")"
    return None


def _remove(db: DB, ws: Workspace, keep: Agent | None = None) -> str:
    """Remove ``ws`` while ``keep`` (the agent running this code) survives:
    the other agents' windows are stopped, but the session ``keep`` is in is
    left for its own close."""
    try:
        if keep is not None:
            for a in db.list_agents(ws.id):
                if a.id != keep.id and agents.is_alive(a):
                    agents._stop(db, a)
        removed = workspaces.remove(db, ws, force=False, keep_session=keep is not None)
        return f"Worktree removed; {removed.branch_note or 'branch kept'}."
    except workspaces.WorkspaceError as e:
        return f"(worktree kept: {e})"


# -- the merge itself, shared with the merge_workspace tool -------------------------


def busy_worker(db: DB, ws: Workspace, exclude_id: str | None) -> Agent | None:
    """A live worker (not a reviewer) still at work in ``ws``, other than
    ``exclude_id``. A worker whose result is recorded is finished even if
    its Stop hook hasn't fired yet."""
    modes = tuple(m for m in agents.REPORTING_MODES if m != "review")
    for a in db.list_agents(ws.id):
        if a.id == exclude_id or a.mode not in modes or a.result is not None:
            continue
        if not agents.is_alive(a):
            continue
        a = agents.reconcile(db, a, samples=1)
        if a.status in ("processing", "waiting"):
            return a
    return None


def merge(db: DB, caller: Agent | None, ws: Workspace, squash: bool = False,
          conflicts_out: list[str] | None = None, waiting_on_checks=None) -> str:
    """Sync the branch with its base, run the merge gates, and merge. The
    reply starts with "Merged" on success, else "Not merged: ...". Merging is
    idempotent: a branch that already merged at its current commit gets
    "Already merged: ...", and one whose worktree is gone "Nothing to merge:
    ...", without a gate or a merge being run. When the sync with the base
    conflicts, the conflicting files are appended to ``conflicts_out``.
    ``waiting_on_checks(sha)`` is called before checks with no passing
    result for the commit yet start to run.
    One merge into a base at a time (see ``base_lock``)."""
    with merge_lock(ws.id), base_lock(ws.repo_root, ws.base_branch):
        return _merge(db, caller, ws, squash, conflicts_out, waiting_on_checks)


def _merge(db: DB, caller: Agent | None, ws: Workspace, squash: bool,
           conflicts_out: list[str] | None = None, waiting_on_checks=None) -> str:
    settled = _settled(db, ws)
    if settled:
        return settled
    cfg = load_repo_config(ws.repo_root, branch=ws.base_branch)
    pilot = autopilot.for_agent(db, caller.id) if caller else None
    review = cfg.review if cfg.review is not None else bool(pilot and pilot.enabled)

    verdict = policy.check_merge(cfg, ws, agents.workspace_worker(db, ws), caller)
    if not verdict.allowed:
        return f"Not merged: the repo's policy refused it: {verdict.reason}"
    try:
        behind, _ahead = git.ahead_behind(ws.path, workspaces.require_base(ws))
        if behind:
            busy = busy_worker(db, ws, caller.id if caller else None)
            if busy:
                return (f"Not merged: {busy.id} is still working on {ws.branch}; "
                        "retry once it reports.")
        sync_result = workspaces.sync_with_base(ws)
    except (git.GitError, workspaces.WorkspaceError) as e:
        return _settled(db, ws) or f"Not merged: {e}"
    if sync_result.status == "conflict":
        if conflicts_out is not None:
            conflicts_out.extend(sync_result.conflicts or [])
        files = ", ".join(sync_result.conflicts) or "?"
        return (f"Not merged: {ws.branch} conflicts with {ws.base_branch} in: {files}. "
                f"Ask the worker to merge {ws.base_branch} and resolve.")
    if sync_result.status == "synced" and review:
        # The branch's own commits are unchanged: an approval of them
        # carries over the merge commit, and the checks below still run
        # on the merged result. Only an unreviewed branch needs a review.
        prior = gates.standing_reviews(db, ws.id, sync_result.old_sha) if sync_result.old_sha else []
        if prior and all(r.approved for r in prior):
            for r in prior:   # each reviewer's, so a later verdict of any of them still counts (#216)
                db.add_review(ws.id, sync_result.new_sha, r.reviewer_id, True,
                              f"Carried over from the approved review of {sync_result.old_sha[:8]}: "
                              f"{ws.base_branch} merged in cleanly (commit {sync_result.new_sha[:8]}), "
                              "and the checks run on the merged result below.")
        else:
            return (f"Not merged: synced {ws.branch} with {ws.base_branch} "
                    f"(new commit {sync_result.new_sha[:8]}); request_review again, then merge.")

    # A worktree removed while the gates ran in it (remove_workspace doesn't
    # wait for this lock) fails them for no reason of the branch's: that is
    # "gone", not a failed check.
    try:
        if waiting_on_checks is not None and cfg.checks:
            sha = gates.head(ws)
            if any(not (c := db.get_check(ws.id, sha, cmd)) or not c.ok for cmd in cfg.checks):
                waiting_on_checks(sha)
        report = gates.run(db, ws, cfg, review_required=review)
        if not report.ok:
            return _settled(db, ws) or f"Not merged. {report.problem}"
        if gates.head(ws) != report.sha:
            return (f"Not merged: {ws.branch} got new commits while the gates ran. "
                    "Call merge_workspace again to check the new commits.")
        # git.merge_into serializes merges into one checkout (checkout_lock).
        target = workspaces.merge_back(db, ws, squash=squash)
    except (git.GitError, workspaces.WorkspaceError) as e:
        return _settled(db, ws) or f"Not merged: {e}"
    db.set_merged(ws.id, report.sha)
    text = f"Merged {ws.branch} into {ws.base_branch} at {target} ({report.summary()})."
    history.record_safely(
        db, ws.repo_root, "merge", agent=caller, with_usage=True, branch=ws.branch,
        task=f"merge {ws.branch} into {ws.base_branch}", result=text,
    )
    _note(db, ws, merged=True, checks_passed=True, actor=caller)
    tasks.on_merged(db, ws)
    codemap.refresh_later(target, repo_root=ws.repo_root)
    doc_note = codemap_docs.docs_refresh_note(ws, target=target)
    if doc_note:
        text += f" {doc_note}"
    if pilot:
        db.bump_progress(pilot.root_id)
        if pilot.goal:
            text += " Next: call check_milestone to verify progress."
    return text
