"""``brindle repo add|rm|ls``: the repos attached to the current session
(brindle Pro; see :mod:`brindle.repos`)."""

from __future__ import annotations

from typing import Optional

import typer

from brindle import agents, repos
from brindle.db import DB

app = typer.Typer(no_args_is_help=True,
                  help="Work across several repos in one session (brindle Pro): attach "
                       "other local git repos so the supervisor can put workers there.")


def _root(db: DB) -> str:
    from brindle.cli import _session_root

    return _session_root(db)


def _fail(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(1)


@app.command("add")
def repo_add(
    path: str = typer.Argument(..., help="A local git repository you own."),
    name: Optional[str] = typer.Option(None, "--name", help="Alias the supervisor uses as repo=... (default: the folder's name)."),
) -> None:
    """Attach a repo to the current session."""
    db = DB()
    root_id = _root(db)
    try:
        a = repos.attach(db, root_id, path, name)
    except repos.RepoError as e:
        _fail(str(e))
    typer.echo(f"attached {a.repo_root} as {a.alias}")
    try:
        agents.send_message(
            db, root_id,
            f"[brindle] The user attached the repo {a.repo_root} to this session as `{a.alias}`. "
            f"Pass repo=\"{a.alias}\" to assign or handoff to put a worker there; it gets that "
            "repo's own checks, review and merge. list_repos shows every attached repo.",
            sender_id=None)
    except Exception:  # noqa: BLE001 - paused or unreachable: it learns on resume
        pass


@app.command("rm")
def repo_rm(ref: str = typer.Argument(..., metavar="ALIAS_OR_PATH")) -> None:
    """Detach a repo from the current session (its worktrees and branches stay)."""
    db = DB()
    root_id = _root(db)
    try:
        a = repos.detach(db, root_id, ref)
    except repos.RepoError as e:
        _fail(str(e))
    typer.echo(f"detached {a.alias} ({a.repo_root})")
    try:
        agents.send_message(db, root_id, f"[brindle] The user detached the repo `{a.alias}` ({a.repo_root}).",
                            sender_id=None)
    except Exception:  # noqa: BLE001
        pass


@app.command("ls")
def repo_ls() -> None:
    """List the repos attached to the current session."""
    db = DB()
    typer.echo(repos.listing_text(db, _root(db)))
