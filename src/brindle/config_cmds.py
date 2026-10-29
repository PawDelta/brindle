"""``brindle config set|unset plan``: the plan-approval setting, the person's
default or the current repo's override (the same writes as the app's Plan
setting, through the same helpers)."""

from __future__ import annotations

import os

import typer

from brindle import config

app = typer.Typer(no_args_is_help=True, help="Change a setting: plan.")

PLAN_HELP = "Plan approvals: Auto / Always / Never. Auto plans first for heavy, cross-repo and CI/migration/auth/deploy changes."


def _root() -> str | None:
    from brindle import git

    try:
        return git.toplevel(os.getcwd())
    except Exception:  # noqa: BLE001 - outside a repo: no project to set
        return None


def _fail(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(1)


def _plans() -> tuple[str, ...]:
    from brindle.daemon.routes_governance import PLANS

    return PLANS


def _check_plan(key: str, value: str | None) -> None:
    if key != "plan":
        _fail(f"unknown setting {key!r}; the only setting here is plan")
    if value is not None and value not in _plans():
        _fail(f"plan must be auto, always or never, not {value!r}")


@app.command("set")
def config_set(key: str = typer.Argument(..., help="The setting: plan."),
               value: str = typer.Argument(..., help=PLAN_HELP),
               project: bool = typer.Option(False, "--project", help="Only for the current repo (.brindle/config.local.json).")) -> None:
    """Set a setting. Plan approvals: the person's default, or the repo's override with --project."""
    _check_plan(key, value)
    if not project:
        config.set_user("plan", value)
        typer.echo(f"✓ plan {value} (your default)")
        return
    root = _root()
    if root is None:
        _fail("--project needs a git repo: run it inside one")
    config.set_local(root, "plan", value)
    typer.echo(f"✓ plan {value} for this repo (.brindle/config.local.json)")


@app.command("unset")
def config_unset(key: str = typer.Argument(..., help="The setting: plan."),
                 project: bool = typer.Option(False, "--project", help="Clear the current repo's override.")) -> None:
    """Clear this repo's override, so the shared file or your default applies again."""
    _check_plan(key, None)
    if not project:
        _fail("only the repo's override can be cleared: add --project")
    root = _root()
    if root is None:
        _fail("--project needs a git repo: run it inside one")
    for k in ("plan", "plan_first"):
        config.unset_local(root, k)
    typer.echo("✓ plan override cleared for this repo")
