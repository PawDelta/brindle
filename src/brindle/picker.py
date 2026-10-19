"""The supervisor picker: choose the CLI the supervisor chat runs on.

It lists only the CLIs this person may use (``team_policy.access``), tags the
one they picked last and the ones that need setting up, and sets up a CLI
that isn't ready (installs it if it's missing, opens its sign-in, then checks
with ``doctor.signed_in_providers``) when it's chosen. Keys move with the
arrows and Enter, or a number picks a row; a terminal that can't give
single keys gets a numbered list and a line prompt instead.

Other screens open from it by a letter: ``w`` workers, ``k`` API keys. They
register themselves in :data:`SCREENS`; until one does, its letter isn't
offered.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from typing import Callable

import typer

# The value `brindle -s` gets when it's given no CLI (see cli._PickerGroup):
# open the picker instead of switching straight to one.
PICK = "<pick>"

# letter -> (what it opens, the screen): "w" workers and "k" keys, once built.
SCREENS: dict[str, tuple[str, Callable[[], None]]] = {}

LOGIN_TIMEOUT = 600.0


@dataclass(frozen=True)
class Row:
    provider: str
    label: str
    tags: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.tags or self.tags == ["current"]


def supervisor_rows(acc, current: str | None, signed_in: list[str], installed: list[str]) -> list[Row]:
    """One row per supervisor CLI ``acc`` allows, tagged ``current``, ``not
    installed`` or ``needs sign-in``."""
    from brindle.pro.team_policy import CLI_NAMES
    from brindle.providers import SUPERVISOR_PROVIDERS

    rows = []
    for p in acc.clis():
        if p not in SUPERVISOR_PROVIDERS:
            continue
        tags = ["current"] if p == current else []
        if p not in installed:
            tags.append("not installed")
        elif p not in signed_in:
            tags.append("needs sign-in")
        rows.append(Row(p, CLI_NAMES.get(p, p), tags))
    return rows


def raw_keys() -> bool:
    """Whether this terminal can hand over single key presses."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


_ARROWS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down",
           "\r": "enter", "\n": "enter", "\x1b": "q", "\x03": "q"}


def read_key() -> str:
    """One key press: a character, or ``up``, ``down``, ``enter``, ``q``."""
    import click

    k = click.getchar()
    return _ARROWS.get(k, k)


def _lines(rows: list[Row], at: int, raw: bool) -> list[str]:
    out = ["Which CLI should the supervisor run on?"]
    for i, r in enumerate(rows):
        mark = ">" if raw and i == at else " "
        tags = f"  ({', '.join(r.tags)})" if r.tags else ""
        out.append(f" {mark} {i + 1}) {r.label} [{r.provider}]{tags}")
    hints = ["↑/↓ or a number, Enter to pick" if raw else "a number to pick"]
    hints += [f"{k} {what}" for k, (what, _) in SCREENS.items()]
    hints.append("q cancel")
    out.append("  " + " · ".join(hints))
    return out


def pick_supervisor(acc, current: str | None = None, *, signed_in: list[str] | None = None,
                    installed: list[str] | None = None, key: Callable[[], str] | None = None,
                    setup: Callable[[str], bool] | None = None,
                    echo: Callable[..., None] = typer.echo) -> str | None:
    """Show the picker and return the chosen CLI, ready to run (set up first
    if it wasn't), or None when the person cancels or nothing is allowed.
    ``key`` reads one choice (default: a key press, or a line where the
    terminal can't do keys); ``setup`` sets a CLI up (default: :func:`sign_in`)."""
    from brindle import doctor

    if installed is None:
        installed = doctor.signin_providers()
    if signed_in is None:
        signed_in = doctor.signed_in_providers()
    rows = supervisor_rows(acc, current, signed_in, installed)
    if not rows:
        echo(f"No supervisor CLI is available to you here{f': {acc.error}' if acc.error else ''}.")
        return None
    raw = key is None and raw_keys()
    if key is None:
        key = read_key if raw else (lambda: typer.prompt("Pick", default="", show_default=False).strip().lower())
    setup = setup or sign_in
    at = next((i for i, r in enumerate(rows) if r.provider == current), 0)
    shown = 0
    while True:
        if raw and shown:
            echo(f"\x1b[{shown}F\x1b[J", nl=False)     # redraw in place
        lines = _lines(rows, at, raw)
        echo("\n".join(lines))
        shown = len(lines)
        k = key()
        chosen = None
        if k == "up":
            at = (at - 1) % len(rows)
        elif k == "down":
            at = (at + 1) % len(rows)
        elif k == "enter" or (k == "" and not raw):
            chosen = rows[at]
        elif k.isdigit() and 1 <= int(k) <= len(rows):
            chosen = rows[int(k) - 1]
        elif k in ("q", "quit"):
            return None
        elif k in SCREENS:
            SCREENS[k][1]()
            shown = 0
        if chosen is None:
            continue
        if chosen.ready or setup(chosen.provider):
            return chosen.provider
        shown = 0      # the setup printed below the list: draw it afresh


def pick_row(title: str, rows: list[str], actions: dict[str, str], *, key: Callable[[], str] | None = None,
             echo: Callable[..., None] = typer.echo, at: int = 0) -> tuple[str, int] | None:
    """A list of ``rows`` to choose from. Returns ``("enter", i)`` for Enter on
    the highlighted row or a number, ``(letter, i)`` for one of ``actions``
    (letter -> what it does) on the highlighted row, or None on cancel.
    ``at`` is the row highlighted to begin with."""
    import unicodedata

    # Row text comes from prompts and branch names: no escape sequences on the terminal.
    rows = ["".join(c if unicodedata.category(c)[0] != "C" else " " for c in r) for r in rows]
    raw = key is None and raw_keys()
    if key is None:
        key = read_key if raw else (lambda: typer.prompt("Pick", default="", show_default=False).strip().lower())
    shown = 0
    while True:
        if raw and shown:
            echo(f"\x1b[{shown}F\x1b[J", nl=False)
        lines = [title] + [f" {'>' if raw and i == at else ' '} {i + 1}) {r}" for i, r in enumerate(rows)]
        hints = ["↑/↓ or a number, Enter to open" if raw else "a number or Enter to open"]
        hints += [f"{k} {what}" for k, what in actions.items()] + ["q cancel"]
        lines.append("  " + " · ".join(hints))
        echo("\n".join(lines))
        shown = len(lines)
        k = key()
        if k == "up":
            at = (at - 1) % len(rows)
        elif k == "down":
            at = (at + 1) % len(rows)
        elif k == "enter" or (k == "" and not raw):
            return "enter", at
        elif k.isdigit() and 1 <= int(k) <= len(rows):
            return "enter", int(k) - 1
        elif k in actions:
            return k, at
        elif k in ("q", "quit"):
            return None


def _setup_worker_model(m, *, is_ready, cli_setup, local_setup, read_secret, echo) -> bool:
    """Make ``m`` ready to run if it isn't: sign its CLI in, set Ollama up
    with the model, or take and check an API key. True when it can run."""
    from brindle import key_check, keystore, worker_models

    if is_ready(m):
        return True
    if m.how == worker_models.CLI:
        return cli_setup(m.provider)
    if m.how == worker_models.LOCAL:
        res = local_setup(m.model)
        if not res:
            echo(f"✗ {res.reason}")
        return bool(res)
    page = key_check.key_page(m.key_env)
    echo(f"{m.label} runs on OpenRouter and needs a key{f' (create one at {page})' if page else ''}.")
    value = read_secret(m.key_env).strip()
    if not value:
        return False
    problem = key_check.verify(m.key_env, value)
    if problem:
        echo(f"✗ {problem}")
        return False
    keystore.set_key(m.key_env, value)
    echo(f"✓ key checked and stored on this machine ({m.key_env}).")
    return True


def workers_screen(acc=None, *, key: Callable[[], str] | None = None, is_ready=None, cli_setup=None,
                   local_setup=None, read_secret=None, echo: Callable[..., None] = typer.echo) -> None:
    """Pick a model for each task size (light, medium, heavy): only the ones
    ``acc`` allows, grouped by how they run. One that isn't ready is set up
    first; the pick is saved as ``routing_<size>`` in the user config."""
    import os

    from brindle import doctor, worker_models
    from brindle import local_setup as local
    from brindle.config import set_user, user_settings
    from brindle.pro import team_policy

    acc = acc or team_policy.access(os.getcwd())
    if key is None:
        key = read_key if raw_keys() else (lambda: typer.prompt("Pick", default="q", show_default=False).strip().lower())
    if is_ready is None:
        signed_in = doctor.signed_in_providers()
        is_ready = lambda m: worker_models.ready(m, signed_in)      # noqa: E731
    cli_setup = cli_setup or sign_in
    local_setup = local_setup or (lambda model: local.setup_model(
        model, say=echo, ask=lambda p: typer.confirm(p.replace(" [y/N]", "").strip(), default=False)))
    read_secret = read_secret or (lambda name: typer.prompt(f"Paste the key for {name} (hidden; Enter to cancel)",
                                                            hide_input=True, default="", show_default=False))
    while True:
        picks = user_settings()
        echo("Which model should each task size run on?")
        for i, t in enumerate(worker_models.TIERS, 1):
            m = worker_models.find(picks.get(f"routing_{t}"))
            echo(f"  {i}) {t}: {m.label if m else 'built-in'}")
        echo("  a number to change one · q back")
        k = key()
        if k not in ("1", "2", "3"):
            if k in ("q", "quit"):
                return
            continue
        tier = worker_models.TIERS[int(k) - 1]
        models = worker_models.offered(acc, tier, os.getcwd())
        echo(f"Models for {tier} tasks:")
        n = 0
        for how, title in worker_models.HOW.items():
            group = [m for m in models if m.how == how]
            if group:
                echo(f" {title}")
            for m in group:
                n += 1
                state = "" if is_ready(m) else "  (needs setting up)"
                echo(f"  {n}) {m.label}{'  (current)' if picks.get(f'routing_{tier}') == m.id else ''}{state}")
        echo("  0) built-in · q back")
        ordered = [m for how in worker_models.HOW for m in models if m.how == how]
        k = key()
        if k == "0":
            set_user(f"routing_{tier}", None)
        elif k.isdigit() and 1 <= int(k) <= len(ordered):
            m = ordered[int(k) - 1]
            if _setup_worker_model(m, is_ready=is_ready, cli_setup=cli_setup, local_setup=local_setup,
                                   read_secret=read_secret, echo=echo):
                worker_models.ensure_profile(m)
                set_user(f"routing_{tier}", m.id)


def _open_workers() -> None:
    workers_screen()


SCREENS["w"] = ("workers", _open_workers)


def _login_command(provider: str) -> list[str]:
    from brindle import antigravity, providers

    if provider == "claude":
        return [providers.claude_binary(), "auth", "login"]
    if provider == "codex":
        return [providers.codex_binary(), "login"]
    return [antigravity.binary()]      # agy signs in on its first screen


def _wsl_browser_env() -> dict[str, str] | None:
    """Under WSL with no Linux browser, an environment that opens a sign-in in
    the Windows browser; None (inherit) otherwise. Without any opener the CLI
    prints its URL, which is then the person's to open."""
    from brindle import providers

    if any(os.environ.get(v) for v in ("BROWSER", "DISPLAY", "WAYLAND_DISPLAY", "SSH_CONNECTION", "SSH_TTY")):
        return None
    opener = providers.windows_browser()
    return {**os.environ, "BROWSER": opener} if opener else None


def sign_in(provider: str, *, run=None, confirm: Callable[..., bool] = typer.confirm,
            echo: Callable[..., None] = typer.echo) -> bool:
    """Get ``provider``'s CLI ready to run a chat: install it if it's missing
    (after asking), open its sign-in, and check that it took. True when it is
    signed in afterwards."""
    from brindle import company_login, doctor
    from brindle.pro.team_policy import CLI_NAMES

    run = run or company_login.run_cmd
    name = CLI_NAMES.get(provider, provider)
    if provider not in doctor.signin_providers():
        cmd = company_login.INSTALL.get(provider)
        if not cmd:
            echo(f"{name} isn't installed. Install it ({doctor.CLI_INSTALL[provider][1]}), then pick it again.")
            return False
        if not confirm(f"{name} isn't installed. Install it now (`{cmd}`)?", default=True):
            return False
        code, _ = run(shlex.split(cmd), timeout=LOGIN_TIMEOUT, interactive=True)
        if code != 0:
            echo(f"✗ installing {name} failed; run `{cmd}` yourself, then pick it again.")
            return False
    echo(f"Opening {name}'s sign-in; come back here when you're done.")
    run(_login_command(provider), timeout=LOGIN_TIMEOUT, interactive=True, env=_wsl_browser_env())
    if provider in doctor.signed_in_providers():
        echo(f"✓ {name} is signed in.")
        return True
    echo(f"✗ {name} still isn't signed in; `brindle doctor` shows what it sees.")
    return False
