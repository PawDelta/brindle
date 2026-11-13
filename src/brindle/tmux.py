"""Minimal tmux driver: one session per workspace, one window per agent."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from brindle import __version__, spawn


class TmuxError(RuntimeError):
    pass


class TmuxTimeout(TmuxError):
    """The tmux server took longer than ``timeout()`` to answer one command:
    it is hung (one accepted connections but answered nothing for 17 minutes
    after a client attached to it from inside one of its own panes), or the
    machine is badly overloaded. Never a verdict about any pane or session:
    a caller that would otherwise judge agents by the answer (a cull, a
    pause, a session drop) must give up instead, which is why list_panes
    raises this rather than returning an empty snapshot."""


class TmuxBusy(TmuxTimeout, spawn.Busy):
    """A tmux read the machine had no room for, or that its request stopped
    (see brindle.spawn). Like a timeout, the answer says nothing about any
    pane or session, so a caller judging agents by it gives up the same way."""


DEFAULT_TIMEOUT = 30.0


def timeout() -> float:
    """How long one tmux command may take, in seconds (BRINDLE_TMUX_TIMEOUT,
    default DEFAULT_TIMEOUT). Without a limit, a wedged server hung every
    brindle process that touched it: `brindle` at its first has-session,
    the sidebar's refresh, `brindle doctor`, every hook."""
    try:
        return float(os.environ.get("BRINDLE_TMUX_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


def _hung(base: list[str], args: tuple[str, ...]) -> TmuxTimeout:
    # the server name follows -L (the flags -u and -L may come in either order)
    server = (f"the tmux server -L {base[base.index('-L') + 1]}" if "-L" in base
              else "the default tmux server")
    return TmuxTimeout(
        f"tmux {args[0] if args else ''} got no answer within {timeout():g}s: {server} is not "
        "responding, so every tmux command on it hangs. If it stays stuck, `tmux kill-server` "
        "(or, failing that, `pkill -x tmux`) restarts it, at the cost of every session on it."
    )


def current_server() -> str | None:
    """The private tmux server brindle is using (BRINDLE_TMUX_SOCKET: the
    test suite's, or a demo recording's), or None for the default server.
    Recorded on each agent at launch (agents.tmux_server): pane ids are only
    meaningful on the server that issued them, so nothing may judge an agent
    by another server's panes."""
    return os.environ.get("BRINDLE_TMUX_SOCKET") or None


def _server_pid(server: str | None) -> int | None:
    proc = _tmux("display-message", "-p", "#{pid}", check=False, server=server)
    out = proc.stdout.strip()
    return int(out) if proc.returncode == 0 and out.isdigit() else None


def cwd_gone(server: str | None) -> bool:
    """Whether the running tmux ``server``'s working directory was deleted
    (and maybe recreated at the same path: a new directory, a different
    inode). Every window such a server opens then starts in the dead
    directory, whatever ``-c`` says (tmux 3.8), and Claude Code exits at once
    with "The current directory no longer exists" unless the pane changes
    directory itself (in_dir, which every pane brindle starts does).
    `brindle doctor` warns about it. False when the server isn't running or
    its directory can't be read."""
    pid = _server_pid(server)
    if pid is None:
        return False
    if os.path.isdir("/proc/self"):     # Linux: the link names a deleted directory so
        try:
            return os.readlink(f"/proc/{pid}/cwd").endswith(" (deleted)")
        except OSError:
            return False
    if not shutil.which("lsof"):
        return False
    try:
        proc = spawn.read(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fin"], timeout=timeout())
    except (spawn.Busy, subprocess.TimeoutExpired, OSError):
        return False
    fields = {line[0]: line[1:] for line in proc.stdout.splitlines() if line[:1] in ("i", "n")}
    if not fields.get("i", "").isdigit() or not fields.get("n"):
        return False
    try:
        return os.stat(fields["n"]).st_ino != int(fields["i"])
    except OSError:
        return True




def _bin() -> str:
    """BRINDLE_TMUX_BIN, else ``tmux`` on PATH."""
    return os.environ.get("BRINDLE_TMUX_BIN") or "tmux"


def _argv(server: str | None) -> list[str]:
    # -u: UTF-8 output even when the caller's locale is unset (the desktop
    # app's bundled engine starts from launchd with no LANG/LC_*, and tmux
    # would otherwise print every non-ASCII byte as "_").
    return [_bin(), "-u", "-L", server] if server else [_bin(), "-u"]


def _base() -> list[str]:
    """``tmux``, or ``tmux -L <name>`` when BRINDLE_TMUX_SOCKET selects a
    private server (used by the test suite so runs can't collide)."""
    return _argv(current_server())


# The default ``server`` of the calls that can ask another tmux server: the
# one brindle is using (see current_server).
_THIS_SERVER: object = object()

# Commands that only ask (and can't start a server): they run as brindle.spawn
# reads, at background priority in the daemon and stopped at their request's
# deadline. Everything else runs as it always has: a command that starts the
# server must not hand it (and every agent pane) background priority.
_READS = frozenset({"capture-pane", "has-session", "list-clients", "list-panes", "list-sessions",
                    "list-windows", "show-environment", "show-options"})


def _is_read(args: tuple[str, ...]) -> bool:
    if not args or ";" in args:
        return False
    return args[0] in _READS or (args[0] == "display-message" and "-p" in args)


def _start(base: list[str], args: tuple[str, ...], input: str | None = None,
           authorize: Callable[[], None] | None = None) -> subprocess.CompletedProcess:
    try:
        if _is_read(args):
            return spawn.read([*base, *args], input=input, timeout=timeout())
        # A change: refused past the request's deadline, else the request runs to its end
        # (brindle.spawn.begin_write). At tmux's normal priority, not through spawn.action.
        spawn.begin_write()
        argv, limit = [*base, *args], timeout()
        if authorize is not None:
            authorize()  # after command preparation, immediately before the external write
        return subprocess.run(argv, capture_output=True, text=True, input=input, timeout=limit)
    except spawn.Busy as e:
        raise TmuxBusy(f"tmux {args[0] if args else ''}: {e}") from None
    except subprocess.TimeoutExpired:
        raise _hung(base, args) from None


def _tmux(*args: str, input: str | None = None, check: bool = True,
          server: object = _THIS_SERVER,
          authorize: Callable[[], None] | None = None) -> subprocess.CompletedProcess:
    if not shutil.which(_bin()):
        from brindle.doctor import tmux_install

        raise TmuxError(f"tmux is not installed. Install: {tmux_install()}")
    base = _base() if server is _THIS_SERVER else _argv(server)  # type: ignore[arg-type]
    proc = (_start(base, args, input) if authorize is None
            else _start(base, args, input, authorize=authorize))
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def _tmux_batch(cmds: list[list[str]]) -> None:
    """Run several tmux commands in one process (``cmd ; cmd``) instead of one
    process each. Best effort like ``check=False``. tmux stops a sequence at the
    first command it rejects (an option an older tmux lacks), so on failure each
    command is run on its own and only the rejected ones are skipped."""
    args: list[str] = []
    for cmd in cmds:
        if args:
            args.append(";")
        # an argument ending in ";" would read as a separator: escape it
        args.extend(a[:-1] + "\\;" if a.endswith(";") and not a.endswith("\\;") else a for a in cmd)
    if args and _tmux(*args, check=False).returncode != 0:
        for cmd in cmds:
            _tmux(*cmd, check=False)


def has_session(session: str) -> bool:
    return _tmux("has-session", "-t", f"={session}", check=False).returncode == 0


def terminal_size() -> tuple[int, int] | None:
    """This process's terminal (columns, rows), the size a client attached
    from it has. None without a terminal (the daemon, a helper, the MCP
    server), or from inside tmux, where the terminal is a pane and not the
    client's size."""
    if os.environ.get("TMUX"):
        return None
    try:
        cols, rows = os.get_terminal_size(sys.__stdout__.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    return (cols, rows) if cols > 0 and rows > 0 else None


def status_lines(session: str) -> int:
    """How many rows ``session``'s status line takes from a client's: its
    ``status`` option as it applies there (the person's config can turn it
    off or make it taller)."""
    status = _tmux("display-message", "-p", "-t", f"={session}:", "#{status}", check=False).stdout.strip()
    return 0 if status == "off" else int(status) if status.isdigit() else 1


def _default_size(session: str, size: tuple[int, int]) -> None:
    """New windows of ``session`` start ``size`` (columns, rows) while no client
    says otherwise. On its own and best effort: a tmux that refuses the option
    refuses only this."""
    _tmux("set-option", "-t", session, "default-size", f"{size[0]}x{size[1]}", check=False)


def ensure_session(session: str, cwd: str, env: dict[str, str], size: tuple[int, int] | None = None) -> None:
    """Create a detached session whose first window is a plain shell, sized
    for the terminal that will attach to it (see terminal_size): its new
    windows start at the size that terminal shows them at, its rows less
    the session's status line, so a chat draws once at the size it's seen
    at. Detached sessions are otherwise 80x24, and attaching resized a chat
    that had already drawn: Claude Code resuming a conversation went blank.

    ``size``: the window size to start them at instead, for a caller with no
    terminal that knows it (a daemon's recovery: agent_window_size, as the
    last daemon left it). It applies to a session that already exists too."""
    if has_session(session):
        for k, v in env.items():
            _tmux("set-environment", "-t", f"={session}", k, v, check=False)
        if size:
            _default_size(session, size)
        for key in [k for k in _SESSION_HOMES if k[1] == session]:
            del _SESSION_HOMES[key]     # session_infos reads its BRINDLE_HOME again
        return
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    terminal = None if size else terminal_size()
    first = size or terminal
    size_args = ["-x", str(first[0]), "-y", str(first[1])] if first else []
    _tmux("new-session", "-d", "-s", session, "-n", "shell", "-c", cwd, *size_args, *env_args)
    if terminal:
        # Known only now: the server (and the person's config) may have just started.
        size = (terminal[0], max(terminal[1] - status_lines(session), 1))
    if size:
        _default_size(session, size)


# tmux refuses a command over its message size (about 16 KB) with "command
# too long". An agent's argv carries its whole system prompt and first
# message, which a supervisor's brief plus a long goal can exceed.
MAX_COMMAND = 8000


def _short(command: list[str], env: dict[str, str] | None = None) -> list[str]:
    """``command``, or, when it is too long for tmux or ``env`` is given, a
    private script that runs it: written 0600 under brindle's home, it
    deletes itself as it starts, exports ``env`` and then execs the original
    argv, so nothing else changes. ``env`` goes this way, never as tmux ``-e
    NAME=VALUE`` arguments, which any user can read in ``ps``."""
    if not env and sum(len(a.encode()) + 1 for a in command) <= MAX_COMMAND:
        return command
    import re
    import shlex
    import tempfile

    from brindle.config import brindle_home

    exports = ""
    for k, v in (env or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise TmuxError(f"not a valid environment variable name: {k!r}")
        exports += f"export {k}={shlex.quote(v)}\n"
    d = brindle_home() / "launch"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="launch-", suffix=".sh", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nrm -f "$0"\n{exports}exec {shlex.join(command)}\n')
    return ["/bin/sh", path]


def inherited_names(session: str) -> set[str]:
    """The names of every variable a new pane of ``session`` would inherit:
    the tmux server's global environment, the session's own, and this
    process's (which started the server, in the common case). Names only,
    never values."""
    names = set(os.environ)
    for scope in (("-g",), ("-t", f"={session}")):
        proc = _tmux("show-environment", *scope, check=False)
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            if line and not line.startswith("-"):   # "-NAME": marked unset already
                names.add(line.partition("=")[0])
    return names


def inherited_env(session: str) -> dict[str, str]:
    """Snapshot the environment a new pane inherits, without logging values."""
    env = {}
    for scope in (("-g",), ("-t", f"={session}")):
        try:
            proc = _tmux("show-environment", *scope, check=False)
        except TmuxError:
            return dict(os.environ) if scope == ("-g",) else env
        if proc.returncode != 0:
            if scope == ("-g",):
                # A new server will inherit the process that creates it.
                return dict(os.environ)
            continue
        for line in proc.stdout.splitlines():
            if line.startswith("-"):
                env.pop(line[1:], None)
            elif "=" in line:
                name, _, value = line.partition("=")
                env[name] = value
    return env


def scrubbed(command: list[str], unset: list[str]) -> list[str]:
    """``command`` started without the variables ``unset``: panes inherit the
    tmux server's whole environment, and tmux has no per-pane way to take a
    variable out of it, so the pane runs ``env -u NAME ... -- command``
    (which execs ``command``, so the pane's process is still the command)."""
    if not unset:
        return command
    return ["/usr/bin/env", *(a for n in unset for a in ("-u", n)), "--", *command]


# The pane tag of the daemon's action terminals (sign-ins, upgrades, the
# app's shell tabs): the person's own terminals, not agents' (see new_window).
TERMINAL_TAG = "@brindle_terminal"
# The sessions the app's terminal views make (daemon.pty_view): each links one
# window it shows, so that window's panes are listed under it too.
VIEW_PREFIX = "brindle-view-"


def in_dir(cwd: str, command: list[str]) -> list[str]:
    """``command`` run in ``cwd``: the pane changes there itself, so it never
    depends on tmux's start directory (a server sitting in a deleted
    directory starts every window there, whatever ``-c`` says)."""
    return ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', cwd, *command]


def new_window(session: str, name: str, cwd: str, command: list[str], env: dict[str, str],
               tag: tuple[str, str] | None = None, keep: Iterable[str] = (),
               allow: Iterable[str] | None = None, deny: Iterable[str] = (),
               agent_env: Iterable[str] = (), enforce: bool = False) -> str:
    """Open a window running ``command``. Returns the agent's PANE id (``%<n>``),
    not the window's: a window can hold more than one pane (e.g. the watch
    dashboard beside a supervisor), and keys sent to a window go to whichever
    pane happens to be active. ``tag`` (a pane option name and value, see
    set_pane_tag) is set on the pane before this returns, so the pane says
    whose it is from the moment anything else can see it.

    An agent's pane starts without any secret-looking variable
    (secrets.secret_looking: brindle's own secrets, and a SLACK_BOT_TOKEN the
    person's shell exported), which it would otherwise inherit from the tmux
    server. Whatever ``env`` sets (a profile's own key) and the names in
    ``keep`` (the agent's provider's credentials, see
    secrets.provider_credentials) are left alone. With ``enforce`` (a repo's
    ``agent_env_mode``), every inherited variable off the allowlist (see
    secrets.env_allowed, with the repo's ``agent_env``) is taken out too;
    with ``allow`` (a profile's ``env_allow``) the allowlist is that, always
    enforced. The names in ``deny`` (an org's managed models: the person's
    own keys) are taken out whatever else says. A pane tagged TERMINAL_TAG
    (an action terminal the person types into) loses only brindle's own
    secrets.

    The pane changes to ``cwd`` itself (in_dir)."""
    from brindle import secrets

    # The federation proxy's token isn't a person's key: set explicitly, so a
    # tmux-level value can't stand in for it.
    exempt = secrets.proxy_exempt(env) if deny else {}
    if exempt:
        env = {**env, **exempt}
    deny = secrets.pane_deny(deny, env)
    names = inherited_names(session)
    if not tag or tag[0] != TERMINAL_TAG:
        # What the launch sets itself is filtered too: a secret-looking name in a
        # profile's env passes only as the provider's credential.
        unset = secrets.pane_unset({*names, *env}, keep=secrets.launch_keep(keep, env), allow=allow,
                                   deny=deny, agent_env=agent_env, enforce=enforce)
        env = {k: v for k, v in env.items() if k not in unset}
    else:
        unset = secrets.pane_unset([n for n in names if secrets.is_job_secret(n)], keep=(*keep, *env),
                                   allow=allow, deny=deny)
    launch = uuid.uuid4().hex[:12]
    proc = _tmux(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:",
        "-n", name, "-c", cwd, "--", *_tags_itself(_short(scrubbed(in_dir(cwd, command), unset), env), tag, launch),
    )
    target = NewPane(proc.stdout.strip())
    target.launch = launch
    if tag and tag[0] == AGENT_TAG:
        _set_for_launch(target, launch, *tag)
    elif tag:
        set_pane_tag(target, *tag)
    # Keep the agent's pane around after it exits so its output can be read.
    _tmux("set-option", "-p", "-t", target, "remain-on-exit", "on", check=False)
    return target


class NewPane(str):
    """A pane id as new_window returns it, with the launch that opened it (mark_launched)."""
    launch = ""


def _tags_itself(command: list[str], tag: tuple[str, str] | None, launch: str = "") -> list[str]:
    """``command`` run only once its agent's pane has tagged itself. Whose it
    is doesn't then depend on whoever opened the window living to say so: the
    tag and the launch are arguments here, so tmux's start command names both
    from the moment the pane exists (agent_panes), before this has even run,
    and nothing can change them after."""
    if not tag or tag[0] != AGENT_TAG:
        return command
    setter = shlex.join([shutil.which(_bin()) or _bin(), *_base()[1:], "set-option", "-p", "-t"])
    return ["/bin/sh", "-c", f'{setter} "$TMUX_PANE" "$0" "$1" 2>/dev/null && shift 2 && exec "$@"; '
            "echo 'brindle: this pane could not say whose it is, so nothing was started' >&2; exit 1",
            *tag, launch or "-", *command]


def _set_for_launch(pane: str, launch: str, key: str, value: str) -> None:
    """Set ``key`` on ``pane`` only if it is still the one ``launch`` opened.
    Pane ids come round again after a tmux restart; tmux itself decides this,
    so nothing can come between the look and the write."""
    _tmux("if-shell", "-F", "-t", pane, f"#{{m:*{AGENT_TAG} * {launch} *,#{{pane_start_command}}}}",
          f"set-option -p -t {pane} {key} {value}", check=False)


def mark_launched(pane: str) -> None:
    """The last step of a launch. A pane new_window didn't return has no launch to finish."""
    if launch := getattr(pane, "launch", ""):
        _set_for_launch(pane, launch, LAUNCHED_TAG, launch)


@dataclass(frozen=True)
class AgentPane:
    pane: str
    agent: str
    alive: bool
    launched: bool      # the launch that opened it finished (mark_launched): one cut off part way isn't set up to run


def agent_panes() -> list[AgentPane]:
    """Every pane on this server that is an agent's, from tmux alone: the only
    record of what is running (agents.settle_panes). Empty when no server runs;
    TmuxError when one doesn't answer."""
    fmt = "\t".join(["#{pane_id}", "#{pane_dead}", f"#{{{AGENT_TAG}}}", f"#{{{LAUNCHED_TAG}}}", "#{pane_start_command}"])
    proc = _tmux("list-panes", "-a", "-F", fmt, check=False)
    if proc.returncode != 0:
        return []
    panes = []
    for line in proc.stdout.splitlines():
        pane, dead, tag, finished, command = line.split("\t", 4)
        named = re.match(rf'/bin/sh -c .*? {AGENT_TAG} "?([\w.-]+)"? "?([\w.-]+)', command)
        if agent := named.group(1) if named else tag:   # what a pane was started as can't be written over
            panes.append(AgentPane(pane, agent, dead.strip() == "0", bool(named and finished == named.group(2))))
    return panes


def gated(command: list[str]) -> tuple[list[str], str]:
    """``command`` held back until open_gate is called with the channel
    returned alongside it: a window's layout (the sidebar split beside it)
    is settled first, so the program draws once, at the size it stays.
    tmux remembers a signal sent before the wait, so the order never hangs."""
    channel = f"brindle-layout-{uuid.uuid4().hex}"
    wait = shlex.join([shutil.which(_bin()) or _bin(), *_base()[1:], "wait-for"])
    return ["/bin/sh", "-c", f'{wait} "$0" 2>/dev/null; exec "$@"', channel, *command], channel


def open_gate(channel: str) -> None:
    _tmux("wait-for", "-S", channel, check=False)


def windows(session: str) -> list[str]:
    proc = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}", check=False)
    return proc.stdout.split() if proc.returncode == 0 else []


# The brindle window as pawdelta.com/brindle draws it (Brindle palette, night):
# a dark brown ground, brindle-brown and fawn accents, warm grey text.
THEME = {
    # The website's shell colours (pawdelta.com/brindle): its terminal blocks stay dark in both themes.
    # bg is the site's #0c0c0e nudged to a colour sixel percentages hit exactly (the logo image).
    "bg": "#0d0d0f", "bg2": "#16161a", "line": "#242427",
    "accent": "#8a5a2b", "accent_light": "#d7af87",
    "text": "#eee6dc", "muted": "#8b7d6f", "muted2": "#b9ab9b",
}


def set_alert(session: str, text: str, style: str = "warn") -> None:
    """Show ``text`` next to the version in ``session``'s status bar (empty: clear it)
    through the session option the status line reads. Called only when it changes."""
    colour = "#e0b04a" if style == "warn" else "#e06c5a"
    value = f"#[fg={colour},bold]{text.replace('#', '##')} #[default]" if text else ""
    _tmux("set-option", "-t", session, "@brindle_alert", value, check=False)


def apply_theme(session: str) -> None:
    """Style one brindle session (never the person's global tmux config)."""
    from brindle import updates

    update = updates.notice(compact=True)
    t = THEME
    opts = {
        "status-style": f"bg={t['bg2']},fg={t['muted2']}",
        # Which brindle runs this session: one started before an upgrade keeps
        # running the old code until it is restarted.
        "status-left": (f"#[bg={t['accent']},fg={t['text']},bold] brindle "
                        f"#[bg={t['bg2']},fg={t['muted2']},nobold] {__version__} " +
                        (f"{update} " if update else "") +
                        "#{@brindle_alert}"),   # org warning/messages, set by pro.status
        "status-left-length": "120",
        "status-right": f"#[fg={t['muted']}]#{{session_name}}  %H:%M ",
        "status-right-length": "60",
        "window-status-format": f"#[fg={t['muted']}] #W ",
        "window-status-current-format": f"#[fg={t['accent_light']},bold] #W ",
        "pane-border-style": f"fg={t['line']}",
        "pane-active-border-style": f"fg={t['accent_light']}",
        "pane-border-lines": "single",
        "window-style": f"bg={t['bg']}",
        "window-active-style": f"bg={t['bg']}",
        "message-style": f"bg={t['accent']},fg={t['text']}",
        "mode-style": f"bg={t['accent']},fg={t['text']}",
        # Wheel scrolling and click-to-focus between the sidebar and the chat.
        "mouse": "on",
    }
    # Lets Claude Code notice when its pane gains or loses focus (it asks for
    # this). Server-wide in tmux, and harmless for other sessions.
    cmds = [["set-option", "-s", "focus-events", "on"]]
    # set-option doesn't accept the "=name" exact-match form other commands do.
    cmds += [["set-option", "-t", session, key, value] for key, value in opts.items()]
    # window options: set on every window of the session
    wins = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_id}", check=False).stdout.split()
    for key in ("window-style", "window-active-style", "pane-border-style",
                "pane-active-border-style", "pane-border-lines", "mode-style",
                "window-status-format", "window-status-current-format"):
        cmds += [["set-option", "-w", "-t", win, key, opts[key]] for win in wins]
    _tmux_batch(cmds)
    set_follow_hooks(session)
    bind_session_keys(session)


# Fired whenever a brindle session's active window changes (picking a different
# window with the mouse, `next-window`, ...) or a client switches into it
# (switch-client from elsewhere): both are how the sidebar's home window can
# change, so both relocate it (see agents.sidebar_follow). Session-scoped
# only, like the rest of this function: never -g, so a plain tmux session the
# person opened themselves is never touched.
FOLLOW_HOOKS = ("session-window-changed", "client-session-changed")


def set_follow_hooks(session: str) -> None:
    """``session`` is baked in literally rather than read back from tmux's
    own ``#{hook_session_name}`` format variable: that variable came back
    empty for client-session-changed in testing (with and without -b), while
    a hook set with ``-t <session>`` only ever fires for that session anyway,
    so there's nothing it would tell us that we don't already know. The
    command redirects its own output and always exits 0, so a bug in it can
    never surface as a visible tmux error or message popup."""
    from brindle.providers import brindle_invocation

    cmd = " ".join(shlex.quote(a) for a in [*brindle_invocation(), "_sidebar-follow", session])
    shell = f"{cmd} >/dev/null 2>&1 || true"
    _tmux_batch([["set-hook", "-t", session, hook, f"run-shell -b {shlex.quote(shell)}"]
                 for hook in FOLLOW_HOOKS])


SIDEBAR_BOTTOM_LINES = 12


def _sidebar_geometry(position: str, size: int) -> tuple[list[str], str]:
    """split/join-pane flags and the resize-pane flag for a sidebar at
    ``position`` ("left", or "bottom" under the chat, full width)."""
    if position == "bottom":
        return ["-v", "-l", str(min(size, SIDEBAR_BOTTOM_LINES))], "-y"
    return ["-h", "-b", "-l", str(size)], "-x"


def split_left(target: str, cwd: str, command: list[str], env: dict[str, str],
               columns: int = 30, position: str = "left") -> str:
    """Open a narrow pane to the LEFT of ``target`` (or, for ``position``
    "bottom", a short one under it) running ``command``, keeping focus on
    ``target``. Returns the new pane's id."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    geometry, axis = _sidebar_geometry(position, columns)
    proc = _tmux(
        "split-window", "-d", *geometry, "-P", "-F", "#{pane_id}",
        "-t", target, "-c", cwd, *env_args, "--", *in_dir(cwd, command),
    )
    pane = proc.stdout.strip()
    size = min(columns, SIDEBAR_BOTTOM_LINES) if position == "bottom" else columns
    _pin_sidebar(target, pane, axis, size)
    return pane


# The window hooks that can hold the sidebar at its size, best first, each with
# the command that wraps the resize. ``window-resized`` is the one made for it,
# but tmux 3.2 (Ubuntu 22.04's) doesn't have it and rejects it as an invalid
# option. There a window's resize fires ``window-layout-changed`` instead. So
# does zooming a pane, and resize-pane unzooms the window, so under that hook
# the sidebar is left alone while the window is zoomed (toggle_sidebar hides it
# that way) and pinned again by the unzoom.
SIDEBAR_PIN_HOOKS = (
    ("window-resized", "{resize}"),
    ("window-layout-changed", "if-shell -F '#{{?window_zoomed_flag,0,1}}' '{resize}'"),
)


def _pin_sidebar(target: str, pane: str, axis: str, size: int) -> None:
    """tmux grows every pane proportionally when a client attaches at a bigger
    size; keep the sidebar ``pane`` at its size whenever ``target``'s window
    is resized, with the first hook this tmux server accepts."""
    resize = f"resize-pane -t {pane} {axis} {size}"
    for hook, command in SIDEBAR_PIN_HOOKS:
        if _tmux("set-hook", "-w", "-t", target, hook, command.format(resize=resize),
                 check=False).returncode == 0:
            return


def _unpin_sidebar(window: str) -> None:
    """Clear the pin from ``window``: the hook _pin_sidebar set there."""
    for hook, _ in SIDEBAR_PIN_HOOKS:
        if _tmux("set-hook", "-w", "-t", window, "-u", hook, check=False).returncode == 0:
            return


# Pane options counting a sidebar's crash restarts (see keep_sidebar_alive).
SIDEBAR_RESTARTS = "@brindle_sidebar_restarts"
SIDEBAR_RESTARTS_SINCE = "@brindle_sidebar_restarts_since"
# Set once the hook has stopped restarting the pane: nothing will revive it.
SIDEBAR_GAVE_UP = "@brindle_sidebar_gave_up"
SIDEBAR_MAX_RESTARTS = 5
SIDEBAR_RESTART_WINDOW = 60


def sidebar_respawn_script(pane: str, cwd: str, env: dict[str, str], log: Path) -> str:
    """The shell script ``pane``'s pane-died hook runs, with the dead
    process's exit status as ``$1``: it runs the sidebar's command again in
    ``pane`` (respawn-pane keeps neither the cwd nor the ``-e`` environment,
    so both are given again) and logs that to ``log``, unless the pane has
    already been restarted SIDEBAR_MAX_RESTARTS times within
    SIDEBAR_RESTART_WINDOW seconds: then it logs that it gives up and leaves
    the dead pane showing its error. It prints nothing and always exits 0,
    whatever fails (the pane already gone, say). Plain sh and tmux, so it still works
    when the engine itself is what fails to start."""
    t = shlex.join([shutil.which(_bin()) or _bin(), *_base()[1:]])
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    respawn = shlex.join(["respawn-pane", "-t", pane, "-c", cwd, *env_args])
    show = f"{t} show-options -p -q -v -t {shlex.quote(pane)}"
    log_q = shlex.quote(str(log))
    stamp = "$(date '+%Y-%m-%d %H:%M:%S') sidebar exited with status $1"
    # The hook's output and a nonzero exit are shown by tmux in view mode
    # over whatever pane the person is looking at (another session's agent,
    # say), so everything goes to the log and the script always exits 0.
    return f"""exec >>{log_q} 2>&1
now=$(date +%s)
n=$({show} {SIDEBAR_RESTARTS}); since=$({show} {SIDEBAR_RESTARTS_SINCE})
if [ -z "$since" ] || [ $((now - since)) -ge {SIDEBAR_RESTART_WINDOW} ]; then n=0; since=$now; fi
n=${{n:-0}}
if [ "$n" -ge {SIDEBAR_MAX_RESTARTS} ]; then
  {t} set-option -p -t {shlex.quote(pane)} {SIDEBAR_GAVE_UP} 1
  echo "{stamp}; restarted $n times in {SIDEBAR_RESTART_WINDOW}s, leaving it down" >> {log_q}
  exit 0
fi
{t} set-option -p -t {shlex.quote(pane)} {SIDEBAR_RESTARTS} $((n + 1))
{t} set-option -p -t {shlex.quote(pane)} {SIDEBAR_RESTARTS_SINCE} "$since"
echo "{stamp}; restarting it ($((n + 1))/{SIDEBAR_MAX_RESTARTS})" >> {log_q}
{t} {respawn} || echo "{stamp}; restarting it failed"
exit 0
"""


# What a new sidebar pane runs until start_sidebar replaces it: anything
# that waits without exiting.
SIDEBAR_STAND_IN = ["cat"]


def start_sidebar(pane: str, cwd: str, command: list[str], env: dict[str, str], log: Path) -> None:
    """Run the sidebar ``command`` in ``pane`` (opened on SIDEBAR_STAND_IN),
    restarted whenever it exits nonzero (a crash, or an upgrade re-exec into
    a half-installed engine), with no waiting between tries: ``remain-on-exit
    failed`` keeps just that pane around after a failed exit, and a hook on
    just that pane respawns it (see sidebar_respawn_script). Both are set
    before ``command`` starts, so a crash on startup comes back too. A zero
    exit, the person quitting, closes the pane as before. Both are pane
    options, never global ones, so no other pane is touched; they move with
    the pane wherever the sidebar goes."""
    # run-shell expands formats: keep any "#" of ours literal.
    script = sidebar_respawn_script(pane, cwd, env, log).replace("#", "##")
    shell = shlex.join(["/bin/sh", "-c", script, "brindle-sidebar"]) + " #{pane_dead_status}"
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    _tmux_batch([["set-option", "-p", "-t", pane, "remain-on-exit", "failed"],
                 ["set-hook", "-p", "-t", pane, "pane-died", f"run-shell -b {shlex.quote(shell)}"],
                 ["respawn-pane", "-k", "-t", pane, "-c", cwd, *env_args, "--", *in_dir(cwd, command)]])


def window_alive(target: str, server: object = _THIS_SERVER) -> bool:
    """Whether ``target`` (a pane or window id) is alive. ``server`` names
    another tmux server to ask (a private socket name, or None for the
    default server): an agent's own, when it isn't the one brindle is using."""
    proc = _tmux("display-message", "-p", "-t", target, "#{pane_dead}", check=False, server=server)
    return proc.returncode == 0 and proc.stdout.strip() == "0"


# Pane-level user options brindle sets to say what a pane is: an agent's (its
# id; see agents.AGENT_TAG) or the dashboard's (its session root's id; see
# agents.SIDEBAR_TAG). list_panes reads them along with liveness, in the same
# call, so no caller needs a second one.
AGENT_TAG = "@brindle_agent"
SIDEBAR_TAG = "@brindle_sidebar"
LAUNCHED_TAG = "@brindle_launched"      # on an agent's pane: the launch that finished in it (mark_launched)
PANE_TAGS = (AGENT_TAG, SIDEBAR_TAG)


class PaneSnapshot(dict):
    """list_panes' result: pane (and window) id -> alive, plus ``tags``,
    pane id -> {tag: value} for every live-or-dead pane that carries one of
    PANE_TAGS, and ``server``, the tmux server it was taken from (see
    current_server): it says nothing about panes on any other server.
    ``exits``: dead pane id -> (exit status, signal), each None when tmux
    doesn't give it: a status for a program that exited, a signal (its
    number; see _signal_number) for one a signal killed. ``activity``: pane
    id -> its window's last output (#{window_activity}, epoch seconds), for
    a reader that skips a screen nothing has printed to since its last look;
    a pane missing from it is unknown."""

    tags: dict[str, dict[str, str]]
    server: str | None
    exits: dict[str, tuple[int | None, int | str | None]]
    activity: dict[str, float]

    def __init__(self, server: str | None = None) -> None:
        super().__init__()
        self.tags = {}
        self.server = server
        self.exits = {}
        self.activity = {}


def _signal_number(text: str) -> int | str | None:
    """#{pane_dead_signal} as a signal number. tmux names the signal
    (sig2name: sys_signame's "term" on macOS and the BSDs) or, where the
    platform has no names, gives its number. A name Python doesn't know is
    kept as tmux gave it; None when the program wasn't killed by a signal."""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    name = text.upper()
    try:
        return signal.Signals[name if name.startswith("SIG") else f"SIG{name}"].value
    except KeyError:
        return text


def list_panes(server: object = _THIS_SERVER) -> PaneSnapshot:
    """Every pane's liveness across every session on this server (or on
    ``server``, another one's name, or None for the default server), in one
    call, keyed by pane id (and also by window id, for agents whose stored
    ``tmux_window`` predates pane-id tracking -- a window counts as alive if
    any of its panes are), with each pane's brindle tags (see PANE_TAGS). A
    snapshot with several agents can share this instead of one
    ``display-message`` per agent. Empty (not an error) when there is no
    server running, or tmux isn't installed at all."""
    result = PaneSnapshot(current_server() if server is _THIS_SERVER else server)  # type: ignore[arg-type]
    fmt = "\t".join(["#{window_id}", "#{pane_id}", "#{pane_dead}", "#{pane_dead_status}",
                     "#{pane_dead_signal}", *(f"#{{{k}}}" for k in PANE_TAGS), "#{window_activity}"])
    try:
        proc = _tmux("list-panes", "-a", "-F", fmt, check=False, server=server)
    except TmuxTimeout:
        raise  # a hung server says nothing about its panes: never "all dead"
    except TmuxError:
        return result
    if proc.returncode != 0:
        return result
    for line in proc.stdout.splitlines():
        window_id, pane_id, dead, status, sig, *values = line.split("\t")
        alive = dead.strip() == "0"
        result[pane_id] = alive
        result[window_id] = result.get(window_id, False) or alive
        if dead.strip() == "1":
            result.exits[pane_id] = (int(status) if status.strip().isdigit() else None,
                                     _signal_number(sig))
        tags = {k: v for k, v in zip(PANE_TAGS, values) if v}
        if tags:
            result.tags[pane_id] = tags
        if len(values) > len(PANE_TAGS) and values[len(PANE_TAGS)].strip().isdigit():
            result.activity[pane_id] = float(values[len(PANE_TAGS)])
    return result


def agent_window_size() -> tuple[int, int] | None:
    """The largest window holding an agent's pane on this server, (columns,
    rows): the size the app or a person's terminal last showed one at, as
    tmux keeps a window at its last client's size. None without one."""
    fmt = "\t".join(["#{window_width}", "#{window_height}", f"#{{{AGENT_TAG}}}"])
    sizes = []
    for line in _tmux("list-panes", "-a", "-F", fmt, check=False).stdout.splitlines():
        width, height, tag = (line.split("\t") + ["", ""])[:3]
        if tag and width.isdigit() and height.isdigit():
            sizes.append((int(width), int(height)))
    return max(sizes, key=lambda s: s[0] * s[1], default=None)


def window_pids(target: str, server: object = _THIS_SERVER) -> list[int]:
    """The process ids of the programs in ``target``'s panes."""
    out = _tmux("list-panes", "-t", target, "-F", "#{pane_pid}", check=False, server=server).stdout
    return [int(p) for p in out.split() if p.isdigit()]


def pane_pids() -> dict[str, int]:
    """The process id of the program in every pane on this server, by pane
    id, in one call (empty when no server runs)."""
    out = _tmux("list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}", check=False).stdout
    pids: dict[str, int] = {}
    for line in out.splitlines():
        pane, _, pid = line.partition("\t")
        if pid.isdigit():
            pids[pane] = int(pid)
    return pids


def window_activity(target: str, server: object = _THIS_SERVER) -> float | None:
    """When ``target``'s window last printed anything (epoch seconds), or
    None if tmux can't say."""
    proc = _tmux("display-message", "-p", "-t", target, "#{window_activity}", check=False,
                 server=server)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def kill_window(target: str) -> None:
    _tmux("kill-window", "-t", target, check=False)


def kill_pane(target: str) -> None:
    _tmux("kill-pane", "-t", target, check=False)


def pane_window(pane: str) -> str | None:
    """The id of the window ``pane`` is currently in, or None if it's gone."""
    proc = _tmux("display-message", "-p", "-t", pane, "#{window_id}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def pane_window_tag(pane: str, key: str) -> tuple[str | None, str | None]:
    """Read a pane's window id and user option together in one tmux call."""
    proc = _tmux("display-message", "-p", "-t", pane,
                 f"#{{window_id}}\t#{{{key}}}", check=False)
    if proc.returncode != 0:
        return None, None
    fields = proc.stdout.rstrip("\n").split("\t", 1)
    if len(fields) != 2:
        return fields[0] or None, None
    return fields[0] or None, fields[1] or None


def pane_session(pane: str, server: object = _THIS_SERVER) -> str | None:
    """The name of the session ``pane`` is currently in, or None if it's gone.
    ``server`` as for capture."""
    proc = _tmux("display-message", "-p", "-t", pane, "#{session_name}", check=False,
                 server=server)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def session_attached(session: str) -> bool:
    """Whether any client is attached to ``session`` (someone is looking at
    it). False for a session that doesn't exist."""
    # "=name:" (exact session, its current window): tmux 3.7's display-message
    # prints nothing for a bare "=name", which read as "nobody's attached" and
    # let the sidebar follow any worker session that changed windows.
    proc = _tmux("display-message", "-p", "-t", f"={session}:", "#{session_attached}", check=False)
    if proc.returncode != 0:
        return False
    return proc.stdout.strip() not in ("", "0")


def client_activity(session: str) -> float | None:
    """When a client attached to ``session`` last had input (epoch seconds,
    the latest across its clients), or None with no client attached."""
    proc = _tmux("list-clients", "-t", f"={session}", "-F", "#{client_activity}", check=False)
    if proc.returncode != 0:
        return None
    times = [float(t) for t in proc.stdout.split() if t.isdigit()]
    return max(times) if times else None


def last_input() -> float | None:
    """When anyone last typed in any brindle session: the latest input of any
    client attached to brindle's server, or None with nobody attached."""
    proc = _tmux("list-clients", "-F", "#{client_activity}", check=False)
    if proc.returncode != 0:
        return None
    times = [float(t) for t in proc.stdout.split() if t.isdigit()]
    return max(times) if times else None


def pane_input(pane: str, server: object = _THIS_SERVER) -> float | None:
    """When a client showing ``pane`` (its session's active pane) last had
    input: the person's keys reach tmux before the program in the pane has
    drawn them. Epoch seconds, the latest across those clients; None when no
    client shows it. Raises TmuxError when the server can't be asked.
    ``server`` as for capture."""
    proc = _tmux("list-clients", "-F", "#{pane_id}\t#{client_activity}", check=False, server=server)
    if proc.returncode != 0:
        if _NO_SERVER.search(proc.stderr):
            return None
        raise TmuxError(f"tmux list-clients: {proc.stderr.strip()}")
    rows = (line.split("\t") for line in proc.stdout.splitlines())
    times = [float(row[1]) for row in rows if len(row) == 2 and row[0] == pane and row[1].isdigit()]
    return max(times) if times else None


def current_session() -> str | None:
    """The tmux session this process runs in (from its pane), or None."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    return pane_session(pane)


def active_window(session: str) -> str | None:
    """The id of ``session``'s currently active window, or None if the
    session doesn't exist."""
    target = session if session.startswith("=") else f"={session}:"
    proc = _tmux("display-message", "-p", "-t", target, "#{window_id}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def set_pane_tag(pane: str, key: str, value: str) -> None:
    _tmux("set-option", "-p", "-t", pane, key, value, check=False)


def get_pane_tag(pane: str, key: str) -> str | None:
    """The pane-level user option ``key``, or None if unset or ``pane`` is
    gone. Pane ids (``%N``) are a per-server counter that restarts at 0 after
    a tmux server restart, so an id from a stale DB row can silently mean a
    completely different, unrelated pane; tagging the pane at creation with
    an id of ours (see agents.SIDEBAR_TAG) lets callers tell the two apart."""
    proc = _tmux("show-options", "-p", "-q", "-t", pane, "-v", key, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def tagged_panes(session: str, key: str) -> list[tuple[str, str, bool, str]]:
    """(pane id, window name, alive, value) of each pane of ``session`` that
    sets the pane-level user option ``key``; empty when there is no such session."""
    fmt = f"#{{pane_id}}\t#{{window_name}}\t#{{pane_dead}}\t#{{{key}}}"
    proc = _tmux("list-panes", "-s", "-t", f"={session}", "-F", fmt, check=False)
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        pane, name, dead, tag = (line.split("\t") + ["", "", ""])[:4]
        if tag:
            out.append((pane, name, dead.strip() == "0", tag))
    return out


def agent_pane_in_window(window: str, sidebar: str | None) -> str | None:
    """The pane to anchor the sidebar beside in ``window``: its active pane,
    or (if that's the sidebar itself) any other pane there. None if the
    sidebar is the only pane (never join it onto itself)."""
    proc = _tmux("display-message", "-p", "-t", window, "#{pane_id}", check=False)
    active = proc.stdout.strip() if proc.returncode == 0 else None
    if active and active != sidebar:
        return active
    panes = _tmux("list-panes", "-t", window, "-F", "#{pane_id}", check=False).stdout.split()
    others = [p for p in panes if p != sidebar]
    return others[0] if others else None


def move_pane(pane: str, target: str, columns: int = 30, position: str = "left") -> None:
    """Relocate ``pane`` (e.g. the sidebar) to sit at the left of ``target``'s
    window, keeping focus on whatever's already active there. Re-points the
    window-resize pin (see split_left) at the new window and clears it from
    the old one, so a resize never tries to resize a pane that's moved on.
    If ``pane`` is the only pane left in its window (whatever it sat beside
    has exited), that window closes behind it: the pane itself moves and
    keeps running, and a window holding nothing but the sidebar is no use to
    anyone. Refusing instead would strand the sidebar there, out of sight."""
    spawn.begin_write()     # the move and its hooks run to their end, client or not
    old_window = pane_window(pane)
    geometry, axis = _sidebar_geometry(position, columns)
    _tmux("join-pane", "-d", *geometry, "-s", pane, "-t", target, check=False)
    if old_window and old_window != pane_window(pane):
        _unpin_sidebar(old_window)
    size = min(columns, SIDEBAR_BOTTOM_LINES) if position == "bottom" else columns
    _pin_sidebar(target, pane, axis, size)


def window_zoomed(window: str) -> bool:
    zoomed = _tmux("display-message", "-p", "-t", window, "#{window_zoomed_flag}", check=False)
    return zoomed.stdout.strip() == "1"


def unzoom(window: str) -> None:
    if window_zoomed(window):
        _tmux("resize-pane", "-Z", "-t", window, check=False)


def toggle_sidebar(pane: str) -> None:
    """Hide or show the sidebar ``pane`` without ending it: zoom the chat
    beside it (the sidebar keeps running, unseen), or unzoom. Same as the
    ``prefix S`` binding (see bind_session_keys)."""
    window = pane_window(pane)
    if not window:
        return
    zoomed = _tmux("display-message", "-p", "-t", window, "#{window_zoomed_flag}", check=False)
    if zoomed.stdout.strip() != "1":
        _tmux("select-pane", "-t", f"{window}.+", check=False)  # off the sidebar
    _tmux("resize-pane", "-Z", "-t", window, check=False)


def clipboard_command() -> str | None:
    """The tool that puts stdin on the system clipboard, if there is one."""
    if shutil.which("pbcopy"):
        return "pbcopy"
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return "wl-copy"
    if shutil.which("xclip"):
        return "xclip -selection clipboard -i"
    if shutil.which("wl-copy"):
        return "wl-copy"
    return None


# Whether the key-pressing window holds a sidebar pane. tmux expands formats in
# an if-shell command before running it, so the inner one is escaped (##) to
# reach `list-panes` intact and be read per pane; the window is named by id
# (expanded to the key-pressing window) rather than left to the shell's context.
HAS_SIDEBAR_SHELL = "tmux list-panes -t '#{window_id}' -F '##{@brindle_sidebar}' | grep -q ."


# A brindle chat pane (an agent's, not the sidebar's), for the mouse bindings
# below: ``@brindle`` is the session option bind_session_keys sets, and the
# sidebar's pane carries SIDEBAR_TAG. Evaluated with ``if-shell -F -t =``, so
# it reads the pane under the mouse, not the active one.
CHAT_PANE = "#{&&:#{@brindle},#{!:#{@brindle_sidebar}}}"

# Mirrors tmux 3.x's own root-table mouse bindings: the fallback that keeps
# every non-brindle pane (and the sidebar) on tmux's default behaviour. Each
# hands the event to the program when it tracks the mouse itself
# (``mouse_any_flag``), and selects text in copy mode otherwise.
_DEFAULT_MOUSE = {
    "MouseDown1Pane": "select-pane -t = ; send-keys -M",
    "MouseDrag1Pane": ('if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" '
                       '{ send-keys -M } { copy-mode -M }'),
    "DoubleClick1Pane": ('select-pane -t = ; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" '
                         '{ send-keys -M } { copy-mode -H ; send-keys -X select-word ; '
                         'run-shell -d 0.3 ; send-keys -X copy-pipe-and-cancel }'),
    "TripleClick1Pane": ('select-pane -t = ; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" '
                         '{ send-keys -M } { copy-mode -H ; send-keys -X select-line ; '
                         'run-shell -d 0.3 ; send-keys -X copy-pipe-and-cancel }'),
    # Unbound in tmux, which then passes the release straight to a program
    # that tracks the mouse (and drops it otherwise): the same, spelled out.
    "MouseUp1Pane": "send-keys -M",
}


# Said once a selection is on the clipboard. Selecting is copying in a chat
# pane: the release (or a double or triple click) pipes the text to the
# clipboard tool, so Cmd+C is not needed, and in Apple Terminal it can't do
# anything anyway (it copies only Terminal's own selection, which a pane that
# tracks the mouse never has). The highlight is gone once tmux has copied, so
# without this the copy looked like it had failed.
COPIED_NOTICE = 'display-message -d 1500 "copied to the clipboard"'


def chat_mouse_bindings(clip: str | None) -> dict[str, str]:
    """What the mouse does in a brindle chat pane, by key: tmux keeps it.

    Claude Code (2.1.29x, fullscreen TUI) asks the terminal for mouse
    tracking, and with ``mouse on`` tmux obliges: every click and drag is
    forwarded to it and none reaches tmux. That lost brindle's drag-to-copy
    (the selection never happened), and a click can land Claude Code's
    focus in a widget that then swallows keys: its Settings dialog (what
    /usage and /config open) has a filter box in which Esc only clears the
    text and never closes the dialog, so the "panel" looks frozen. So in a
    chat pane a press just focuses the pane (and the release goes nowhere:
    tmux would otherwise still pass it on), a drag selects in copy mode
    (and MouseDragEnd1Pane copies it to the clipboard), and a double or
    triple click copies the word or line. The wheel isn't touched: a
    fullscreen chat scrolls its own transcript with it. The sidebar and
    any other pane keep tmux's defaults."""
    pipe = f"send-keys -X copy-pipe-and-cancel {shlex.quote(clip)}" if clip else "send-keys -X copy-pipe-and-cancel"
    if clip:
        pipe += f" ; {COPIED_NOTICE}"
    return {
        "MouseDown1Pane": "select-pane -t =",
        "MouseUp1Pane": "",
        "MouseDrag1Pane": "if-shell -F -t = '#{pane_in_mode}' 'send-keys -M' 'copy-mode -M'",
        "DoubleClick1Pane": f"select-pane -t = ; copy-mode -H ; send-keys -X select-word ; run-shell -d 0.3 ; {pipe}",
        "TripleClick1Pane": f"select-pane -t = ; copy-mode -H ; send-keys -X select-line ; run-shell -d 0.3 ; {pipe}",
    }


def bind_session_keys(session: str) -> None:
    """Chat-friendly mouse selection and the sidebar toggle. Key tables are
    server-wide in tmux, so every binding is guarded on the ``@brindle`` session
    option set here and falls back to tmux's own default for any other
    session: only brindle's sessions behave differently."""
    cmds = [["set-option", "-t", session, "@brindle", "1"]]
    clip = clipboard_command()
    for table in ("copy-mode", "copy-mode-vi"):
        default = "send-keys -X copy-pipe-and-cancel"
        ours = f"send-keys -X copy-pipe-and-cancel {shlex.quote(clip)} ; {COPIED_NOTICE}" if clip else default
        cmds.append(["bind-key", "-T", table, "MouseDragEnd1Pane",
                     "if-shell", "-F", "#{@brindle}", ours, default])
        # A drag that ends outside the pane (or any other way tmux misses the
        # release) leaves a chat pane in copy mode, where C-c only cancels the
        # mode: the chat never saw it, so Ctrl+C looked dead. In a chat pane
        # it leaves copy mode and passes the key on, as the person meant it.
        cmds.append(["bind-key", "-T", table, "C-c", "if-shell", "-F", CHAT_PANE,
                     "send-keys -X cancel ; send-keys C-c", "send-keys -X cancel"])
    # The mouse stays tmux's in a chat pane (see chat_mouse_bindings), even
    # when the chat asks to track it.
    for key, ours in chat_mouse_bindings(clip).items():
        cmds.append(["bind-key", "-T", "root", key,
                     "if-shell", "-F", "-t", "=", CHAT_PANE, ours, _DEFAULT_MOUSE[key]])
    # prefix S: hide/show the sidebar (unbound in stock tmux); if this window
    # has none (it lives in another session), pull it here instead.
    from brindle.providers import brindle_invocation

    pull = " ".join(shlex.quote(a) for a in [*brindle_invocation(), "sidebar", "--session", "#{session_name}"])
    pull = f"run-shell -b {shlex.quote(pull + ' >/dev/null 2>&1 || true')}"
    hide = "if-shell -F '#{@brindle_sidebar}' 'select-pane -t :.+'; resize-pane -Z"
    has_sidebar = HAS_SIDEBAR_SHELL
    unzoomed = f"if-shell {shlex.quote(has_sidebar)} {shlex.quote(hide)} {shlex.quote(pull)}"
    toggle = f"if-shell -F '#{{window_zoomed_flag}}' 'resize-pane -Z' {shlex.quote(unzoomed)}"
    cmds.append(["bind-key", "S", "if-shell", "-F", "#{@brindle}", toggle, ""])
    _tmux_batch(cmds)


# The default server's socket name: the one `tmux` without -L uses. It holds
# the person's own sessions and every brindle session started without a
# private socket, so nothing in brindle may stop it or remove its socket.
DEFAULT_SOCKET = "default"


def _ours(server: str | None) -> str:
    """``server`` as a private server brindle may stop, or TmuxError."""
    if not server or server == DEFAULT_SOCKET:
        raise TmuxError("refusing to stop the default tmux server: it holds sessions that "
                        "aren't brindle's to end (only a private -L server, such as a test "
                        "run's, is)")
    return server


def kill_server() -> None:
    """Stop the private tmux server brindle is using (BRINDLE_TMUX_SOCKET: a
    test run's, a demo recording's) and remove its socket. Never the default
    server: TmuxError when no private socket is selected."""
    sock = _ours(current_server())
    _tmux("kill-server", check=False)
    # tmux leaves a killed server's socket file behind; a private server's
    # is ours to tidy.
    remove_socket(sock)


def socket_dir() -> Path:
    """Where tmux keeps its named sockets (``tmux -L <name>``)."""
    return Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"


def abandoned(name: str) -> bool:
    """Whether nothing serves the named server's socket: it is no socket, or
    a connection to it is refused. A failed tmux command is no proof: a
    process that may not connect (a sandbox: "Operation not permitted")
    can't tell, and deleting a live server's socket leaves the server
    running unreachable, while the next tmux call starts a second one."""
    import errno
    import socket
    import stat

    path = socket_dir() / name
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            return True
    except OSError:
        return True
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(str(path))
        except OSError as e:
            return e.errno == errno.ECONNREFUSED
    return False


def remove_socket(name: str) -> None:
    """Remove a private server's socket file (never the default server's)."""
    if not name or name == DEFAULT_SOCKET:
        return
    try:
        (socket_dir() / name).unlink()
    except OSError:
        pass


def other_servers(prefix: str = "brindle-") -> list[str]:
    """Names of the named tmux servers (sockets) starting with ``prefix``,
    other than the one brindle is using, live or not."""
    try:
        names = [p.name for p in socket_dir().iterdir() if p.name.startswith(prefix)]
    except OSError:
        return []
    return sorted(n for n in names if n != os.environ.get("BRINDLE_TMUX_SOCKET"))


def _on(server: str, *args: str) -> subprocess.CompletedProcess:
    return _start(_argv(server), args)


def server_homes(server: str) -> list[str | None] | None:
    """The BRINDLE_HOME of each session on the named ``server`` (None for a
    session without one), or None when no server is listening there. A
    server that is listening but not answering raises TmuxTimeout: it is
    not "nothing listening", and its socket must stay."""
    if not shutil.which(_bin()):
        return None
    proc = _on(server, "list-sessions", "-F", "#{session_name}")
    if proc.returncode != 0:
        return None
    homes: list[str | None] = []
    for name in proc.stdout.split():
        env = _on(server, "show-environment", "-t", f"={name}", "BRINDLE_HOME").stdout.strip()
        home = env.partition("=")[2] if env.startswith("BRINDLE_HOME=") else ""
        homes.append(home or None)
    return homes


def reap_server(server: str) -> None:
    """Stop the named private ``server`` (if it's running) and remove its
    socket. Never the default server (TmuxError)."""
    server = _ours(server)
    if shutil.which(_bin()):
        _on(server, "kill-server")
    remove_socket(server)


def list_sessions() -> list[tuple[str, bool]]:
    """``(name, attached)`` for every session on brindle's server; empty when
    there is no server (a hung one raises TmuxTimeout instead)."""
    try:
        proc = _tmux("list-sessions", "-F", "#{session_name} #{session_attached}", check=False)
    except TmuxTimeout:
        raise
    except TmuxError:
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        name, _, attached = line.rpartition(" ")
        out.append((name, attached.strip() not in ("", "0")))
    return out


def session_pane_ids(session: str) -> list[str]:
    proc = _tmux("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}", check=False)
    return proc.stdout.split() if proc.returncode == 0 else []


@dataclass(frozen=True)
class Place:
    window: str     # its window's id
    session: str
    name: str       # its window's name


def pane_places() -> dict[str, Place]:
    """Where every pane on this server is, by pane id, in one call: empty
    when no server runs. Raises TmuxTimeout when the server doesn't answer."""
    proc = _tmux("list-panes", "-a", "-F", "#{pane_id}\t#{window_id}\t#{session_name}\t#{window_name}",
                 check=False)
    places = {}
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        pane, window, session, name = (line.split("\t", 3) + ["", "", ""])[:4]
        places[pane] = Place(window, session, name)
    return places


def close(panes: Iterable[str] = (), windows: Iterable[str] = (), sessions: Iterable[str] = ()) -> None:
    """Kill ``panes``, then ``windows``, then the sessions named ``sessions``,
    in one tmux command (_tmux_batch: best effort)."""
    _tmux_batch([*(["kill-pane", "-t", p] for p in panes), *(["kill-window", "-t", w] for w in windows),
                 *(["kill-session", "-t", f"={s}"] for s in sessions)])


def session_panes(session: str) -> list[tuple[int, str]]:
    """``(pid, current folder)`` of every pane of ``session``; the folder is
    empty when tmux can't say (the process's folder was deleted, say)."""
    proc = _tmux("list-panes", "-s", "-t", f"={session}", "-F",
                 "#{pane_pid}\t#{pane_current_path}", check=False)
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        pid, _, path = line.partition("\t")
        if pid.isdigit():
            out.append((int(pid), path.strip()))
    return out


@dataclass
class PaneInfo:
    id: str
    pid: int
    path: str            # its current folder; '' when tmux can't say (deleted)
    command: str         # what runs in it now
    dead: bool
    start: str           # the command it was started with
    activity: float      # its window's last output (epoch seconds)

    def idle(self) -> bool:
        """Dead, a bare shell, or a brindle sidebar (see session_idle)."""
        return (self.dead or "watch --sidebar" in self.start
                or self.command.lstrip("-") in SHELLS)


@dataclass
class SessionInfo:
    name: str
    attached: bool
    created: float
    activity: float      # the last input any client gave it (tmux's session_activity)
    home: str | None     # its BRINDLE_HOME, None if it has none
    panes: list[PaneInfo]

    def idle(self) -> bool:
        return all(p.idle() for p in self.panes)


_INFO_FORMAT = "\t".join([
    "#{session_name}", "#{session_attached}", "#{session_created}", "#{session_activity}",
    "#{pane_id}", "#{pane_pid}", "#{pane_dead}", "#{window_activity}", "#{pane_current_command}",
    "#{pane_current_path}", "#{pane_start_command}"])


def session_infos(prefix: str = "brindle_", fresh: bool = False) -> list[SessionInfo]:
    """Every session on brindle's server whose name starts with ``prefix``,
    with its panes, in one listing (plus one environment read each, then
    remembered: ``fresh`` reads them again, for a check just before a session
    is stopped, as another process may have given it to another home). Empty
    with no server; a hung one raises TmuxTimeout."""
    try:
        proc = _tmux("list-panes", "-a", "-F", _INFO_FORMAT, check=False)
    except TmuxTimeout:
        raise
    except TmuxError:
        return []
    if proc.returncode != 0:
        return []
    found: dict[str, SessionInfo] = {}

    def num(text: str) -> float:
        return float(text) if text.strip().isdigit() else 0.0

    for line in proc.stdout.splitlines():
        parts = line.split("\t", 10)
        if len(parts) < 11 or not parts[0].startswith(prefix):
            continue
        name, attached, created, activity, pane, pid, dead, wact, cmd, path, start = parts
        s = found.get(name)
        if s is None:
            s = found[name] = SessionInfo(name, attached.strip() not in ("", "0"), num(created),
                                          num(activity), None, [])
        if pid.isdigit():
            s.panes.append(PaneInfo(pane, int(pid), path.strip(), cmd.strip(), dead == "1",
                                    start, num(wact)))
    server = current_server()
    for s in found.values():
        key = (server, s.name, s.created)
        if key in _SESSION_HOMES and not fresh:
            s.home = _SESSION_HOMES[key]
            continue
        env = _tmux("show-environment", "-t", f"={s.name}", "BRINDLE_HOME", check=False).stdout.strip()
        s.home = (env.partition("=")[2] if env.startswith("BRINDLE_HOME=") else "") or None
        _SESSION_HOMES[key] = s.home
    for key in [k for k in _SESSION_HOMES if k[0] == server and k[1] not in found]:
        del _SESSION_HOMES[key]
    return list(found.values())


# Each session's BRINDLE_HOME as session_infos read it, by (server, name, created):
# set when brindle creates the session, so read once (the cull lists sessions every minute).
_SESSION_HOMES: dict[tuple[str | None, str, float], str | None] = {}


SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu"}


def session_idle(session: str) -> bool:
    """Whether every pane of ``session`` is dead, an idle shell, or a brindle
    sidebar: nothing the person started (a dev server, an editor) runs in it."""
    proc = _tmux("list-panes", "-s", "-t", f"={session}", "-F",
                 "#{pane_dead}\t#{pane_current_command}\t#{pane_start_command}", check=False)
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        dead, _, rest = line.partition("\t")
        current, _, start = rest.partition("\t")
        if dead == "1" or "watch --sidebar" in start:
            continue
        if current.lstrip("-") not in SHELLS:
            return False
    return True


def kill_session(session: str) -> None:
    _tmux("kill-session", "-t", f"={session}", check=False)


def capture(target: str, lines: int = 200, escapes: bool = False,
            server: object = _THIS_SERVER, join: bool = True) -> str:
    """``escapes`` includes SGR colour/attribute codes (capture-pane -e):
    needed to tell styled placeholder text apart from something typed.
    ``join`` glues wrapped lines back together (-J); False keeps one line
    per screen row, for a caller that redraws rows in place.
    ``server``: the tmux server ``target`` is on, when it isn't the one
    brindle is using (an agent's own; see agents.server_of)."""
    flags = (["-J"] if join else []) + (["-e"] if escapes else [])
    return _tmux("capture-pane", "-p", *flags, "-t", target, "-S", f"-{lines}",
                 server=server).stdout


def paste(target: str, text: str, submit: bool = True, lead: str | None = None,
          server: object = _THIS_SERVER, authorize: Callable[[], None] | None = None,
          debounce: Callable[[], None] | None = None,
          progress: Callable[[str], None] | None = None, resume: str | None = None) -> None:
    """Paste ``text`` as one bracketed paste (so newlines don't submit early),
    then press Enter. ``lead``, a single line, is typed before it instead of
    pasted: agent CLIs treat pasted text as untrusted content, and typed
    text as the person's own words, so the lead is what vouches for it.
    ``server`` as for capture. ``authorize`` is checked at each external write,
    after preparation: buffer loading or a preceding command may have blocked
    across an identity change. ``progress`` records completed writes durably;
    ``resume`` skips a lead or body already verified in the same composer."""
    options = {"authorize": authorize} if authorize is not None else {}
    buf = f"brindle-{uuid.uuid4().hex[:8]}"
    try:
        if resume != "body":
            _tmux("load-buffer", "-b", buf, "-", input=text, server=server, **options)
            if lead and resume != "lead":
                _tmux("send-keys", "-t", target, "-l", lead.replace("\n", " ") + " ", server=server, **options)
                if progress is not None:
                    progress("lead")
            _tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", target, server=server, **options)
            if progress is not None:
                progress("body")
    except BaseException:
        # Discard a prepared buffer even when authorization was revoked. This
        # is cleanup only: it must never type into the now-locked pane.
        try:
            _tmux("delete-buffer", "-b", buf, server=server, check=False)
        except (TmuxError, OSError):
            pass  # retain the original failure, so the caller releases its claim
        raise
    if submit and debounce is not None:
        debounce()
    if submit and debounce is None:
        # TUIs debounce paste events; Enter too soon gets folded into the paste.
        time.sleep(0.3)
    if submit:
        _tmux("send-keys", "-t", target, "Enter", server=server, **options)
        if progress is not None:
            progress("submitted")


def send_keys(target: str, *keys: str, server: object = _THIS_SERVER) -> None:
    """``server`` as for capture."""
    _tmux("send-keys", "-t", target, *keys, server=server)


# What tmux says when no server listens on the socket (it is missing, or
# stale): older tmux names the errno. Any other "error connecting to" (a
# socket it may not open) says nothing about the server's windows.
_NO_SERVER = re.compile(r"no server running|error connecting to .*\((No such file or directory|Connection refused)\)")


def tagged_pane_ids(keys: Iterable[str], server: object = _THIS_SERVER) -> list[str]:
    """Every pane on ``server`` (as for capture) that sets one of the pane
    options ``keys``; empty when no server runs there. Raises TmuxError when
    the server can't be asked, so an answer it never gave isn't read as "none"."""
    keys = list(keys)
    fmt = "\t".join(["#{pane_id}", *(f"#{{{k}}}" for k in keys)])
    proc = _tmux("list-panes", "-a", "-F", fmt, check=False, server=server)
    if proc.returncode != 0:
        if _NO_SERVER.search(proc.stderr):
            return []
        raise TmuxError(f"tmux list-panes: {proc.stderr.strip()}")
    rows = (line.split("\t") for line in proc.stdout.splitlines())
    return [pane for pane, *tags in rows if any(tags)]


def server_pid(server: object = _THIS_SERVER) -> int | None:
    """The pid of the tmux server itself (every pane's first process is its
    child), or None when it can't say."""
    try:
        out = _tmux("list-sessions", "-F", "#{pid}", check=False, server=server).stdout.split()
    except TmuxError:
        return None
    return int(out[0]) if out and out[0].isdigit() else None


def pane_windows(server: object = _THIS_SERVER) -> dict[int, str]:
    """Every pane's process id on ``server`` -> its window's id, as tmux has
    them now; empty when no server runs there. Raises TmuxError when the
    server can't be asked, so an answer it never gave isn't read as "none"."""
    proc = _tmux("list-panes", "-a", "-F", "#{pane_pid}\t#{window_id}", check=False, server=server)
    if proc.returncode != 0:
        if _NO_SERVER.search(proc.stderr):
            return {}
        raise TmuxError(f"tmux list-panes: {proc.stderr.strip()}")
    rows = (line.split("\t") for line in proc.stdout.splitlines())
    return {int(pid): window for pid, window in rows if pid.isdigit()}


def window_size_option(target: str, server: object = _THIS_SERVER) -> str | None:
    """The ``window-size`` set on ``target``'s window itself (not one it
    inherits from the global options); None when it sets none. Raises
    TmuxError when tmux can't say. ``server`` as for capture."""
    return _tmux("show-options", "-w", "-v", "-t", target, "window-size", server=server).stdout.strip() or None


def unset_window_size(target: str, server: object = _THIS_SERVER) -> None:
    """Unset the ``window-size`` set on ``target``'s window: it is sized by
    tmux's own rule again. Never the global option. ``server`` as for capture."""
    _tmux("set-option", "-w", "-u", "-t", target, "window-size", server=server)


def attach_command(session: str, window: str | None = None) -> list[str]:
    target = f"={session}" if window is None else window
    return [*_base(), "attach-session", "-t", target]


def inside_this_server() -> bool:
    """Whether this process runs in a pane of the tmux server brindle is
    using. TMUX is set in every pane; a launcher that unset it to get past
    tmux's nesting check (``env -u TMUX brindle``, run in a pane) still
    leaves TMUX_PANE, which this server will recognise. From inside, a
    session is opened with switch_client, never by attaching a second
    client from within one of the server's own panes: a client nested in
    its own server that way sat on the default server for 17 minutes while
    every other tmux command on it hung."""
    if os.environ.get("TMUX"):
        return True
    pane = os.environ.get("TMUX_PANE")
    return bool(pane) and pane_session(pane) is not None


def own_client() -> str | None:
    """The client attached to the session this process's pane is in, or None
    (no TMUX_PANE, or nobody is attached to that session)."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    proc = _tmux("list-clients", "-t", pane, "-F", "#{client_name}", check=False)
    clients = proc.stdout.split() if proc.returncode == 0 else []
    return clients[0] if clients else None


def switch_client(target: str) -> bool:
    """Point the client attached to this process's own session at ``target``
    (a session, ``=name``, or a window or pane id). Returns False, switching
    nothing, when that session has no client: a bare ``switch-client`` would
    pick the most recently active client, i.e. someone else's terminal."""
    client = own_client()
    if client is None:
        return False
    _tmux("switch-client", "-c", client, "-t", target)
    return True


def select_window(target: str) -> None:
    """Focus the window holding ``target`` and, for a pane id, that pane."""
    _tmux("select-window", "-t", target, check=False)
    if target.startswith("%"):
        _tmux("select-pane", "-t", target, check=False)
