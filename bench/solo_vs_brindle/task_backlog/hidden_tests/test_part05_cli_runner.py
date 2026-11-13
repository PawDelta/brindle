"""Ticket 5: CliRunner can emulate an interactive terminal of a given width."""

import os
import shutil
import sys

import pytest

import click
from click.testing import CliRunner

LONG_HELP = (
    "This command copies every record from the source database into the target "
    "database, converting column types where the two schemas disagree."
)


@click.command()
def streams():
    click.echo(f"{sys.stdin.isatty()} {sys.stdout.isatty()} {sys.stderr.isatty()}")


@click.command()
def red():
    click.secho("hi", fg="red")


@click.command(help=LONG_HELP)
def copy():
    pass


@click.command()
def size():
    click.echo(tuple(shutil.get_terminal_size()))


def help_lines(result):
    return [line for line in result.output.splitlines() if line.strip()]


def test_tty_makes_stdout_and_stderr_terminals():
    result = CliRunner(tty=True).invoke(streams)
    assert result.output == "False True True\n"


def test_tty_false_is_not_a_terminal():
    result = CliRunner(tty=False).invoke(streams)
    assert result.output == "False False False\n"


def test_tty_keeps_styles_by_default():
    result = CliRunner(tty=True).invoke(red)
    assert result.output == click.style("hi", fg="red") + "\n"


def test_tty_strips_styles_when_color_is_false():
    result = CliRunner(tty=True).invoke(red, color=False)
    assert result.output == "hi\n"


def test_invoke_can_turn_tty_on_for_one_call():
    runner = CliRunner()
    assert runner.invoke(streams, tty=True).output == "False True True\n"
    assert runner.invoke(streams).output == "False False False\n"


def test_invoke_can_turn_tty_off_for_one_call():
    runner = CliRunner(tty=True)
    assert runner.invoke(streams, tty=False).output == "False False False\n"
    assert runner.invoke(red, tty=False).output == "hi\n"


def test_isolation_accepts_tty():
    runner = CliRunner()
    with runner.isolation(tty=True):
        stdout_is_tty = sys.stdout.isatty()
    assert stdout_is_tty is True


def test_clear_writes_its_escape_sequence():
    @click.command()
    def cli():
        click.clear()
        click.echo("done")

    result = CliRunner(tty=True).invoke(cli)
    assert result.output == "\x1b[2J\x1b[1;1Hdone\n"


def test_progress_bar_is_drawn():
    @click.command()
    def cli():
        with click.progressbar(range(4), label="Work") as items:
            for _ in items:
                pass

    result = CliRunner(tty=True).invoke(cli)
    assert result.exit_code == 0
    assert "[####################################]  100%" in result.output
    assert "Work" in result.output


def test_pager_is_not_started():
    @click.command()
    def cli():
        click.echo_via_pager("paged text")

    result = CliRunner(tty=True).invoke(cli, env={"PAGER": "definitely-not-a-pager"})
    assert result.exit_code == 0
    assert "paged text" in result.output


def test_narrow_terminal_wraps_help():
    result = CliRunner(terminal_width=40).invoke(copy, ["--help"])
    lines = help_lines(result)
    assert max(len(line) for line in lines) <= 40
    assert len(lines) > len(help_lines(CliRunner().invoke(copy, ["--help"])))


def test_wide_terminal_keeps_long_lines():
    result = CliRunner(terminal_width=200).invoke(copy, ["--help"])
    assert f"  {LONG_HELP}" in result.output.splitlines()


def test_invoke_width_applies_to_one_call():
    runner = CliRunner(terminal_width=40)
    assert f"  {LONG_HELP}" in runner.invoke(
        copy, ["--help"], terminal_width=200
    ).output.splitlines()
    lines = help_lines(runner.invoke(copy, ["--help"]))
    assert max(len(line) for line in lines) <= 40


def test_terminal_size_is_reported():
    assert CliRunner(terminal_width=57).invoke(size).output == "(57, 24)\n"
    assert CliRunner(tty=True).invoke(size, terminal_width=101).output == (
        "(101, 24)\n"
    )


def test_environment_is_restored(monkeypatch):
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    CliRunner(terminal_width=50).invoke(size)
    assert "COLUMNS" not in os.environ
    assert "LINES" not in os.environ


def test_terminal_width_wins_over_env_overrides(monkeypatch):
    monkeypatch.setenv("COLUMNS", "33")
    monkeypatch.delenv("LINES", raising=False)
    sizes = {"COLUMNS": "120", "LINES": "60"}
    runner = CliRunner(terminal_width=40, env=sizes)
    assert runner.invoke(size).output == "(40, 24)\n"
    assert runner.invoke(size, env=sizes, terminal_width=70).output == "(70, 24)\n"
    assert os.environ["COLUMNS"] == "33"
    assert "LINES" not in os.environ


@pytest.mark.parametrize("width", [0, -1, "80", 1.5, True])
def test_invalid_width_is_rejected(width):
    with pytest.raises(ValueError):
        CliRunner(terminal_width=width)


@pytest.mark.parametrize("width", [0, -3, "wide"])
def test_invalid_width_for_one_call_is_rejected(width):
    with pytest.raises(ValueError):
        CliRunner().invoke(size, terminal_width=width)


def test_context_terminal_width_still_wins():
    @click.command(help=LONG_HELP, context_settings={"terminal_width": 200})
    def cli():
        pass

    result = CliRunner(terminal_width=40).invoke(cli, ["--help"])
    assert f"  {LONG_HELP}" in result.output.splitlines()
