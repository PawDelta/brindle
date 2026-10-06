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

from brindle import agents, git, procs, tmux
from brindle.config import brindle_home, load_repo_config, worktrees_dir
from brindle.db import DB, Agent, Workspace

# An agent this new may not have its window recorded yet.
LAUNCH_GRACE = 60.0


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

    # 3. Workers stuck on a prompt nobody is answering, or gone silent.
    done.extend(note_stuck(db, now, panes))
    done.extend(note_silent(db, now, panes))

    # 4. Sidebar locks of sessions that are over.
    removed = clean_locks(db, now)
    if removed:
        done.append(f"removed {removed} stale sidebar lock(s)")

    # 5. The Ollama server brindle started, once no running session uses it
    # (a chat whose tmux went away never paused, so nothing else stops it).
    from brindle.native import serve

    done.extend(serve.stop_unused(db))
    return done


# How long a worker may sit on a permission or trust prompt before its
# supervisor is told (once per spell of waiting).
STUCK_AFTER = 90.0


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
        body = (f"Worker {a.id} ({a.profile}){where} has been waiting on a prompt for "
                f"{int(now - since)}s and can't continue until someone answers it.{attach} "
                f"or remove the workspace if it's no longer needed.{auto_mode_note(a, ws)}"
                f"{request}\n\nIts screen:\n{tail}")
        try:
            agents.send_message(db, a.parent_id, body, sender_id=a.id)
        except agents.AgentError:
            continue  # its supervisor isn't running; try again on a later sweep
        db.update_agent(a.id, stuck_noted=since)
        done.append(f"told {a.parent_id} that worker {a.id} is stuck on a prompt")
    return done


# How long a worker no hook reports on (Codex) may show nothing new before its
# supervisor is told. Such a CLI can sit on a startup error forever: signed in
# without a plan that includes it, say (issue #42).
SILENT_AFTER = 600.0


def note_silent(db: DB, now: float, panes: dict[str, bool]) -> list[str]:
    """Tell each supervisor, once per silence, about a worker whose status
    no hook reports ('unknown') or that never got past 'starting', and whose
    pane hasn't printed anything for SILENT_AFTER seconds without a result."""
    done = []
    owners = agents.pane_owners(db, panes)
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or not a.parent_id or a.result is not None
                or a.dismissed_at is not None or a.status not in ("unknown", "starting")
                or not agents.runs_process(a) or not agents.same_server(a, panes)
                or not agents.is_alive(a, panes) or not agents.owns_pane(db, a, owners)):
            continue
        last = tmux.window_activity(a.tmux_window, server=agents.server_of(a)) or a.created_at
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
        body = (f"Worker {a.id} ({a.profile}, {a.provider}){where} has shown nothing new for "
                f"{int((now - last) // 60)} min and hasn't reported. Its CLI may be stuck at "
                f"startup (for example signed in without a plan that includes it). {look} "
                "cancel it and assign the task to another profile."
                f"\n\nIts screen:\n{tail}")
        try:
            agents.send_message(db, a.parent_id, body, sender_id=a.id)
        except agents.AgentError:
            continue
        db.update_agent(a.id, stuck_noted=last)
        done.append(f"told {a.parent_id} that worker {a.id} has gone silent")
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


def prune_retired(db: DB, now: float | None = None) -> list[str]:
    """Remove the worktrees of finished workers whose branch is already
    merged (see ``view.retired``), and their branches unless the repo turns
    ``delete_merged_branches`` off. A worktree with
    uncommitted changes (or the one this is run from) is kept and reported."""
    from brindle import view, workspaces

    now = time.time() if now is None else now
    alive = view.live_agents(db, tmux.list_panes())
    cwd = os.path.realpath(os.getcwd())
    done = []
    for ws in db.find_workspaces():
        if not os.path.isdir(ws.path):
            db.delete_workspace(ws.id)
            done.append(f"forgot missing workspace {ws.id} ({ws.path})")
            continue
        if not view.retired(db, ws, alive, now):
            continue
        path = os.path.realpath(ws.path)
        if cwd == path or cwd.startswith(path + os.sep):
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
        missing = not os.path.isdir(ws.path)
        merged = ws.kind == "worktree" and workspaces_merged(ws)
        if not missing and not merged:
            continue
        agents.close(db, a.id)
        state = "paused" if a.status == "paused" else "exited"
        workspace_state = "gone" if missing else "merged"
        done.append(f"closed stale {state} agent {a.id}: workspace {workspace_state}")
    return done


def workspaces_merged(ws: Workspace) -> bool:
    """Whether a workspace branch is already contained in its configured base."""
    return bool(ws.base_branch) and git.ok(
        ["merge-base", "--is-ancestor", f"refs/heads/{ws.branch}",
         f"refs/heads/{ws.base_branch}"], ws.repo_root)


def orphan_sessions(db: DB) -> list[str]:
    """Kill brindle tmux sessions (``brindle_*``) that no running agent is in,
    unless someone is attached to them or something other than an idle shell
    runs in them (a dev server in its shell window, say)."""
    from brindle import view

    panes = tmux.list_panes()
    alive = view.live_agents(db, panes)
    live_panes = {a.tmux_window for a in db.list_agents() if a.id in alive and a.tmux_window}
    live_sessions = {ws.tmux_session for ws in db.find_workspaces()
                     if any(a.id in alive for a in db.list_agents(ws.id))}
    done = []
    for name, attached in tmux.list_sessions():
        if not name.startswith("brindle_") or attached or name in live_sessions:
            continue
        if live_panes & set(tmux.session_pane_ids(name)) or not tmux.session_idle(name):
            continue
        tmux.kill_session(name)
        done.append(f"killed tmux session {name} (no running agent)")
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
        m = re.fullmatch(r"brindle-test-(\d+)", name)
        if homes is None:
            tmux.remove_socket(name)  # nothing listening: just the file
            continue
        if m and not procs.alive(int(m.group(1))):
            reason = "its test run is over"
        elif homes and all(h is None or not os.path.isdir(h) for h in homes):
            reason = "its brindle home is gone"
        else:
            continue
        tmux.reap_server(name)
        done.append(f"stopped tmux server {name} ({reason})")
    return done


def empty_worktree_dirs() -> int:
    """Remove empty folders under the worktrees dir (``<repo>/feat`` once its
    last worktree is gone). Returns how many."""
    root = worktrees_dir()
    removed = 0
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
    n = empty_worktree_dirs()
    if n:
        done.append(f"removed {n} empty worktree folder(s)")
    return done


def sweep_quietly(db: DB) -> None:
    """A sweep that never raises: culling must never break what calls it."""
    try:
        sweep(db)
    except Exception:  # noqa: BLE001
        pass
