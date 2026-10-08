"""``brindle keys set|list|unset``: model API keys kept in the OS keychain
(or a 0600 file), so every agent pane gets them without exporting them.
Values are read from a hidden prompt or stdin, never argv, and never printed."""

from __future__ import annotations

import getpass
import os
import sys

import typer

from brindle import keystore

app = typer.Typer(no_args_is_help=True,
                  help="Store model API keys once; every agent pane gets the ones its provider reads.")


def _root() -> str | None:
    from brindle import git

    try:
        return git.toplevel(os.getcwd())
    except Exception:  # noqa: BLE001 - outside a repo: built-in names only
        return None


def _fail(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(1)


@app.command("set")
def keys_set(name: str = typer.Argument(..., help="The variable, e.g. ANTHROPIC_API_KEY or OPENAI_API_KEY.")) -> None:
    """Store a key. The value is read from a hidden prompt, or from stdin when piped."""
    try:
        keystore.check_name(name, _root())
    except keystore.CredentialError as e:
        _fail(str(e))
    if sys.stdin.isatty():
        value = getpass.getpass(f"{name}: ")
    else:
        value = sys.stdin.readline()
    try:
        keystore.set_key(name, value, _root())
    except keystore.CredentialError as e:
        _fail(str(e))
    typer.echo(f"stored {name}")


@app.command("list")
def keys_list() -> None:
    """Show stored key names with their last four characters."""
    try:
        found = keystore.list_keys(_root())
    except keystore.CredentialError as e:
        _fail(str(e))
    if not found:
        typer.echo("no keys stored; `brindle keys set <NAME>` stores one")
    for name, tail in found.items():
        typer.echo(f"{name}  ...{tail}")


@app.command("unset")
def keys_unset(name: str = typer.Argument(..., help="The variable to forget.")) -> None:
    """Remove a stored key."""
    try:
        keystore.unset_key(name)
    except keystore.CredentialError as e:
        _fail(str(e))
    typer.echo(f"removed {name}")
