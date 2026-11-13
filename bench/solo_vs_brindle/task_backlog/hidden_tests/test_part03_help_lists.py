"""Ticket 3: lists and whitespace-only lines in help text."""

import click


def wrap(text, width=78, **kwargs):
    return click.wrap_text(text, width, preserve_paragraphs=True, **kwargs)


def test_bullets_keep_their_own_lines():
    assert wrap("Formats:\n- csv\n- json") == "Formats:\n- csv\n- json"


def test_long_item_hangs_under_its_text():
    text = "- csv: comma separated values, one record per line"
    assert wrap(text, 24) == (
        "- csv: comma separated\n  values, one record per\n  line"
    )


def test_star_and_plus_markers():
    assert wrap("Pick:\n* red\n+ green") == "Pick:\n* red\n+ green"


def test_numbered_items_hang_under_their_text():
    text = "Steps:\n1. fetch the newest list of packages\n10) install them all"
    assert wrap(text, 22) == (
        "Steps:\n1. fetch the newest\n   list of packages\n10) install them all"
    )


def test_space_after_marker_is_normalised():
    assert wrap("-   csv\n2.\tjson") == "- csv\n2. json"


def test_unmarked_lines_continue_the_item():
    text = "Formats:\n- json\n  with comments\nand trailing commas\n- csv"
    assert wrap(text) == "Formats:\n- json with comments and trailing commas\n- csv"


def test_intro_text_wraps_before_the_list():
    text = "These are the output formats that the export command understands:\n- csv"
    assert wrap(text, 30) == (
        "These are the output formats\nthat the export command\nunderstands:\n- csv"
    )


def test_nested_item_keeps_its_extra_indent():
    text = "- fruit\n  - apples from the orchard\n- bread"
    assert wrap(text, 20) == (
        "- fruit\n  - apples from the\n    orchard\n- bread"
    )


def test_indented_list_paragraph():
    assert wrap("    - csv\n    - json") == "    - csv\n    - json"


def test_option_names_do_not_start_items():
    text = "Choices:\n- fast\n--verbose shows each step"
    assert wrap(text) == "Choices:\n- fast --verbose shows each step"


def test_numbers_without_marker_do_not_start_items():
    text = "Years:\n- 2023\n2024 came next\n1.5 times busier"
    assert wrap(text) == "Years:\n- 2023 2024 came next 1.5 times busier"


def test_list_uses_the_wrapping_indents():
    text = "- one two three four\n- five"
    assert wrap(text, 14, initial_indent="> ", subsequent_indent="> ") == (
        "> - one two\n>   three four\n> - five"
    )


def test_list_between_paragraphs():
    text = "Intro.\n\n- a\n- b\n\nOutro."
    assert wrap(text) == "Intro.\n\n- a\n- b\n\nOutro."


def test_whitespace_only_line_separates_paragraphs():
    assert wrap("First paragraph.\n   \nSecond paragraph.") == (
        "First paragraph.\n\nSecond paragraph."
    )


def test_whitespace_only_line_ends_a_no_rewrap_block():
    text = "\b\nkeep   this\n\t\nand rewrap\nthis"
    assert wrap(text) == "keep   this\n\nand rewrap this"


def test_command_help_renders_list(runner):
    @click.command()
    def cli():
        """Export the data.

        Formats:
        - csv: comma separated values, one record per line of the output file,
          quoted where needed
        - json
        """

    result = runner.invoke(cli, ["--help"])
    assert result.output.splitlines()[2:8] == [
        "  Export the data.",
        "",
        "  Formats:",
        "  - csv: comma separated values, one record per line of the output file, quoted",
        "    where needed",
        "  - json",
    ]


def test_option_help_renders_list(runner):
    @click.command()
    @click.option(
        "--fmt",
        help="Output format:\n- csv: values separated by commas, quoted where needed\n- json",
    )
    def cli(fmt):
        pass

    result = runner.invoke(cli, ["--help"])
    lines = result.output.splitlines()
    start = lines.index("  --fmt TEXT  Output format:")
    assert lines[start + 1 : start + 4] == [
        "              - csv: values separated by commas, quoted where needed",
        "              - json",
        "  --help      Show this message and exit.",
    ]


def test_epilog_renders_list(runner):
    @click.command(epilog="See also:\n- the guide\n- the FAQ")
    def cli():
        pass

    result = runner.invoke(cli, ["--help"])
    assert result.output.endswith("\n  See also:\n  - the guide\n  - the FAQ\n")


def test_help_ending_in_whitespace_line_has_no_extra_blank_lines(runner):
    # What a docstring ending in an indented blank line looks like.
    @click.command(help="Top level command\n\n        ")
    def cli():
        pass

    result = runner.invoke(cli, ["--help"])
    assert result.output.splitlines() == [
        "Usage: cli [OPTIONS]",
        "",
        "  Top level command",
        "",
        "Options:",
        "  --help  Show this message and exit.",
    ]
