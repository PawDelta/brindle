"""Ticket 1: terminal hyperlinks in click.style, click.secho and click.unstyle."""

import io

import pytest

import click

URL = "https://example.com/docs"
OPEN = f"\x1b]8;;{URL}\x1b\\"
CLOSE = "\x1b]8;;\x1b\\"
RESET = "\x1b[0m"


def test_link_wraps_text():
    assert click.style("docs", link=URL) == f"{OPEN}docs{RESET}{CLOSE}"


def test_link_goes_outside_the_style_codes():
    styled = click.style("docs", fg="red", bold=True, link=URL)
    assert styled == f"{OPEN}\x1b[31m\x1b[1mdocs{RESET}{CLOSE}"


def test_link_is_closed_without_reset():
    styled = click.style("docs", fg="blue", reset=False, link=URL)
    assert styled == f"{OPEN}\x1b[34mdocs{CLOSE}"


def test_link_on_non_string_text():
    assert click.style(42, link=URL) == f"{OPEN}42{RESET}{CLOSE}"


@pytest.mark.parametrize("link", [None, ""])
def test_no_link_adds_nothing(link):
    assert click.style("docs", fg="green", link=link) == click.style(
        "docs", fg="green"
    )


def test_url_is_kept_verbatim():
    url = "https://example.com/a;b?x=1&y=caf%C3%A9#top"
    styled = click.style("docs", link=url)
    assert styled.startswith(f"\x1b]8;;{url}\x1b\\")
    assert click.unstyle(styled) == "docs"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/\x1b]8;;evil",
        "https://example.com/\x07",
        "https://example.com/\nnext",
        "https://example.com/\x7f",
        "https://example.com/\x00",
    ],
)
def test_control_characters_in_link_are_rejected(url):
    with pytest.raises(ValueError):
        click.style("docs", link=url)


def test_unstyle_removes_link_and_styles():
    assert click.unstyle(click.style("docs", fg="green", link=URL)) == "docs"


def test_unstyle_removes_bel_terminated_link():
    text = f"\x1b]8;;{URL}\x07docs\x1b]8;;\x07"
    assert click.unstyle(text) == "docs"


def test_unstyle_removes_link_with_parameters():
    text = f"\x1b]8;id=42;{URL}\x1b\\docs\x1b]8;;\x1b\\"
    assert click.unstyle(text) == "docs"


def test_unstyle_keeps_surrounding_text():
    text = "see " + click.style("the docs", link=URL) + " now"
    assert click.unstyle(text) == "see the docs now"


def test_secho_passes_link_through(runner):
    @click.command()
    def cli():
        click.secho("docs", fg="red", link=URL)

    result = runner.invoke(cli, color=True)
    assert result.output == click.style("docs", fg="red", link=URL) + "\n"


def test_echo_strips_link_without_color(runner):
    @click.command()
    def cli():
        click.secho("docs", link=URL)

    result = runner.invoke(cli, color=False)
    assert result.output == "docs\n"


def test_echo_strips_link_for_non_terminal_file():
    out = io.StringIO()
    click.echo("see " + click.style("docs", link=URL), file=out)
    assert out.getvalue() == "see docs\n"


def test_wrap_text_ignores_link_sequences():
    words = [f"word{i}" for i in range(12)]
    linked = " ".join(click.style(w, link=f"{URL}/{w}") for w in words)
    wrapped = click.wrap_text(linked, width=24)
    assert [click.unstyle(line) for line in wrapped.splitlines()] == (
        click.wrap_text(" ".join(words), width=24).splitlines()
    )


def test_help_layout_is_unchanged_by_links(runner):
    def make(help_text):
        @click.command()
        @click.option("--name", help=help_text)
        def cli(name):
            pass

        return cli

    words = "Read the user guide for every option this command takes"
    plain = runner.invoke(make(f"{words}, then come back."), ["--help"])
    linked = runner.invoke(
        make(f"{click.style(words, link=URL)}, then come back."),
        ["--help"],
        color=True,
    )
    assert URL in linked.output
    assert click.unstyle(linked.output) == plain.output


def test_styled_text_inside_a_link_round_trips():
    inner = click.style("red", fg="red") + " and plain"
    styled = click.style(inner, link=URL, underline=True)
    assert styled.startswith(OPEN)
    assert styled.endswith(CLOSE)
    assert click.unstyle(styled) == "red and plain"
