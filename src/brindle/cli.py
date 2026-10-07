"""brindle command line."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from brindle import agents, git, tmux, view, workspaces
from brindle import history as history_mod
from brindle import repo_cmds
from brindle.usage import format_tokens
from brindle.config import write_template
from brindle.db import DB, Workspace
from brindle.profiles import list_profiles, load_profile

class _App(typer.Typer):
    """`brindle` as a command. A tmux server that isn't answering (see
    tmux.TmuxTimeout) is reported in one line, whichever command it bit,
    instead of as a traceback: nothing can be done with it from here."""

    def __call__(self, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except tmux.TmuxTimeout as e:
            typer.secho(str(e), fg="red", err=True)
            raise SystemExit(1)


app = _App(add_completion=False, help="""brindle: run coding agents in parallel, each on its own git branch.

Run `brindle` with no arguments to open (or reopen) a supervisor chat here, with
the live dashboard underneath. Outside a git repo it starts a scratch session;
`brindle transfer <repo>` moves that work into a real repository later.

Paid features (hosted learning, per-worktree services, team policies, CI):
`brindle account` shows what you have and how to get the rest.""")
agent_app = typer.Typer(no_args_is_help=True, help="Manage agents.")
app.add_typer(agent_app, name="agent")
app.add_typer(repo_cmds.app, name="repo")


def _fail(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(1)


def _ws(db: DB, ref: Optional[str]) -> Workspace:
    try:
        if ref:
            return workspaces.resolve(db, ref)
        ws = workspaces.current(db)
        if ws:
            return ws
    except workspaces.WorkspaceError as e:
        _fail(str(e))
    _fail("not inside a brindle workspace; pass a workspace name")
    raise AssertionError


def _attach(ws: Workspace, window: str | None = None) -> None:
    try:
        if not tmux.has_session(ws.tmux_session):
            tmux.ensure_session(ws.tmux_session, ws.path, workspaces.workspace_env(ws))
        if window:
            tmux.select_window(window)
        if tmux.inside_this_server():
            # From inside one of this server's panes (TMUX set, or only
            # TMUX_PANE left after `env -u TMUX brindle`): switch the client
            # there. Never attach a second client from within the server's
            # own pane, which hung the whole server once (see
            # tmux.inside_this_server).
            tmux.switch_client(window or f"={ws.tmux_session}")
            return
    except tmux.TmuxError as e:
        _fail(str(e))
    subprocess.run(tmux.attach_command(ws.tmux_session))
    _after_detach(ws)


def _after_detach(ws: Workspace) -> None:
    """Back at the user's own prompt: say what happened and what's still running."""
    from brindle import scratch

    if tmux.has_session(ws.tmux_session):
        typer.echo("Detached; everything is still running. Run `brindle` here to reopen.")
        return
    agents._stop_local_models(DB())  # the Ollama brindle started, if nothing else uses it
    typer.echo("brindle session paused: nothing is running, and all work is saved.")
    typer.echo("  `brindle continue` picks it up where you left off; `brindle` starts fresh.")
    if scratch.is_scratch(ws.path) and not scratch.transferred_to(ws.path):
        typer.echo(f"  Scratch work is saved in {ws.path}; `brindle transfer <repo>` moves it into a repo.")


def _run(fn, *args, **kwargs):
    from brindle.scratch import ScratchError

    try:
        return fn(*args, **kwargs)
    except (git.GitError, workspaces.WorkspaceError, agents.AgentError,
            tmux.TmuxError, ScratchError, KeyError, ValueError) as e:
        _fail(str(e).strip("'\""))


# -- workspaces --------------------------------------------------------------


@app.command()
def init(
    yes: bool = typer.Option(False, "--yes", "-y", help="Write the config without asking."),
) -> None:
    """Set brindle up in this repo: detect setup and test commands, write .brindle/config.json, check the tools.

    Reads the repo's lockfiles and manifests to fill in `setup` (what a new
    worktree needs), `checks` (what must pass before a branch merges) and
    `copy` (git-ignored env files), then runs the same checks as `brindle
    doctor`. An existing config is left alone."""
    from brindle import detect, doctor as doctor_mod
    from brindle.config import CONFIG_DIR, CONFIG_FILE

    try:
        root = git.main_repo_root(os.getcwd())
    except git.GitError:
        _fail("not in a git repo. Run `git init` first, or just run `brindle`: "
              "outside a repo it starts a scratch session you can transfer later.")
    path = Path(root) / CONFIG_DIR / CONFIG_FILE
    found = detect.detect(root)
    typer.echo(f"detected: {', '.join(found.stacks) or 'no known stack'}")
    values = {"setup": found.setup, "checks": found.checks, "copy": found.copy}
    for key, cmds in values.items():
        typer.echo(f"  {key:<7} {'; '.join(cmds) if cmds else '-'}")
    for note in found.notes:
        typer.secho(f"  ! {note}", fg="yellow")
    if path.exists():
        typer.echo(f"kept {path} (it already exists; add anything above it's missing)")
    else:
        if sys.stdin.isatty() and not yes:
            typer.confirm(f"write {path.relative_to(root)}?", default=True, abort=True)
        write_template(root, values)
        typer.secho(f"✓ wrote {path}", fg="green")
    typer.echo("")
    results = [c for c in doctor_mod.checks(root) if c.level != doctor_mod.OK]
    # Optional pieces (Codex, a local model, ...) are one line on a first run.
    optional = [c.name for c in results if doctor_mod.is_optional(c)]
    shown = [c for c in results if not doctor_mod.is_optional(c)]
    if shown:
        typer.echo(doctor_mod.render(shown))
    if optional:
        typer.echo(f"optional, not set up: {', '.join(optional)} (`brindle doctor` says how)")
    if any(c.level == doctor_mod.FAIL for c in results):
        raise typer.Exit(1)
    typer.secho("Ready. Commit .brindle/config.json, then run `brindle` and tell the supervisor what to build.",
                fg="green")


@app.command()
def demo(
    local: bool = typer.Option(False, "--local", help="Workers and reviewers on a local model (Ollama) instead of Claude/Codex."),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
) -> None:
    """Watch brindle work on a tiny practice repo: two workers in parallel, reviews, gated merges, verified milestones.

    Creates a small Python repo under ~/.brindle/demo/ with two failing test
    files and a two-milestone goal, then starts the supervisor there with
    autopilot on. Takes a few minutes; nothing is created where you run it."""
    from brindle import demo as demo_mod

    root = demo_mod.create(local=local)
    typer.echo(f"demo repo: {root}")
    os.chdir(root)
    start(agent="supervisor", prompt=None, provider=None, attach=attach, watch=True,
          autopilot=True, branch=None, worktree=None)


@app.command()
def new(
    branch: Optional[str] = typer.Argument(None, help="Branch for the workspace (created if needed). Required unless --pr is given."),
    base: Optional[str] = typer.Option(None, "--base", "-b", help="Base branch (default: repo default). Not allowed with --pr."),
    pr: Optional[int] = typer.Option(None, "--pr", help="Check out this GitHub PR's head branch (via `gh`), based on the PR's base branch. Don't also pass BRANCH or --base; the head branch is always fetched."),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Agent profile to start (default from config; 'none' for no agent)."),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p", help="First message for the agent."),
    provider: Optional[str] = typer.Option(None, help="Override the profile's provider (claude, codex, antigravity, shell)."),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Don't fetch the base branch first."),
    no_setup: bool = typer.Option(False, "--no-setup", help="Skip setup commands."),
    attach: bool = typer.Option(False, "--attach", help="Attach to the tmux session afterward."),
) -> None:
    """Create a worktree on a new branch and start an agent in it."""
    if pr is not None:
        if branch:
            _fail("pass either BRANCH or --pr, not both (--pr uses the PR's head branch)")
        if base:
            _fail("--base can't be combined with --pr (the PR's base branch is used)")
    elif not branch:
        _fail("missing BRANCH (or pass --pr <number>)")
    db = DB()
    if pr is not None:
        created = _run(workspaces.create_from_pr, db, os.getcwd(), pr, run_setup=not no_setup)
    else:
        created = _run(
            workspaces.create, db, os.getcwd(), branch, base,
            fetch=False if no_fetch else None, run_setup=not no_setup,
        )
    ws = created.workspace
    typer.secho(f"✓ {ws.id}", fg="green", bold=True)
    typer.echo(f"  branch  {ws.branch} ({created.how}, from {created.start_point})")
    typer.echo(f"  path    {ws.path}")
    typer.echo(f"  ports   {ws.port_base}-{ws.port_base + 9}  (BRINDLE_PORT_BASE)")
    if created.copied:
        typer.echo(f"  copied  {', '.join(created.copied)}")
    if created.setup:
        if created.setup.ok:
            typer.echo("  setup   ok")
        else:
            typer.secho(f"  setup   FAILED\n{created.setup.log}", fg="yellow")
            typer.echo(f"  (workspace kept; fix and re-run with `brindle setup {ws.name}`)")

    from brindle.config import load_repo_config

    profile = agent or load_repo_config(ws.repo_root).default_agent
    window = None
    if profile != "none":
        a = _run(agents.spawn, db, ws, profile, prompt=prompt, provider_name=provider)
        window = a.tmux_window
        typer.echo(f"  agent   {a.id} ({a.profile}/{a.provider})")
    typer.echo(f"\n  brindle attach {ws.name}")
    if attach:
        _attach(ws, window)


@app.command()
def start(
    agent: str = typer.Option("supervisor", "--agent", "-a", help="Agent profile."),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    provider: Optional[str] = typer.Option(None),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
    watch: bool = typer.Option(True, "--watch/--no-watch", help="Show the brindle watch dashboard in a pane under the agent."),
    autopilot: Optional[bool] = typer.Option(None, "--autopilot/--no-autopilot", help="The supervisor drives toward a goal until it's verified (default: on, or `autopilot` in .brindle/config.json)."),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Run in the worktree for this branch, creating the branch and worktree if needed."),
    worktree: Optional[str] = typer.Option(None, "--worktree", "-w", help="Run in the worktree at this path (created with --branch if it doesn't exist)."),
) -> None:
    """Start a fresh chat with an agent here (default: a supervisor), with the dashboard alongside.

    If a session is still running here, you choose: open it, start the new
    one in its own worktree (both run at once), or pause it and start fresh.
    Without a terminal to ask in, it's paused, and `brindle continue` brings
    it back. With --branch or --worktree it runs in that worktree instead,
    which gets the repo's .brindle config."""
    from brindle.config import load_repo_config

    db = DB()
    if branch or worktree:
        ws = _run(workspaces.checkout_for, db, os.getcwd(), branch=branch, worktree=worktree)
    else:
        ws = _here_or_scratch(db, reuse_scratch=False)
        running = _running_session(db, ws)
        if running and attach and sys.stdin.isatty():
            choice = _ask_about_running(running)
            if choice == "o":
                _attach(ws, running.tmux_window)
                return
            if choice == "n":
                ws = _run(_session_worktree, db, ws)
                typer.echo(f"✓ new session in its own worktree: {ws.path} ({ws.branch})")
    from brindle.providers import NOT_SUPERVISOR, SUPERVISOR_PROVIDERS

    if provider and agent == "supervisor" and provider in NOT_SUPERVISOR:
        _fail(f"the {provider} provider can't run the supervisor; use one of: "
              f"{', '.join(SUPERVISOR_PROVIDERS)}")
    _preflight(agent, provider, ws.repo_root)
    _trust_codex_hook(ws.repo_root)
    # Nothing slow before the chat starts: the paused session's leftover
    # processes, old paused sessions' worktrees and the pool refill are all
    # handled by the detached cull.
    _pause_running(db, ws, stop_procs=False)
    if autopilot is None:
        autopilot = agent == "supervisor" and _run(load_repo_config, ws.repo_root).autopilot
    a = _run(agents.spawn, db, ws, agent, prompt=prompt, provider_name=provider,
             watch_pane=watch, background_setup=True, autopilot=autopilot)
    # After the chat's window exists and is recorded: the cull's session
    # retention closes dropped sessions' windows by their stored pane ids,
    # which a freshly started tmux server hands out again from %0.
    _cull_detached(ws.repo_root)
    typer.echo(f"✓ {a.profile} agent {a.id} in {ws.id} ({ws.branch})")
    _local_models_detached(ws.repo_root)
    _settings_sync_detached()
    if autopilot:
        _say_autopilot(db, a.id)
    if attach:
        _attach(ws, a.tmux_window)


def _preflight(agent: str, provider: Optional[str], repo_root: str) -> None:
    """Stop before launching when the chat can't start (no tmux, no CLI)."""
    from brindle import doctor as doctor_mod
    from brindle.profiles import load_profile

    name = provider or _run(load_profile, agent, repo_root).provider
    problems = doctor_mod.preflight(name)
    if problems:
        for p in problems:
            typer.secho(f"✗ {p}", fg="red", err=True)
        _fail("brindle can't start yet. `brindle doctor` checks everything else.")


def _trust_codex_hook(repo_root: str) -> None:
    """With the permission policy on and Codex installed, trust brindle's Codex
    hook if it isn't yet (or brindle moved), and say so in one line, so Codex
    workers follow the policy from their first launch. A trust the person
    removed stays removed; `brindle doctor` points at it."""
    import shutil

    from brindle import codex_hook
    from brindle.providers import codex_binary

    try:
        binary = codex_binary()
        if not codex_hook.policy_on(repo_root) or not (shutil.which(binary) or os.path.isfile(binary)):
            return
        command = codex_hook.hook_command()
        before = codex_hook.status(command)
        if before not in (codex_hook.NEW, codex_hook.MOVED):
            return
        if codex_hook.ensure(binary, command) == codex_hook.TRUSTED:
            typer.echo("✓ trusted brindle's Codex permission hook (in ~/.codex/config.toml): "
                       "Codex workers follow permission_policy")
    except Exception:  # noqa: BLE001 - a convenience; never block the start
        pass


def _local_models_detached(repo_root: str) -> None:
    """Start the local model server the native profiles need, if it isn't
    running, without making the person wait (see brindle.native.serve). Says so
    on one line when there's something to start."""
    from brindle.config import load_repo_config
    from brindle.native import serve
    from brindle.providers import brindle_invocation

    try:
        pending = serve.needed(repo_root, load_repo_config(repo_root))
    except Exception:  # noqa: BLE001 -- a convenience; never block the start
        return
    if not pending:
        return
    who = ", ".join(sorted({n for s in pending for n in s.profiles}))
    typer.echo(f"  local models: starting ollama in the background for {who} (log: {serve.log_path()})")
    subprocess.Popen([*brindle_invocation(), "_local-models", "--repo", repo_root], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _settings_sync_detached() -> None:
    """Pull this person's synced settings (brindle Pro) without making them
    wait; the helper does nothing unless the plan includes settings sync."""
    from brindle.providers import brindle_invocation

    subprocess.Popen([*brindle_invocation(), "_sync-settings"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cull_detached(repo_root: str | None = None) -> None:
    """Clean up leftover agent processes and stale workers (and, given
    ``repo_root``, apply its paused-session retention) without making the
    person wait for it (see brindle.cull, brindle.sessions.enforce)."""
    from brindle.providers import brindle_invocation

    repo = ["--repo", repo_root] if repo_root else []
    subprocess.Popen([*brindle_invocation(), "_cull", *repo], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _say_autopilot(db: DB, root_id: str) -> None:
    from brindle import autopilot as pilot

    ap = db.get_autopilot(root_id)
    if ap and ap.goal:
        done, total = pilot.counts(db, root_id)
        typer.echo(f"  autopilot: {ap.goal} ({done} of {total} milestones verified)")
    else:
        typer.echo("  autopilot: on. Tell the supervisor what we're building.")
    typer.echo("  `brindle autopilot off` hands the wheel back to you.")


def _live_sessions(db: DB, ws: Workspace) -> list:
    """The live sessions (interactive agents) in this checkout. An agent whose
    recorded pane id a newer agent has since taken (tmux reuses ids) isn't
    live: that pane is the newer agent's."""
    owners = agents.pane_owners(db)
    return [a for a in db.list_agents(ws.id)
            if a.mode == "interactive" and a.status not in ("paused", "done")
            and agents.is_alive(a) and agents.owns_pane(db, a, owners)]


def _running_session(db: DB, ws: Workspace):
    """The live session (its interactive agent) in this checkout, if any."""
    live = _live_sessions(db, ws)
    return live[0] if live else None


def _ask_about_running(running) -> str:
    import click

    typer.echo(f"A brindle session is already running here ({running.id}).")
    typer.echo("  [o] open it   [n] new session in its own worktree   [p] pause it and start fresh")
    return typer.prompt("Which", type=click.Choice(["o", "n", "p"]), default="o", show_choices=False)


def _session_worktree(db: DB, ws: Workspace) -> Workspace:
    """A worktree for a second session in this repo: branch brindle/session-N,
    cut from what this checkout has checked out, so the two sessions never
    share files or a branch."""
    n = 2
    while git.ok(["rev-parse", "--verify", "--quiet", f"refs/heads/brindle/session-{n}"], ws.repo_root):
        n += 1
    return workspaces.create(db, ws.repo_root, f"brindle/session-{n}", base=ws.branch or None,
                             start=git.out(["rev-parse", "HEAD"], ws.path), apply_prefix=False).workspace


def _pause_running(db: DB, ws: Workspace, stop_procs: bool = True) -> None:
    """At most one live session per checkout: pause any that's still running.
    ``stop_procs=False`` when a detached cull follows (see agents.pause)."""
    from brindle import scratch

    for a in _live_sessions(db, ws):
        # A session starts here next: keep its local models loaded.
        agents.pause(db, a.id, stop_procs=stop_procs, stop_local_models=False)
        typer.echo(f"Paused the session that was still running here ({a.id}); "
                   f"`brindle continue {a.id}` brings it back.")
    # Chats left running in scratch sessions whose work moved into this repo.
    for aid in scratch.pause_transferred_to(db, ws.repo_root):
        typer.echo(f"Paused scratch session chat {aid}: its work moved into this repo; "
                   f"`brindle continue {aid}` brings it back.")


def _describe(s) -> str:
    workers = len(s.members) - 1
    ago = _ago(time.time() - s.paused_at)
    what = f", {workers} worker(s)" if workers else ""
    branches = f": {', '.join(s.branches[:3])}" + (" …" if len(s.branches) > 3 else "") if s.branches else ""
    return f"{s.root.id}  paused {ago} ago{what}{branches}"


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


@app.command("continue")
def continue_cmd(
    session_id: Optional[str] = typer.Argument(None, help="Session to resume (default: the most recent)."),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
) -> None:
    """Pick up a paused session: the chat and its workers resume where they stopped."""
    from brindle import scratch, sessions

    db = DB()
    cwd = os.getcwd()
    try:
        git.main_repo_root(cwd)
        ws = _run(workspaces.adopt_root, db, cwd)
    except git.GitError:
        ws = scratch.for_origin(db, cwd)
        if ws is None:
            _fail("no scratch session started from this folder to continue; run `brindle` to start one")
    available = sessions.paused(db, ws.repo_root)
    if not available:
        live = agents.find_running(db, ws, "supervisor")
        if live and attach:
            typer.echo(f"Session {live.id} is already running; reopening it.")
            _attach(ws, live.tmux_window)
            return
        _fail("no paused sessions here. Run `brindle` to start a fresh one.")
    if session_id:
        matches = [s for s in available if s.root.id.startswith(session_id)]
        if len(matches) != 1:
            _fail(f"no single paused session matches {session_id!r}. Available:\n  "
                  + "\n  ".join(_describe(s) for s in available))
        chosen = matches[0]
    else:
        chosen = available[0]
    others = [s for s in available if s.root.id != chosen.root.id]
    _pause_running(db, ws)
    resumed = _run(agents.resume, db, chosen.root.id)
    typer.secho(f"↺ continuing {_describe(chosen)} ({len(resumed)} agent(s) restarted)", fg="green")
    _local_models_detached(ws.repo_root)
    if others:
        typer.echo("Other paused sessions (brindle continue <id>):")
        for s in others:
            typer.echo(f"  {_describe(s)}")
    if attach:
        root = db.get_agent(chosen.root.id)
        _attach(chosen.workspace, root.tmux_window)


@app.command("sessions")
def sessions_cmd() -> None:
    """List paused sessions in this repo, with the disk their worktrees use."""
    from brindle import sessions

    db = DB()
    root = _run(git.main_repo_root, os.getcwd())
    found = sessions.paused(db, root)
    if not found:
        typer.echo("no paused sessions")
        return
    for s in found:
        # Members can share a workspace (a worker and its reviewer): count each once.
        spaces = {m.workspace_id: db.get_workspace(m.workspace_id) for m in s.members[1:]}
        size = sum(sessions.disk_usage(w.path) for w in spaces.values()
                   if w and w.kind == "worktree" and os.path.isdir(w.path))
        typer.echo(f"{_describe(s)}  ({size / 1e6:.0f} MB in worktrees)")
    typer.echo(f"Keeps the newest {sessions.KEEP} for up to {sessions.MAX_AGE_DAYS} days. `brindle prune` cleans up now.")


@app.command()
def prune() -> None:
    """Clean up now: old paused sessions, merged worktrees and leftover tmux sessions.

    Drops paused sessions beyond the newest few or older than a week, and old
    scratch sessions with nothing left to transfer. Removes the worktrees of
    finished workers whose branch is already merged, brindle tmux sessions that
    only hold idle shells and no running agent, leftover brindle tmux servers,
    stale locks and empty worktree folders. A removed worktree's branch goes
    too once fully merged (unless delete_merged_branches is false); nothing is
    merged, unmerged branches are kept, and worktrees with uncommitted changes
    stay."""
    from brindle import sessions

    db = DB()
    dropped = 0
    try:
        dropped = sessions.enforce(db, git.main_repo_root(os.getcwd()))
    except git.GitError:
        pass
    removed = sessions.prune_scratch(db)
    typer.echo(f"dropped {dropped} paused session(s), removed {removed} old scratch session(s)")
    from brindle import cull

    for line in cull.prune(db):
        typer.echo(line)


def _here_or_scratch(db: DB, reuse_scratch: bool) -> Workspace:
    """This checkout, or (outside any git repo) a scratch session for this folder."""
    from brindle import scratch

    cwd = os.getcwd()
    try:
        git.main_repo_root(cwd)
    except git.GitError:
        existing = scratch.for_origin(db, cwd) if reuse_scratch else None
        if existing:
            typer.echo(f"↺ scratch session {existing.id}")
            return existing
        ws = _run(scratch.create, db, cwd)
        typer.secho(f"No git repository here, so this is a scratch session, tracked in {ws.path}", fg="cyan")
        typer.echo("Nothing is created in this folder. When you're ready, move the work into a real repo:")
        typer.echo("  brindle transfer ~/path/to/repo    (or ask the supervisor to do it)")
        return ws
    _offer_pending_scratch(db, cwd)
    return _run(workspaces.adopt_root, db, cwd)


def _offer_pending_scratch(db: DB, cwd: str) -> None:
    """Inside a real repo: offer to bring in scratch work that hasn't moved yet."""
    from brindle import scratch

    if scratch.is_scratch(cwd) or not sys.stdin.isatty():
        return
    for s in scratch.pending(db)[:3]:
        n = scratch.commit_count(s)
        dirty = " + uncommitted changes" if git.dirty_files(s.path) else ""
        started = scratch.origin_of(s.path) or "?"
        if typer.confirm(f"Bring scratch session {s.name} ({n} commit(s){dirty}, started in {started}) into this repo?", default=False):
            _do_transfer(db, s, cwd, None)


def _do_transfer(db: DB, s: Workspace, target: str, branch: Optional[str]) -> None:
    from brindle import scratch

    t = _run(scratch.transfer, db, s, target, branch)
    extra = " (uncommitted work was committed first)" if t.snapshot else ""
    typer.secho(f"✓ moved {t.commits} commit(s){extra} onto branch {t.workspace.branch}", fg="green")
    typer.echo(f"  workspace {t.workspace.id} at {t.workspace.path}")
    typer.echo(f"  review: brindle diff {t.workspace.name}   merge: brindle merge {t.workspace.name}   PR: brindle pr {t.workspace.name}")


@app.command()
def transfer(
    target: Optional[str] = typer.Argument(None, help="A folder inside the real git repo (default: here)."),
    source: Optional[str] = typer.Option(None, "--from", help="Scratch session name or id (default: the one you're in, or the newest)."),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Branch to create (default: brindle/from-<session>)."),
) -> None:
    """Move a scratch session's work into a real repository, on its own branch."""
    from brindle import scratch

    db = DB()
    here = os.getcwd()
    if source:
        s = _ws(db, source)
    elif scratch.is_scratch(here):
        s = _ws(db, None)
    else:
        options = scratch.pending(db)
        if not options:
            _fail("no scratch sessions with work to transfer")
        s = options[0]
    dest = target or here
    if scratch.is_scratch(dest):
        _fail("give the path of the real repository to move the work into, e.g. brindle transfer ~/Projects/myapp")
    _do_transfer(db, s, dest, branch)


@app.command()
def handover(
    to: str = typer.Option(..., "--to", help="Branch or worktree path for the new supervisor (created if needed)."),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Handoff note: the new supervisor's first message includes it."),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
) -> None:
    """Hand this repo's supervisor session to a new supervisor on another branch or worktree.

    The goal and milestones, workers, queued tasks and your note move to the
    new supervisor; the old one is paused."""
    from brindle import sessions

    db = DB()
    root_id = _session_root(db)
    dest = _run(workspaces.checkout_for_target, db, os.getcwd(), to)
    new = _run(sessions.handover, db, root_id, dest, note)
    typer.secho(f"✓ handed {root_id} over to {new.id} in {dest.id} ({dest.branch})", fg="green")
    _cull_detached(dest.repo_root)
    if attach:
        _attach(dest, new.tmux_window)


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    cont: bool = typer.Option(False, "--continue", "-c", help="Pick up the most recent paused session instead of starting fresh."),
    autopilot: Optional[bool] = typer.Option(None, "--autopilot/--no-autopilot", help="Start with autopilot on or off (default: on, or `autopilot` in .brindle/config.json)."),
    provider: Optional[str] = typer.Option(None, "--provider", help="Run the supervisor on this CLI instead of Claude Code (codex, antigravity)."),
    show_version: bool = typer.Option(False, "--version", help="Print brindle's version and exit."),
) -> None:
    """Bare `brindle`: a fresh supervisor chat here (a scratch session outside git)."""
    if show_version:
        from brindle import __version__

        typer.echo(f"brindle {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        from brindle import legacy

        try:
            legacy_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            legacy_root = None
        legacy.warn_once(legacy_root)
        if cont:
            continue_cmd(session_id=None, attach=True)
        else:
            start(agent="supervisor", prompt=None, provider=provider, attach=True, watch=True,
                  autopilot=autopilot, branch=None, worktree=None)


def _session_root(db: DB) -> str:
    """This repo's current session: the one running here, else the newest
    paused one."""
    from brindle import sessions

    cwd = os.getcwd()
    try:
        ws = workspaces.adopt_root(db, cwd)
    except git.GitError:
        from brindle import scratch

        ws = scratch.for_origin(db, cwd) or workspaces.current(db)
        if ws is None:
            _fail("no brindle session here")
    live = agents.find_running(db, ws, "supervisor")
    if live:
        return live.id
    found = sessions.paused(db, ws.repo_root)
    if found:
        return found[0].root.id
    _fail("no brindle session here. Run `brindle` to start one.")
    raise AssertionError


@app.command("delegation")
def delegation_cmd(
    level: Optional[str] = typer.Argument(None, help="conservative, balanced or fast. Omit to show the current one."),
    repo: bool = typer.Option(False, "--repo", help="Only for this repo (.brindle/config.local.json), not every repo."),
) -> None:
    """How readily the supervisor hands work to workers: conservative, balanced (default) or fast.

    conservative does most work in its own chat (fewest tokens); fast splits
    work across parallel workers straight away (quickest, most tokens). It's
    saved in ~/.brindle/config.json for every repo and session (with --repo,
    for this repo only), and a running supervisor is told at once."""
    from brindle import autopilot as pilot
    from brindle.config import RepoConfig, load_repo_config, set_local, set_user, user_settings

    try:
        root = git.out(["rev-parse", "--show-toplevel"], os.getcwd())
    except git.GitError:
        root = None
    if level is None:
        current = load_repo_config(root).delegation if root else \
            user_settings().get("delegation", RepoConfig().delegation)
        typer.echo(f"delegation: {current}  (conservative, balanced, fast)")
        return
    if level not in pilot.DELEGATIONS:
        _fail("delegation must be conservative, balanced or fast")
    if repo:
        if not root:
            _fail("--repo needs to run inside a git repo")
        set_local(root, "delegation", level)
        typer.echo(f"✓ delegation {level} for this repo (.brindle/config.local.json)")
    else:
        path = set_user("delegation", level)
        typer.echo(f"✓ delegation {level} for every repo ({path})")
    _tell_supervisors_about_delegation(DB())


def _tell_supervisors_about_delegation(db: DB) -> None:
    """Send each running supervisor its repo's delegation rule as it now stands."""
    from brindle import autopilot as pilot
    from brindle.config import load_repo_config

    for ws in db.find_workspaces():
        running = _running_session(db, ws)
        if not running:
            continue
        try:
            rule = pilot.delegation_rule(load_repo_config(ws.repo_root))
            agents.send_message(db, running.id, "[brindle] The person changed how readily you delegate. "
                                "From now on this replaces your earlier delegation rule:\n\n" + rule)
            typer.echo(f"  told the running supervisor ({running.id})")
        except (agents.AgentError, ValueError):
            pass


@app.command("autopilot")
def autopilot_cmd(
    action: Optional[str] = typer.Argument(None, help="on, off, or check (run the milestone checks now). Omit to show progress."),
) -> None:
    """Show the goal's progress, or turn autopilot on or off.

    With autopilot on, the supervisor keeps working until every milestone's
    check passes or it needs you."""
    from brindle import autopilot as pilot

    db = DB()
    root_id = _session_root(db)
    root = db.get_agent(root_id)
    if action in ("on", "off"):
        on = action == "on"
        pilot.set_enabled(db, root_id, on)
        if root and agents.is_alive(root):
            note = ("Autopilot is on again: keep driving toward the goal (get_progress shows where it stands)."
                    if on else "Autopilot is now off: stop driving and wait for the user's instructions.")
            try:
                agents.send_message(db, root_id, f"[brindle autopilot] {note}")
            except agents.AgentError:
                pass
        typer.echo(f"autopilot {action} for session {root_id}")
        if on and not (root and agents.is_alive(root)):
            typer.echo("It takes effect when the session runs: `brindle continue`.")
        return
    if action == "check":
        ws = db.get_workspace(root.workspace_id) if root else None
        if ws is None or db.get_autopilot(root_id) is None:
            _fail("autopilot isn't set up for this session")
        typer.echo(_run(pilot.check_milestones, db, root_id, ws))
        return
    if action:
        _fail(f"unknown action {action!r}: use on, off or check")
    typer.echo(pilot.progress(db, root_id))
    u = pilot.usage()
    if u:
        typer.echo(pilot.usage_note(u))


@app.command()
def doctor() -> None:
    """Check that brindle has what it needs, and say what to do about anything missing.

    tmux, the agent CLIs, a writable home, leftover processes; in a repo, its
    config, checks and code map."""
    from brindle import doctor as doctor_mod

    root = None
    try:
        root = git.main_repo_root(os.getcwd())
    except git.GitError:
        pass
    results = doctor_mod.checks(root)
    typer.echo(doctor_mod.render(results))
    if any(c.level == doctor_mod.FAIL for c in results):
        raise typer.Exit(1)


@app.command("ls")
def list_cmd(
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON array instead of a table."),
) -> None:
    """List workspaces and their agents."""
    db = DB()
    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    rows = view.session_workspaces(db, repo_root)
    panes = tmux.list_panes()
    if as_json:
        typer.echo(json.dumps([view.workspace_entry(db, ws, panes=panes) for ws in rows], indent=2))
        return
    if not rows:
        typer.echo("no workspaces")
        return
    several = len({ws.repo_root for ws in rows}) > 1
    shown_repo = None
    for ws in rows:
        if several and ws.repo_root != shown_repo:
            shown_repo = ws.repo_root
            typer.secho(f"{os.path.basename(ws.repo_root.rstrip(os.sep))}  ({ws.repo_root})", fg="cyan")
        if not os.path.isdir(ws.path):
            typer.secho(f"{ws.id}  (missing: {ws.path})", fg="red")
            continue
        e = view.workspace_entry(db, ws, panes=panes)
        info = ""
        if e["ahead"] is not None:
            dirty = f" *{e['dirty']}" if e["dirty"] else ""
            info = f"  ↑{e['ahead']} ↓{e['behind']}{dirty} vs {ws.base_branch}"
        typer.secho(f"{ws.id}", bold=True, nl=False)
        typer.echo(f"  [{ws.branch}]{info}")
        for a in e["agents"]:
            tokens = f"  {a['tokens']}" if a.get("tokens") else ""
            typer.echo(f"    {a['id']}  {a['profile']:<12} {a['provider']:<7} {a['status']:<11} {a['mode']}{tokens}")


@app.command()
def history(
    limit: int = typer.Option(50, "--limit", help="Most recent rows to show."),
    kind: Optional[str] = typer.Option(
        None, "--kind", help=f"Only this kind: one of {', '.join(history_mod.KINDS)}."
    ),
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    share: bool = typer.Option(False, "--share", help="Summarize this repo's session in a few lines to paste into Slack or a post."),
    session: Optional[str] = typer.Option(None, "--session", help="With --share: this session (an id from `brindle sessions`) instead of the current one."),
) -> None:
    """Durable history of worker results, reviews, merges and milestone checks."""
    db = DB()
    if share:
        root_id = session or _session_root(db)
        typer.echo(_run(history_mod.share_card, db, root_id))
        return
    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            typer.echo("not in a git repo: showing all repos")
    rows = db.list_history(repo_root, kind, limit)
    if not rows:
        typer.echo("no history")
        return
    total = 0
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r.ts))
        tokens = history_mod.tokens_summary(r.tokens)
        total += history_mod.tokens_total(r.tokens)
        branch = r.branch or "-"
        typer.echo(
            f"{when}  {r.kind:<13} {branch:<28} {tokens:<16} {history_mod.row_summary(r)}"
        )
    typer.echo(f"\ntotal tokens: {format_tokens(total)}")


# -- brindle cost (dollar spend from history; see brindle.cost) ------------------------------------

cost_app = typer.Typer(invoke_without_command=True,
                       help="Dollar spend at list prices: bare `brindle cost` is the last 30 days; "
                            "`brindle cost report` (brindle Pro) breaks it down.")
app.add_typer(cost_app, name="cost")


def _cost_repo(all_repos: bool) -> str | None:
    if all_repos:
        return None
    repo_root = _here_repo()
    if repo_root is None:
        typer.echo("not in a git repo: showing all repos")
    return repo_root


@cost_app.callback()
def cost_cmd(
    ctx: typer.Context,
    days: int = typer.Option(30, "--days", min=1, help="How many days back."),
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
) -> None:
    """Spend over the last 30 days, by model."""
    if ctx.invoked_subcommand is not None:
        return
    from brindle import cost

    repo_root = _cost_repo(all_repos)
    typer.echo(cost.describe_summary(cost.summary(DB(), repo_root, days=days), repo_root))


@cost_app.command("report")
def cost_report(
    days: int = typer.Option(30, "--days", min=1, help="How many days back."),
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
) -> None:
    """brindle Pro: spend by day, profile and goal, cost per merged branch, review pass rates."""
    from brindle import cost

    try:
        cost.require_entitled()
    except cost.NotEntitled as e:
        _fail(str(e))
    repo_root = _cost_repo(all_repos)
    typer.echo(cost.describe_report(cost.report(DB(), repo_root, days=days), repo_root))


@cost_app.command("request")
def cost_request_cmd(
    usd: float = typer.Option(..., "--usd", help="How many more dollars you need."),
    reason: str = typer.Option(..., "--reason", help="Why, for the admin who approves it."),
    goal: Optional[str] = typer.Option(None, "--goal", help="Raise this goal's budget (its first line) "
                                       "instead of this month's."),
) -> None:
    """brindle Enterprise: ask an admin to approve spend over your budget."""
    from brindle.pro import cost_centers

    repo_root = _here_repo()
    if repo_root is None:
        _fail("not in a git repo")
    first = (goal or "").strip().splitlines()[0] if (goal or "").strip() else None
    try:
        rec = cost_centers.request(usd, reason, repo_root, scope="goal" if first else "month", goal=first)
    except cost_centers.CostCenterError as e:
        _fail(str(e))
    typer.echo(f"Filed request {rec['id']} for ${rec['amount_usd']:.2f}. Once an admin approves it, "
               f"the {rec['scope']} limit goes up by that much; check with `brindle cost requests`.")


@cost_app.command("requests")
def cost_requests_cmd() -> None:
    """brindle Enterprise: your cost approval requests; checks the pending ones with the server."""
    from brindle.pro import cost_centers

    if not cost_centers.held():
        _fail('cost centers are an Enterprise feature ("cost_centers"); your plan doesn\'t include it. '
              "See `brindle account`.")
    for line in cost_centers.poll():
        typer.echo(line)
    typer.echo(cost_centers.describe(cost_centers.tracked()))


@cost_app.command("estimate")
def cost_estimate_cmd(
    tasks_n: Optional[int] = typer.Option(
        None, "--tasks", help="How many tasks. Default: the current goal's unverified milestones, else 1."),
    weight: Optional[str] = typer.Option(None, "--weight", help="light, medium or heavy."),
    profile: Optional[str] = typer.Option(None, "--profile", help="Worker profile (default: the repo's default_agent)."),
    reviewer: Optional[str] = typer.Option(None, "--reviewer", help="Reviewer profile (default: the repo's reviewer)."),
) -> None:
    """Estimate a goal's cost from this repo's history: a range, and a cheaper alternative."""
    from brindle import cost_estimate
    from brindle.config import WEIGHTS, load_repo_config

    if not cost_estimate.entitled():
        _fail("cost estimates are a brindle Pro feature (\"cost\"); your plan doesn't include it. "
              "See `brindle account`.")
    if weight is not None and weight not in WEIGHTS:
        _fail(f"--weight must be one of {', '.join(WEIGHTS)}")
    try:
        repo_root = git.main_repo_root(os.getcwd())
    except git.GitError:
        _fail("not in a git repo")
    db = DB()
    cfg = load_repo_config(repo_root)
    specs = []
    if tasks_n is None:
        try:
            live = agents.find_running(db, workspaces.adopt_root(db, os.getcwd()), "supervisor")
        except git.GitError:
            live = None
        root_id = live.id if live else None
        if root_id:
            specs = [cost_estimate.TaskSpec(profile or m.profile or cfg.default_agent, weight)
                     for m in db.milestones(root_id) if m.status != "passed"]
        if not specs:
            tasks_n = 1
    if tasks_n is not None:
        if tasks_n < 1:
            _fail("--tasks must be at least 1")
        specs = [cost_estimate.TaskSpec(profile or cfg.default_agent, weight)] * tasks_n
    typer.echo(cost_estimate.describe(specs, reviewer or cfg.reviewer, repo_root=repo_root, db=db))


# -- brindle permissions (the permission policy; see brindle.permissions) -------------------------

permissions_app = typer.Typer(no_args_is_help=True,
                              help="The rules brindle answers workers' permission requests with "
                                   "(when permission_policy is \"on\").")
app.add_typer(permissions_app, name="permissions")


def _here_repo() -> str | None:
    try:
        return git.main_repo_root(os.getcwd())
    except git.GitError:
        return None


@permissions_app.command("list")
def permissions_list() -> None:
    """Every rule in force (built-in, this repo's denies, yours and learned), with its source."""
    from brindle import permissions as perms

    repo = _here_repo()
    if repo:
        from brindle.config import load_repo_config

        state = load_repo_config(repo).permission_policy
        typer.echo(f"permission_policy: {state if state == 'on' else 'off'}\n")
    for r in perms.all_rules(repo):
        typer.echo(f"{r.id:<12} {r.decision:<5} {r.kind:<5} {r.source:<7} {r.describe()}")


@permissions_app.command("check")
def permissions_check(profile: str | None = typer.Option(None, "--profile", help="Profile name to check.")) -> None:
    """Show the effective rule set for each provider without changing settings."""
    from brindle import permissions as perms

    repo = _here_repo()
    try:
        selected_profiles = [load_profile(profile, repo)] if profile else list_profiles(repo)
    except KeyError as e:
        typer.echo(str(e))
        raise typer.Exit(2)
    from brindle import antigravity

    base_rules = perms.all_rules(repo)
    agy = antigravity.settings_report()
    mirrored = bool(agy["mirror_repos"])
    warned: set[tuple[str, str]] = set()
    for selected in selected_profiles:
        profile_denies = perms.profile_rules(selected)
        rules = [*base_rules, *profile_denies]
        typer.echo(f"\nProfile {selected.name}: effective policy")
        for provider in ("Claude Code", "Codex"):
            typer.echo(f"\n{provider} (brindle hook):")
            for rule in rules:
                typer.echo(f"  {rule.decision:5} {rule.kind:5} {rule.describe()} ({rule.source})")
        typer.echo("\nAntigravity:")
        for rule in rules:
            if rule in profile_denies:
                # agy's settings.json is shared by every agy agent, so a
                # profile's own denies can only live in the hook.
                typer.echo(f"  deny  {rule.kind:5} {rule.describe()} (profile; brindle hook only)")
                continue
            entry = perms._agy_entry(rule)
            if rule.match_type == "git-readonly":
                entry = "built-in git status/diff/log/show allow entries"
            elif rule.match_type == "git-push":
                entry = "built-in git push deny entry"
            where = "settings + hook" if mirrored else "hook; settings mirror off"
            if entry:
                typer.echo(f"  {rule.decision:5} {rule.kind:5} {entry} ({rule.source}; {where})")
            elif rule.decision == "deny":
                typer.echo(f"  deny  {rule.kind:5} {rule.describe()} (brindle hook only)")
            else:
                typer.echo(f"  ask   {rule.kind:5} {rule.describe()} (hook allow is ignored)")
                key = (rule.decision, rule.describe())
                if key not in warned:
                    typer.echo(f"warning: Antigravity ignores hook allow for {rule.describe()}; no settings equivalent")
                    warned.add(key)
        for invalid in perms.invalid_profile_rules(selected):
            typer.echo(f"warning: dropped invalid permission_denies entry {invalid!r}")
    typer.echo(f"\nAntigravity settings ({agy['path']}), as agy reads them:")
    typer.echo("  brindle mirror: " + (f"on for {', '.join(agy['mirror_repos'])}" if mirrored
                                      else "off (no repo with permission_policy on)"))
    if agy["error"]:
        typer.echo(f"  {agy['error']}")
    elif not agy["entries"]:
        typer.echo("  no permissions entries")
    for key, entry, owner in agy["entries"]:
        typer.echo(f"  {key:5} {entry} ({'brindle mirror' if owner == 'brindle' else 'yours, not managed by brindle'})")


@permissions_app.command("suggestions")
def permissions_suggestions() -> None:
    """Requests you approved at least twice that no rule covers yet (accept one with `accept ID`)."""
    from brindle import permissions as perms

    rows = perms.suggestions()
    if not rows:
        typer.echo("no suggestions")
        return
    for s in rows:
        typer.echo(f"{s.id:<12} allow {s.kind:<5} exact {s.match!r}  (approved {s.count}x)")


@permissions_app.command("accept")
def permissions_accept(rule_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Turn a suggestion into an allow rule."""
    from brindle import permissions as perms

    rule = perms.accept(rule_id)
    if rule is None:
        typer.echo(f"no suggestion {rule_id} (see `brindle permissions suggestions`)")
        raise typer.Exit(1)
    typer.echo(f"{rule.id}: allow {rule.describe()} (learned)")
    _resync_agy()


def _add_permission_rule(decision: str, kind: str, match: str, prefix: bool, glob: bool) -> None:
    from brindle import permissions as perms

    if prefix and glob:
        typer.echo("--prefix and --glob can't be combined")
        raise typer.Exit(2)
    try:
        rule = perms.add_rule(kind, match, decision, "prefix" if prefix else "glob" if glob else "exact")
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(2)
    typer.echo(f"{rule.id}: {decision} {rule.describe()}")
    _resync_agy()


_KIND_HELP = "read, write, edit, bash, fetch, mcp or other."


@permissions_app.command("allow")
def permissions_allow(
    kind: str = typer.Argument(..., help=_KIND_HELP),
    match: str = typer.Argument(..., help="The command, path, URL or tool name (exact unless --prefix/--glob)."),
    prefix: bool = typer.Option(False, "--prefix", help="Match anything starting with MATCH."),
    glob: bool = typer.Option(False, "--glob", help="MATCH is a shell-style glob (* also crosses /)."),
) -> None:
    """Allow requests that match. A bash allow never covers a command with shell metacharacters."""
    _add_permission_rule("allow", kind, match, prefix, glob)


@permissions_app.command("deny")
def permissions_deny(
    kind: str = typer.Argument(..., help=_KIND_HELP),
    match: str = typer.Argument(..., help="The command, path, URL or tool name (exact unless --prefix/--glob)."),
    prefix: bool = typer.Option(False, "--prefix", help="Match anything starting with MATCH."),
    glob: bool = typer.Option(False, "--glob", help="MATCH is a shell-style glob (* also crosses /)."),
) -> None:
    """Deny requests that match (a deny beats any allow)."""
    _add_permission_rule("deny", kind, match, prefix, glob)


@permissions_app.command("forget")
def permissions_forget(rule_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Remove one of your or learned rules (or a suggestion's approval count). Built-in rules stay."""
    from brindle import permissions as perms

    gone = perms.forget(rule_id)
    if gone is None:
        typer.echo(f"no rule or suggestion {rule_id} of yours (built-in and repo rules can't be forgotten)")
        raise typer.Exit(1)
    typer.echo(f"forgot {gone}")
    _resync_agy()


@permissions_app.command("reset")
def permissions_reset(yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation.")) -> None:
    """Back to the built-in rules: drop your rules, learned rules and approval counts."""
    from brindle import permissions as perms

    if not yes:
        typer.confirm("Remove all your and learned permission rules and approval counts?",
                      default=False, abort=True)
    perms.reset()
    typer.echo("permission rules reset to the defaults")
    _resync_agy()


def _resync_agy() -> None:
    """Keep the copy of brindle's rules in agy's settings current (only if brindle keeps one)."""
    from brindle import antigravity

    antigravity.resync_permissions()


@permissions_app.command("sync-agy")
def permissions_sync_agy() -> None:
    """Mirror your rules into Antigravity's settings (~/.gemini/antigravity-cli/settings.json).

    agy ignores a hook's "allow", so brindle adds the allow rules agy can express to
    its "permissions.allow" (and denies to "deny"), and changes only entries it added.
    Run in a repo: it counts as on or off by that repo's permission_policy; with
    every repo off, brindle's entries are removed."""
    from brindle import antigravity
    from brindle.config import load_repo_config

    repo = _here_repo()
    cfg = load_repo_config(repo) if repo else None
    try:
        if cfg is None:
            added, removed = antigravity.sync_permissions()
        else:
            on = cfg.permission_policy == "on"
            added, removed = antigravity.sync_permissions(repo, on=on, checks=list(cfg.checks or []))
    except antigravity.SettingsError as e:
        typer.echo(str(e))
        raise typer.Exit(1)
    path = antigravity.settings_path()
    for e in added:
        typer.echo(f"+ {e}")
    for e in removed:
        typer.echo(f"- {e}")
    typer.echo(f"{path}: {len(added)} added, {len(removed)} removed"
               if added or removed else f"{path}: already in sync")


@permissions_app.command("install-codex-hook")
def permissions_install_codex_hook(
    yes: bool = typer.Option(False, "--yes", "-y", help="Make the change (without it, only show it)."),
) -> None:
    """Trust brindle's Codex permission hook now, for every worktree.

    Codex runs a hook only once it's trusted. brindle passes its hook to the
    Codex agents it starts (only when permission_policy is on), with the same
    command for every agent, and trusts it itself the first time it's needed;
    this does it now, or gives back a trust removed from Codex's config. It's
    recorded in Codex's config.toml (hooks.state), through Codex itself."""
    from brindle import codex_hook
    from brindle.providers import codex_binary

    binary = codex_binary()
    try:
        state = codex_hook.inspect(binary)
    except codex_hook.CodexHookError as e:
        typer.echo(f"couldn't ask Codex about the hook: {e}")
        raise typer.Exit(1)
    typer.echo(f"hook command: {state.command}")
    if state.status == "trusted" and codex_hook.trusted(state.command):
        typer.echo("already trusted; nothing to change")
        return
    typer.echo(f"Codex reports it as: {state.status}")
    typer.echo(f"will set in {state.config}:")
    typer.echo(f'  [hooks.state."{state.key}"]')
    typer.echo(f'  trusted_hash = "{state.hash}"')
    typer.echo("and remember it in ~/.brindle/permissions.json (codex_hook)")
    if not yes:
        typer.echo("nothing changed; run again with --yes to trust it")
        return
    try:
        codex_hook.trust(binary, state)
    except codex_hook.CodexHookError as e:
        typer.echo(f"couldn't record the trust: {e}")
        raise typer.Exit(1)
    typer.echo("trusted: Codex workers get brindle's permission hook while permission_policy is on")


# -- brindle profile (agent profiles: extends, rule packs) ----------------------------------

profile_app = typer.Typer(no_args_is_help=True,
                          help="Agent profiles: create one, check them, see one resolved "
                               "(its `extends` parent and rule packs folded in).")
app.add_typer(profile_app, name="profile")

PROFILE_TEMPLATE = """---
name: {name}
description: {description}
{extends}{rules}---
{prompt}
"""


@profile_app.command("new")
def profile_new(
    name: str = typer.Argument(..., help="The profile's name (its file is <name>.md)."),
    extends: Optional[str] = typer.Option(None, "--extends", "-e", help="Parent profile to build on (e.g. developer)."),
    rules: Optional[str] = typer.Option(None, "--rules", "-r", help="Rule packs, comma-separated (e.g. security/backend,style/minimal-diff)."),
    description: str = typer.Option("", "--description", "-d"),
    user: bool = typer.Option(False, "--user", help="Write to ~/.brindle/agents instead of this repo's .brindle/agents."),
) -> None:
    """Write a new profile file to .brindle/agents/ (or ~/.brindle/agents with --user)."""
    from brindle.config import user_profiles_dir
    from brindle.profiles import ProfileError, load_profile, load_rule_pack

    if not _PROFILE_NAME.match(name):
        _fail(f"profile name {name!r}: use letters, digits, '-', '_' and '.'")
    repo = _here_repo()
    if user:
        target_dir = user_profiles_dir()
    elif repo:
        target_dir = Path(repo) / ".brindle" / "agents"
    else:
        _fail("not in a git repository: run this in the repo, or pass --user for ~/.brindle/agents")
    target = target_dir / f"{name}.md"
    if target.exists():
        _fail(f"{target} already exists")
    if extends:
        try:
            load_profile(extends, repo)
        except (KeyError, ProfileError) as e:
            _fail(f"--extends {extends}: {e}")
    packs = [p.strip() for p in (rules or "").split(",") if p.strip()]
    for pack in packs:
        try:
            load_rule_pack(pack, repo)
        except (KeyError, ProfileError) as e:
            _fail(f"--rules {pack}: {e}")
    if extends:
        prompt = ("Additional instructions for this role go here; they follow the parent's prompt.")
    else:
        prompt = "You are an agent running under brindle. Describe the role here."
    text = PROFILE_TEMPLATE.format(
        name=name,
        description=description or f"A {name} agent",
        extends=f"extends: {extends}\n" if extends else "provider: claude\n",
        rules=f"rules: {', '.join(packs)}\n" if packs else "",
        prompt=prompt,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    typer.echo(f"✓ wrote {target}")
    typer.echo(f"  check it with: brindle profile show {name}")


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _lint_one(name: str, repo: str | None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one profile."""
    from brindle import providers
    from brindle.profiles import (ProfileError, load_profile, load_rule_pack, missing_add_dirs)
    from brindle.rule_checks import compile_patterns

    errors: list[str] = []
    warnings: list[str] = []
    try:
        p = load_profile(name, repo)
    except (KeyError, ProfileError) as e:
        return [str(e)], []
    try:
        providers.get_provider(p.provider)
    except KeyError as e:
        errors.append(str(e))
    for pack_name in p.rules:
        try:
            pack = load_rule_pack(pack_name, repo)
        except (KeyError, ProfileError) as e:
            errors.append(str(e))
            continue
        _, bad = compile_patterns(pack)
        errors.extend(f"rule pack {pack_name}: deny_patterns entry doesn't compile: {b}" for b in bad)
        if not pack.prompt and not pack.mechanical:
            warnings.append(f"rule pack {pack_name} is empty")
    errors.extend(f"permission_denies: invalid entry {e}" for e in p.permission_denies_errors)
    if not p.prompt.strip():
        warnings.append("the prompt is empty")
    for d in missing_add_dirs(p):
        warnings.append(f"add_dirs entry doesn't exist: {d}")
    return errors, warnings


@profile_app.command("lint")
def profile_lint(
    name: Optional[str] = typer.Argument(None, help="One profile; default: every profile visible here."),
) -> None:
    """Check profiles: extends chains, rule packs, patterns, providers and permission denies."""
    from brindle.profiles import profile_names

    repo = _here_repo()
    names = [name] if name else profile_names(repo)
    failed = 0
    for n in names:
        errors, warnings = _lint_one(n, repo)
        if errors:
            failed += 1
            typer.secho(f"✗ {n}", fg="red")
        elif warnings:
            typer.secho(f"! {n}", fg="yellow")
        else:
            typer.echo(f"✓ {n}")
        for e in errors:
            typer.secho(f"    error: {e}", fg="red")
        for w in warnings:
            typer.secho(f"    warning: {w}", fg="yellow")
    if failed:
        raise typer.Exit(1)


@profile_app.command("show")
def profile_show(
    name: str,
    prompt_only: bool = typer.Option(False, "--prompt", help="Print only the resolved prompt (with the rule packs' text)."),
) -> None:
    """Print a profile as brindle resolves it: fields, extends chain, rule packs and prompt."""
    from dataclasses import MISSING, fields

    from brindle.profiles import (ProfileError, load_profile, load_rule_packs, profile_source,
                                  rules_prompt)

    repo = _here_repo()
    try:
        p = load_profile(name, repo)
        packs = load_rule_packs(p, repo)
    except (KeyError, ProfileError) as e:
        _fail(str(e))
    full = f"{p.prompt.strip()}\n\n{rules_prompt(packs)}".strip() if packs else p.prompt.strip()
    if prompt_only:
        typer.echo(full)
        return
    typer.echo(f"{p.name}: {p.description}")
    typer.echo(f"  source    {profile_source(name, repo)}")
    chain, parent = [], p.extends
    while parent:
        chain.append(parent)
        try:
            parent = load_profile(parent, repo).extends
        except (KeyError, ProfileError):
            break
    if chain:
        typer.echo(f"  extends   {' -> '.join(chain)}")
    skip = {"name", "description", "prompt", "extends", "rules", "permission_denies_errors"}
    for f in fields(p):
        if f.name in skip:
            continue
        value = getattr(p, f.name)
        default = f.default if f.default_factory is MISSING else f.default_factory()  # type: ignore[misc]
        if value in (None, False, [], {}) or value == default:
            continue
        shown = ", ".join(value) if isinstance(value, list) else (
            " ".join(f"{k}={v}" for k, v in value.items()) if isinstance(value, dict) else str(value))
        typer.echo(f"  {f.name:<9} {shown}")
    if packs:
        typer.echo("  rules")
        for pack in packs:
            typer.echo(f"    {pack.name:<22} {pack.description}  [{pack.source}]")
            if pack.deny_deps:
                typer.echo(f"      deny_deps: {', '.join(pack.deny_deps)}")
            if pack.require_tests_for:
                typer.echo(f"      require_tests_for: {', '.join(pack.require_tests_for)}")
            for pat in pack.deny_patterns:
                typer.echo(f"      deny_pattern: {pat}")
    typer.echo("")
    typer.echo(full)


# -- brindle rules (learned rules, brindle Pro) ----------------------------------------------

rules_app = typer.Typer(no_args_is_help=True,
                        help="Learned rules (brindle Pro): rules suggested from review findings "
                             "that keep recurring, to add to .brindle/rules/learned.md or reject.")
app.add_typer(rules_app, name="rules")


def _rules_repo() -> str:
    try:
        return git.main_repo_root(os.getcwd())
    except git.GitError:
        _fail("not in a git repo")


@rules_app.command("suggest")
def rules_suggest(
    refresh: bool = typer.Option(True, "--refresh/--no-refresh",
                                 help="Group new review findings first (one call to a cheap or local model)."),
) -> None:
    """Show the rules suggested from review findings that recurred across tasks."""
    from brindle import learned_rules

    repo = _rules_repo()
    db = DB()
    try:
        learned_rules.require_entitled()
        if refresh:
            done = learned_rules.refresh(db, repo)
            if done.called:
                typer.echo(f"grouped {done.findings} new review finding(s)")
    except learned_rules.LearnedRulesError as e:
        _fail(str(e))
    found = learned_rules.suggestions(db, repo)
    if not found:
        n = learned_rules.repeats(repo)
        typer.echo(f"no suggestions: a rule is suggested once the same review finding has come "
                   f"up in {n} tasks of one profile's work")
        return
    for r in found:
        typer.echo(f"{r.key}  {r.title}  ({learned_rules.task_count(r)} tasks, profile {r.profile})")
        typer.echo(f"    {r.rule}")
        for k, items in learned_rules.checks_of(r).items():
            typer.echo(f"    {k}: {', '.join(items)}")
    typer.echo("\naccept one with `brindle rules accept <key>` (adds it to .brindle/rules/learned.md), "
               "or `brindle rules reject <key>` so it isn't suggested again")


@rules_app.command("accept")
def rules_accept(key: str = typer.Argument(..., help="The suggestion's key (or its start).")) -> None:
    """Add a suggested rule to .brindle/rules/learned.md, which every profile in the repo uses."""
    from brindle import learned_rules

    repo = _rules_repo()
    try:
        r, path = learned_rules.accept(DB(), repo, key)
    except learned_rules.LearnedRulesError as e:
        _fail(str(e))
    typer.echo(f"✓ added rule {r.key} to {path}")
    typer.echo("  commit it so the rule is reviewed and shared; check it with "
               "`brindle profile show developer`")


@rules_app.command("reject")
def rules_reject(key: str = typer.Argument(..., help="The suggestion's key (or its start).")) -> None:
    """Reject a suggested rule: it is remembered and never suggested again."""
    from brindle import learned_rules

    try:
        r = learned_rules.reject(DB(), _rules_repo(), key)
    except learned_rules.LearnedRulesError as e:
        _fail(str(e))
    typer.echo(f"✓ rejected rule {r.key}; it won't be suggested again")


@app.command("_learn-rules", hidden=True)
def learn_rules_cmd(repo_root: str) -> None:
    """Group new review findings in the background (learned_rules.refresh_later)."""
    import fcntl

    from brindle import learned_rules
    from brindle.config import brindle_home

    db = _helper_db()
    with open(brindle_home() / "learned-rules.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return      # another refresh is at it; it picks this finding up too, or the next one does
        try:
            learned_rules.refresh(db, repo_root)
        except learned_rules.LearnedRulesError as e:
            typer.echo(f"brindle: learned rules: {e}", err=True)


@app.command()
def learning() -> None:
    """What brindle Pro's hosted learner has learned about which profiles fit which tasks (nothing is learned on this machine)."""
    from brindle import learning as learning_mod
    from brindle.config import load_repo_config

    try:
        repo_root = git.main_repo_root(os.getcwd())
    except git.GitError:
        typer.echo("not in a git repo")
        raise typer.Exit(1)
    cfg = load_repo_config(repo_root)
    from brindle import plugins

    name = plugins.learning_name(cfg)
    if name == plugins.OFF:
        configured = (cfg.learning or plugins.OFF).strip()
        if configured == plugins.AUTO:
            typer.echo("learning is off: hosted learning needs brindle Pro "
                       "(`brindle account` shows your plan; `brindle account upgrade` gets it). "
                       "Nothing is learned on this machine.")
        elif configured == plugins.OFF:
            typer.echo('learning is off. Set "learning" to "auto" or "cloud" in .brindle/config.json '
                       "to use hosted learning (brindle Pro).")
        else:
            typer.echo(f'learning is off: "learning": {configured!r} is not a supported value '
                       '(use "auto", "cloud" or "off"). Learning is hosted only (brindle Pro).')
        return
    p = learning_mod.plugin(cfg, repo_root)
    if p is None:
        typer.echo("hosted learning is unavailable")
        raise typer.Exit(1)
    typer.echo(p.report())


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True,
                               "help_option_names": []})   # --help is the plugin's: it lists every subcommand
def account(ctx: typer.Context) -> None:
    """brindle Pro/Team: paid features, login, upgrade, billing, orgs. Bare `brindle account` shows what you have."""
    from brindle import account as account_mod
    from brindle.config import load_repo_config

    try:
        repo_root = git.main_repo_root(os.getcwd())
    except git.GitError:
        repo_root = os.getcwd()
    try:
        cfg = load_repo_config(repo_root)
    except ValueError:
        from brindle.config import RepoConfig

        cfg = RepoConfig()
    raise typer.Exit(account_mod.run(cfg, repo_root, list(ctx.args), echo=typer.echo))


# -- brindle audit (brindle Enterprise: the local tamper-evident audit chain) ---------------------

audit_app = typer.Typer(no_args_is_help=True,
                        help="brindle Enterprise: the local tamper-evident audit log "
                             "(~/.brindle/audit; see `src/brindle/pro/audit_chain.py`).")
app.add_typer(audit_app, name="audit")


def _audit_repo(repo: Optional[str]) -> str:
    """The main repo root for ``--repo`` (default: here), or the path as given."""
    start = repo or os.getcwd()
    try:
        return git.main_repo_root(start)
    except git.GitError:
        return os.path.abspath(start)


org_app = typer.Typer(help="Your org's messages (brindle Pro Team).")
app.add_typer(org_app, name="org")


@org_app.command("messages")
def org_messages(
    keep: bool = typer.Option(False, "--keep", help="Don't mark the messages read."),
) -> None:
    """List the notices your org's admins sent you, and mark them read."""
    from brindle.pro import status

    notes, fresh = status.fetch_messages()
    if not notes:
        typer.echo("No new messages." if fresh else "No messages (couldn't reach the org; "
                   "these are the last ones seen).")
        return
    for n in notes:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(n["at"])) if n["at"] else ""
        who = n["from_role"] + (" (to everyone)" if n["scope"] == "org" else "")
        typer.echo(f"{when}  {who}: {n['text']}".strip())
    if fresh and not keep:
        status.mark_read([n["id"] for n in notes])


@audit_app.command("verify")
def audit_verify(
    repo: Optional[str] = typer.Option(None, "--repo", help="The repo whose log to verify (default: here)."),
) -> None:
    """Recompute the chain and check every signature; exit 1 at the first broken record."""
    from brindle.pro import audit_chain

    try:
        report = audit_chain.verify(_audit_repo(repo))
    except audit_chain.AuditError as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)
    typer.echo(report.describe())
    if not report.ok:
        raise typer.Exit(1)


@audit_app.command("export")
def audit_export(
    repo: Optional[str] = typer.Option(None, "--repo", help="The repo whose log to export (default: here)."),
    since: Optional[str] = typer.Option(None, "--since", help="Only records from this ISO 8601 time on."),
    fmt: str = typer.Option("jsonl", "--format", help="jsonl or csv."),
) -> None:
    """Print the audit records (unverified) as JSONL or CSV."""
    from brindle.pro import audit_chain

    start = None
    if since:
        try:
            start = audit_chain.parse_time(since)
        except ValueError:
            typer.echo(f"audit: --since wants an ISO 8601 time, not {since!r}")
            raise typer.Exit(2)
    try:
        out = audit_chain.export(_audit_repo(repo), since=start, fmt=fmt)
    except audit_chain.AuditError as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)
    sys.stdout.write(out)
    sys.stdout.flush()


@audit_app.command("ship")
def audit_ship(
    force: bool = typer.Option(False, "--force", help="Retry sinks that are backing off now."),
) -> None:
    """Send the audit records to the configured sinks now (brindle Enterprise, audit_export); exit 1 if a sink failed."""
    from brindle.pro import audit_export

    exporter = audit_export.AuditExporter()
    try:
        cfg = exporter.config()
    except audit_export.NotConfigured as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)
    if not cfg.sinks:
        typer.echo('audit: no sinks; add "audit_export": {"sinks": [...]} to ~/.brindle/config.json')
        raise typer.Exit(2)
    if not exporter.entitled():
        typer.echo("audit: your plan doesn't include audit export (brindle Enterprise)")
        raise typer.Exit(2)
    from brindle import airgap

    if airgap.enabled():
        typer.echo(f"audit: air-gap mode is on via {airgap.source()}: nothing is sent")
        raise typer.Exit(2)
    results = exporter.flush(force=force)
    if not results:
        typer.echo("audit: another brindle process is sending right now; run this again in a moment")
        raise typer.Exit(1)
    failed = False
    for name, res in results.items():
        failed = failed or bool(res.error)
        typer.echo(f"{name}: sent {res.sent}" + (f", dropped {res.dropped}" if res.dropped else "")
                   + (f"; FAILED: {res.error}" if res.error else ""))
    if failed:
        raise typer.Exit(1)


@audit_app.command("prune")
def audit_prune(
    repo: Optional[str] = typer.Option(None, "--repo", help="The repo whose log to prune (default: every log)."),
    days: Optional[float] = typer.Option(None, "--days", help="Keep this many days (default: audit_export.retention_days)."),
) -> None:
    """Drop audit records older than the retention, leaving a signed checkpoint so `brindle audit verify` still passes."""
    from brindle.pro import audit_chain, audit_export

    exporter = audit_export.AuditExporter()
    try:
        keep = days if days is not None else exporter.config().retention_days
    except audit_export.NotConfigured as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)
    if not keep or keep <= 0:
        typer.echo('audit: give --days or set "audit_export": {"retention_days": N} in ~/.brindle/config.json')
        raise typer.Exit(2)
    if not exporter.entitled():
        typer.echo("audit: your plan doesn't include audit export (brindle Enterprise)")
        raise typer.Exit(2)
    try:
        dropped = exporter.prune(keep, repo_root=_audit_repo(repo) if repo else None)
    except audit_chain.AuditError as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)
    typer.echo(f"pruned {sum(dropped.values())} record(s) older than {keep:g} day(s)"
               + "".join(f"\n  {n}: {k}" for n, k in dropped.items()))


@audit_app.command("pubkey")
def audit_pubkey() -> None:
    """This install's Ed25519 public key (hex), which every audit record is signed with."""
    from brindle.pro import audit_chain

    try:
        typer.echo(audit_chain.public_key_hex())
    except audit_chain.AuditError as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)


@app.command()
def watch(
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    once: bool = typer.Option(False, "--once", help="Print one snapshot and exit."),
    sidebar: bool = typer.Option(False, "--sidebar", hidden=True),
) -> None:
    """Live dashboard of workspaces and agents (highlights agents waiting on you)."""
    from brindle import watch as watch_mod

    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    if once or not sys.stdout.isatty():
        typer.echo(watch_mod.print_once(DB(), repo_root, color=sys.stdout.isatty()))
        return
    watch_mod.run(repo_root, sidebar=sidebar)
    if sidebar:
        # Quit on purpose (a crash raises instead): keep it gone, so switching
        # windows doesn't bring it back. `brindle continue` starts a fresh one.
        agents.dismiss_sidebar(DB(), os.environ.get("TMUX_PANE"))


@app.command()
def close(
    agent_id: Optional[str] = typer.Argument(None, help="Agent to close (an unambiguous prefix works)."),
    exited: bool = typer.Option(False, "--exited", help="Close every agent that has stopped or finished."),
    all_repos: bool = typer.Option(False, "--all", help="With --exited: every repo, not just this one."),
) -> None:
    """Hide agents from the dashboard for good, stopping any still running.

    Their worktrees, branches and records stay; `brindle ls` still lists them."""
    db = DB()
    if exited == bool(agent_id):
        _fail("pass an agent id, or --exited")
    panes = tmux.list_panes()
    if agent_id:
        target = _run(agents.get, db, agent_id)
        was_running = agents.is_alive(target, panes) and agents.owns_pane(db, target)
        a = _run(agents.close, db, agent_id, panes)
        typer.echo(f"✓ closed {a.id}" + (" (stopped it first)" if was_running else ""))
        return
    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    alive = view.live_agents(db, panes)
    closed = [a for ws in db.find_workspaces(repo_root) for a in db.list_agents(ws.id)
              if a.dismissed_at is None and (a.id not in alive or a.status == "done")]
    for a in closed:
        agents.close(db, a.id, panes)
    typer.echo(f"✓ closed {len(closed)} agent(s)" if closed else "nothing to close")


@app.command()
def attach(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Attach to a workspace's tmux session, at the agent that needs you (else its busiest or newest agent)."""
    if not sys.stdin.isatty():
        # From a chat's `!` or a script there's no terminal to attach: tmux
        # would switch whatever client it finds instead, or nothing at all.
        _fail("brindle attach needs a terminal: run it in a terminal window, or select the "
              "agent in the brindle sidebar and press ⏎.")
    db = DB()
    ws = _ws(db, workspace)
    _attach(ws, agents.attach_target(db, ws))


@app.command()
def cd(workspace: str) -> None:
    """Print a workspace's path (use: cd "$(brindle cd NAME)")."""
    typer.echo(_ws(DB(), workspace).path)


@app.command("open")
def open_cmd(
    workspace: Optional[str] = typer.Argument(None),
    editor: str = typer.Option(os.environ.get("BRINDLE_EDITOR", "code"), help="Editor command."),
) -> None:
    """Open a workspace in your editor."""
    subprocess.run([editor, _ws(DB(), workspace).path])


@app.command()
def setup(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Re-run setup commands in a workspace."""
    from brindle.config import load_repo_config

    ws = _ws(DB(), workspace)
    cmds = load_repo_config(ws.repo_root).setup
    res = workspaces.run_commands(cmds, ws.path, workspaces.workspace_env(ws))
    typer.echo(res.log or "(no setup commands)")
    raise typer.Exit(0 if res.ok else 1)


@app.command()
def status(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Branch status against base: ahead/behind, uncommitted files, unpushed."""
    ws = _ws(DB(), workspace)
    st = _run(git.status, ws.path, ws.base_branch)
    typer.echo(f"{ws.id}  [{st.branch}]  base {st.base or '-'}")
    typer.echo(f"  {st.ahead} ahead, {st.behind} behind")
    typer.echo(f"  unpushed: {'no upstream' if st.unpushed is None else st.unpushed}")
    for f in st.dirty_files:
        typer.echo(f"  M {f}")


@app.command()
def diff(
    workspace: Optional[str] = typer.Argument(None),
    stat: bool = typer.Option(False, "--stat"),
) -> None:
    """Everything the branch changes vs. its base (commits + uncommitted)."""
    ws = _ws(DB(), workspace)
    text = _run(git.diff, ws.path, workspaces.require_base(ws), stat)
    if sys.stdout.isatty() and not stat:
        subprocess.run(["less", "-R"], input=text, text=True)
    else:
        typer.echo(text or "(no changes)")


@app.command()
def sync(
    workspace: Optional[str] = typer.Argument(None),
    merge: bool = typer.Option(False, "--merge", help="Merge base in instead of rebasing."),
) -> None:
    """Bring the base branch's latest commits into this workspace."""
    ws = _ws(DB(), workspace)
    if git.dirty_files(ws.path):
        _fail("uncommitted changes; commit or stash first")
    ref = _run(git.sync, ws.path, workspaces.require_base(ws), "merge" if merge else "rebase")
    typer.echo(f"✓ {ws.branch} is up to date with {ref}")


@app.command()
def commit(
    workspace: Optional[str] = typer.Argument(None),
    message: str = typer.Option(..., "--message", "-m"),
) -> None:
    """Stage everything and commit in a workspace."""
    ws = _ws(DB(), workspace)
    sha = _run(git.commit_all, ws.path, message)
    typer.echo(f"✓ {sha}" if sha else "nothing to commit")


@app.command()
def push(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Push the workspace branch and set its upstream."""
    ws = _ws(DB(), workspace)
    _run(git.push, ws.path, ws.branch)
    typer.echo(f"✓ pushed {ws.branch}")


@app.command()
def pr(
    workspace: Optional[str] = typer.Argument(None),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    draft: bool = typer.Option(False, "--draft"),
) -> None:
    """Push and open a pull request against the base branch."""
    ws = _ws(DB(), workspace)
    url = _run(workspaces.pull_request, ws, title, draft)
    typer.echo(url)
    _offer_delete_on_merge(ws.repo_root)


def _offer_delete_on_merge(repo_root: str) -> None:
    """The first PR in a repo where GitHub keeps merged branches: offer to
    turn on its automatic deletion (with the person's own gh login)."""
    if not workspaces.keeps_merged_branches(repo_root):
        return
    how = "`gh repo edit --delete-branch-on-merge`"
    if sys.stdin.isatty() and typer.confirm(
            "GitHub keeps this repo's branches after their PRs merge. Delete them automatically?",
            default=True, err=True):
        if workspaces.delete_branches_on_merge(repo_root):
            typer.echo("✓ GitHub now deletes a PR's branch when it merges.", err=True)
            return
        typer.secho(f"couldn't change it (it needs admin rights on the repo); an admin can run {how}.",
                    fg="yellow", err=True)
    elif not sys.stdin.isatty():
        typer.secho(f"tip: GitHub keeps this repo's branches after their PRs merge; {how} "
                    "deletes them automatically.", fg="yellow", err=True)


@app.command("merge")
def merge_cmd(
    workspace: Optional[str] = typer.Argument(None),
    squash: bool = typer.Option(False, "--squash"),
) -> None:
    """Merge the workspace branch into its base branch locally."""
    db = DB()
    ws = _ws(db, workspace)
    target = _run(workspaces.merge_back, db, ws, squash)
    typer.echo(f"✓ merged {ws.branch} into {ws.base_branch} ({target})")


@app.command("services")
def services_cmd(
    action: str = typer.Argument("ls", help="ls, up or down."),
    workspace: Optional[str] = typer.Argument(None, help="Workspace (default: the current one)."),
) -> None:
    """Per-worktree Docker services (brindle Pro): list, start or stop a workspace's."""
    from brindle import services as services_mod
    from brindle.config import load_repo_config

    if action not in ("ls", "up", "down"):
        _fail("action must be ls, up or down")
    ws = _ws(DB(), workspace)
    cfg = load_repo_config(ws.repo_root)
    if not cfg.services:
        _fail('no services configured; add a "services" list to .brindle/config.json')
    if action == "up":
        done = services_mod.up(ws, cfg)
        typer.echo("started: " + (", ".join(done) or "nothing"))
    elif action == "down":
        done = services_mod.down(ws, cfg)
        typer.echo("stopped: " + (", ".join(done) or "nothing"))
    else:
        lines = services_mod.status(ws)
        typer.echo("\n".join(lines) if lines else f"no services running for {ws.id}")


@app.command()
def rm(
    workspace: str,
    force: bool = typer.Option(False, "--force", "-f", help="Discard uncommitted changes; ignore teardown failure."),
    delete_branch: Optional[bool] = typer.Option(None, "--delete-branch/--keep-branch", "-D/-K", help="Delete the branch (only if merged, unless --force), or keep it. Default: delete it once fully merged into its base."),
) -> None:
    """Stop a workspace's agents and remove its worktree; a fully merged branch goes too.

    An unmerged branch is always kept; -K keeps any branch, and a repo can
    set delete_merged_branches to false."""
    db = DB()
    ws = _ws(db, workspace)
    if ws.base_branch and os.path.isdir(ws.path) and not delete_branch:
        try:
            st = git.status(ws.path, ws.base_branch)
        except git.GitError:
            st = None  # e.g. the base branch is gone; the note is only a courtesy
        if st and st.ahead and st.unpushed != 0:
            typer.secho(
                f"note: {ws.branch} has {st.ahead} commit(s) not in {ws.base_branch} "
                "and not pushed; the branch is kept.", fg="yellow",
            )
    removed = _run(workspaces.remove, db, ws, force=force, delete_branch=delete_branch)
    if removed.teardown and not removed.teardown.ok:
        typer.secho(f"teardown failed (ignored with --force):\n{removed.teardown.log}", fg="yellow")
    typer.echo(f"✓ removed {ws.id}. {removed.branch_note or 'branch deleted'}")


# -- agents -----------------------------------------------------------------


@agent_app.command("spawn")
def agent_spawn(
    profile: str,
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    provider: Optional[str] = typer.Option(None),
) -> None:
    """Start another agent in an existing workspace."""
    db = DB()
    ws = _ws(db, workspace)
    a = _run(agents.spawn, db, ws, profile, prompt=prompt, provider_name=provider)
    typer.echo(f"✓ {a.id} ({a.profile}/{a.provider}) in {ws.id}")


@agent_app.command("profiles")
def agent_profiles() -> None:
    """List available agent profiles."""
    root = None
    try:
        root = git.main_repo_root(os.getcwd())
    except git.GitError:
        pass
    from brindle.providers import unusable

    why: dict[tuple, str | None] = {}
    hidden: dict[str, list[str]] = {}
    for p in list_profiles(root):
        # A profile's env can carry the CLI's key, so one on a signed-out CLI can still run.
        key = (p.provider, tuple(sorted(p.env.items())))
        if key not in why:
            why[key] = unusable(p.provider, p.env)
        if why[key]:
            hidden.setdefault(why[key], []).append(p.name)
            continue
        typer.echo(f"{p.name:<14} {p.provider:<7} {p.description}")
    for reason, names in hidden.items():
        typer.secho(f"hidden ({reason}): {', '.join(names)}", dim=True)


@agent_app.command("kill")
def agent_kill(agent_id: str) -> None:
    """Stop an agent and close its window."""
    db = DB()
    _run(agents.kill, db, agent_id)
    typer.echo(f"✓ killed {agent_id}")


@agent_app.command("peek")
def agent_peek(agent_id: str, lines: int = typer.Option(40, "--lines", "-n")) -> None:
    """Print the last lines of an agent's terminal."""
    db = DB()
    a = _run(agents.get, db, agent_id)
    if not a.tmux_window:
        typer.secho(f"{a.id} has no terminal (it runs as its supervisor's own subagent)"
                    if not agents.runs_process(a) else f"{a.id} has no terminal", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(tmux.capture(a.tmux_window, lines=lines).rstrip())


@agent_app.command("turns")
def agent_turns(agent_id: str) -> None:
    """List a worker's turn snapshots (what `brindle agent rewind --to` can go back to)."""
    from brindle import rewind

    db = DB()
    a = _run(agents.get, db, agent_id)
    ws = db.get_workspace(a.workspace_id)
    if ws is None:
        _fail(f"{a.id}'s workspace is gone")
    typer.echo(rewind.format_turns(ws.repo_root, a.id))


@agent_app.command("rewind")
def agent_rewind(
    agent_id: str,
    to: int = typer.Option(..., "--to", help="the turn to go back to (see `brindle agent turns`)"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="run the fresh session with this profile"),
    note: Optional[str] = typer.Option(None, "--note", help="what to tell the fresh session"),
) -> None:
    """Rewind a worker to the state after turn N and start a fresh session there.

    Its worktree (branch, uncommitted edits, untracked files) goes back to how
    it was after that turn, the worker is stopped, and a fresh session starts in
    its workspace briefed on the original task, turns 1..N and your note.
    """
    from brindle import rewind

    db = DB()
    try:
        fresh = rewind.rewind(db, agent_id, to, profile=profile, note=note)
    except (rewind.RewindError, agents.AgentError, git.GitError, FileNotFoundError, ValueError) as e:
        _fail(str(e))
        raise AssertionError
    ws = db.get_workspace(fresh.workspace_id)
    typer.echo(f"✓ rewound {agent_id} to turn {to}; {fresh.id} ({fresh.profile}/{fresh.provider}) "
               f"continues in {ws.id if ws else fresh.workspace_id}")


@app.command()
def send(agent_id: str, message: str) -> None:
    """Send a message to an agent (queued until it's idle)."""
    db = DB()
    outcome = _run(agents.send_message, db, agent_id, message, person=True)
    typer.echo(outcome)


@app.command()
def mcp() -> None:
    """Run the brindle MCP server on stdio (agents launch this automatically)."""
    from brindle.mcp_server import main

    main()


# -- brindle ci (brindle Team) ---------------------------------------------------

ci_app = typer.Typer(no_args_is_help=True,
                     help="brindle CI (brindle Team): the client the CI workflows run. "
                          "`brindle ci init` sets a repository up.")
app.add_typer(ci_app, name="ci")


def _ci_call(fn, *args, **kwargs):
    from brindle.ci_client import CIError, refuse_airgap

    try:
        refuse_airgap()
        return fn(*args, **kwargs)
    except CIError as e:
        _fail(f"brindle ci: {e}")


def _ci_providers(env, repo: Optional[str], names: Optional[str], org: Optional[bool] = None) -> list[str]:
    from brindle import ci_adapters

    if names:
        return sorted({n.strip() for n in names.split(",") if n.strip()})
    return ci_adapters.providers_available(ci_adapters.default_adapters(os.getcwd()), env,
                                           ci_adapters.repo_is_org(env, repo) if org is None else org)


@ci_app.command("start")
def ci_start(
    repo: Optional[str] = typer.Option(None, "--repo", help="owner/name (default: GITHUB_REPOSITORY)."),
    out: str = typer.Option(..., "--out", help="Directory for the plan and run token (the run job's artifact)."),
    providers: Optional[str] = typer.Option(None, "--providers", help="Comma-separated provider names whose keys the workflow found."),
    issue: Optional[int] = typer.Option(None, "--issue", help="Work on this issue."),
    goal_text: Optional[str] = typer.Option(None, "--goal-text", help="Work on this text (first line: title)."),
    dispatch: Optional[str] = typer.Option(None, "--dispatch", help="Claim a run the server created (run_...)."),
    validate: bool = typer.Option(False, "--validate", help="Validate a pull request instead (with --pr and --head)."),
    pr: Optional[int] = typer.Option(None, "--pr", help="The pull request to validate."),
    head: Optional[str] = typer.Option(None, "--head", help="Its head commit (40 hex)."),
    fork: Optional[str] = typer.Option(None, "--fork", help="true when the pull request comes from a fork."),
) -> None:
    """Start a brindle CI run or validation and write its plan and run token to --out (needs BRINDLE_PRO_TOKEN)."""
    from brindle import ci_client

    def go():
        from brindle import ci_hosts

        host = ci_hosts.get_host(env=os.environ)
        full = host.repo(os.environ, repo)
        is_fork = ci_client.parse_bool(fork)
        if validate and is_fork is None:
            is_fork = host.is_fork(os.environ, full)
        trigger = ci_client.trigger_for(issue, goal_text, dispatch, validate=validate, pr=pr, head=head,
                                        fork=is_fork)
        code = ci_client.start(full, trigger, out, client=host.client(os.environ),
                               token=ci_client.ci_token(os.environ),
                               providers=_ci_providers(os.environ, full, providers, host.org_hint), say=typer.echo)
        raise typer.Exit(code)

    _ci_call(go)


@ci_app.command("run")
def ci_run(
    plan: str = typer.Option(..., "--plan", help="The plan file written by `brindle ci start`."),
    run_token_file: str = typer.Option(..., "--run-token-file", help="The run token file (deleted once read)."),
) -> None:
    """Run a started brindle CI run or validation here, then upload the result or the evidence."""
    from brindle import ci_client

    def go():
        try:
            token = Path(plan).read_text("utf-8").strip()
        except OSError as e:
            raise ci_client.CIError(f"can't read the plan: {e.strerror or e}")
        from brindle import ci_hosts

        host = ci_hosts.get_host(env=os.environ)
        ci_client.run(token, run_token_file, cwd=os.getcwd(), env=os.environ, client=host.client(os.environ),
                      repo=host.repo(os.environ), org=host.org_hint,
                      texts=lambda: ci_client.read_plan_texts(plan), say=typer.echo)

    _ci_call(go)


@ci_app.command("report")
def ci_report(
    plan_dir: str = typer.Option(..., "--plan-dir", help="The directory `brindle ci start` wrote to."),
    start: Optional[str] = typer.Option(None, "--start", help="How the start job ended: success, failure or cancelled."),
    run: Optional[str] = typer.Option(None, "--run", help="How the run job ended: success, failure or cancelled."),
) -> None:
    """Report how the start and run jobs ended (needs BRINDLE_PRO_TOKEN)."""
    from brindle import ci_client

    def go():
        from brindle import ci_hosts

        host = ci_hosts.get_host(env=os.environ)
        ci_client.report(plan_dir, client=host.client(os.environ), token=ci_client.ci_token(os.environ),
                         start=start, run=run, say=typer.echo)

    _ci_call(go)


@ci_app.command("doctor")
def ci_doctor(
    repo: Optional[str] = typer.Option(None, "--repo", help="owner/name (default: GITHUB_REPOSITORY)."),
) -> None:
    """Which provider CLIs and credential names are here, and which providers CI may use on this repository."""
    from brindle import ci_client, ci_hosts

    def go():
        env = os.environ
        if ci_hosts.GitLabHost.detect(env) and not ci_hosts.GitHubHost.detect(env):
            # the job's own project and the host's owner rule, not `gh`
            full, org = env.get("CI_PROJECT_PATH") or repo, ci_hosts.GitLabHost.org_hint
        else:
            full, org = env.get("GITHUB_REPOSITORY") or repo, None
        typer.echo(ci_client.doctor(env, full, os.getcwd(), org=org))

    _ci_call(go)


@ci_app.command("init")
def ci_init(
    repo: Optional[str] = typer.Option(None, "--repo", help="owner/name (default: the repository here)."),
    org: Optional[str] = typer.Option(None, "--org", help="The brindle Team org whose CI token to use."),
    providers: Optional[str] = typer.Option(None, "--providers", help="Comma-separated providers to set keys for (asked otherwise)."),
    credential: Optional[str] = typer.Option(None, "--credential", help="How Claude signs in: key (the ANTHROPIC_API_KEY secret) or federation (workload identity federation; asked otherwise)."),
    host: str = typer.Option("github", "--host", help="github (default) or gitlab (brindle Enterprise: writes .gitlab-ci.yml)."),
    force: bool = typer.Option(False, "--force", help="With --host gitlab: replace an existing .gitlab-ci.yml."),
    workspace_id: Optional[str] = typer.Option(None, "--workspace-id", help="With an organization-level API key or identity federation: the workspace (wrkspc_...), stored as the ANTHROPIC_WORKSPACE_ID variable (asked otherwise, defaulting to $ANTHROPIC_WORKSPACE_ID)."),
    rule_id: Optional[str] = typer.Option(None, "--rule-id", help="Identity federation: the rule ID (fdrl_...; asked otherwise, defaulting to $ANTHROPIC_FEDERATION_RULE_ID)."),
    organization_id: Optional[str] = typer.Option(None, "--organization-id", help="Identity federation: the Anthropic organization ID (asked otherwise, defaulting to $ANTHROPIC_ORGANIZATION_ID)."),
    service_account_id: Optional[str] = typer.Option(None, "--service-account-id", help="Identity federation: the service account ID (svac_...; asked otherwise, defaulting to $ANTHROPIC_SERVICE_ACCOUNT_ID)."),
    required_check: Optional[str] = typer.Option(None, "--required-check", metavar="NAME", help="When the default branch requires no status checks: make this check (a workflow job's name) required, without asking. brindle only fixes builds where a required check fails. Unattended (no terminal), this is the only way init makes a check required."),
    no_required_check: bool = typer.Option(False, "--no-required-check", help="When the default branch requires no status checks: only warn, don't offer to make a job required."),
) -> None:
    """Set a repository up for brindle CI: the GitHub App, the secrets, the issue label, a required check, the workflows (as a pull request), then doctor.

    Without a terminal on stdin nothing is asked: each question takes its default (the options, then the environment), and a yes/no question that would change the repository's settings is answered no."""
    from brindle import ci_client, ci_hosts

    host = host.strip().lower()
    if host == ci_hosts.GITLAB:
        _ci_call(ci_hosts.init_gitlab, cwd=os.getcwd(), force=force, say=typer.echo)
        return
    if host != ci_hosts.GITHUB:
        _fail(f"brindle ci: host must be {' or '.join(ci_hosts.HOSTS)}")
    if force:
        _fail("brindle ci: --force only applies with --host gitlab")
    if required_check and no_required_check:
        _fail("--required-check and --no-required-check don't go together")
    names = [p.strip() for p in providers.split(",") if p.strip()] if providers else None

    def ask(question: str, default: str) -> str:
        if not sys.stdin.isatty():
            # a [Y/n] question changes the repository's settings: unattended, only an option says yes
            return "n" if question.endswith("[Y/n]") else default
        # a [Y/n] question shows its own default
        return typer.prompt(question, default=default, show_default=not question.endswith("[Y/n]"))

    _ci_call(ci_client.init, repo=repo, org=org, providers=names, cwd=os.getcwd(), env=os.environ,
             credential=credential, workspace_id=workspace_id, rule_id=rule_id, organization_id=organization_id,
             service_account_id=service_account_id, required_check=required_check,
             no_required_check=no_required_check, ask=ask, say=typer.echo)


# -- internal ----------------------------------------------------------------


@app.command("_hook", hidden=True)
def hook(event: str, agent: Optional[str] = typer.Option(None, "--agent"),
         payload: Optional[str] = typer.Argument(None)) -> None:
    if event == "agy-pre-tool":
        # agy reads no answer as deny, so this always prints one (ask on any failure).
        from brindle import antigravity

        try:
            text = sys.stdin.read()
        except Exception:  # noqa: BLE001
            text = ""
        typer.echo(antigravity.pre_tool_main(text))
        return
    if event.startswith("agy-"):
        from brindle import antigravity

        typer.echo(antigravity.hook_main(DB(), event, sys.stdin.read()))
        return
    # --agent is baked into the hook command at launch; the environment is
    # only a fallback for sessions launched by an older brindle (and may be stale).
    # Codex's permission hook is the same command for every agent (so one
    # trust covers them all): Codex passes the agent's environment through.
    agent_id = agent or os.environ.get("BRINDLE_AGENT_ID")
    if not agent_id and event == "codex-permission-request":
        try:
            from brindle import antigravity

            agent_id = antigravity.agent_from_parent()
        except Exception:  # noqa: BLE001
            agent_id = None
    if not agent_id:
        return
    # Codex's notify passes the JSON as an argument; Claude Code's hooks use stdin.
    text = payload if payload is not None else sys.stdin.read()
    if event in agents.PERMISSION_EVENTS:
        try:  # any failure means no output: the person is asked as usual
            out = agents.hook_main(DB(), agent_id, event, text, trusted=agent is not None)
        except Exception:  # noqa: BLE001
            return
    else:
        out = agents.hook_main(DB(), agent_id, event, text, trusted=agent is not None)
    if out:
        typer.echo(out)


def _helper_db() -> DB:
    """The DB for a detached helper (``_after-launch``, ``_cull``, ...). One
    can outlive whatever started it; if its brindle home is gone by then (a
    finished test run's temp dir), it stops rather than recreate the home
    and act on an empty DB."""
    from brindle.config import db_path

    if not db_path().exists():
        raise typer.Exit(0)
    return DB()


@app.command("_after-launch", hidden=True)
def after_launch_cmd(agent_id: str) -> None:
    from brindle.providers import get_provider

    db = _helper_db()
    a = db.get_agent(agent_id)
    if a and a.tmux_window:
        get_provider(a.provider).after_launch(a.tmux_window)
        agents.ready(db, agent_id)


@app.command("_headless", hidden=True)
def headless_cmd(agent_id: str, resume: Optional[str] = typer.Option(None)) -> None:
    """A headless worker's pane: runs its `claude -p` turns (agents.run_headless)."""
    raise typer.Exit(agents.run_headless(DB(), agent_id, resume))


@app.command("_native", hidden=True)
def _native(agent_id: str, resume: Optional[str] = typer.Option(None, "--resume")):
    """A native worker's pane: brindle's own agent loop (brindle.native.runner)."""
    from brindle.native import runner

    raise typer.Exit(runner.run_native(DB(), agent_id, resume))


@app.command("_ended", hidden=True)
def ended_cmd(agent_id: str) -> None:
    import signal

    # Runs inside the window it's about to close; don't die with it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    agents.ended(DB(), agent_id)


@app.command("_quit", hidden=True)
def quit_cmd(root_id: str) -> None:
    import signal

    # Runs detached from the sidebar it's about to close; don't die with it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    db = DB()
    root = db.get_agent(root_id)
    if root is not None and root.status not in ("paused", "done"):
        agents.pause(db, root_id)


@app.command("_close", hidden=True)
def close_cmd(agent_id: str, delay: float = typer.Option(0.0)) -> None:
    time.sleep(delay)
    try:
        agents.kill(_helper_db(), agent_id)
    except agents.AgentError:
        pass


@app.command("_statusline", hidden=True)
def statusline_cmd() -> None:
    from brindle.providers import status_line

    out = status_line(sys.stdin.read())
    if out:
        typer.echo(out)


@app.command("sidebar")
def sidebar_cmd(session: Optional[str] = typer.Option(None, "--session", hidden=True)) -> None:
    """Bring this brindle session's sidebar into the tmux session you're in.

    The sidebar follows you between brindle windows and sessions, but it can be
    left behind in another session (say a worker's that has since been
    removed). Run this from the chat's tmux session to pull it back; if it was
    closed it is started again. In tmux, `prefix S` does the same when this
    window has no sidebar, and otherwise hides/shows it."""
    from brindle.sidebar_follow import sidebar_here

    name = session or tmux.current_session()
    if not name:
        _fail("not inside a brindle tmux session: run this from the chat's window")
    try:
        typer.echo(sidebar_here(DB(), name))
    except ValueError as e:
        _fail(str(e))


@app.command("_sidebar-follow", hidden=True)
def sidebar_follow_cmd(session: str) -> None:
    """Run from the session-window-changed / client-session-changed hooks
    tmux.apply_theme sets on every brindle session: relocate the sidebar pane
    here (see agents.sidebar_follow). Never raises: this runs from a tmux
    hook, where an uncaught error would show as a message popup or a
    nonzero exit tmux might complain about."""
    try:
        agents.sidebar_follow(DB(), session)
    except Exception:
        pass


@app.command("_local-models", hidden=True)
def local_models_cmd(repo: Optional[str] = typer.Option(None, "--repo")) -> None:
    """Start Ollama for the native profiles and load their models (detached
    from `brindle`; what happened goes to ~/.brindle/ollama.log)."""
    from brindle.config import RepoConfig, load_repo_config
    from brindle.native import serve

    try:
        cfg = load_repo_config(repo) if repo else RepoConfig()
        lines = serve.ensure(repo, cfg)
        with open(serve.log_path(), "a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"== brindle: {line}\n")
    except Exception:  # noqa: BLE001 -- detached: nobody to report to
        pass


@app.command("_sync-settings", hidden=True)
def sync_settings_cmd() -> None:
    """Pull synced settings (brindle Pro); detached from `brindle`."""
    from brindle.pro import settings_sync

    settings_sync.pull()


@app.command("_cull", hidden=True)
def cull_cmd(repo: Optional[str] = typer.Option(None, "--repo")) -> None:
    from brindle import cull, sessions

    db = _helper_db()
    if repo:
        try:
            sessions.enforce(db, repo)
        except Exception:  # noqa: BLE001 -- detached: nobody to report to
            pass
    cull.sweep_quietly(db)


@app.command("_flush", hidden=True)
def flush_cmd(agent_id: str, delay: float = typer.Option(0.0)) -> None:
    time.sleep(delay)
    agents.flush(_helper_db(), agent_id)


@app.command("_deliver-checks", hidden=True)
def deliver_checks_cmd(reviewer_id: str, workspace_id: str) -> None:
    """Run a repo's checks for a reviewer and deliver the summary to its
    inbox. Started detached from request_review, so the checks still finish
    and get delivered even if the MCP server that started it has exited."""
    from brindle.config import load_repo_config

    db = _helper_db()
    ws = db.get_workspace(workspace_id)
    if ws is None:
        return
    agents.deliver_check_summary(db, reviewer_id, ws, load_repo_config(ws.repo_root))


@app.command("_check-milestones", hidden=True)
def check_milestones_cmd(root_id: str, workspace_id: str,
                         position: Optional[int] = typer.Option(None, "--position")) -> None:
    """Run a session's milestone checks and deliver the result to its
    supervisor's inbox. Started detached from check_milestone."""
    from brindle import autopilot
    from brindle.mcp_server import record_milestone_changes

    db = _helper_db()
    ws = db.get_workspace(workspace_id)
    root = db.get_agent(root_id)
    if ws is None or root is None:
        return
    before = {m.id: m.status for m in db.milestones(root_id)}
    try:
        text = autopilot.check_milestones(db, root_id, ws, position)
        record_milestone_changes(db, root_id, ws, before, root, position, text)
    except Exception as e:  # noqa: BLE001 - the supervisor must hear about it either way
        text = f"The milestone check failed to run: {e}"
    try:
        autopilot.start_audit(db, root_id, ws)
    except Exception as e:  # noqa: BLE001
        db.update_autopilot(root_id, state="running")
        text += (f"\n\nThe completion audit couldn't start: {e}. Fix that and call check_milestone "
                 "again, or ask the user whether to turn it off (\"goal_audit\": false in "
                 ".brindle/config.json).")
    finally:
        db.update_autopilot(root_id, checking_since=None)
    if db.get_agent(root_id) is None:
        return
    body = f"[brindle] Milestone check finished.\n\n{text}"
    try:
        agents.send_message(db, root_id, body)
    except (agents.AgentError, tmux.TmuxError):
        # Not running (or unreachable): it stays queued; the next Stop hook hands it over.
        db.enqueue(root_id, body, None)


@app.command("_warm-checks", hidden=True)
def warm_checks_cmd(workspace_id: str) -> None:
    """Run and cache a branch's checks right after its worker reports, so the
    review and the merge gate find the result ready instead of each running
    the suite. Started detached from report_result."""
    from brindle import gates
    from brindle.config import load_repo_config

    db = _helper_db()
    ws = db.get_workspace(workspace_id)
    if ws is None:
        return
    sha = gates.head(ws)
    try:
        # A new commit makes this run's result useless (the review and the
        # gate want the new head), so give up and free the check slot.
        gates.check_summary(db, ws, load_repo_config(ws.repo_root),
                            cancel=lambda: gates.head(ws) != sha)
    except gates.Abandoned:
        pass


@app.command("_pool-fill", hidden=True)
def pool_fill_cmd(repo_root: str) -> None:
    """Top the worktree pool back up to `pool_size`. Started detached, after a
    claim and at supervisor start (see `workspaces.create`, `start`)."""
    from brindle import pool

    pool.fill_locked(_helper_db(), repo_root)
