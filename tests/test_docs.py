"""The docs cover every command and tool brindle actually has.

Checks README.md always, and the pawdelta.com/brindle page too when
BRINDLE_SITE_PAGE points at its HTML source.
"""

import asyncio
import html
import os
import re
from pathlib import Path

import pytest
import typer

from brindle.cli import app
from brindle.mcp_server import mcp

README = Path(__file__).resolve().parent.parent / "README.md"


def _commands() -> list[str]:
    """Every public command, e.g. "ls" and "agent spawn"."""
    found = []

    def walk(cmd, prefix: str) -> None:
        if hasattr(cmd, "commands"):
            for name, sub in cmd.commands.items():
                if not sub.hidden:
                    walk(sub, f"{prefix} {name}".strip())
        elif prefix:
            found.append(prefix)

    walk(typer.main.get_command(app), "")
    return sorted(found)


def _tools() -> list[str]:
    return sorted(t.name for t in asyncio.run(mcp.list_tools()))


def _text(path: Path) -> str:
    raw = path.read_text()
    if path.suffix == ".html":
        raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return raw


def _pages() -> list[Path]:
    pages = [README]
    site = os.environ.get("BRINDLE_SITE_PAGE")
    if site:
        pages.append(Path(site))
    return pages


def _mentions_command(text: str, cmd: str) -> bool:
    """`brindle ls`, or a grouped form like `brindle attach / cd / open` or
    `brindle agent spawn/kill/peek`."""
    *group, name = cmd.split()
    prefix = "brindle " + "".join(f"{g} " for g in group)
    for m in re.finditer(rf"{prefix}[\w-]+(?: ?/ ?[\w-]+)*", text):
        if name in re.split(r" ?/ ?", m.group(0)[len(prefix):]):
            return True
    return False


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_command_is_documented(page):
    text = _text(page)
    missing = [c for c in _commands() if not _mentions_command(text, c)]
    assert not missing, f"{page.name} doesn't document: {', '.join(missing)}"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_agent_tool_is_documented(page):
    text = _text(page)
    missing = [t for t in _tools() if not re.search(rf"\b{t}\b", text)]
    assert not missing, f"{page.name} doesn't mention MCP tools: {', '.join(missing)}"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_recommended_use_section(page):
    assert re.search(r"Recommended (use|workflow)", _text(page)), \
        f"{page.name} has no recommended-use section"


def test_command_help_has_no_hard_wraps():
    """Docstring line breaks mid-sentence show up as ragged lines in --help."""
    ragged = []

    def walk(cmd, name: str) -> None:
        if hasattr(cmd, "commands"):
            for sub_name, sub in cmd.commands.items():
                if not sub.hidden:
                    walk(sub, f"{name} {sub_name}".strip())
        first_para = (cmd.help or "").strip().split("\n\n")[0]
        if name and "\n" in first_para:
            ragged.append(name)

    walk(typer.main.get_command(app), "")
    assert not ragged, f"summary paragraph spans lines in: {', '.join(ragged)}"


@pytest.mark.parametrize("args", [["ci"], ["ci", "run", "--issue", "42"], ["ci", "init"]])
def test_ci_is_a_stub_until_it_returns(args):
    """`brindle ci` is documented as on its way back; every form of it says so and fails."""
    from typer.testing import CliRunner

    from brindle.cli import CI_MOVED

    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert result.output.strip() == CI_MOVED
    assert CI_MOVED == ("brindle CI is part of brindle Team. It is moving to a hosted control "
                        "plane and will be back in a later release.")
    assert "`brindle ci` says so and exits 1" in README.read_text()


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_sidebar_key_is_documented(page):
    from brindle.watch import KEYS

    text = _text(page)
    missing = [k for k, _ in KEYS if k not in text]
    assert not missing, f"{page.name} doesn't list sidebar keys: {', '.join(missing)}"
