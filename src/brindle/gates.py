"""Merge gates: what must hold before an agent may merge a worker's branch.

Checked by the ``merge_workspace`` MCP tool, in the worker's worktree:
1. Everything is committed.
2. A reviewer agent approved this exact commit (``review`` in the repo config;
   by default only in autopilot sessions). New commits need a new review.
3. pre-commit (the framework) passes over the branch's changes, when the repo
   has a ``.pre-commit-config.yaml``. Plain git hooks already ran when each
   commit was made.
4. The branch passes the worker's profile rule packs (``brindle.rule_checks``).
5. Every command in ``checks`` exits 0.

brindle runs these itself, so "done" means verified, not just claimed. People
merging with ``brindle merge`` aren't gated: that's their own call.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import time
from contextlib import contextmanager

import shutil
from dataclasses import dataclass, field

from brindle import autopilot, git, workspaces
from brindle.config import RepoConfig
from brindle.db import DB, Workspace


@dataclass
class Report:
    ok: bool = True
    passed: list[str] = field(default_factory=list)
    problem: str | None = None
    sha: str | None = None        # the commit the gates checked

    def fail(self, problem: str) -> "Report":
        self.ok, self.problem = False, problem
        return self

    def summary(self) -> str:
        return "; ".join(self.passed) if self.passed else "no gates configured"


def head(ws: Workspace) -> str:
    return git.out(["rev-parse", "HEAD"], ws.path)


Abandoned = autopilot.CheckCancelled   # raised when a run's ``cancel`` turns true


def run_checked(db: DB, ws: Workspace, cmd: str, env: dict[str, str], timeout: int,
                cancel=None) -> tuple[bool, str]:
    """Run ``cmd`` in ``ws``, or reuse the cached PASSING result for the same
    (workspace, HEAD sha, command) if the tree was clean when that result was
    cached. A dirty tree always runs fresh and is never cached, since the
    result then reflects more than just the commit at ``sha``. A failure or
    timeout is never cached either, so a retry always re-runs it; and a
    result is only cached if the sha and clean state still hold *after* the
    command ran, in case it took long enough for something else to commit or
    leave files behind.

    ``cancel`` (optional) is polled while waiting for the lock or a slot and
    while the command runs; once it is true the run is given up and
    ``Abandoned`` raised, so a run nobody wants any more frees its slot.

    A process that waited on the lock while the holder failed or timed out
    reuses that outcome instead of re-running the whole command."""
    sha = head(ws)
    dirty = bool(git.dirty_files(ws.path))
    if dirty:
        return _run_queued(db, ws, cmd, env, timeout, cancel)
    # One run at a time per (workspace, commit, command): a check warmed when
    # the worker reported, a reviewer's summary and the merge gate can all
    # want the same result at once; the later ones wait, then reuse it.
    waiting_since = time.time()
    with _check_lock(ws.id, sha, cmd, cancel) as result_file:
        cached = db.get_check(ws.id, sha, cmd)
        if cached is not None and cached.ok:
            return True, cached.output or ""
        if (shared := _read_outcome(result_file, waiting_since)) is not None:
            return shared
        ok, out = _run_queued(db, ws, cmd, env, timeout, cancel)
        if head(ws) == sha and not git.dirty_files(ws.path):
            if ok:
                db.set_check(ws.id, sha, cmd, ok, out)
            else:
                _write_outcome(result_file, ok, out)
        return ok, out


def _read_outcome(path, since: float) -> tuple[bool, str] | None:
    """The failure another process recorded after ``since`` (while we waited
    for the lock), else None."""
    try:
        data = json.loads(path.read_text())
        if data["finished"] >= since:
            return bool(data["ok"]), str(data["output"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_outcome(path, ok: bool, output: str) -> None:
    try:
        path.write_text(json.dumps({"ok": ok, "output": output, "finished": time.time()}))
    except OSError:
        pass


# -- the queue: check runs share the machine -------------------------------------

SLOT_POLL = 1.0          # seconds between tries for a free slot
SLOW_FACTOR = 2.0        # a run is "far past" its last duration at this multiple of it...
SLOW_MIN_EXTRA = 60.0    # ...and at least this many seconds over
SLOW_TIMEOUT_FRACTION = 0.75   # but never later than this share of check_timeout


def _concurrency(ws: Workspace) -> int:
    from brindle.config import load_repo_config

    try:
        return int(load_repo_config(ws.repo_root).check_concurrency)
    except (ValueError, TypeError):
        return RepoConfig().check_concurrency


@contextmanager
def _check_slot(limit: int, cancel=None):
    """Hold one of ``limit`` machine-wide slots for a check run, waiting for
    one to come free: several branches' full suites at once swap the machine,
    and every run then takes hours. The slots are lock files under the brindle
    home, so every brindle process (the merge gate, ``_review-after-checks``,
    ``_warm-checks``, milestone checks) shares them, and a process that dies
    gives its slot back. ``limit`` 0 or less means no cap."""
    if limit <= 0:
        yield
        return
    from brindle.config import brindle_home

    lock_dir = brindle_home() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    files = [open(lock_dir / f"check-slot-{i}.lock", "w") for i in range(limit)]
    try:
        held = False
        while not held:
            for f in files:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                held = True
                break
            else:
                if cancel is not None and cancel():
                    raise Abandoned("slot")
                time.sleep(SLOT_POLL)
        yield
    finally:
        for f in files:
            f.close()   # closing releases the slot


def _run_queued(db: DB, ws: Workspace, cmd: str, env: dict[str, str], timeout: int,
                cancel=None) -> tuple[bool, str]:
    """Run ``cmd`` once a slot is free (``timeout`` covers the run, not the
    wait). A run that goes far past the command's last passing duration in
    this repo is reported to the supervisor while it is still running."""
    with _check_slot(_concurrency(ws), cancel):
        last = db.check_duration(ws.repo_root, cmd)
        watch = _watch_slow(db, ws, cmd, last, timeout) if last is not None else None
        started = time.monotonic()
        try:
            if cancel is None:
                ok, out = autopilot.run_check(cmd, ws.path, env, timeout)
            else:
                ok, out = autopilot.run_check(cmd, ws.path, env, timeout, cancel=cancel)
        finally:
            if watch:
                watch.cancel()
        if ok:
            db.set_check_duration(ws.repo_root, cmd, time.monotonic() - started)
        return ok, out


def slow_after(last: float) -> float:
    """Seconds into a run at which it counts as far past its last duration."""
    return max(last * SLOW_FACTOR, last + SLOW_MIN_EXTRA)


def _watch_slow(db: DB, ws: Workspace, cmd: str, last: float, timeout: int) -> threading.Timer | None:
    # Warn before check_timeout ends the run, even if the usual "far past" mark
    # lies beyond it: at ~75% of the timeout at the latest.
    after = min(slow_after(last), SLOW_TIMEOUT_FRACTION * timeout)
    if db.path == ":memory:":
        return None
    timer = threading.Timer(after, _report_slow, args=(db.path, ws, cmd, last, after, timeout))
    timer.daemon = True
    timer.start()
    return timer


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f}s"


def _report_slow(db_path: str, ws: Workspace, cmd: str, last: float, after: float, timeout: int) -> None:
    """Tell ``ws``'s supervisor a check run is far past its last duration.
    Runs on a timer thread, so with its own database connection; never raises."""
    try:
        db = DB(db_path)
        to_id = supervisor_id(db, ws)
        if to_id is None:
            return
        tell(db, to_id,
             f"[brindle] `{cmd}` on `{ws.branch}` has been running for {_minutes(after)}; its last "
             f"passing run took {_minutes(last)}. The machine may be overloaded or the run stuck. "
             f"It is stopped at check_timeout ({timeout}s) either way; look for stray test "
             "processes, or lower \"check_concurrency\" in .brindle/config.json.")
    except Exception:  # noqa: BLE001 - a notice must never break the check run
        pass


def supervisor_id(db: DB, ws: Workspace) -> str | None:
    """Who hears about ``ws``'s checks: its worker's parent, else (the
    supervisor's own checkout) the interactive agent in it."""
    from brindle import agents

    worker = agents.workspace_worker(db, ws)
    if worker and worker.parent_id and db.get_agent(worker.parent_id):
        return worker.parent_id
    for a in db.list_agents(ws.id):
        if a.mode == "interactive":
            return a.id
    return None


def tell(db: DB, to_id: str, text: str, sender_id: str | None = None) -> None:
    """Send ``text`` to an agent, or leave it queued for its next Stop when
    it can't be reached right now."""
    from brindle import agents, tmux

    try:
        agents.send_message(db, to_id, text, sender_id=sender_id)
    except (agents.AgentError, tmux.TmuxError):
        db.enqueue(to_id, text, sender_id)


@contextmanager
def _check_lock(ws_id: str, sha: str, cmd: str, cancel=None):
    """Exclusive lock for one (workspace, sha, cmd); yields the path of the
    file where a holder leaves a failed outcome for the waiters. With
    ``cancel``, waiting for the lock is given up (``Abandoned``) once it is true."""
    from brindle.config import brindle_home

    key = hashlib.sha1(f"{ws_id}\0{sha}\0{cmd}".encode()).hexdigest()[:16]
    lock_dir = brindle_home() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"check-{key}.lock", "w") as f:
        if cancel is None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if cancel():
                        raise Abandoned("lock")
                    time.sleep(SLOT_POLL)
        try:
            yield lock_dir / f"check-{key}.result"
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


MAX_FAILURE_CHARS = 4_000


def summary_failed(summary: str) -> bool:
    """Whether a ``check_summary`` reports a failing check."""
    return any(line.startswith("FAIL `") for line in summary.splitlines())


def rule_summary(db: DB, ws: Workspace) -> str:
    """The worker's rule packs checked against the branch, as PASS/FAIL lines
    in the form of ``check_summary``; empty when the worker has no packs."""
    from brindle import rule_checks

    if db is None:   # no agents to look up a worker in (a bare check run)
        return ""
    result = rule_checks.run(db, ws)
    return result.summary() if result else ""


def check_summary(db: DB, ws: Workspace, cfg: RepoConfig, cancel=None) -> str:
    """Run the worker's rule packs and each of ``cfg.checks`` (cached by sha)
    and produce a short pass/fail summary for a reviewer, with output only
    for the ones that failed, capped so one big failure can't blow up the
    reviewer's prompt."""
    rules = rule_summary(db, ws)
    if not cfg.checks:
        return rules
    env = workspaces.workspace_env(ws)
    lines = [rules] if rules else []
    budget = MAX_FAILURE_CHARS
    for cmd in cfg.checks:
        if cancel is None:
            ok, out = run_checked(db, ws, cmd, env, cfg.check_timeout)
        else:
            ok, out = run_checked(db, ws, cmd, env, cfg.check_timeout, cancel=cancel)
        if ok:
            lines.append(f"PASS `{cmd}`")
            continue
        if budget <= 0:
            lines.append(f"FAIL `{cmd}` (output omitted; failure budget spent)")
            continue
        # Keep the tail, not the head: run_check's own exit-code marker is the
        # last line, and that's the part a reviewer needs most.
        shown = out if len(out) <= budget else "... (truncated)\n" + out[-budget:]
        budget -= len(shown)
        lines.append(f"FAIL `{cmd}`\n{shown}")
    return "\n".join(lines)


def run(db: DB, ws: Workspace, cfg: RepoConfig, *, review_required: bool) -> Report:
    r = Report()
    dirty = git.dirty_files(ws.path)
    if dirty:
        shown = ", ".join(dirty[:8]) + (" ..." if len(dirty) > 8 else "")
        return r.fail(f"{ws.name} has uncommitted changes ({shown}). The worker must commit them first.")

    sha = r.sha = head(ws)
    if review_required:
        review = db.latest_review(ws.id, sha)
        if review is None:
            return r.fail(
                f"{ws.branch} at {sha[:8]} hasn't been reviewed. Call request_review "
                f"(workspace=\"{ws.id}\") and merge once it approves."
            )
        if not review.approved:
            return r.fail(
                f"The review of {ws.branch} at {sha[:8]} requested changes:\n{review.summary or ''}\n"
                "Send these to the worker, then request another review."
            )
        r.passed.append(f"review approved {sha[:8]}")

    if cfg.pre_commit and (problem := _pre_commit(db, ws, cfg)):
        return r.fail(problem)
    if cfg.pre_commit and _has_pre_commit(ws) and shutil.which("pre-commit"):
        r.passed.append("pre-commit passed")

    from brindle import rule_checks

    rules = rule_checks.run(db, ws)
    if problem := rule_checks.gate_problem(rules, ws.branch):
        return r.fail(problem)
    if rules:
        r.passed.append("rules passed (" + ", ".join(rules.packs) + ")")

    env = workspaces.workspace_env(ws)
    for cmd in cfg.checks:
        ok, out = run_checked(db, ws, cmd, env, cfg.check_timeout)
        if not ok:
            return r.fail(f"Check failed in {ws.branch}:\n{out}\nSend this to the worker to fix.")
        r.passed.append(f"`{cmd}` passed")
    return r


def _has_pre_commit(ws: Workspace) -> bool:
    from pathlib import Path

    return (Path(ws.path) / ".pre-commit-config.yaml").is_file()


def _pre_commit(db: DB, ws: Workspace, cfg: RepoConfig) -> str | None:
    """Run pre-commit over the files the branch changes. Returns a problem, or None."""
    if not _has_pre_commit(ws) or not shutil.which("pre-commit"):
        return None
    base = workspaces.require_base(ws)
    start = git.merge_base(ws.path, git.base_ref(ws.path, base))
    ok, out = run_checked(
        db, ws, f"pre-commit run --from-ref {start} --to-ref HEAD",
        workspaces.workspace_env(ws), cfg.check_timeout,
    )
    if ok:
        return None
    return f"pre-commit hooks failed on {ws.branch}:\n{out}\nSend this to the worker to fix."
