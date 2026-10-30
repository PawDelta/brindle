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
    status = key_check.UNCHECKED
    if value:
        status, problem = key_check.evaluate(name, value)
        if problem:
            raise keystore.CredentialError(f"{name} not stored: {problem}")
    keystore.set_key(name, value, repo_root, status=status)


def retest(name: str) -> str:
    """Re-check the stored key with its provider and remember the result and when,
    including an inconclusive one (unchecked). The result is kept only if the key
    wasn't replaced or removed meanwhile; the reply is what is stored then.
    Raises CredentialError when no key is stored."""
    value = keystore.get_key(name)
    if not value:
        raise keystore.CredentialError(f"no {name} is stored")
    status, _ = key_check.evaluate(name, value)
    keystore.set_check(name, status, value)
    return keystore.get_record(name).get("status") or key_check.UNCHECKED


def retest_provider(provider: str) -> None:
    """After an auth failure: re-check each stored key the provider reads, so a
    revoked one shows as rejected. Never raises."""
    for name in keystore.provider_key_names(provider):
        try:
            if keystore.get_key(name):
                retest(name)
        except Exception:  # noqa: BLE001 - a best-effort follow-up to a failure already handled
            pass


def importable(name: str) -> bool:
    """True when ``name`` is set in the daemon's environment but not stored."""
    return bool(os.environ.get(name, "").strip()) and not keystore.get_key(name)


def import_env(name: str, repo_root: str | None) -> str:
    """Store the daemon's environment value of ``name``, validated like a save.
    Returns the stored value's last four characters."""
    check(name, repo_root)
    value = os.environ.get(name, "").strip()
    if not value:
        raise keystore.KeyNameError(f"{name} isn't set in the daemon's environment")
    store(name, value, repo_root)
    return value[-4:]


def bills_instead_of_subscription(name: str) -> bool:
    """True when storing this key makes a signed-in subscription's agents bill the key
    (the warning `keys set` prints)."""
    from brindle import providers

    if name == "ANTHROPIC_API_KEY":
        return bool(providers.subscription_login("claude"))
    if name == "CODEX_API_KEY":
        return bool(providers.codex_chatgpt_login())
    return False


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
        remove(name)
    except keystore.CredentialError as e:
        _fail(str(e))
    typer.echo(f"removed {name}")


def remove(name: str) -> None:
    """What ``keys unset`` does."""
    keystore.unset_key(name)
