"""The pipeline: review and merge a reported branch without the supervisor.

Every stage between a worker's report and its merge used to wait for a
supervisor turn: report, turn, review, verdict, turn, merge, turn, remove.
With a supervisor carrying millions of tokens of context, each turn is
seconds to minutes, and it sits on the critical path of every branch.

With ``pipeline`` on (the default), brindle runs those stages itself:

1. A worker reports (``report_result``). brindle starts the review at once
   and runs the repo's checks in the background.
2. The reviewer approves: the branch is ready. brindle merges every ready
   branch of the same base (through the same gates ``merge_workspace``
   uses), the one whose hunks overlap the others' least first and the rest
   synced onto the new tip in turn, removes each worktree, and sends the
   supervisor one message per branch: merged, with the worker's report and
   the review.
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
import threading
from contextlib import contextmanager

from brindle import (
    agents, autopilot, codemap, conflicts, events, gates, git, history, learning, policy, savings,
    tasks, workspaces,
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
def _file_lock(name: str):
    from brindle.config import brindle_home

    lock_dir = brindle_home() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{name}.lock", "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
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
    held = getattr(_held, "ids", None)
    if held is None:
        held = _held.ids = set()
    if ws_id in held:
        yield
        return
    with _file_lock("merge-" + hashlib.sha1(ws_id.encode()).hexdigest()[:16]):
        held.add(ws_id)
        try:
            yield
        finally:
            held.discard(ws_id)


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
    ``approved=``, ``escalated=True`` or ``merged=`` (see learning.note);
    the events plugin hears it as a review, escalated, merge or remove
    event from ``actor``."""
    try:
        cfg = load_repo_config(ws.repo_root)
        worker = worker or agents.workspace_worker(db, ws)
    except Exception:
        return
    try:
        learning.note(db, cfg, worker, ws, **event)
    except Exception:
        pass
    savings.note_outcome(db, worker, **event)
    if event.get("approved") is not None:
        events.emit(cfg, "review", ws, worker, actor=actor, approved=event["approved"])
    if event.get("escalated"):
        events.emit(cfg, "escalated", ws, worker, actor=actor)
    if event.get("merged") is True:
        events.emit(cfg, "merge", ws, worker, actor=actor)
    elif event.get("merged") is False:
        events.emit(cfg, "remove", ws, worker, actor=actor, merged=False)


def note_review(db: DB, ws: Workspace, approved: bool, reviewer: Agent | None = None) -> None:
    _note(db, ws, approved=approved, actor=reviewer)


def note_removed_unmerged(db: DB, ws: Workspace, actor: Agent | None = None) -> None:
    _note(db, ws, merged=False, actor=actor)


def note_removed_merged(db: DB, ws: Workspace, actor: Agent | None = None,
                        worker: Agent | None = None) -> None:
    """A worktree whose branch had merged is being removed: an event only
    (the learner heard about the merge). Pass ``worker`` when the
    workspace's records are already gone."""
    try:
        cfg = load_repo_config(ws.repo_root)
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
    reviews = db.reviews_at(ws.id, gates.head(ws))
    if not reviews or not all(r.approved for r in reviews) or len(reviews) < 2:
        return True   # one is missing, or one asked for changes (the worker is fixing it)
    reviewers = [db.get_agent(r.reviewer_id) for r in reviews]
    return not any(a is not None and a.profile == profile for a in reviewers)


def on_report(db: DB, worker: Agent, ws: Workspace, result: str) -> bool:
    """Start the review of a reported branch. Returns whether the pipeline
    took the report (so the caller doesn't forward it to the supervisor)."""
    try:
        cfg = load_repo_config(ws.repo_root)
    except ValueError:
        return False
    if not enabled(cfg, worker, ws):
        return False
    parent = db.get_agent(worker.parent_id) if worker.parent_id else None
    if parent is None:
        return False
    if git.dirty_files(ws.path):
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent.id,
              f"[brindle pipeline] {worker.id} reported on `{ws.branch}` but left uncommitted "
              f"changes, so it can't be reviewed or merged. Its report:\n\n{result}", worker.id)
        return True
    try:
        second = second_profile(db, cfg, worker, ws)
        # With a second review both calls carry a concrete profile, so the
        # reviewer dedupe compares profiles and neither can swallow the other.
        first = agents.default_review_profile(cfg, worker, db, ws.repo_root, worker.task) if second else None
        started = agents.start_review(db, parent, ws, first, None, cfg)   # with checks: after they finish
        if second:
            try:
                agents.start_review(db, parent, ws, second, None, cfg)
            except agents.AgentError:
                if started is not None:
                    agents.close(db, started.id)   # don't leave a lone first review running
                raise
    except agents.AgentError as e:
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent.id,
              f"[brindle pipeline] {worker.id} reported on `{ws.branch}`, but no reviewer could "
              f"start ({e}). Review it yourself with workspace_diff, then merge_workspace.\n\n"
              f"Its report:\n\n{result}", worker.id)
        return True
    db.update_agent(worker.id, pipeline="reviewing")
    return True


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
    if worker is None or not worker.pipeline:
        return False, False
    parent_id = worker.parent_id
    try:
        cfg = load_repo_config(ws.repo_root)
    except ValueError:
        cfg = RepoConfig()
    report = worker.result or ""
    if approved:
        if not cfg.auto_merge_default_branch and ws.base_branch \
                and ws.base_branch == git.default_branch(ws.repo_root):
            db.update_agent(worker.id, pipeline=None)
            _escalate(db, ws, worker)
            _tell(db, parent_id,
                  f"[brindle pipeline] `{ws.branch}` was approved, but its base `{ws.base_branch}` "
                  "is the repo's default branch, which brindle doesn't merge into on its own. "
                  f"This needs you: merge_workspace(\"{ws.id}\") when you're ready to merge it "
                  "(set \"merge_into\" to another branch, or \"auto_merge_default_branch\": true, "
                  f"in .brindle/config.json to change this).\n\nWorker's report:\n{report}\n\n"
                  f"Review ({reviewer.id}): approved.\n{summary}", worker.id)
            return True, False
        # Ready means: the review on record for the branch's current commit
        # approves it. The verdict argument alone doesn't make it so: the
        # worker may have committed since the reviewer read the branch, or
        # a later verdict may have overruled this one.
        try:
            recorded = db.latest_review(ws.id, gates.head(ws))
        except git.GitError:
            recorded = None
        if recorded is None or not recorded.approved:
            db.update_agent(worker.id, pipeline=None)
            _escalate(db, ws, worker)
            why = ("its latest commit hasn't been reviewed" if recorded is None
                   else "a later review of that commit requested changes")
            _tell(db, parent_id,
                  f"[brindle pipeline] {reviewer.id} approved `{ws.branch}`, but {why}, so it "
                  f"wasn't merged. This needs you: request_review(\"{ws.id}\") again, then "
                  f"merge_workspace(\"{ws.id}\").\n\nWorker's report:\n{report}\n\n"
                  f"Review ({reviewer.id}): approved.\n{summary}", worker.id)
            return True, False
        if second_review_pending(db, cfg, worker, ws):
            return True, False   # the other reviewer's verdict decides; both must approve
        db.update_agent(worker.id, pipeline="ready")
        return True, True
    rounds = (worker.pipeline_rounds or 0) + 1
    if rounds <= cfg.review_rounds and agents.is_alive(worker):
        db.update_agent(worker.id, pipeline="fixing", pipeline_rounds=rounds)
        try:
            agents.send_message(
                db, worker.id,
                f"Review of your branch (round {rounds} of {cfg.review_rounds}) requested "
                f"changes:\n\n{summary}\n\nFix what's valid, commit, and call report_result "
                "again; brindle will have it reviewed again.",
                sender_id=reviewer.id,
            )
            return True, False
        except agents.AgentError:
            pass
    if _retry_on_heavy(db, worker, ws, cfg,
                       f"the review still asked for changes after {rounds - 1} fix round(s): "
                       f"{summary}", report):
        return True, False
    db.update_agent(worker.id, pipeline=None)
    _escalate(db, ws, worker)
    _tell(db, parent_id,
          f"[brindle pipeline] `{ws.branch}` still has review findings after {rounds - 1} fix "
          f"round(s). This needs you: decide what to do.\n\nLatest review ({reviewer.id}):\n"
          f"{summary}\n\nWorker's last report:\n{report}", worker.id)
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
    with merge_lock(ws.id):
        current = db.get_agent(worker.id)
        if current is None or current.pipeline != "ready":
            return f"Skipped {ws.branch}: no longer ready."
        return _finish_locked(db, current, ws, reviewer)


def _finish_locked(db: DB, worker: Agent, ws: Workspace, reviewer: Agent | None) -> str:
    parent_id = worker.parent_id
    parent = db.get_agent(parent_id) if parent_id else None
    try:
        cfg = load_repo_config(ws.repo_root)
    except ValueError:
        cfg = RepoConfig()
    report = worker.result or ""
    try:
        review = db.latest_review(ws.id, gates.head(ws))
    except git.GitError:
        review = None
    reviewed_by = review.reviewer_id if review and review.reviewer_id else (reviewer.id if reviewer else "?")
    verdict = f"Review ({reviewed_by}): approved.\n{review.summary or ''}" if review else ""
    conflicting: list[str] = []
    try:
        text = merge(db, parent, ws, conflicts_out=conflicting)
    except workspaces.WorkspaceError as e:   # one branch's trouble mustn't stall the others
        text = f"Not merged: {e}"
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
        _tell(db, parent_id,
              f"[brindle pipeline] {text} Removing the worktree.\n\nWorker's report:\n{report}"
              f"\n\n{verdict}", worker.id)
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
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent_id,
              f"[brindle pipeline] `{ws.branch}` was approved but couldn't be merged: {text}\n"
              "This needs you: fix it (or have the worker fix it with send_message), then "
              f"merge_workspace(\"{ws.id}\").\n\nWorker's report:\n{report}", worker.id)
    return text


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
          f"[brindle pipeline] `{ws.branch}` (heavy) failed on {worker.profile}: {why} Retrying "
          f"once on {ESCALATION_PROFILE} ({retry.id}) in the same worktree; it is reviewed and "
          "checked again before it merges. Nothing to do unless that fails too.\n\n"
          f"Worker's last report:\n{report}", worker.id)
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
              f"[brindle pipeline] `{ws.branch}` was approved, but merging `{base}` into it "
              f"conflicts in protected path{'s' if len(guarded) > 1 else ''}: "
              f"{', '.join(guarded)} (\"protected_paths\" in .brindle/config.json). brindle never "
              "resolves those itself. This needs you: resolve the conflict yourself, or decide "
              f"who should (in {ws.path}: `git merge {base}`, resolve, commit), then "
              f"request_review and merge_workspace(\"{ws.id}\").\n\nAll conflicting files: "
              f"{', '.join(files)}\n\nWorker's report:\n{report}", worker.id)
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
              f"[brindle pipeline] `{ws.branch}` was approved, but merging `{base}` into it "
              f"conflicts in: {', '.join(files)}, its worker {worker.id} has finished, and no "
              f"worker could be started to resolve it ({e}). This needs you: resolve it (in "
              f"{ws.path}: `git merge {base}`, resolve, commit) or assign someone, then "
              f"request_review and merge_workspace(\"{ws.id}\").\n\nWorker's report:\n{report}",
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
          conflicts_out: list[str] | None = None) -> str:
    """Sync the branch with its base, run the merge gates, and merge. The
    reply starts with "Merged" on success, else "Not merged: ...". Merging is
    idempotent: a branch that already merged at its current commit gets
    "Already merged: ...", and one whose worktree is gone "Nothing to merge:
    ...", without a gate or a merge being run. When the sync with the base
    conflicts, the conflicting files are appended to ``conflicts_out``."""
    with merge_lock(ws.id):
        return _merge(db, caller, ws, squash, conflicts_out)


def _merge(db: DB, caller: Agent | None, ws: Workspace, squash: bool,
           conflicts_out: list[str] | None = None) -> str:
    settled = _settled(db, ws)
    if settled:
        return settled
    cfg = load_repo_config(ws.repo_root)
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
    except git.GitError as e:
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
        prior = db.latest_review(ws.id, sync_result.old_sha) if sync_result.old_sha else None
        if prior and prior.approved:
            db.add_review(ws.id, sync_result.new_sha, prior.reviewer_id, True,
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
        report = gates.run(db, ws, cfg, review_required=review)
        if not report.ok:
            return _settled(db, ws) or f"Not merged. {report.problem}"
        if gates.head(ws) != report.sha:
            return (f"Not merged: {ws.branch} got new commits while the gates ran. "
                    "Call merge_workspace again to check the new commits.")
        # git.merge_into serializes merges into one checkout (checkout_lock).
        target = workspaces.merge_back(db, ws, squash=squash)
    except git.GitError as e:
        return _settled(db, ws) or f"Not merged: {e}"
    db.set_merged(ws.id, report.sha)
    text = f"Merged {ws.branch} into {ws.base_branch} at {target} ({report.summary()})."
    history.record_safely(
        db, ws.repo_root, "merge", agent=caller, with_usage=True, branch=ws.branch,
        task=f"merge {ws.branch} into {ws.base_branch}", result=text,
    )
    _note(db, ws, merged=True, checks_passed=True, actor=caller)
    tasks.on_merged(db, ws)
    codemap.refresh_later(ws.repo_root)
    if pilot:
        db.bump_progress(pilot.root_id)
        if pilot.goal:
            text += " Next: call check_milestone to verify progress."
    return text
