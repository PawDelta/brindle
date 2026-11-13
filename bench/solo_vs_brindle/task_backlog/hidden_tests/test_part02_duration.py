"""Ticket 2: the click.Duration parameter type."""

from datetime import timedelta

import pytest

import click


def convert(value, **bounds):
    return click.Duration(**bounds).convert(value, None, None)


def command(**option_kwargs):
    @click.command()
    @click.option("--every", **option_kwargs)
    def cli(every):
        return every

    return cli


def run(cli, args, **kwargs):
    return cli.main(args, standalone_mode=False, **kwargs)


def test_exported_from_click():
    assert click.Duration is click.types.Duration
    assert isinstance(click.Duration(), click.ParamType)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90s", timedelta(seconds=90)),
        ("45m", timedelta(minutes=45)),
        ("2d", timedelta(days=2)),
        ("1w", timedelta(weeks=1)),
        ("500ms", timedelta(milliseconds=500)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("1h 30m", timedelta(hours=1, minutes=30)),
        ("1.5h", timedelta(minutes=90)),
        ("1.5ms", timedelta(microseconds=1500)),
        ("0s", timedelta(0)),
        (
            "1w2d3h4m5s6ms",
            timedelta(weeks=1, days=2, hours=3, minutes=4, seconds=5, milliseconds=6),
        ),
        ("1H30M", timedelta(hours=1, minutes=30)),
        ("2D 4H", timedelta(days=2, hours=4)),
        ("  45m  ", timedelta(minutes=45)),
    ],
)
def test_parses_components(text, expected):
    assert convert(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("90", timedelta(seconds=90)), ("2.5", timedelta(seconds=2.5)), (" 7 ", timedelta(seconds=7))],
)
def test_bare_number_is_seconds(text, expected):
    assert convert(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "soon",
        "1x",
        "h",
        "1 h",
        "30m1h",
        "1h1h",
        "1s5ms5s",
        "-5s",
        "+5s",
        "1.h",
        ".5s",
        "1h30",
        "5s5",
        "1,5h",
        "9" * 21 + "s",
        "1w" + "9" * 21 + "d",
        "9" * 30,
    ],
)
def test_invalid_input_is_rejected(text):
    with pytest.raises(click.BadParameter):
        convert(text)


def test_error_names_the_value(runner):
    @click.command()
    @click.option("--every", type=click.Duration())
    def cli(every):
        pass

    result = runner.invoke(cli, ["--every", "soon"])
    assert result.exit_code == 2
    assert "'soon'" in result.output


def test_timedelta_passes_through():
    value = timedelta(minutes=3, microseconds=7)
    assert convert(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [(30, timedelta(seconds=30)), (1.5, timedelta(seconds=1.5)), (0, timedelta(0))],
)
def test_numbers_are_seconds(value, expected):
    assert convert(value) == expected


@pytest.mark.parametrize(
    "value", [True, -1, -0.5, [1], None, 10**30, float("inf"), float("nan")]
)
def test_other_values_are_rejected(value):
    with pytest.raises(click.BadParameter):
        convert(value)


def test_too_large_value_is_a_usage_error_even_with_max(runner):
    huge = "9" * 21 + "s"

    @click.command()
    @click.option("--every", type=click.Duration(max=60))
    def cli(every):
        pass

    result = runner.invoke(cli, ["--every", huge])
    assert result.exit_code == 2
    assert repr(huge) in result.output


def test_bounds_are_inclusive():
    bounds = {"min": timedelta(seconds=1), "max": 60}
    assert convert("1s", **bounds) == timedelta(seconds=1)
    assert convert("1m", **bounds) == timedelta(minutes=1)
    assert convert(timedelta(seconds=30), **bounds) == timedelta(seconds=30)


@pytest.mark.parametrize(
    ("value", "bounds"),
    [
        ("999ms", {"min": 1}),
        ("61s", {"max": timedelta(minutes=1)}),
        (timedelta(hours=2), {"min": 10, "max": 3600}),
        (5, {"min": timedelta(seconds=6)}),
    ],
)
def test_values_out_of_bounds_are_rejected(value, bounds):
    with pytest.raises(click.BadParameter):
        convert(value, **bounds)


def test_default_is_checked_against_bounds(runner):
    cli = command(type=click.Duration(max=3600), default=timedelta(hours=2))
    result = runner.invoke(cli, [])
    assert result.exit_code == 2


def test_min_greater_than_max_is_an_error():
    with pytest.raises(ValueError):
        click.Duration(min=timedelta(minutes=2), max=60)


def test_metavar_in_help(runner):
    result = runner.invoke(command(type=click.Duration()), ["--help"])
    assert "--every DURATION" in result.output


def test_info_dict():
    assert click.Duration(min=1, max=timedelta(minutes=2)).to_info_dict() == {
        "param_type": "Duration",
        "name": "duration",
        "min": 1.0,
        "max": 120.0,
    }
    assert click.Duration().to_info_dict() == {
        "param_type": "Duration",
        "name": "duration",
        "min": None,
        "max": None,
    }


def test_timedelta_type_means_duration():
    cli = command(type=timedelta)
    assert run(cli, ["--every", "1h"]) == timedelta(hours=1)


def test_timedelta_default_infers_duration():
    cli = command(default=timedelta(minutes=5))
    assert run(cli, []) == timedelta(minutes=5)
    assert run(cli, ["--every", "10s"]) == timedelta(seconds=10)


def test_value_from_environment_variable(monkeypatch):
    monkeypatch.setenv("BENCH_EVERY", "2m")
    cli = command(type=click.Duration(), envvar="BENCH_EVERY")
    assert run(cli, []) == timedelta(minutes=2)


def test_multiple_values():
    cli = command(type=click.Duration(), multiple=True)
    assert run(cli, ["--every", "1s", "--every", "1m"]) == (
        timedelta(seconds=1),
        timedelta(minutes=1),
    )
