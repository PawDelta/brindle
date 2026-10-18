"""Culling: stopping what's left over, and closing workers nobody needs.

A sweep, run by the sidebar every minute, when ``brindle`` starts, and by
``brindle prune``, does two things:

1. Leftover processes. Any process belonging to an agent that shouldn't be
   running (paused, done, closed, forgotten, or whose tmux window is gone,
   for instance after tmux itself went away, or now belongs to a newer agent:
   see agents.owns_pane) is stopped (see brindle.procs). An
   agent whose window vanished while it was marked running is recorded the
   way ``agents.pause`` would have: paused, so ``brindle continue`` can still
   bring it back, or done if it had already reported.

2. Stale workers. A worker that reported and has sat idle for ``stale_after``
   minutes (repo config, default 30; 0 turns this off), with nothing queued
   for it, is closed: stopped and hidden from the sidebar. So is a worker
   whose window has been gone that long. Closing never touches its worktree
   or branch, so its supervisor can still review and merge the work.

It also stops the Ollama server brindle started once no running session uses
it (see brindle.native.serve.stop_unused).

Interactive agents (supervisor chats) are never closed here; only their
leftover processes are stopped.

Every judgement from a pane list is about the tmux server it came from (see
tmux.current_server and agents.same_server): an agent whose window is on
another server, such as the person's real sessions seen from a demo
recording's private server, is left alone, since its pane id means nothing
here. A sweep run with BRINDLE_TMUX_SOCKET set once stopped three live
workers that way.

A tmux server that isn't answering (tmux.TmuxTimeout, raised by the pane
list rather than returned as "no panes") ends a sweep before it judges
anything: no pane list is not the same as every pane being dead.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path

from brindle import agents, git, procs, tmux
from brindle.config import CONFIG_DIR, WORKTREES_DIR, brindle_home, load_repo_config, worktrees_dir
from brindle.db import DB, Agent, Workspace
from brindle.pro import route_health

# An agent this new may not have its window recorded yet.
LAUNCH_GRACE = 60.0
# How long a message waits for its normal delivery (a hook, the start-up
# flush) before a sweep retries it.
FLUSH_RETRY_AFTER = 30.0


def _root(db: DB, a: Agent) -> str:
    from brindle import autopilot

    return autopilot.root_of(db, a.id)


def _stale_after(db: DB, a: Agent, cache: dict[str, float]) -> float:
    ws = db.get_workspace(a.workspace_id)
    if ws is None:
        return 0.0
    if ws.repo_root not in cache:
        try:
            cache[ws.repo_root] = load_repo_config(ws.repo_root).stale_after * 60.0
        except ValueError:
            cache[ws.repo_root] = 0.0
    return cache[ws.repo_root]


def _usage_paused(db: DB, a: Agent) -> bool:
    """A worker stopped for the Claude usage limit: it comes back on its own."""
    ap = db.get_autopilot(_root(db, a))
    return ap is not None and ap.state == "usage_paused"


def sweep(db: DB, now: float | None = None) -> list[str]:
    """One pass. Returns what it did, one line each."""
    now = time.time() if now is None else now
    panes = tmux.list_panes()
    table = procs.table()
    owners = agents.pane_owners(db, panes)
    done: list[str] = []

    from brindle import codemap

    removed_graphs = codemap.prune_stores(now)
    if removed_graphs:
        done.append(f"removed {removed_graphs} stale graph store(s)")

    # 0. A Claude Code inbox held by an agent that isn't Claude Code is its
    # launcher's: messages to it would go to the supervisor's chat.
    from brindle import inbox

    dropped = inbox.drop_foreign(db)
    if dropped:
        done.append(f"forgot the launcher's inbox recorded for {dropped} agent(s)")

    def alive(a: Agent) -> bool:
        # A pane id a newer agent has since been given isn't this one's.
        return agents.is_alive(a, panes) and agents.owns_pane(db, a, owners)

    def here(a: Agent) -> bool:
        # Only this server's panes can say whether this agent is running.
        return agents.same_server(a, panes)

    # 0b. A chat that ended without pausing its session: killing its tmux
    # session takes down the wrapper that would have (agents._pause_when_done),
    # leaving every agent it started running. Pause the whole session now;
    # step 1 then stops the processes of everything it paused.
    for a in db.list_agents():
        if (a.mode == "interactive" and a.parent_id is None and a.status not in ("paused", "done")
                and a.dismissed_at is None and agents.runs_process(a) and here(a) and not alive(a)
                and now - max(a.created_at, a.status_since or 0) > LAUNCH_GRACE):
            paused = agents.pause(db, a.id, stop_procs=False, stop_local_models=False)
            done.append(f"paused session {a.id} ({len(paused)} agent(s)): its chat is gone")

    # 1. Processes of agents that shouldn't be running.
    leftovers = []
    for aid in procs.all_agent_ids(table):
        a = db.get_agent(aid)
        if a is None or a.status in ("paused", "done") or a.dismissed_at is not None:
            leftovers.append(aid)
        elif (agents.runs_process(a) and here(a) and not alive(a)
              and now - max(a.created_at, a.status_since or 0) > LAUNCH_GRACE):
            db.end_native_subagents(a.id)
            db.set_status(a.id, "done" if a.mode != "interactive" and a.result is not None
                          else "paused")
            agents._note_reviewer_end(db, a)
            leftovers.append(aid)
    if leftovers:
        found = procs.agent_pids(leftovers, procs=table)
        stopped = procs.terminate({p for pids in found.values() for p in pids})
        if stopped:
            done.append(f"stopped {stopped} leftover process(es) of {len(found)} agent(s)")

    # 1b. Autopilot sessions at the Claude usage limit: stop their workers,
    # or bring them back once the window has reset.
    from brindle import autopilot

    done.extend(autopilot.usage_sweep(db, now))

    # 1c. Workers at their dollar budget (brindle Pro): warn, or stop if asked to.
    from brindle import budget

    done.extend(budget.sweep(db, now))

    # 1d. The org's kill switch (Enterprise managed rollout): stop running workers.
    from brindle.pro import rollout

    done.extend(rollout.sweep(db, now))

    # 1e. Cost-center approvals an admin has decided (Enterprise): raise the limit.
    from brindle.pro import cost_centers

    done.extend(cost_centers.poll(only_due=True, now=now))

    # 1f. Agents whose provider's sign-in is gone (or wrong): pause them, and
    # resume them once it's valid again.
    from brindle import signin_pause

    done.extend(signin_pause.sweep(db, now))

    # 1f'. Cloud logins: renewed in the background; a session whose login only
    # the person can renew waits, and they're told once.
    from brindle import login_pause

    done.extend(login_pause.sweep(db, now, panes))

    # 1g. Periodic cost events (Team org_budgets): report delta spend for supervisor and workers.
    from brindle import events

    roots = {ws.repo_root for ws in db.find_workspaces() if ws.repo_root}
    for r in roots:
        try:
            cfg = load_repo_config(r)
            done.extend(events.emit_periodic_cost(cfg, r))
        except Exception:
            pass

    # 2. Workers nobody needs any more.
    limits: dict[str, float] = {}
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or a.dismissed_at is not None
                or not agents.runs_process(a) or not here(a)):
            continue
        if a.status == "paused" and _usage_paused(db, a):
            continue
        limit = _stale_after(db, a, limits)
        idle_for = now - max(a.created_at, a.status_since or 0)
        if limit <= 0 or idle_for <= limit:
            continue
        running = alive(a)
        finished = running and a.result is not None and a.status == "idle" and not db.pending_count(a.id)
        # A stopped worker of a paused session comes back with `brindle
        # continue`; one whose session carried on without it won't.
        root = db.get_agent(_root(db, a))
        gone = not running and (a.status != "paused" or root is None or root.status != "paused")
        if finished or gone:
            agents.close(db, a.id, panes)
            done.append(f"closed {'idle' if finished else 'stopped'} worker {a.id} "
                        f"after {int(idle_for // 60)} min")

    # 2b. Messages a flush left queued because the pane wasn't ready (the
    # person typing, say): an idle agent with no turn-end hook (agy), or one
    # just waiting, would never get them otherwise. flush itself still waits
    # for typed input, a held login, and claims the agent before pasting.
    for a in db.list_agents():
        oldest = db.oldest_pending(a.id)
        if (oldest is not None and now - oldest >= FLUSH_RETRY_AFTER and a.status == "idle"
                and not a.headless and a.dismissed_at is None and agents.runs_process(a)
                and here(a) and alive(a)):
            try:
                if agents.flush(db, a.id):
                    done.append(f"delivered a queued message to {a.id}")
            except (agents.AgentError, tmux.TmuxError):
                pass  # stays queued for the next sweep

    # 3. Launches stopped on a startup prompt; workers stuck on a prompt
    # nobody is answering, or gone silent.
    done.extend(settle_startups(db, now, panes))
    done.extend(note_stuck(db, now, panes))
    done.extend(note_idle_unreported(db, now, panes))
    done.extend(note_reported_stuck(db, now, panes))
    done.extend(note_silent(db, now, panes))
    done.extend(note_model_refused(db, now, panes))

    # 4. Sidebar locks of sessions that are over.
    removed = clean_locks(db, now)
    if removed:
        done.append(f"removed {removed} stale sidebar lock(s)")

    # 5. The Ollama server brindle started, once no running session uses it
    # (a chat whose tmux went away never paused, so nothing else stops it).
    from brindle.native import serve

    done.extend(serve.stop_unused(db))

    # 6. Finished work: merged worktrees and ones whose folder is gone leave
    # the live view, archived (not only when someone runs `brindle prune`).
    done.extend(prune_retired(db, now, cautious=True))
    return done


# How long a worker may sit on a permission or trust prompt before its
# supervisor is told (once per spell of waiting).
STUCK_AFTER = 90.0
# How long after a launch a sweep starts looking at a CLI that hasn't shown
# its input box: past the 30 seconds its own after_launch watches.
STARTUP_GRACE = 40.0


def settle_startups(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Every launch, worker or supervisor, whose CLI hasn't shown its input
    box STARTUP_GRACE seconds on (brindle.prompt_answer keeps them, whatever
    the agent's status says: an argv task starts 'processing', a startup
    menu can read as 'waiting'). If its pane is ready now, or shows a
    startup prompt (an agy worker once sat on a reworded trust prompt for 40
    minutes), run after_launch again: it answers the prompt by what it asks,
    or leaves it for the person and its supervisor is told."""
    from brindle import prompt_answer

    done = []
    owners = agents.pane_owners(db, panes)
    for agent_id, pane, launched_at in prompt_answer.pending(db):
        a = db.get_agent(agent_id)
        if not a or a.result is not None or a.dismissed_at is not None or a.status == "done":
            # It ended before its CLI settled: nothing left to answer.
            db.conn.execute("UPDATE startup_prompts SET pending=0 WHERE agent_id=?", (agent_id,))
            continue
        if (a.tmux_window != pane or now - launched_at < STARTUP_GRACE or a.status == "paused"
                or not agents.same_server(a, panes) or not agents.is_alive(a, panes)
                or not agents.owns_pane(db, a, owners)):
            continue
        provider = agents.get_provider(a.provider)
        try:
            screen = tmux.capture(pane, lines=60, server=agents.server_of(a))
        except tmux.TmuxError:
            continue
        if provider.tool_permission(screen):
            continue  # the permission policy's, never a startup prompt
        if not provider.input_ready(screen) and (not prompt_answer.looks_like_prompt(screen)
                                                 or prompt_answer.cached_kind(screen) == "not_a_prompt"):
            continue
        if prompt_answer.finish_launch(db, a):
            done.append(f"{a.id} past its startup prompts")
    return done


def note_stuck(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once, about a worker that has been waiting on a
    prompt (a permission request, or Claude Code's folder-trust dialog) for
    STUCK_AFTER seconds: it can't go on until someone answers, and nobody
    may be looking at its pane. The screen is read here too, since a trust
    dialog comes up before any hook runs to report it."""
    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or not a.parent_id or a.result is not None
                or a.dismissed_at is not None or a.status not in ("starting", "processing", "waiting")
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        if a.status != "waiting":
            agents.screen_status(db, a, samples=1)
            a = db.get_agent(a.id) or a
        since = a.status_since or a.created_at
        if a.status != "waiting" or now - since < STUCK_AFTER or a.stuck_noted == since:
            continue
        ws = db.get_workspace(a.workspace_id)
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            screen = ""
        tail = "\n".join([ln for ln in screen.rstrip().splitlines() if ln.strip()][-12:])
        where = f" on branch `{ws.branch}`" if ws else ""
        attach = f" Attach with `brindle attach {ws.name}` to answer it," if ws else " Answer it in its pane,"
        pending = agents.pending_permission(db, a.id)
        request = (f"\n\n{pending} Only the user can answer it; tell them exactly what is pending."
                   if pending else "")
        # The supervisor sees a message's first line (agents.one_line), so the
        # prompt itself goes there: which prompt it is says whether it's an
        # access outside the project they can route around.
        prompt = " | ".join(tail.splitlines())
        body = (f"Worker {a.id} ({a.profile}){where} has been waiting on a prompt for "
                f"{int(now - since)}s{': ' + prompt if prompt else ''}\n\n"
                f"It can't continue until someone answers it.{attach} "
                f"or remove the workspace if it's no longer needed.{auto_mode_note(a, ws)}"
                f"{request}\n\nIts screen:\n{tail}")
        try:
            agents.send_message(db, a.parent_id, body, sender_id=a.id)
        except agents.AgentError:
            continue  # its supervisor isn't running; try again on a later sweep
        db.update_agent(a.id, stuck_noted=since)
        done.append(f"told {a.parent_id} that worker {a.id} is stuck on a prompt")
    return done


# How long a worker may sit idle without having reported before its supervisor
# is told (once per idle spell).
IDLE_UNREPORTED_AFTER = 300.0


def note_idle_unreported(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once per idle spell, about a worker that has sat
    idle for IDLE_UNREPORTED_AFTER seconds without reporting, goal or not
    (autopilot's Stop hook only checks on workers when a goal is set). It's
    a reminder: the notice its stop sent may have gone unread, or been lost.

    A worker whose provider has no hook for a turn starting (Codex) stays
    'idle' from its first finished turn on, through any later turns, so it
    also needs a pane that has printed nothing for that long, and the spell
    starts at its last output.

    A worker in a pipeline fix round already has a result from its first
    report, so it counts as unreported while it hasn't reported again since
    the review that asked for fixes (committing doesn't count), and its spell
    starts no earlier than that review."""
    from brindle.providers import get_provider

    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        fix_round = _fix_round_since(db, a) if a.result is not None else None
        # Only these are ever told (a synchronous handoff's caller is waiting).
        if (a.mode not in agents.FORWARDING_MODES or not a.parent_id
                or (a.result is not None and fix_round is None)
                or a.dismissed_at is not None or a.status != "idle"
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        since = max(a.status_since or a.created_at, fix_round or 0)
        provider = get_provider(a.provider)
        if not (provider.uses_hooks and provider.announces_turns):
            last = tmux.window_activity(a.tmux_window, server=agents.server_of(a))
            if last is None:
                continue  # no telling whether a later turn is running
            since = max(since, last)
        # Still changing files (a script or a background command editing its
        # worktree) isn't idle. Commits don't count: see _fix_round_since.
        since = max(since, _last_edit(db.get_workspace(a.workspace_id)))
        if now - since < IDLE_UNREPORTED_AFTER or a.stuck_noted == since:
            continue
        if agents.unreported_note_expired(a):
            db.update_agent(a.id, unreported_noted=None)
        # Not told while a command it started is still running; tried again then.
        if agents.tell_parent_unreported(db, db.get_agent(a.id) or a):
            db.update_agent(a.id, stuck_noted=since)
            done.append(f"told {a.parent_id} that worker {a.id} is idle without reporting")
    return done


def _last_edit(ws: Workspace | None) -> float:
    """When a file with uncommitted changes in ``ws`` was last written; 0 if none."""
    newest = 0.0
    if ws is None or not os.path.isdir(ws.path):
        return newest
    try:
        # -z: paths unquoted (git.dirty_files' would be C-quoted past ASCII),
        # and a rename's source after its destination, as an entry of its own.
        entries = git.run(["status", "--porcelain", "-z", "--untracked-files=all"],
                          ws.path).stdout.split("\0")
    except git.GitError:
        return newest
    source = False
    for entry in entries:
        if source or len(entry) < 4:
            source = False
            continue
        source = entry[0] in "RC"
        try:
            newest = max(newest, os.stat(os.path.join(ws.path, entry[3:])).st_mtime)
        except OSError:
            pass   # deleted
    return newest


# How long a reported branch may wait for a review that nobody started, and how
# long a message may wait for a live worker that isn't taking it, before the
# supervisor is told.
UNPICKED_AFTER = 1800.0
UNDELIVERED_AFTER = 300.0


def note_reported_stuck(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once per spell, about a worker that reported but
    whose work went nowhere: its branch waits in the pipeline for a review no
    reviewer is doing (none running, no verdict for its commit) for
    UNPICKED_AFTER seconds, or twice the repo's checks if they take longer
    (once nothing checks it either, the review is first started again: see
    pipeline.restart_review);
    or it's sitting idle with brindle messages it hasn't taken for
    UNDELIVERED_AFTER seconds. Nobody else would notice either: the
    supervisor waits for a message that never comes."""
    from brindle import gates, pipeline

    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if a.mode not in pipeline.WORKER_MODES or not a.parent_id or a.result is None:
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None:
            continue
        where = f"branch `{ws.branch}`, workspace {ws.id}"
        oldest = db.oldest_pending(a.id)
        if (oldest is not None and now - oldest >= UNDELIVERED_AFTER and a.dismissed_at is None
                and a.status in ("idle", "waiting")
                and agents.same_server(a, panes) and agents.is_alive(a, panes)
                and agents.owns_pane(db, a, owners)):
            since = oldest
            body = (f"Worker {a.id} ({where}) has {db.pending_count(a.id)} brindle message(s) "
                    f"waiting {int((now - oldest) // 60)} min that it hasn't taken. "
                    f"What brindle sees: {agents.liveness(db, a, ws, panes)}. Re-send what it needs "
                    f"with send_message, or replace the worker.")
        elif (a.pipeline == "reviewing" and a.reviewing_since is not None
                and pipeline._settled(db, ws) is None):
            try:
                head = gates.head(ws)
                wait = agents.expected_check_wait(db, ws, load_repo_config(ws.repo_root)) or 0.0
            except (git.GitError, ValueError):
                continue
            # The spell starts at the report that put it in review, or at a
            # later reviewer's start (a fallback review, say).
            reviewers = [r for r in db.list_agents(ws.id) if r.mode == "review"]
            since = max([a.reviewing_since] + [r.created_at for r in reviewers])
            if (now - since < pipeline.RESTART_REVIEW_AFTER or db.latest_review(ws.id, head)
                    or any(r.dismissed_at is None and r.status not in ("paused", "done")
                           and agents.is_alive(r, panes) for r in reviewers)):
                continue
            # Its check run died (#186, #190): start it again before asking anyone.
            if pipeline.restart_review(db, a, ws, since):
                done.append(f"restarted the review of `{ws.branch}`, which nothing was working on")
                continue
            if now - since < max(UNPICKED_AFTER, 2 * wait):
                continue
            body = (f"Worker {a.id} reported on {where}, and for {int((now - since) // 60)} min no "
                    f"reviewer has been running and its commit {head[:8]} has had no verdict. Call "
                    f"request_review(\"{ws.id}\") to get it reviewed.")
        else:
            continue
        if a.stuck_noted == since:
            continue
        try:
            agents.send_message(db, a.parent_id, body, sender_id=a.id)
        except agents.AgentError:
            continue
        db.update_agent(a.id, stuck_noted=since)
        done.append(f"told {a.parent_id} that reported worker {a.id} is stuck")
    return done


def _fix_round_since(db: DB, a: Agent) -> float | None:
    """When the review that put a worker's branch in a fix round was given,
    if the worker is still in that round, whether or not it has committed
    since; None otherwise. Reporting again moves the branch on to 'reviewing'."""
    if a.pipeline != "fixing":
        return None
    review = db.last_review(a.workspace_id)
    return review.created_at if review is not None else None


# How long a worker no hook reports on (Codex) may show nothing new before its
# supervisor is told. Such a CLI can sit on a startup error forever: signed in
# without a plan that includes it, say (issue #42).
SILENT_AFTER = 600.0


def note_silent(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once per silence, about a worker whose status
    no hook reports ('unknown'), that never got past 'starting', or that is
    mid-turn ('processing': a running turn's spinner redraws the pane), and
    whose pane hasn't printed anything for SILENT_AFTER seconds without a
    result."""
    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or not a.parent_id or a.result is not None
                or a.dismissed_at is not None
                or a.status not in ("unknown", "starting", "processing")
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        activity = tmux.window_activity(a.tmux_window, server=agents.server_of(a))
        if a.status == "processing" and (activity is None or a.headless):
            continue  # no screen to go by, and its turn may well be running
        last = activity or a.created_at
        if now - last < SILENT_AFTER or a.stuck_noted == last:
            continue
        ws = db.get_workspace(a.workspace_id)
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            screen = ""
        tail = "\n".join([ln for ln in screen.rstrip().splitlines() if ln.strip()][-12:])
        where = f" on branch `{ws.branch}`" if ws else ""
        look = f"Look with `brindle attach {ws.name}`, or" if ws else "Look at its pane, or"
        why = ("Its CLI may be hung mid-turn." if a.status == "processing" else
               "Its CLI may be stuck at startup (for example signed in without a plan "
               "that includes it).")
        body = (f"Worker {a.id} ({a.profile}, {a.provider}){where} has shown nothing new for "
                f"{int((now - last) // 60)} min and hasn't reported. {why} {look} "
                "cancel it and assign the task to another profile."
                f"\n\nIts screen:\n{tail}")
        try:
            agents.send_message(db, a.parent_id, body, sender_id=a.id)
        except agents.AgentError:
            continue
        db.update_agent(a.id, stuck_noted=last)
        done.append(f"told {a.parent_id} that worker {a.id} has gone silent")
    return done


def _task_weight(db: DB, a, ws) -> str | None:
    if ws is None:
        return None
    return next((t.weight for t in db.list_tasks(ws.repo_root) if t.agent_id == a.id), None)


def note_model_refused(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once, about a worker whose model the cloud
    account refused (cause and one-line fix), where its tier's next profile
    takes over (routing skips the refused one for an hour, so the other tasks
    aren't lost the same way), and about Claude Code's own fallback to an
    older model."""
    from brindle import model_access
    from brindle.config import load_repo_config
    from brindle.providers import ClaudeCode

    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or not a.parent_id or a.result is not None
                or a.dismissed_at is not None or a.provider != "claude"
                or a.status not in ("starting", "idle", "processing", "waiting")
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            continue
        ws = db.get_workspace(a.workspace_id)
        where = f" on branch `{ws.branch}`" if ws else ""
        error = ClaudeCode.api_error(screen)
        swapped = None
        try:
            routing = load_repo_config(ws.repo_root).routing if ws else {}
        except ValueError:
            routing = {}
        refused = route_health.provider_outcome(error)     # the status code only, never the text
        if refused and ws and model_access.first_time(f"route:{a.id}:{refused}"):
            route, model = agents.route_context(db, a, ws)
            route_health.record(ws.repo_root, agent=a.provider, route=route, model=model, outcome=refused)
        if model_access.classify(error) and model_access.first_time(f"refused:{a.id}"):
            swapped = model_access.swap(routing, _task_weight(db, a, ws), a.profile, error,
                                      repo_root=ws.repo_root if ws else None)
        if swapped is not None:
            cause, to = swapped
            move = (f"Other tasks of its weight tier go to {to} for the next hour; re-assign this "
                    f"one with agent_profile={to}." if to
                    else "No other profile in its weight tier is left to take over.")
            body = (f"Worker {a.id} ({a.profile}){where} can't call its model: {cause.line()}. "
                    f"{move}")
        else:
            warning = model_access.alias_fallback(screen)
            if not warning or not model_access.first_time(f"alias:{a.id}"):
                continue
            body = f"Worker {a.id} ({a.profile}){where}: {warning}"
        try:
            agents.send_message(db, a.parent_id, body, sender_id=a.id)
        except agents.AgentError:
            continue
        done.append(f"told {a.parent_id} about worker {a.id}'s model")
    return done


def auto_mode_note(a, ws) -> str:
    """A sentence for the stuck-worker message when the worker's profile
    asked Claude Code for auto mode, which should have answered ordinary
    prompts itself: Claude Code switches auto mode off for a session when
    it has a notice to acknowledge, a policy setting forbids it, or its
    classifier keeps failing, and says nothing to brindle when it does."""
    if a.provider != "claude":
        return ""
    try:
        from brindle.profiles import load_profile

        mode = load_profile(a.profile, ws.repo_root if ws else None).permission_mode
    except Exception:
        return ""
    if (mode or "").lower() != "auto":
        return ""
    return (" Its profile runs in Claude Code's auto mode. Auto mode still asks about some "
            "actions on purpose (working outside the project, such as entering another worktree, "
            "or a command its classifier declined); the screen below shows which prompt it is. "
            "If it's an ordinary command, auto mode is probably switched off in that session: run "
            "`claude --permission-mode auto` once yourself to see whether Claude Code has a notice "
            "to acknowledge, and check your Claude Code settings and usage limit.")


def clean_locks(db: DB, now: float | None = None) -> int:
    """Delete ``locks/sidebar-<id>.lock`` files whose session root is gone,
    paused or done. Only ones untouched for LAUNCH_GRACE (every use of a lock
    rewrites it) and not held right now, so a launch that's using one can't
    lose it from under it."""
    now = time.time() if now is None else now
    removed = 0
    for path in (brindle_home() / "locks").glob("sidebar-*.lock"):
        a = db.get_agent(path.stem.removeprefix("sidebar-"))
        if a is not None and a.status not in ("paused", "done") and a.dismissed_at is None:
            continue
        try:
            if now - path.stat().st_mtime <= LAUNCH_GRACE:
                continue
            with open(path, "a") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# -- `brindle prune` ---------------------------------------------------------------


def prune_retired(db: DB, now: float | None = None, *, cautious: bool = False) -> list[str]:
    """Remove the worktrees of finished workers whose branch is already
    merged (see ``view.retired``), and their branches unless the repo turns
    ``delete_merged_branches`` off. A worktree with
    uncommitted changes (or the one this is run from) is kept and reported.
    The records of a workspace whose folder is gone are forgotten (its
    branch stays). Either way its records are archived first (see
    ``history.archive``). ``cautious`` (the automatic sweep): a missing
    folder is only forgotten once nothing runs there and it's past
    ``view.UNUSED_WORKTREE_GRACE``, so one being created isn't."""
    from brindle import history, pipeline, view, workspaces

    now = time.time() if now is None else now
    alive = view.live_agents(db, tmux.list_panes())
    try:
        cwd = os.path.realpath(os.getcwd())
    except OSError:
        cwd = ""  # run from a folder since deleted: it's in no worktree
    done = []
    for ws in db.find_workspaces():
        if not os.path.isdir(ws.path):
            everyone = db.list_agents(ws.id)
            if cautious and (now - ws.created_at <= view.UNUSED_WORKTREE_GRACE
                             or any(a.id in alive for a in everyone)):
                continue
            if not history.archive(db, ws, everyone, "worktree missing"):
                done.append(f"kept the records of missing workspace {ws.id}: couldn't archive them")
                continue
            pipeline.on_workspace_cleanup(db, ws)
            db.delete_workspace(ws.id)
            done.append(f"forgot missing workspace {ws.id} ({ws.path})")
            continue
        if not view.retired(db, ws, alive, now):
            continue
        path = os.path.realpath(ws.path)
        if cwd and (cwd == path or cwd.startswith(path + os.sep)):
            done.append(f"kept {ws.branch}: you're in its worktree")
            continue
        try:
            dirty = git.dirty_files(ws.path)
        except git.GitError:
            continue
        if dirty:
            done.append(f"kept {ws.branch}: merged, but {len(dirty)} uncommitted file(s) in {ws.path}")
            continue
        try:
            removed = workspaces.remove(db, ws)
        except (workspaces.WorkspaceError, git.GitError) as e:
            done.append(f"kept {ws.branch}: {e}")
            continue
        branch = "branch deleted" if removed.branch_deleted else f"branch {ws.branch} kept"
        done.append(f"removed merged worktree {ws.path} ({branch})")
    if cautious:  # every sweep: only what it did, not what it keeps leaving alone
        done = [line for line in done if not line.startswith("kept ")]
    return done


def prune_stale_agents(db: DB, now: float | None = None) -> list[str]:
    """Close old stopped agents when their worktree is already merged or gone.

    Paused sessions remain resumable while they still have useful work. Once
    their workspace is merged or missing, keeping them visible serves no
    purpose; use the workspace's configured stale_after retention period.
    """
    now = time.time() if now is None else now
    limits: dict[str, float] = {}
    panes = tmux.list_panes()
    done = []
    for a in db.list_agents():
        # Only agents nothing is running for: a paused one, or one whose
        # process has exited. An idle agent with a live pane is just waiting
        # at its prompt (a supervisor between requests) and stays. Whether a
        # process has exited is only this server's panes' to say.
        if a.dismissed_at is not None:
            continue
        stopped = a.status == "paused" or (
            a.status in ("idle", "starting", "processing", "waiting", "unknown")
            and agents.same_server(a, panes) and not agents.is_alive(a, panes)
        )
        if not stopped:
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None:
            continue
        since = max(a.created_at, a.status_since or 0)
        limit = _stale_after(db, a, limits)
        if limit <= 0 or now - since <= limit:
            continue
        # A repo deleted outright leaves records whose git commands can't even start.
        missing = not os.path.isdir(ws.path) or not os.path.isdir(ws.repo_root)
        merged = not missing and ws.kind == "worktree" and workspaces_merged(ws)
        if not missing and not merged:
            continue
        agents.close(db, a.id)
        state = "paused" if a.status == "paused" else "exited"
        workspace_state = "gone" if missing else "merged"
        done.append(f"closed stale {state} agent {a.id}: workspace {workspace_state}")
    return done


def workspaces_merged(ws: Workspace) -> bool:
    """Whether a workspace branch is already contained in its configured base
    (False when the repo itself is gone)."""
    return bool(ws.base_branch) and os.path.isdir(ws.repo_root) and git.ok(
        ["merge-base", "--is-ancestor", f"refs/heads/{ws.branch}",
         f"refs/heads/{ws.base_branch}"], ws.repo_root)


def orphan_sessions(db: DB) -> list[str]:
    """Kill brindle tmux sessions (``brindle_*``) that no running agent is in,
    unless someone is attached to them or something other than an idle shell
    runs in them (a dev server in its shell window, say). A session no
    workspace row names, whose every pane sits in a folder that is gone (its
    worktree was removed, or the row forgotten), is orphaned whatever runs
    in it: an agent CLI left behind, an Antigravity worker, is stopped too."""
    from brindle import view

    panes = tmux.list_panes()
    alive = view.live_agents(db, panes)
    live_panes = {a.tmux_window for a in db.list_agents() if a.id in alive and a.tmux_window}
    live_sessions = {ws.tmux_session for ws in db.find_workspaces()
                     if any(a.id in alive for a in db.list_agents(ws.id))}
    known_sessions = {ws.tmux_session for ws in db.find_workspaces()}
    done = []
    for name, attached in tmux.list_sessions():
        if not name.startswith("brindle_") or attached or name in live_sessions:
            continue
        if live_panes & set(tmux.session_pane_ids(name)):
            continue
        folders = tmux.session_panes(name)
        abandoned = (name not in known_sessions and bool(folders)
                     and not any(path and os.path.isdir(path) for _, path in folders))
        if not abandoned and not tmux.session_idle(name):
            continue
        if abandoned:
            procs.terminate(procs.descendants([pid for pid, _ in folders]))
        tmux.kill_session(name)
        done.append(f"killed tmux session {name} "
                    f"({'its worktree is gone' if abandoned else 'no running agent'})")
    return done


def orphan_servers() -> list[str]:
    """Stop leftover private brindle tmux servers (``tmux -L brindle-*``) and
    remove their sockets: a test run's that outlived the run (it is named
    after the pytest process, see tests/conftest.py), or any whose sessions
    all belong to a BRINDLE_HOME that no longer exists."""
    done = []
    for name in tmux.other_servers():
        try:
            homes = tmux.server_homes(name)
        except tmux.TmuxTimeout:
            # Listening but not answering: not "nothing listening", so its
            # socket stays, and nothing gets killed on a guess.
            done.append(f"left tmux server {name} alone: it isn't answering")
            continue
        m = re.fullmatch(r"brindle-test-(\d+)(-.*)?", name)
        if m and procs.alive(int(m.group(1))):
            # A live test run's server (or one it made on purpose, ``-other``):
            # its sessions often have no BRINDLE_HOME, or one not yet created,
            # which would read as "its brindle home is gone", and one still
            # starting can read as "nothing listening". Either way it is a
            # server another pytest run is using right now.
            continue
        if homes is None:
            tmux.remove_socket(name)  # nothing listening: just the file
            continue
        if m:
            reason = "its test run is over"
        elif homes and all(h is None or not os.path.isdir(h) for h in homes):
            reason = "its brindle home is gone"
        else:
            continue
        tmux.reap_server(name)
        done.append(f"stopped tmux server {name} ({reason})")
    return done


def empty_worktree_dirs(db: DB) -> int:
    """Remove empty folders under the worktrees dirs (``feat`` once its last
    worktree is gone): each known repo's ``<repo>/.brindle/worktrees`` and the
    pre-#182 ``~/.brindle/worktrees``. A configured ``worktree_root`` may hold
    other things, so it is left alone. Returns how many."""
    roots = {worktrees_dir()} | {
        Path(r) / CONFIG_DIR / WORKTREES_DIR for r in {w.repo_root for w in db.find_workspaces()}
    }
    removed = 0
    for root in roots:
        for path, _dirs, _files in os.walk(root, topdown=False):
            if os.path.realpath(path) == os.path.realpath(root):
                continue
            try:
                os.rmdir(path)
                removed += 1
            except OSError:
                pass  # not empty
    return removed


def prune(db: DB) -> list[str]:
    """Everything ``brindle prune`` cleans up beyond session retention."""
    done = sweep(db)
    done += prune_stale_agents(db)
    done += prune_retired(db)
    done += orphan_sessions(db)
    done += orphan_servers()
    n = empty_worktree_dirs(db)
    if n:
        done.append(f"removed {n} empty worktree folder(s)")
    return done


def sweep_quietly(db: DB) -> None:
    """A sweep that never raises: culling must never break what calls it.
    A step that fails must not stop finished work leaving the live view
    (#167): that one is retried on its own."""
    try:
        sweep(db)
    except Exception:  # noqa: BLE001
        try:
            prune_retired(db, cautious=True)
        except Exception:  # noqa: BLE001
            pass
