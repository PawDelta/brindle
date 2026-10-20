"""``brindle keys set|list|unset``: model API keys kept in the OS keychain
(or a 0600 file), so every agent pane gets them without exporting them.
Values are read from a hidden prompt or stdin, never argv, and never printed."""

from __future__ import annotations

import getpass
import os
import sys
import webbrowser

import typer

from brindle import key_check, keystore

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


class PolicyDenied(keystore.CredentialError):
    """The org's policy denies personal keys, or couldn't be read (fail closed)."""


def check(name: str, repo_root: str | None) -> None:
    """Refuse a name brindle won't store, or any key while the org's policy denies them."""
    keystore.check_name(name, repo_root)
    try:
        denied = key_check.personal_keys_denied(repo_root)
    except Exception as e:  # noqa: BLE001 - policy unreadable: fail closed
        raise PolicyDenied(f"couldn't read your org's key policy: {e}") from e
    if denied:
        raise PolicyDenied("your org's policy denies personal API keys; brindle won't store one")


def store(name: str, value: str, repo_root: str | None) -> None:
    """What ``keys set`` does with a value: check it, verify it with its
    provider where there is a check, and keep it. Raises CredentialError,
    whose message never contains the value."""
    check(name, repo_root)
    value = value.strip()
    if value:
        problem = key_check.verify(name, value)
        if problem:
            raise keystore.CredentialError(f"{name} not stored: {problem}")
    keystore.set_key(name, value, repo_root)


@app.command("set")
def keys_set(name: str = typer.Argument(..., help="The variable, e.g. ANTHROPIC_API_KEY or OPENAI_API_KEY.")) -> None:
    """Store a key. The value is read from a hidden prompt, or from stdin when piped."""
    try:
        check(name, _root())
    except keystore.CredentialError as e:
        _fail(str(e))
    page = key_check.key_page(name)
    if sys.stdin.isatty():
        if page:
            typer.echo(f"Get a key at {page}")
            if input("Press o to open it in your browser, or Enter to paste a key: ").strip().lower() == "o":
                webbrowser.open(page)
        value = getpass.getpass(f"{name}: ")
    else:
        value = sys.stdin.readline()
    value = value.strip()
    try:
        store(name, value, _root())
    except keystore.CredentialError as e:
        _fail(str(e))
    typer.echo(f"stored {name}")
    if name == "ANTHROPIC_API_KEY":
        _warn_login()
        _offer_approval(value)
    elif name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        from brindle import providers

        if providers.codex_chatgpt_login():
            msg = f"Codex is signed in with ChatGPT: {providers.CODEX_INTERACTIVE_USES_PLAN}"
            if name == "CODEX_API_KEY":
                msg += f"; {providers.CODEX_EXEC_BILLS_KEY}"
            typer.secho(msg, fg="yellow")
    elif name == "GEMINI_API_KEY":
        from brindle import providers

        def ask(prompt: str) -> bool:
            try:
                return typer.confirm(prompt, default=False)
            except typer.Abort:
                return False

        providers.offer_agy_gemini(ask, typer.echo)


def _warn_login() -> None:
    from brindle import providers

    if providers.subscription_login("claude"):
        typer.secho("Claude Code will bill this key instead of your subscription in brindle panes; "
                    "set auth: subscription on a profile to keep the login", fg="yellow")


def _offer_approval(value: str) -> None:
    """Ask once whether Claude Code may use this key in brindle panes without
    its own "use this API key?" question. Declining (or no answer) changes nothing."""
    from brindle import providers

    if providers.claude_org_managed() or providers.api_key_approved(value.strip()):
        return
    try:
        yes = typer.confirm("Let Claude Code use this key in brindle panes without asking?", default=False)
    except typer.Abort:
        yes = False
    if not yes:
        typer.echo("not approved; Claude Code will ask the first time it sees the key")
        return
    try:
        providers.approve_api_key(value)
    except (OSError, ValueError) as e:
        _fail(f"couldn't update Claude Code's config: {e}")
    typer.echo("approved for Claude Code")


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
