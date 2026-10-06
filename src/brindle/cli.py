"""brindle command line."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from brindle import agents, git, tmux, view, workspaces
from brindle import history as history_mod
from brindle.usage import format_tokens
from brindle.config import write_template
from brindle.db import DB, Workspace
from brindle.profiles import list_profiles, load_profile

app = typer.Typer(add_completion=False, help="""brindle: run coding agents in parallel, each on its own git branch.

Run `brindle` with no arguments to open (or reopen) a supervisor chat here, with
the live dashboard underneath. Outside a git repo it starts a scratch session;
`brindle transfer <repo>` moves that work into a real repository later.

Paid features (hosted learning, per-worktree services, team policies, CI):
`brindle account` shows what you have and how to get the rest.""")
agent_app = typer.Typer(no_args_is_help=True, help="Manage agents.")
app.add_typer(agent_app, name="agent")


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
    if not tmux.has_session(ws.tmux_session):
        tmux.ensure_session(ws.tmux_session, ws.path, workspaces.workspace_env(ws))
    if window:
        tmux.select_window(window)
    if os.environ.get("TMUX"):
        subprocess.run([*tmux._base(), "switch-client", "-t", window or f"={ws.tmux_session}"])
        return
    subprocess.run(tmux.attach_command(ws.tmux_session))
    _after_detach(ws)


def _after_detach(ws: Workspace) -> None:
    """Back at the user's own prompt: say what happened and what's still running."""
    from brindle import scratch

    if tmux.has_session(ws.tmux_session):
        typer.echo("Detached; everything is still running. Run `brindle` here to reopen.")
        return
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


def _running_session(db: DB, ws: Workspace):
    """The live session (its interactive agent) in this checkout, if any."""
    for a in db.list_agents(ws.id):
        if a.mode == "interactive" and a.status not in ("paused", "done") and agents.is_alive(a):
            return a
    return None


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
    for a in db.list_agents(ws.id):
        if a.mode == "interactive" and a.status not in ("paused", "done") and agents.is_alive(a):
            # A session starts here next: keep its local models loaded.
            agents.pause(db, a.id, stop_procs=stop_procs, stop_local_models=False)
            typer.echo(f"Paused the session that was still running here ({a.id}); "
                       f"`brindle continue {a.id}` brings it back.")


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
    rows = db.find_workspaces(repo_root)
    panes = tmux.list_panes()
    if as_json:
        typer.echo(json.dumps([view.workspace_entry(db, ws, panes=panes) for ws in rows], indent=2))
        return
    if not rows:
        typer.echo("no workspaces")
        return
    for ws in rows:
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


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
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
                     help="Run brindle headless in CI: an issue in, a pull request out (brindle Team).")
app.add_typer(ci_app, name="ci")


@ci_app.command("run")
def ci_run(
    goal: Optional[str] = typer.Option(None, "--goal", help="The goal, as text (a goals.md-shaped text brings its milestones)."),
    goal_file: Optional[str] = typer.Option(None, "--goal-file", help="Read the goal from this file (goals.md format or plain text)."),
    issue: Optional[int] = typer.Option(None, "--issue", help="Take the goal from this GitHub issue (title and body, via gh); the PR closes it."),
    timeout: float = typer.Option(60, "--timeout", help="Minutes to wait for every milestone to be verified."),
    max_workers: Optional[int] = typer.Option(None, "--max-workers", help="Cap on workers running at once (sets max_agents in .brindle/config.local.json)."),
    base: Optional[str] = typer.Option(None, "--base", help="Branch to cut the work from and open the PR against (default: the repo's base)."),
    no_pr: bool = typer.Option(False, "--no-pr", help="Don't push or open a pull request; just report."),
    bundle: Optional[str] = typer.Option(None, "--bundle", help="Write the verified branch to this git bundle (and PATH.json) for `brindle ci publish`, instead of pushing; implies --no-pr. This run then needs no token that can write to GitHub."),
    entitlement: Optional[str] = typer.Option(None, "--entitlement", help="Read the entitlement from this file (written by `brindle ci entitle`) instead of exchanging BRINDLE_PRO_TOKEN."),
) -> None:
    """Run a supervisor with autopilot on, unattended, until the goal is verified; then open a PR.

    The work happens on a fresh `brindle/ci-<issue or slug>` branch. Exits 0
    with the PR URL when every milestone's check passes; otherwise exits 1
    with what happened (the supervisor's question, a stall, the timeout).
    Needs the `ci` feature (brindle Team); in CI, set BRINDLE_PRO_TOKEN to an org CI
    token from `brindle account org ci-token create`.

    Agents run the repo's code and can reach what this process holds. Where
    that code isn't trusted, pass --entitlement and --bundle so neither the CI
    token nor a push token is on the machine, and publish the bundle from
    another one with `brindle ci publish`."""
    from brindle import ci

    raise typer.Exit(ci.run_cli(goal=goal, goal_file=goal_file, issue=issue, timeout_min=timeout,
                                max_workers=max_workers, base=base, pr=not no_pr, echo=typer.echo,
                                bundle=bundle, entitlement=entitlement))


@ci_app.command("entitle")
def ci_entitle(
    out: str = typer.Option(..., "--out", help="Where to write the entitlement (mode 0600)."),
) -> None:
    """Exchange BRINDLE_PRO_TOKEN for the short-lived entitlement and write it to a file.

    Run it as its own step before `brindle ci run --entitlement FILE`, so the
    long-lived CI token is never in the process that starts agents."""
    from brindle import ci

    try:
        path = ci.entitle(out)
    except ci.CIError as e:
        _fail(str(e))
    typer.echo(f"wrote {path}")


@ci_app.command("publish")
def ci_publish(
    path: str = typer.Argument(..., help="The bundle `brindle ci run --bundle` wrote (PATH.json sits next to it)."),
    repo: Optional[str] = typer.Option(None, "--repo", help="owner/name on github.com (default: $GITHUB_REPOSITORY)."),
    base: Optional[str] = typer.Option(None, "--base", help="Branch the pull request targets (default: the repository's default branch; never the bundle's say)."),
) -> None:
    """Push a bundle from `brindle ci run --bundle` and open its pull request.

    This is the step that holds the token that can push, so run it where no
    agent ran. It verifies the bundle, takes only its `brindle/ci-` branch into
    a fresh bare repo, pushes it and opens the PR with gh. It never checks out
    or runs repo code, hooks or agents."""
    from brindle import ci

    raise typer.Exit(ci.publish_cli(path, repo, base, echo=typer.echo))


@ci_app.command("init")
def ci_init(
    label: str = typer.Option("brindle", "--label", help="Issues given this label start a run."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing workflow file."),
) -> None:
    """Write .github/workflows/brindle.yml: `brindle ci run` on labelled issues, publishing from a second job."""
    from brindle import ci

    root = _run(git.main_repo_root, os.getcwd())
    try:
        path = ci.init(root, label=label, force=force)
    except ci.CIError as e:
        _fail(str(e))
    typer.echo(f"wrote {path}")
    typer.echo("Add the BRINDLE_PRO_TOKEN secret (from `brindle account org ci-token create`) and "
               "ANTHROPIC_API_KEY, and allow GitHub Actions to create pull requests in the repo's "
               "Actions settings. Only people you trust with write access should be able to apply "
               "the label.")


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
    gates.check_summary(db, ws, load_repo_config(ws.repo_root))


@app.command("_pool-fill", hidden=True)
def pool_fill_cmd(repo_root: str) -> None:
    """Top the worktree pool back up to `pool_size`. Started detached, after a
    claim and at supervisor start (see `workspaces.create`, `start`)."""
    from brindle import pool

    pool.fill_locked(_helper_db(), repo_root)
