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
    for p in acc.clis(installed):
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


def _login_command(provider: str) -> list[str]:
    from brindle import antigravity, providers

    if provider == "claude":
        return [providers.claude_binary(), "auth", "login"]
    if provider == "codex":
        return [providers.codex_binary(), "login"]
    return [antigravity.binary()]      # agy signs in on its first screen


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
    run(_login_command(provider), timeout=LOGIN_TIMEOUT, interactive=True)
    if provider in doctor.signed_in_providers():
        echo(f"✓ {name} is signed in.")
        return True
    echo(f"✗ {name} still isn't signed in; `brindle doctor` shows what it sees.")
    return False
