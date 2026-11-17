"""Proxy and certificate settings of the person's login shell, for the
provider CLIs brindle launches.

The app's daemon is started by launchd with a Finder-launched app's
environment (daemon.rs ``daemon_plist``): no ``HTTPS_PROXY``, no
``NODE_EXTRA_CA_CERTS``. A CLI started from the person's terminal has them,
so a company's MCP hosts behind its proxy answered there and dropped in
brindle only.

When the daemon starts it runs the person's login shell once, without a
terminal (``$SHELL -l -c``, stdin closed, LIMIT seconds). The shell prints
only the names in ALLOWED, and of those only the ones whose values pass
``valid`` are kept. Nothing else of the shell's environment is asked for or
kept: never ``PATH``, ``NODE_OPTIONS``, ``DYLD_*``, ``LD_*`` or a
``*_PRELOAD``. A shell that hangs, fails or exits non-zero gives nothing.

What was read stays in this process's memory, and leaves it only for the
environment of a provider CLI: one the daemon launches, or one whose pane
asks for its own (below). It is never logged, stored, put in a message or
shown: this module has no logger, and nothing it raises or returns outside
``for_launch`` and ``fetch`` carries a value.

A launch by the daemon (agents._window_environment: a session's own CLI, and
every agent of a session it brings back) adds those settings to the provider
CLI's own environment, and only the ones it would otherwise start without.

Only the daemon reads, and only what it read is ever added. A worker is
launched by a helper under its supervisor's CLI, whose environment an agent
can set (``HTTPS_PROXY=... brindle ...`` from its shell): proxy and CA
settings taken from there, or from a login shell started there, would let an
agent route another agent's traffic, and its credentials, through a proxy of
its choosing. So such a launch adds nothing itself, and its helper never
holds a value. Instead the pane asks the daemon as it starts: its command
runs through a shim (``shim``, ``start_pane``) that asks over the daemon's
Unix socket (``fetch``), sets what it is given and becomes the command. The
daemon knows the pane by the calling process (brindle.daemon.launch_env) and
answers only that agent's own pane, with only what its own launch of that
agent would have added. The shim takes only allowed names with valid values
from the answer, and never one its environment has already. With no daemon,
a refusal, or no answer within FETCH_SECONDS, the command starts as it did
before: without them.

``login_shell_env: "off"`` in ~/.brindle/config.json turns all of it off.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from typing import Callable, Mapping
from urllib.parse import urlsplit

SETTING = "login_shell_env"
LIMIT = 5.0             # seconds the login shell gets to print its environment
MAX_VALUE = 4096        # bytes: longer than any proxy URL or path
FETCH_PATH = "/v1/launch-env"   # where a pane asks the daemon (brindle.daemon.launch_env)
FETCH_SECONDS = 10.0    # how long a pane waits for the daemon's answer before it starts without
SHIM = "_launch-env"    # ``brindle _launch-env <agent> -- <command>``: see brindle.entry

PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
HOST_LISTS = ("NO_PROXY", "no_proxy")
PATHS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
         "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY")
PASSPHRASE = "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE"
# Claude Code's client certificate: no other provider's CLI is handed it.
CLAUDE_ONLY = frozenset({"CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", PASSPHRASE})
# Every name that is ever read. Nothing is added by config, a profile or the shell.
ALLOWED = frozenset({*PROXIES, *HOST_LISTS, *PATHS, PASSPHRASE})

_PROXY_SCHEMES = frozenset({"http", "https", "socks", "socks4", "socks4a", "socks5", "socks5h"})
# One NO_PROXY entry: a host, a domain (".corp.example", "*.corp.example"), an
# address or a network, with an optional port.
_HOST = re.compile(r"[A-Za-z0-9*._-]+(:\d+)?(/\d+)?|\[?[0-9A-Fa-f:.]+\]?(:\d+)?(/\d+)?")
# What the login shell itself is started with: enough to find the person's
# profile, none of the daemon's own settings or secrets.
_SHELL_ENV = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "LANG")
# What the login shell runs: /bin/sh printing the allowed names it inherited
# as NUL-terminated NAME=value records, and nothing else of the environment
# (`env -0` would print all of it, and older macOS has no -0). MARK, a record
# of its own, comes before the first and after the last: whatever the
# person's profile prints (a banner, with or without "=" in it) is outside
# them and never read. No single quote in it: it is handed to the shell
# inside one, which zsh, bash and fish read alike.
MARK = "brindle-login-shell-env"
_PRINT = (f'printf "\\0%s\\0" {MARK}; '
          "for n in " + " ".join(sorted(ALLOWED)) + '; do eval "v=\\${$n-}"; '
          'if [ -n "$v" ]; then printf "%s=%s\\0" "$n" "$v"; fi; done; '
          f'printf "%s\\0" {MARK}')


def enabled() -> bool:
    from brindle import config

    return config.login_shell_env_setting()


def _plain(value: str) -> bool:
    """Not empty, not huge, and nothing in it but printable text."""
    return (0 < len(value.encode("utf-8", "replace")) <= MAX_VALUE
            and not any(ord(c) < 32 or ord(c) == 127 or c == "�" for c in value))


def _proxy(value: str) -> bool:
    try:
        url = urlsplit(value)
        url.port     # raises on a port that is no port
    except ValueError:
        return False
    return url.scheme in _PROXY_SCHEMES and bool(url.hostname) and " " not in value


def _host_list(value: str) -> bool:
    hosts = [h.strip() for h in value.split(",") if h.strip()]
    return bool(hosts) and all(_HOST.fullmatch(h) for h in hosts)


def _path(name: str, value: str) -> bool:
    """An existing file or directory, by its absolute path (a directory list
    for SSL_CERT_DIR, as OpenSSL reads it)."""
    paths = value.split(os.pathsep) if name == "SSL_CERT_DIR" else [value]
    return all(os.path.isabs(p) and (os.path.isfile(p) or os.path.isdir(p)) for p in paths)


def valid(name: str, value: str) -> bool:
    """Whether ``value`` is what ``name`` is for: a proxy an http(s) or socks
    URL, NO_PROXY a list of hosts, a certificate, key or bundle a file or
    directory that exists. The passphrase is only checked for being text."""
    if name not in ALLOWED or not _plain(value):
        return False
    if name in PROXIES:
        return _proxy(value)
    if name in HOST_LISTS:
        return _host_list(value)
    return name == PASSPHRASE or _path(name, value)


def parse(output: bytes) -> dict[str, str]:
    """The allowed, valid settings among the NUL-terminated NAME=value records
    between the shell's two MARK records. What the person's profile printed
    before the first or after the last is not looked at; output without both
    (a shell cut short) gives nothing."""
    mark = MARK.encode()
    _, began, rest = output.partition(b"\0" + mark + b"\0")
    records = rest.split(b"\0")
    if not began or mark not in records:
        return {}
    found: dict[str, str] = {}
    for record in records[:records.index(mark)]:
        name, eq, value = record.decode("utf-8", "replace").partition("=")
        if eq and valid(name, value):
            found[name] = value
    return found


def _login_shell(environ: Mapping[str, str]) -> str | None:
    shell = environ.get("SHELL") or ""
    if not shell:
        try:
            import pwd

            shell = pwd.getpwuid(os.getuid()).pw_shell
        except (ImportError, KeyError, OSError):
            return None
    return shell if os.path.isabs(shell) else None


def _run(argv: list[str], **kw):
    """The login shell, through brindle's one gate for processes: its stdin
    closed, its own session, stopped (the whole group) at its time limit."""
    from brindle import spawn

    return spawn.read(argv, **kw)


def read(run: Callable | None = None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Run the person's login shell once, without a terminal, and return the
    allowed, valid settings it exports. {} when it can't be run, runs past
    LIMIT, or exits non-zero; what went wrong is deliberately not reported,
    since a failed run's output and error carry the shell's environment."""
    environ = os.environ if environ is None else environ
    shell = _login_shell(environ)
    if shell is None:
        return {}
    try:
        proc = (run or _run)([shell, "-l", "-c", f"/bin/sh -c '{_PRINT}'"], text=False, timeout=LIMIT,
                             env={k: environ[k] for k in _SHELL_ENV if k in environ})
    except Exception:  # noqa: BLE001 - a hang, a missing shell, no room: nothing is read
        return {}
    if proc.returncode != 0 or not isinstance(proc.stdout, bytes):
        return {}
    return parse(proc.stdout)


# What the daemon's one read found: in memory only, for this process's launches.
_values: dict[str, str] = {}
_loading: threading.Thread | None = None
# Whether this process is the one that reads (the daemon): its own launches add
# the settings, so their panes have nothing to ask for (asks_daemon).
_reader = False


def load(run: Callable | None = None) -> None:
    """The daemon's start: read the login shell's settings once, unless turned off."""
    global _values, _reader
    _reader = True
    _values = read(run) if enabled() else {}


def load_in_background() -> None:
    """``load`` off the daemon's way, so a slow profile never holds up its
    start. A launch waits for it (for_launch), at most the shell's limit."""
    global _loading, _reader
    _reader = True
    _loading = threading.Thread(target=load, name="brindle-login-shell-env", daemon=True)
    _loading.start()


def for_launch(provider: str, present: Mapping[str, str] | frozenset[str] = frozenset(),
               environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The settings to add to the environment of a ``provider`` CLI being
    launched: what the daemon's read found, less every name in ``present``
    (what the launch sets already) and every name this process's own
    environment has. Claude Code's client certificate goes to Claude Code
    only. Nothing in a process that read nothing (any but the daemon)."""
    if not enabled():
        return {}
    if _loading is not None:
        _loading.join()
    environ = os.environ if environ is None else environ
    return {n: v for n, v in _values.items()
            if n not in present and not environ.get(n) and (provider == "claude" or n not in CLAUDE_ONLY)}


def kept(provider: str) -> frozenset[str]:
    """The allowed names that look like secrets (secrets.secret_looking) and so
    would be scrubbed from a pane: Claude Code's client key and its passphrase,
    which a Claude pane keeps (secrets.pane_unset's ``keep``)."""
    if provider != "claude" or not enabled():
        return frozenset()
    return frozenset({"CLAUDE_CODE_CLIENT_KEY", PASSPHRASE})


# -- a pane no daemon launched: it asks the daemon itself ----------------------------------------


def asks_daemon() -> bool:
    """Whether the pane of a launch by this process should ask the daemon for
    the settings (``shim``): not in the daemon, whose own launch adds them,
    not when turned off, and not with no daemon's socket to ask at."""
    if _reader or not enabled():
        return False
    from brindle.daemon import lifecycle

    return lifecycle.socket_path().exists()


def shim(agent_id: str, argv: list[str]) -> list[str]:
    """``argv`` as ``agent_id``'s pane starts it when it asks the daemon first:
    this same brindle (the one launching, whatever ``brindle`` is on the
    pane's PATH), which becomes ``argv``. It names the agent and the command
    only: no value is ever on a command line."""
    return [sys.executable, "-P", "-m", "brindle", SHIM, agent_id, "--", *argv]


def fetch(agent_id: str) -> dict[str, str]:
    """What the daemon would have added to ``agent_id``'s pane, asked by that
    pane's own process over the daemon's Unix socket. {} when there is no
    daemon, it refuses (the caller isn't that agent's pane), or it doesn't
    answer within FETCH_SECONDS. Of the answer, only allowed names with valid
    values are kept, as of the login shell's own output."""
    from brindle.daemon import holds

    got = holds.call(FETCH_PATH, {"agent_id": agent_id}, FETCH_SECONDS)
    found = got.get("env") if got else None
    if not isinstance(found, dict):
        return {}
    return {n: v for n, v in found.items() if isinstance(n, str) and isinstance(v, str) and valid(n, v)}


def start_pane(agent_id: str, argv: list[str]) -> None:
    """The shim, as the pane's own process: ask the daemon, set what it gives
    where this environment has nothing, and become ``argv``. Whatever goes
    wrong with the asking, ``argv`` starts all the same; what went wrong is
    not reported, and nothing asked for is printed or kept anywhere else."""
    try:
        found = fetch(agent_id)
    except Exception:  # noqa: BLE001 - no answer is no reason not to start
        found = {}
    for name, value in found.items():
        if not os.environ.get(name):
            os.environ[name] = value
    from brindle import spawn

    spawn.become(argv)
