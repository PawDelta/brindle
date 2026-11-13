# Backlog: five tickets for Click

You are working in a checkout of [Click](https://github.com/pallets/click), the
Python package for building command line interfaces. Below are five tickets
from the issue tracker. Fix or implement all five.

The tickets are independent of each other: each one is in a different part of
the code base, and they can be done in any order.

Ground rules:

- Everything a ticket names (functions, parameters, classes) is public API and
  must be spelled exactly as written.
- Existing behaviour must stay as it is except where a ticket changes it, and
  the existing test suite must keep passing (`.venv/bin/python -m pytest
  tests`). Where a ticket changes behaviour that an existing test pins, update
  that test.
- Add tests for each ticket to the repository's test suite, next to the
  existing tests for that area, and follow the project's conventions (`ruff`
  formatting, docstrings with `versionadded` / `versionchanged` notes for
  version 8.6.0).
- Do not edit `CHANGES.md` or the documentation under `docs/`: release notes
  and docs for this batch are written separately.
- No new dependencies. Click supports Python 3.11 and later.

---

## Ticket 1: hyperlinks in styled text

**Type:** feature. **Area:** terminal styling (`click.style`, `click.secho`,
`click.unstyle`).

Most terminals now support clickable links through the OSC 8 escape sequence,
and people want to print them with Click, for example a "see the docs" link in
an error message.

Add a `link` parameter to `click.style()`:

```python
click.style("the docs", fg="blue", link="https://example.com/docs")
```

- When `link` is a non-empty string, the result is wrapped in an OSC 8
  hyperlink. The result is, in this order: the opening sequence
  `"\x1b]8;;" + link + "\x1b\\"`, then the style codes and the text exactly as
  `style()` produces them today (including the reset code at the end when
  `reset` is on), then the closing sequence `"\x1b]8;;\x1b\\"`. So
  `click.style("x", fg="red", link=URL)` is
  `"\x1b]8;;" + URL + "\x1b\\" + "\x1b[31mx\x1b[0m" + "\x1b]8;;\x1b\\"`.
- The link is always closed at the end of the text, even with `reset=False`
  (only the style codes carry over then, not the link).
- `link=None` (the default) and `link=""` add nothing: the result is the same
  as without the parameter.
- The URL is inserted verbatim. It may contain any printable characters,
  including `;`, `?`, `&`, `#` and `%`. A URL containing a control character
  (any character below U+0020, or U+007F) is rejected with `ValueError`, since
  it could end the escape sequence early or inject another one.
- `click.secho()` accepts `link` too and passes it on to `style()`.

Link sequences must behave like every other styling sequence:

- `click.unstyle()` removes them and keeps the link text. It must handle OSC 8
  sequences written by other programs as well: terminated by `ESC \` or by
  `BEL` (`"\x07"`), and with parameters between `8;` and the second `;`
  (for example `"\x1b]8;id=42;https://example.com\x1b\\"`).
- `click.echo()` removes them whenever it removes styles today (output that is
  not a terminal, or `color=False`).
- Anything that measures the visible width of text (help page layout,
  `click.wrap_text`) ignores them, so a linked word takes as much room as the
  same word without a link.

## Ticket 2: a duration parameter type

**Type:** feature. **Area:** parameter types (`click.types`).

Options such as `--every 5m` or `--wait 1h30m` are common, and every project
writes its own parser for them. Add a `click.Duration` parameter type
(defined in `click.types`, exported as `click.Duration`) that converts values
to `datetime.timedelta`.

```python
@click.option("--every", type=click.Duration(min=1, max=timedelta(days=1)))
```

**Text input.** Leading and trailing whitespace is ignored. A value is either:

- a bare number: a number of seconds (`"90"`, `"2.5"`); or
- one or more *components*, each a number immediately followed by a unit
  (`"90s"`, `"1h30m"`, `"1.5h"`). Components may be separated by whitespace
  (`"1h 30m"`) but a number and its unit may not (`"1 h"` is invalid).

A number is one or more digits, optionally followed by `.` and one or more
digits (so `".5s"`, `"1.h"` and `"1,5h"` are invalid). There is no sign:
`"-5s"` and `"+5s"` are invalid. The units are `w` (weeks), `d` (days), `h`
(hours), `m` (minutes), `s` (seconds) and `ms` (milliseconds), in any letter
case (`"1H30M"` is fine). Units must appear in that order, each at most once:
`"30m1h"` and `"1h1h"` are invalid. A unitless number cannot be mixed with
components (`"1h30"` is invalid). An empty or blank value is invalid. The
result is the sum of the components, so `"1w2d3h4m5s6ms"` is
`timedelta(weeks=1, days=2, hours=3, minutes=4, seconds=5, milliseconds=6)`
and `"1.5ms"` is 1,500 microseconds.

**Other input.** A `timedelta` is accepted as is. An `int` or `float` is a
number of seconds; a negative number is invalid. Anything else (including
`bool`) is invalid.

A value too large for a `timedelta` (such as `"999999999999999999999s"` or
`10**30`), or not a finite number (`inf`, `nan`), is invalid too: it fails
with the same bad-parameter error, never with another exception.

**Errors.** An invalid value fails like any other Click type (`BadParameter`,
usage error exit code 2), and the message shows the value as its `repr`, for
example `'soon'`.

**Bounds.** `Duration(min=None, max=None)`: each bound is a `timedelta` or a
number of seconds, and both are inclusive. A value outside them fails with a
bad-parameter error. This applies to every value, including defaults and
values given as a `timedelta`. `min` greater than `max` raises `ValueError`
when the type is created.

**Integration.**

- The metavar in help pages is `DURATION` (`--every DURATION`).
- `to_info_dict()` returns `{"param_type": "Duration", "name": "duration",
  "min": ..., "max": ...}` with each bound as a number of seconds (a `float`),
  or `None` when not set.
- Passing `type=datetime.timedelta` to an option or argument means
  `click.Duration()`. An option with no `type` and a `timedelta` default also
  gets `click.Duration()` (today it gets a string type and returns the
  default's `str()`).
- Environment variables, `multiple=True` and the other parameter features work
  as with any type.

## Ticket 3: lists in help text are run together

**Type:** bug. **Area:** help formatting (`click.formatting`).

Reported: "I documented the accepted formats as a bulleted list in my
command's docstring, and `--help` shows it as one long line:
`Formats: - csv - json`." The same happens in option help and epilogs. Help
text is rewrapped paragraph by paragraph, and the rewrapping knows nothing
about lists.

Fix it in `click.wrap_text(..., preserve_paragraphs=True)`, which renders the
paragraphs of command help, epilogs and option help:

- A line is a **list item** when, after its leading whitespace, it starts with
  a marker followed by at least one space or tab and then text. The markers
  are `-`, `*`, `+`, or one or more digits followed by `.` or `)` (`1.`,
  `10)`). Anything else is not an item: `--verbose shows...`,
  `2024 came next`, `1.5 times...`.
- In a paragraph with at least one item, the lines before the first item are
  ordinary text, wrapped as today. Every item starts on a new line. A line that
  is not an item continues the item before it (joined with a single space).
  No blank lines are added before or between items.
- An item is rendered as its marker, one space (the whitespace after the
  marker is normalised to one space), and its text, wrapped to the width.
  Continuation lines are indented to line up with the start of the item text:
  by the marker's length plus one.
- An item line indented further than the first line of its paragraph keeps
  the extra indentation (a nested item), and its continuation lines line up
  with its own text.
- The paragraph's indentation and the `initial_indent` / `subsequent_indent`
  arguments apply as they do to paragraphs: an item's first line starts with
  `initial_indent`, its continuation lines with `subsequent_indent`.
- `\b` (no-rewrap) paragraphs are not affected.

Example, at width 24:

```
Formats:                          Formats:
- csv: comma separated     ->     - csv: comma separated
  values, one per line              values, one per line
-   json                          - json
```

A second bug in the same function: a line that contains only whitespace
(spaces or tabs) does not separate paragraphs, although it looks exactly like
an empty line. `"First.\n   \nSecond."` renders as `"First.     Second."`.
Treat a whitespace-only line like an empty line everywhere: it separates
paragraphs and ends a `\b` block. One visible effect: help text that ends with
a whitespace-only line (a docstring whose last line is indented blank space)
no longer gets extra blank lines before `Options:`.

## Ticket 4: `get_app_dir` returns relative paths, and has no data or cache folders

**Type:** bug and small feature. **Area:** utilities (`click.get_app_dir`).

**Bug.** On Linux and other Unix systems, `click.get_app_dir("Foo Bar")`
uses `$XDG_CONFIG_HOME` whenever it is set. If it is set to an empty string
(as some session managers do) the result is the relative path `foo-bar`, and
if it is relative the result is relative too. The XDG Base Directory
specification says such values are invalid and must be ignored. Fix it: an
`XDG_*_HOME` variable is used only when its value is an absolute path;
otherwise the default applies. On Windows, treat an empty or relative
`APPDATA` / `LOCALAPPDATA` the same way as an unset one.

**Feature.** Applications also need somewhere to keep data, caches and state,
not only configuration. Add a keyword-only parameter `kind` to
`get_app_dir(app_name, roaming=True, force_posix=False, *, kind="config")`.
The kinds are `"config"` (the default, today's behaviour), `"data"`,
`"cache"` and `"state"`; anything else (including other letter cases) raises
`ValueError`.

| `kind` | Unix (`XDG_*` variable, else default) | macOS | Windows |
|--------|---------------------------------------|-------|---------|
| config | `$XDG_CONFIG_HOME`, else `~/.config` | `~/Library/Application Support` | `APPDATA` if `roaming`, else `LOCALAPPDATA` |
| data | `$XDG_DATA_HOME`, else `~/.local/share` | `~/Library/Application Support` | `APPDATA` if `roaming`, else `LOCALAPPDATA` |
| cache | `$XDG_CACHE_HOME`, else `~/.cache` | `~/Library/Caches` | `LOCALAPPDATA` |
| state | `$XDG_STATE_HOME`, else `~/.local/state` | `~/Library/Application Support` | `LOCALAPPDATA` |

The application folder inside the base folder is named as today: on Unix the
"posixified" name (`foo-bar`), on macOS and Windows the name as given
(`Foo Bar`). macOS ignores the `XDG_*` variables, as it does today. Each kind
reads only its own variable. As today, the platform is decided from
`sys.platform` when the function is called (`"darwin"` is macOS), and Windows
falls back to the home folder when its variable is unset.

With `force_posix=True`, `kind="config"` returns `~/.foo-bar` as today, and
the other kinds return a folder of that name inside it: `~/.foo-bar/data`,
`~/.foo-bar/cache`, `~/.foo-bar/state`.

As today, `get_app_dir` only computes the path and never creates the folder.

## Ticket 5: let `CliRunner` emulate an interactive terminal

**Type:** feature. **Area:** test utilities (`click.testing`).

Code that behaves differently in a terminal (colours, progress bars,
`click.clear()`) cannot be tested with `CliRunner`, because its streams never
look like a terminal. And help output is always wrapped at 80 columns, so
narrow and wide terminals cannot be tested either.

Add two parameters to `CliRunner`, stored as attributes of the same name:

```python
CliRunner(tty=False, terminal_width=80)
```

and the same two to `CliRunner.invoke()` and `CliRunner.isolation()`, where
they default to `None`, meaning "use the runner's value". A value given to
`invoke()` applies to that call only.

**`tty`.** With `tty=True`, during the invocation:

- `sys.stdout.isatty()` and `sys.stderr.isatty()` return `True`. `sys.stdin`
  holds the scripted input and is still not a terminal
  (`sys.stdin.isatty()` is `False`).
- Styles are kept in the output, as in a real terminal. The `color` parameter
  of `invoke()` and `isolation()` now defaults to `None`, which means "the
  same as `tty`": an explicit `color=False` still strips styles and
  `color=True` still keeps them.
- So Click's terminal-only behaviour happens: a progress bar is drawn (with
  its bar and percentage) instead of printing only its label, and
  `click.clear()` writes its escape sequence `"\x1b[2J\x1b[1;1H"`.
- `click.echo_via_pager()` must not start a pager: its text goes straight to
  the captured output, as it does today.

With `tty=False` (the default) nothing changes from today.

**`terminal_width`.** During the invocation:

- Help pages are wrapped to `terminal_width` columns instead of the fixed 80.
  A command that sets `terminal_width` in its own `context_settings` still
  gets that width.
- `shutil.get_terminal_size()` reports `terminal_width` columns and 24 lines,
  even when the runner's or the call's `env` overrides set `COLUMNS` or
  `LINES`. The process environment is restored afterwards, as for the `env`
  overrides.

`terminal_width` must be a positive `int` (not a `bool`). Any other value
raises `ValueError`, from the constructor or from `invoke()` / `isolation()`.

Apart from what `shutil.get_terminal_size()` reports, the default
`CliRunner()` behaves exactly as today.
