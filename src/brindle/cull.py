"""Culling: stopping what's left over, and closing workers nobody needs.

A sweep, run by the sidebar every minute, when ``brindle`` starts, and by
``brindle prune``, does two things:

1. Leftover processes. Any process belonging to an agent that shouldn't be
   running (paused, done, closed, forgotten, or whose tmux window is gone,
   for instance after tmux itself went away, or now belongs to a newer agent:
   see agents.owns_pane) is stopped (see brindle.procs). An
   agent whose window vanished while it was marked running is recorded the
   way ``agents.pause`` would have: paused, so ``brindle continue`` can still
   bring it back, or done if it had already reported. A worker whose pane
   is dead (its CLI killed outside brindle, often with no process left to
   find) is marked failed with its exit status, and its supervisor told
   (note_dead_panes); done instead if it had reported.

2. Stale workers. A worker that reported and has sat idle for ``stale_after``
   minutes (repo config, default 30; 0 turns this off), with nothing queued
   for it, is closed: stopped and hidden from the sidebar. So is a worker
   whose window has been gone that long. Closing never touches its worktree
   or branch, so its supervisor can still review and merge the work.

It also stops the Ollama server brindle started once no running session uses
it (see brindle.native.serve.stop_unused), and reaps what outlived its work
(see brindle.reap): orphan and finished workers' tmux sessions, empty shells,
supervisors idle for days (paused, resumable), old coordinator rows, and
workers whose branch already landed.

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
import signal
import time
from contextlib import ExitStack
from pathlib import Path

from brindle import agents, config, git, procs, status_cache, tmux
from brindle.config import CONFIG_DIR, WORKTREES_DIR, brindle_home, load_repo_config, worktrees_dir
from brindle.db import DB, Agent, Workspace
from brindle.pro import route_health

# An agent this new may not have its window recorded yet.
LAUNCH_GRACE = agents.LAUNCH_GRACE
LOCK_MAX_AGE = 24 * 3600   # a lock file untouched this long is cleaned up
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

    from brindle import bus

    bus.prune(db, now=now)

    # 0. A Claude Code inbox held by an agent that isn't Claude Code is its
    # launcher's: messages to it would go to the supervisor's chat.
    from brindle import inbox

    dropped = inbox.drop_foreign(db)
    if dropped:
        done.append(f"forgot the launcher's inbox recorded for {dropped} agent(s)")
    from brindle import wake

    wake.prune({a.id for a in db.list_agents()})

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

    # 0c. Workers whose CLI was killed outside brindle: a dead pane, often
    # with nothing left for step 1 to find.
    done.extend(note_dead_panes(db, now, panes))

    # 1. Processes of agents that shouldn't be running.
    leftovers = []
    for aid in procs.all_agent_ids(table):
        a = db.get_agent(aid)
        if (a is None or a.status in ("paused", "done") or a.dismissed_at is not None
                or (a.status == "failed" and here(a) and not alive(a))):
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
    # What closed panes left with launchd as its parent, its CLI gone (procs.hand_off):
    # judged from the process table as it is now, not as the sweep began.
    orphans = procs.handed_orphans(now=now)
    if orphans and (stopped := procs.terminate(set(orphans), owned=orphans)):
        done.append(f"stopped {stopped} process(es) a closed pane left behind")

    # 1b. Autopilot sessions at the Claude usage limit: stop their workers,
    # or bring them back once the window has reset.
    from brindle import autopilot

    done.extend(autopilot.usage_sweep(db, now))

    # 1b'. Tasks queued for room on the machine whose helper is gone: start them once there is.
    from brindle import tasks

    started = tasks.start_with_room(db)
    if started:
        done.append(f"started {started} task(s) that were waiting for room")

    # 1c. Workers at their dollar budget (brindle Pro): warn, or stop if asked to.
    from brindle import budget

    done.extend(budget.sweep(db, now))

    # 1d. The org's kill switch (Enterprise managed rollout): stop running workers.
    from brindle.pro import rollout

    done.extend(rollout.sweep(db, now))

    # 1e. Cost-center approvals an admin has decided (Enterprise): raise the limit.
    from brindle.pro import cost_centers

    done.extend(cost_centers.poll(only_due=True, now=now))

    # 1e'. Plan and org setup changes made on the server (brindle Pro): refetch
    # an entitlement older than a few minutes; a removed Claude route stops
    # reaching panes. Before 1f, which checks sign-ins against that route.
    from brindle import company_login

    done.extend(company_login.sync())

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

    # 1h. Repos with the coordinator on (brindle Pro): wake one where
    # something it watches has happened and none is running.
    from brindle import coordinator

    try:
        done.extend(coordinator.wake(db, now))
    except Exception:  # noqa: BLE001 - coordination must never stop the sweep
        coordinator.log.exception("brindle: coordinator wake failed")

    # 2. Workers nobody needs any more.
    from brindle import pipeline

    limits: dict[str, float] = {}
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or a.dismissed_at is not None
                or not agents.runs_process(a) or not here(a)):
            continue
        if a.status == "paused" and _usage_paused(db, a):
            continue
        limit = _stale_after(db, a, limits)
        idle_since = max(a.created_at, a.status_since or 0)
        idle_for = now - idle_since
        if limit <= 0 or idle_for <= limit:
            continue
        running = alive(a)
        finished = running and a.result is not None and a.status == "idle" and not db.pending_count(a.id)
        # A stopped worker of a paused session comes back with `brindle
        # continue`; one whose session carried on without it won't.
        root = db.get_agent(_root(db, a))
        gone = not running and (a.status != "paused" or root is None or root.status != "paused")
        ws = db.get_workspace(a.workspace_id) if a.pipeline in PIPED else None
        if (finished or gone) and ws is not None and pipeline._settled(db, ws) is None:
            # The pipeline still owns its branch: closing it would leave the
            # branch with nobody on it. A stopped one is note_stalled's; one
            # in review is note_reported_stuck's (its stuck_noted de-dupe). One
            # in a fix round is note_idle_unreported's: told from here too, each
            # overwrote the other's stuck_noted and both repeated for hours.
            fix_round_told = a.mode in agents.FORWARDING_MODES and _fix_round_since(db, a) is not None
            if finished and a.parent_id and a.pipeline != "reviewing" and not fix_round_told and a.stuck_noted != idle_since:
                done.extend(_tell_stalled(db, a, idle_since, (
                    f"Worker {a.id} has sat idle {int(idle_for // 60)} min, but its branch `{ws.branch}` "
                    f"is still in the pipeline ({pipeline.review_state(db, ws) or a.pipeline}), so "
                    "brindle left it open. Check it's not stuck: send_message it what it needs.")))
            continue
        if finished or gone:
            agents.close(db, a.id, panes)
            done.append(f"closed {'idle' if finished else 'stopped'} worker {a.id} "
                        f"after {int(idle_for // 60)} min")

    # 2b. Messages a flush left queued because the pane wasn't ready (the
    # person typing, say): an idle agent with no turn-end hook (agy), or one
    # just waiting, would never get them otherwise. flush itself still waits
    # for typed input, a held login, and claims the agent before pasting.
    # First, mail a waiter died holding: no later Stop may ever start another.
    from brindle import wake

    wake.reclaim(db)
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
    decline_environment_setups(db, panes)
    done.extend(settle_startups(db, now, panes))
    done.extend(note_stuck(db, now, panes))
    # Before the idle reminder: a stop on an API error is told with its kind.
    done.extend(note_api_errors(db, now, panes))
    done.extend(note_idle_unreported(db, now, panes))
    done.extend(note_unreported_commits(db, now, panes))
    done.extend(note_reported_stuck(db, now, panes))
    done.extend(note_stalled(db, now, panes))
    done.extend(note_long_commands(db, now, panes))
    done.extend(note_silent(db, now, panes))
    done.extend(note_model_refused(db, now, panes))
    from brindle import open_items

    done.extend(open_items.sweep(db, now, panes))

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

    # 7. What outlived its work: orphan and finished tmux sessions, empty
    # shells, idle supervisors, old coordinator rows, superseded workers.
    from brindle import reap

    done.extend(reap.run(db, now))
    return done


# What a worker can be doing while its CLI runs ('unknown': a Codex worker,
# whose status no hook reports). 'starting' is launch_health's to judge.
RUNNING_STATUSES = ("processing", "idle", "waiting", "unknown")


def exit_cause(status: int | None, signum: int | str | None) -> str:
    """How a dead pane's program ended, for people: "status 143 (SIGTERM)",
    "signal 9 (SIGKILL)", "status 1". A status above 128 from a shell is
    128 plus the signal that killed what it ran. A signal tmux named but
    Python doesn't know stays as tmux named it."""
    def name(n: int) -> str | None:
        try:
            return signal.Signals(n).name
        except ValueError:
            return None

    if isinstance(signum, str):
        return f"signal {signum}"
    if signum is not None:
        return f"signal {signum} ({name(signum) or 'unknown signal'})"
    if status is None:
        return "an unknown status"
    sig = name(status - 128) if status > 128 else None
    return f"status {status} ({sig})" if sig else f"status {status}"


def note_dead_panes(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """A worker or reviewer still marked running whose pane is dead (its CLI
    was killed outside brindle, or exited on its own) is marked stopped: done
    if it had already reported, else 'failed' with failed_why naming the exit,
    and its supervisor is told once (the status change is what keeps it to
    once). A dead pane's exit status is only in a tmux.PaneSnapshot."""
    exits = getattr(panes, "exits", None)
    if not exits:
        return []
    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or a.status not in RUNNING_STATUSES
                or a.dismissed_at is not None or a.tmux_window not in exits
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.owns_pane(db, a, owners)
                or now - max(a.created_at, a.status_since or 0) <= LAUNCH_GRACE):
            continue
        cause = exit_cause(*exits[a.tmux_window])
        # Only from the status it was read in, and only once: a hook that
        # moved it since, or another sweep that got there first, wins.
        with ExitStack() as hold:
            if a.mode == "review":
                hold.enter_context(agents.review_dispatch_lock(a.workspace_id))
                current = db.get_agent(a.id)
                if (current is None or current.tmux_window != a.tmux_window
                        or current.tmux_server != a.tmux_server or agents.alive_or_starting(current)):
                    continue
            status = db.claim_exited(a.id, a.status, f"exited: {cause}")
        if status is None:
            continue
        db.end_native_subagents(a.id)
        if status == "done":
            done.append(f"marked {a.id} done: its pane died ({cause}) after it reported")
            continue
        agents._note_reviewer_end(db, db.get_agent(a.id) or a)
        done.append(f"marked {a.id} failed: its pane died ({cause})")
        if not a.parent_id:
            continue
        ws = db.get_workspace(a.workspace_id)
        if a.mode == "review" and ws is not None:
            from brindle import pipeline

            if pipeline.on_reviewer_dead(db, a, ws):
                continue
        where = f" on branch `{ws.branch}`" if ws else ""
        who = "Reviewer" if a.mode == "review" else "Worker"
        body = (f"{who} {a.id} ({a.profile}, {a.provider}){where} stopped before it reported: "
                f"its process exited with {cause} outside brindle, and its pane is dead. Its "
                "worktree and branch are untouched. Resume it with `brindle continue` (which "
                "restarts a live session's failed workers), or rewind it to an earlier turn "
                "with rewind_agent.")
        if a.mode == "review":
            from brindle import launch_health

            if error := launch_health.last_error(a):
                body += f" Last pane error: {error}"
        try:
            agents.send_message(db, a.parent_id, body, sender_id=a.id)
        except agents.AgentError:
            db.enqueue(a.parent_id, body, a.id)
        done.append(f"told {a.parent_id} that {a.id}'s pane died")
    return done


# How long a worker may sit on a permission or trust prompt before its
# supervisor is told (once per spell of waiting).
STUCK_AFTER = 90.0
# How long after a launch a sweep starts looking at a CLI that hasn't shown
# its input box: past the ordinary 30s startup wait. Providers with a longer
# startup budget retain ownership until that budget has elapsed too.
STARTUP_GRACE = 40.0


# How old a window's last output must be before a screen read of it is
# remembered: tmux counts that time in whole seconds, so an output in the same
# second as the read would leave it looking unchanged.
SCREEN_SETTLE = 2.0


def _screen_key(a: Agent, panes) -> str | None:
    """What a read of ``a``'s screen is remembered by: its pane and its window's
    last output, from the sweep's one pane listing (``PaneSnapshot.activity``).
    None when that's unknown or too recent to tell a later output apart."""
    activity = getattr(panes, "activity", {}).get(a.tmux_window)
    if activity is None or time.time() - activity < SCREEN_SETTLE:
        return None
    return f"{a.tmux_window}:{activity:.0f}"


def _screen_unchanged(db: DB, a: Agent, reader: str, key: str | None) -> bool:
    """Whether ``reader`` already read ``a``'s screen as it is (nothing printed
    since) and found nothing to do: a capture of it would say the same again.
    Kept in the DB, as the sidebars take turns at the cull."""
    return key is not None and db.mark(a.id, f"screen:{reader}") == key


def _screen_read(db: DB, a: Agent, reader: str, key: str | None) -> None:
    if key is not None and db.mark(a.id, f"screen:{reader}") != key:
        db.set_mark(a.id, f"screen:{reader}", key)


def decline_environment_setups(db: DB, panes: dict[str, bool]) -> None:
    """Dismiss Claude's environment upsell even in an interactive supervisor
    with no queued delivery and no waiting hook. A screen read once with no
    upsell on it isn't read again until the pane prints something."""
    from brindle import prompt_answer

    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.provider != "claude" or a.headless or a.dismissed_at is not None
                or a.status not in ("starting", "idle", "processing", "waiting")
                or not agents.same_server(a, panes) or not agents.is_alive(a, panes)
                or not agents.owns_pane(db, a, owners)):
            continue
        key = _screen_key(a, panes)
        if db.mark(a.id, "teach-auto-mode") is None and _screen_unchanged(db, a, "setup", key):
            continue
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            continue
        if prompt_answer.decline_teach_auto_mode(db, a, screen):
            continue
        provider = agents.get_provider(a.provider)
        if (db.mark(a.id, "teach-auto-mode") is not None and provider.input_ready(screen)
                and provider.screen_state(screen) == "idle"):
            db.set_mark(a.id, "teach-auto-mode", None)
            agents.screen_status(db, a, samples=2, gap=0)
        if db.mark(a.id, "teach-auto-mode") is None:
            _screen_read(db, a, "setup", key)


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
        if now - launched_at < getattr(provider, "STARTUP_SECONDS", 30) + 10:
            continue  # its original after-launch helper still owns startup
        try:
            screen = tmux.capture(pane, lines=60, server=agents.server_of(a))
        except tmux.TmuxError:
            continue
        if provider.tool_permission(screen):
            # The permission policy's, never a startup prompt to answer: its supervisor is told.
            if prompt_answer.tell(db, a, "\n".join(prompt_answer._tail(screen))):
                done.append(f"told {a.parent_id} that worker {a.id} is waiting on input")
            continue
        brief = db.conn.execute("SELECT submitted_prompt FROM launch_health WHERE agent_id=?", (a.id,)).fetchone()
        partial = brief and prompt_answer.brief_draft(screen, brief["submitted_prompt"])
        if not partial and not provider.input_ready(screen) and (not prompt_answer.looks_like_prompt(screen)
                                                 or prompt_answer.cached_kind(screen) == "not_a_prompt"):
            continue
        if prompt_answer.finish_launch(db, a):
            done.append(f"{a.id} past its startup prompts")
    return done


def note_stuck(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Forward the live menus of waiting workers immediately. Hook-only
    reviewer requests are immediate too; other waiting states retain their
    grace period. Report once per spell of waiting.

    Only a waiting worker's pane is read: hooks mark a wait (a permission
    request), a question a turn ended on is forwarded by the turn-end hook
    (prompt_answer.at_turn_end), and startup prompts are settle_startups'."""
    from brindle import permission_routing, prompt_answer

    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or not a.parent_id or a.result is not None
                or a.dismissed_at is not None or a.status != "waiting"
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        if permission_routing.waits_on_supervisor(db, a):
            continue    # it has the whole request and may answer: "only the user can" would be wrong
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            screen = ""
        if prompt_answer.decline_teach_auto_mode(db, a, screen):
            continue
        health = db.conn.execute("SELECT submitted_prompt FROM launch_health WHERE agent_id=?",
                                 (a.id,)).fetchone()
        brief = (health["submitted_prompt"] if health else None) or a.task or ""
        # A hook-held permission request can leave the CLI's spinner running
        # while the person answers it, just as in agents.screen_status.
        if ((prompt_answer.pane_busy(screen) and db.hook_permission_request(a.id) is None)
                or prompt_answer.brief_draft(screen, brief)):
            continue
        if prompt_answer.blocking_prompt(screen, a.provider, brief):
            if prompt_answer.tell(db, a, "\n".join(prompt_answer._tail(screen))):
                done.append(f"told {a.parent_id} that worker {a.id} is waiting on input")
            continue
        since = a.status_since or a.created_at
        if a.stuck_noted == since or (a.mode != "review" and now - since < STUCK_AFTER):
            continue
        ws = db.get_workspace(a.workspace_id)
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
        if not _send_note(db, a, since, body):
            continue  # noted by another sweep, or its supervisor isn't running (tried again later)
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
        since = _idle_since(db, a, fix_round or 0, panes)
        if since is None:
            continue  # no telling whether a later turn is running
        if now - since < IDLE_UNREPORTED_AFTER or a.stuck_noted == since:
            continue
        won, previous = db.claim_note(a.id, since)
        if not won:
            continue
        # Not told while a command it started is still running; tried again then.
        sent = False
        try:
            sent = agents.tell_parent_unreported(db, a, remind=True, now=now)
        finally:
            if not sent:
                db.release_note(a.id, since, previous)
        if sent:
            done.append(f"told {a.parent_id} that worker {a.id} is idle without reporting")
    return done


def _idle_since(db: DB, a: Agent, floor: float = 0, panes=None) -> float | None:
    """When ``a``, which its hooks report idle, last showed any sign of work, or
    None when there's no telling. A provider with no hook for a turn starting
    (Codex) stays 'idle' through later turns, so its pane's last output counts
    (from the sweep's ``panes`` listing, asked of tmux only when it lacks it);
    files changing in its worktree (a script or a background command) count
    too. Commits don't: see _fix_round_since."""
    from brindle.providers import get_provider

    since = max(a.status_since or a.created_at, floor)
    provider = get_provider(a.provider)
    if not (provider.uses_hooks and provider.announces_turns):
        last = getattr(panes, "activity", {}).get(a.tmux_window)
        if last is None:
            last = tmux.window_activity(a.tmux_window, server=agents.server_of(a))
        if last is None:
            return None
        since = max(since, last)
    return max(since, _last_edit(db.get_workspace(a.workspace_id)))


# How long a report may sit received or accepted, undelivered, before the
# supervisor is told (a report that is being handled takes seconds).
REPORT_GRACE = 120.0


def _tell_once(db: DB, a: Agent, key: str, body: str) -> bool:
    """Tell ``a``'s supervisor ``body``, once per ``key`` (a mark of its own in
    agent_marks, so one notice never stands in for another): claimed in the
    database first, and let go if the send fails."""
    won, previous = db.claim_mark(a.id, key, "told")
    if not won:
        return False
    sent = False
    try:
        agents.send_message(db, a.parent_id, body, sender_id=a.id)
        sent = True
    except agents.AgentError:
        pass
    finally:
        if not sent:
            db.set_mark(a.id, key, previous)
    return sent


def note_unreported_commits(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once, about a worker whose report may have been
    lost (a broken MCP connection, then back to waiting, with nobody told the
    branch is ready): a report stuck received or accepted in the outbox past
    REPORT_GRACE, or, for a worker idle at its prompt, commits no delivered
    report covers (a branch with no report at all included)."""
    from brindle import gates

    done = []
    for row in db.outbox_stuck(now - REPORT_GRACE):
        a = db.get_agent(row["agent_id"])
        if (a is None or not a.parent_id or a.dismissed_at is not None
                or a.mode not in agents.FORWARDING_MODES):
            continue
        ws = db.get_workspace(a.workspace_id)
        where = f"{ws.branch} (worker {a.id}, workspace {ws.id})" if ws else f"worker {a.id}"
        if _tell_once(db, a, f"report:{row['id']}:{row['state']}", (
                f"{where} has a report that was {row['state']} but not delivered; the worker may "
                f"have failed to report. Ask it to call report_result again, or workspace_diff "
                f"to review the branch.")):
            done.append(f"told {a.parent_id} that worker {a.id} has an undelivered report")
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.FORWARDING_MODES or not a.parent_id
                or a.dismissed_at is not None or a.status != "idle"
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None or not os.path.isdir(ws.path) or not ws.base_branch:
            continue
        reported = db.outbox_last_delivered_head(a.id)
        # HEAD from the git files, and the notice's de-dupe, before any git (the idle
        # look's status, the count): a worker already told about stays idle for its
        # whole stale_after, and the cull looks once a minute.
        # A count of none is remembered by what it was counted from (HEAD, the base,
        # the reported head): the same commits count none again.
        head = status_cache.read_head(ws.path)
        base = config._branch_sha(Path(ws.path), ws.base_branch)
        counted = f"{head} {base} {reported}" if head and base else None
        if head is not None and (head == reported or db.mark(a.id, f"commits:{head}") == "told"
                                 or (counted and db.mark(a.id, "commits-none") == counted)):
            continue
        since = _idle_since(db, a, panes=panes)
        if since is None or now - since < IDLE_UNREPORTED_AFTER or agents.command_running(db, a):
            continue
        try:
            head = head or gates.head(ws)
            if head == reported or db.mark(a.id, f"commits:{head}") == "told":
                continue
            span = f"{reported}..HEAD" if reported and git.ok(["merge-base", "--is-ancestor", reported, "HEAD"], ws.path) \
                else f"{ws.base_branch}..HEAD"
            covered = len(git.run(["rev-list", span], ws.path).stdout.split())
        except git.GitError:
            continue
        if not covered and counted:
            db.set_mark(a.id, "commits-none", counted)
        if covered and _tell_once(db, a, f"commits:{head}", (
                f"{ws.branch} has {covered} unreported commits; the worker may have failed to "
                f"report (worker {a.id}, workspace {ws.id}). Ask it to call report_result again, "
                f"or workspace_diff to review the branch.")):
            done.append(f"told {a.parent_id} that worker {a.id} has unreported commits")
    return done


def _last_edit(ws: Workspace | None) -> float:
    """When a file with uncommitted changes in ``ws`` was last written; 0 if none.
    Which files are uncommitted comes from ``status_cache`` (git only when the
    checkout moved; the cull asks once a minute for every idle worker); each
    one's time is stat'ed now."""
    newest = 0.0
    if ws is None or not os.path.isdir(ws.path):
        return newest
    try:
        entries = status_cache.cached_any_status(ws.path, ws.base_branch).dirty_files
    except git.GitError:
        return newest
    root = os.fsencode(ws.path)
    for entry in entries:
        # As porcelain v1 prints them: C-quoted past ASCII, a rename as "old -> new".
        names = [entry, *(entry.rsplit(" -> ", 1)[1:])]
        for name in names:
            try:
                newest = max(newest, os.stat(os.path.join(root, status_cache._unquote(name))).st_mtime)
                break
            except OSError:
                pass   # deleted, or the rename's destination is the name to look at
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
            if agents.session_paused(db, a):
                continue   # it starts nothing: this sweep picks the branch up after the resume
            try:
                head = gates.head(ws)
                cfg = load_repo_config(ws.repo_root)
                wait = agents.expected_check_wait(db, ws, cfg) or 0.0
            except (git.GitError, ValueError):
                continue
            # The spell starts at the report that put it in review, or at a
            # later reviewer's start (a fallback review, say).
            reviewers = [r for r in db.list_agents(ws.id) if r.mode == "review"]
            reviewing = any(r.dismissed_at is None and r.status not in ("paused", "done")
                            and agents.is_alive(r, panes) for r in reviewers)
            unacted = None if reviewing else pipeline.unacted_verdict(db, ws, a, head, now)
            if unacted is not None and a.stuck_noted != unacted.created_at:
                # A verdict on record that never moved the branch on (the
                # pipeline dropped it): act on it once, and say so.
                if not db.claim_note(a.id, unacted.created_at)[0]:
                    continue   # another process's sweep took it
                if not pipeline.replay_verdict(db, ws, unacted, a, head, now):
                    continue   # acted on since ``a`` was read
                verdict = "approved" if unacted.approved else "requested changes on"
                state = pipeline.review_state(db, ws) or "out of the pipeline"
                done.extend(_tell_stalled(db, a, unacted.created_at, (
                    f"Reviewer {unacted.reviewer_id} {verdict} {where} at {unacted.sha[:8]} "
                    f"{int((now - unacted.created_at) // 60)} min ago, but the pipeline never acted on "
                    f"it. brindle has acted on it now; the branch stands at: {state}."), claimed=True))
                continue
            since = max([a.reviewing_since] + [r.created_at for r in reviewers])
            if now - since < cfg.review_start_delay or db.latest_review(ws.id, head) or reviewing:
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
        if a.stuck_noted == since or not _send_note(db, a, since, body):
            continue
        done.append(f"told {a.parent_id} that reported worker {a.id} is stuck")
    return done


# The pipeline's stages: a worker in one has a branch the pipeline still owns.
PIPED = ("reviewing", "fixing", "ready")


def _send_note(db: DB, a: Agent, since: float, body: str, near: float = 0.0) -> bool:
    """Send ``a``'s supervisor ``body``, once per ``since`` (kept in
    stuck_noted). Every brindle process sweeps from its own read of the
    workers, so the note is claimed in the database before it is sent: only
    one of them sends it, and the claim is let go if the send fails."""
    won, previous = db.claim_note(a.id, since, near)
    if not won:
        return False
    sent = False
    try:
        agents.send_message(db, a.parent_id, body, sender_id=a.id)
        sent = True
    except agents.AgentError:
        return False
    finally:
        if not sent:
            db.release_note(a.id, since, previous)
    return True


def _tell_stalled(db: DB, a: Agent, since: float, body: str, *, claimed: bool = False) -> list[str]:
    """Tell ``a``'s supervisor ``body`` about its piped branch, once per
    ``since`` (see ``_send_note``). A caller that has ``claimed`` it already
    says so."""
    if claimed:
        try:
            agents.send_message(db, a.parent_id, f"[brindle pipeline] {body}", sender_id=a.id)
        except agents.AgentError:
            return []
    elif not _send_note(db, a, since, f"[brindle pipeline] {body}"):
        return []
    return [f"told {a.parent_id} that the branch of {a.id} is stalled"]


def note_stalled(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once per spell, about a branch the pipeline
    owns (in review, fixing or ready) that nothing has worked on for the
    repo's stall_alert_after minutes: no live worker, no reviewer running,
    no check run or merge at work. A branch in review with no verdict on
    its commit is note_reported_stuck's, which starts its review again
    first."""
    from brindle import gates, pipeline

    done = []
    owners = agents.pane_owners(db, panes)
    limits: dict[str, float] = {}
    for a in db.list_agents():
        if a.mode not in pipeline.WORKER_MODES or not a.parent_id or a.pipeline not in PIPED:
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None or not agents.same_server(a, panes) or pipeline._settled(db, ws) is not None:
            continue
        if agents.is_alive(a, panes) and agents.owns_pane(db, a, owners):
            continue
        reviewers = [r for r in db.list_agents(ws.id) if r.mode == "review"]
        if any(r.dismissed_at is None and r.status not in ("paused", "done")
               and agents.is_alive(r, panes) for r in reviewers):
            continue
        if pipeline.review_running(ws.id) or any(
                gates.marked(kind, ws.id) for kind in ("checks-running", "checks-room", "merge-run")):
            continue
        try:
            head = gates.head(ws)
        except git.GitError:
            continue
        last = db.last_review(ws.id)
        if a.pipeline == "reviewing" and db.latest_review(ws.id, head) is None:
            continue
        if ws.repo_root not in limits:
            try:
                limits[ws.repo_root] = load_repo_config(ws.repo_root).stall_alert_after * 60.0
            except ValueError:
                limits[ws.repo_root] = 0.0
        since = max([a.created_at, a.status_since or 0, a.reviewing_since or 0, a.dismissed_at or 0,
                     a.reopened_at or 0, last.created_at if last else 0]
                    + [r.created_at for r in reviewers])
        if limits[ws.repo_root] <= 0 or now - since < limits[ws.repo_root] or a.stuck_noted == since:
            continue
        state = pipeline.review_state(db, ws) or a.pipeline
        told = _tell_stalled(db, a, since, (
            f"Branch `{ws.branch}` (workspace {ws.id}) is in the pipeline ({state}), but nothing is "
            f"working on it: worker {a.id} isn't running, and no reviewer or check run is at work, "
            f"for {int((now - since) // 60)} min. This needs you: send_message the worker what it "
            f"needs, assign a new one on the branch, or merge_workspace(\"{ws.id}\") if it's approved."))
        done.extend(told)
        if told and a.pipeline == "fixing":   # an approved branch only waits for its merge
            from brindle import open_items

            open_items.track(db, a, "its process ended in a review fix round", now)
    return done


# A tool call still running in a worker's pane: "⏺ Bash(cmd)" over
# "⎿  Running… (41m 9s · esc to interrupt)".
_RUNNING = re.compile(r"^\s*⎿\s*Running(?: command)?(?:…|\.{3})\s*\((?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+)s\b")
_TOOL_CALL = re.compile(r"^\s*⏺\s*\w+\((.*)$")
# Two reads of one command's start differ by a second or so.
SAME_COMMAND_WITHIN = 120.0


def _running_command(screen: str) -> tuple[str, float] | None:
    """The command the pane shows running, as (its first line, seconds it has run)."""
    lines = screen.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        m = _RUNNING.match(lines[i])
        if m:
            hours, minutes, seconds = (int(g or 0) for g in m.groups())
            call = next((_TOOL_CALL.match(ln) for ln in reversed(lines[:i]) if _TOOL_CALL.match(ln)), None)
            return (call.group(1).strip() if call else ""), hours * 3600.0 + minutes * 60.0 + seconds
    return None


def note_long_commands(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once per command, about a worker whose pane has
    shown one shell command running for the repo's long_command_alert_after
    seconds. Nothing is stopped: a pane with a command running counts as
    active, so no other notice would ever say the worker sits in it."""
    from brindle import redact

    done = []
    owners = agents.pane_owners(db, panes)
    limits: dict[str, float] = {}
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or not a.parent_id or a.dismissed_at is not None
                or a.status not in ("processing", "waiting") or not agents.runs_process(a)
                or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        # 'waiting' also lingers after a prompt was answered in the terminal,
        # until the command finishes; only a held request is a real wait.
        if a.status == "waiting" and db.hook_permission_request(a.id) is not None:
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None:
            continue
        if ws.repo_root not in limits:
            try:
                limits[ws.repo_root] = float(load_repo_config(ws.repo_root).long_command_alert_after)
            except ValueError:
                limits[ws.repo_root] = 0.0
        if limits[ws.repo_root] <= 0:
            continue
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            continue
        running = _running_command(screen)
        if running is None or running[1] < limits[ws.repo_root]:
            continue
        command, elapsed = running
        since = float(int((now - elapsed) // 60 * 60))   # the command's start, to the minute
        first = redact.mask(command.splitlines()[0]) if command else "(unknown)"
        body = (f"[brindle] Worker {a.id} on branch `{ws.branch}` has been inside one command for "
                f"{int(elapsed // 60)} min: {first}\n\nNothing was stopped. Check what it is doing "
                f"(`brindle attach {ws.name}`) or send_message the worker.")
        if _send_note(db, a, since, body, near=SAME_COMMAND_WITHIN):
            done.append(f"told {a.parent_id} that worker {a.id} has run one command for {int(elapsed // 60)} min")
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
        if not _send_note(db, a, last, body):
            continue
        done.append(f"told {a.parent_id} that worker {a.id} has gone silent")
    return done


# How long a worker's pane must have shown a turn ended on an API error, with
# nothing new, before a sweep takes it that no hook will say so: Claude Code's
# own attempts redraw the pane, and its StopFailure hook runs within seconds.
API_ERROR_GRACE = 60.0


def note_api_errors(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """A Claude Code worker whose pane shows its last turn ended on an API
    error (529 Overloaded, a 5xx, ...) and has printed nothing since for
    API_ERROR_GRACE, and whose supervisor hasn't been told about this stop:
    its StopFailure hook never ran, so it would sit there forever. It may
    still say 'processing', or a screen read (agents.reconcile, which the
    dashboard runs) may have moved it to 'idle' already. Mark it idle (the
    screen shows its prompt) and tell its supervisor once per stop
    (agents.tell_parent_api_error). A model the account refuses is
    note_model_refused's."""
    from brindle import model_access
    from brindle.providers import ClaudeCode

    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.FORWARDING_MODES or not a.parent_id or a.result is not None
                or a.dismissed_at is not None or a.provider != "claude" or a.headless
                or a.status not in ("processing", "idle")
                or (a.status == "idle" and db.mark(a.id, agents.API_ERROR_MARK) == agents.api_error_stop(a))
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        # The window's last output from the sweep's pane listing; asked of tmux only when it lacks it.
        activity = getattr(panes, "activity", {}).get(a.tmux_window)
        if activity is None:
            activity = tmux.window_activity(a.tmux_window, server=agents.server_of(a))
        if activity is None or now - activity < API_ERROR_GRACE:
            continue
        key = _screen_key(a, panes)
        if _screen_unchanged(db, a, "api", key):
            continue
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            continue
        error = ClaudeCode.api_error(screen)
        if not error or model_access.classify(error):
            _screen_read(db, a, "api", key)
            continue
        if a.status == "processing":
            db.set_status(a.id, "idle", only_if="processing")
        current = db.get_agent(a.id)
        if current is None or current.status != "idle":
            continue   # a hook moved it first
        if agents.tell_parent_api_error(db, current, error):
            done.append(f"told {a.parent_id} that worker {a.id} stopped on an API error")
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
        # What a screen says is told once (first_time): one read until the pane prints again.
        key = _screen_key(a, panes)
        if _screen_unchanged(db, a, "refused", key):
            continue
        try:
            screen = tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
        except tmux.TmuxError:
            continue
        _screen_read(db, a, "refused", key)
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
    return removed + _clean_old_locks(now)


# Lock kinds whose acquirers (gates._open_locked) recheck the file after locking,
# so deleting one while it is held can't leave a waiter on an unlinked inode.
# (not check-slot-N.lock: the machine-wide slots are opened without a recheck).
_REVALIDATED_LOCKS = re.compile(r"(check|checks-running|checks-room|review-request|review-run|merge-run)-[0-9a-f]{16}\.lock")


def _clean_old_locks(now: float) -> int:
    """Delete old ``locks/*.lock`` files of the kinds above, untouched for
    LOCK_MAX_AGE, but only after taking it non-blocking, so a lock somebody
    holds is never removed."""
    removed = 0
    for path in (brindle_home() / "locks").glob("*.lock"):
        if not _REVALIDATED_LOCKS.fullmatch(path.name):
            continue
        try:
            if now - path.stat().st_mtime <= LOCK_MAX_AGE:
                continue
            with open(path) as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # a concurrent cleanup may have replaced the file since we opened it
                if not os.path.samestat(os.stat(path), os.fstat(f.fileno())):
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
            # Chosen from an earlier look: stop nothing, so a worker that is back
            # at work since keeps it (its CLI is then in use there).
            removed = workspaces.remove(db, ws, stop=())
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
    from brindle import disk

    disk.now_and_then(db)   # old backups, aged archive, big logs, merged worktrees: at most hourly
