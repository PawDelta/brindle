"""An agent's processes, wherever they run.

Closing an agent's tmux window isn't enough to stop it. Claude Code can host
the real session under its own background daemon (the pane holds only a
client), and the daemon pre-starts spare processes with the same arguments,
so the agent, its brindle MCP server and anything it started (a test run, a
dev server) can outlive the window.

The environment can't tell whose a process is: the daemon inherits the
environment of whichever agent first started it, and passes it to every
session it hosts, other agents' and the person's own. So an agent's
processes are found from what brindle sets explicitly:

- its tmux pane,
- any process whose command line carries the MCP config brindle launched it
  with (``"BRINDLE_AGENT_ID": "<id>"`` for Claude Code, ``BRINDLE_AGENT_ID =
  "<id>"`` for Codex): the pane's client and the daemon-hosted session alike,
- its brindle MCP server (``brindle mcp``, whose environment brindle sets itself),
  and the pre-started spare that owns it, if that's what it hangs off,

plus everything those started. Claude Code's daemon itself, this process and
its ancestors are never included.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from brindle import spawn

# Seconds ``ps`` may take, waiting for room included (see brindle.spawn).
PS_LIMIT = 30.0
PROC = Path("/proc")   # Linux's process table; absent on macOS
log = logging.getLogger(__name__)


@dataclass
class Proc:
    pid: int
    ppid: int
    text: str    # command line, plus the process's environment
    cmd: str | None = None   # the command line alone (None: a hand-made table's ``text`` is one)
    birth: str | None = None   # when it was started (lineage); None in a plain table


def command_line(p: Proc) -> str:
    """``p``'s command line, without its environment: what it runs, not
    what it inherited (TMUX=, a PWD, whatever its parent had)."""
    return p.text.split(" BRINDLE_", 1)[0].strip() if p.cmd is None else p.cmd.strip()


def _ps(flags: str) -> dict[int, tuple[int, str]]:
    listing = spawn.read(["ps", flags, "-o", "pid=,ppid=,command="], timeout=PS_LIMIT).stdout
    out = {}
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), parts[2])
    return out


def table() -> dict[int, Proc]:
    """Every process of this user."""
    out: dict[int, Proc] = {}
    proc = PROC
    if (proc / "self" / "stat").exists():
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                stat = (d / "stat").read_text()
                cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                env = (d / "environ").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            out[int(d.name)] = Proc(int(d.name), ppid, f"{cmd} {env}", cmd)
        return out
    # macOS (BSD ps): `e` appends each process's environment to its command,
    # with nothing to tell where one ends, so the command line is read apart.
    # A process that started between the two has no command line: it counts
    # as a command (see is_command), whatever its environment says.
    commands = _ps("xww")
    for pid, (ppid, text) in _ps("xeww").items():
        out[pid] = Proc(pid, ppid, text, commands.get(pid, (ppid, ""))[1])
    return out


def _ancestors(procs: dict[int, Proc]) -> set[int]:
    out, pid = set(), os.getpid()
    while pid in procs and pid not in out:
        out.add(pid)
        pid = procs[pid].ppid
    out.add(os.getpid())
    return out


def _is_daemon(p: Proc) -> bool:
    return bool(re.search(r"\bclaude daemon\b|\bdaemon run\b", command_line(p)))


def _names_agent(p: Proc, agent_id: str) -> bool:
    return (f'"BRINDLE_AGENT_ID": "{agent_id}"' in p.text
            or f'BRINDLE_AGENT_ID = \\"{agent_id}\\"' in p.text
            or f'BRINDLE_AGENT_ID = "{agent_id}"' in p.text)


def _is_brindle_mcp_for(p: Proc, agent_id: str) -> bool:
    return (re.search(r"-m brindle mcp\b|\bbrindle mcp\b", p.text) is not None
            and f"BRINDLE_AGENT_ID={agent_id}" in p.text)


def _home_matches(p: Proc, from_config: bool = False) -> bool:
    """Only this brindle's agents: a test run (or a second install) uses its own
    BRINDLE_HOME. ``from_config``: read it from the MCP config on the command
    line (a CLI brindle launched), not the environment (brindle's MCP server)."""
    if from_config:
        m = (re.search(r'"BRINDLE_HOME": "([^"]*)"', p.text)
             or re.search(r'BRINDLE_HOME = \\?"([^"\\]*)', p.text))
    else:
        m = re.search(r"(?:^|\s)BRINDLE_HOME=(\S+)", p.text)
    return (m.group(1) if m else None) == os.environ.get("BRINDLE_HOME")


def root_agent(p: Proc) -> str | None:
    """The agent ``p`` is a root process of, read from ``p`` alone (its text
    holds its command line and environment): a CLI launched with brindle's
    MCP config, or a brindle MCP server. :func:`agent_pids` finds the same
    roots by scanning every process."""
    for aid in re.findall(r'BRINDLE_AGENT_ID(?:": | = \\?)"([0-9a-f]+)', p.text):
        if _names_agent(p, aid) and _home_matches(p, from_config=True):
            return aid
    for aid in re.findall(r"(?:^|\s)BRINDLE_AGENT_ID=([0-9a-f]+)", p.text):
        if _is_brindle_mcp_for(p, aid) and _home_matches(p):
            return aid
    return None


def is_spare(p: Proc) -> bool:
    """A pre-started spare session: a root when its brindle MCP server is."""
    return "bg-spare" in command_line(p)


def is_daemon(p: Proc) -> bool:
    """Claude Code's daemon: what it hosts isn't the agent's that started it."""
    return _is_daemon(p)


def _panes_stamp() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / "agent-panes.stamp"


def agent_panes_changed() -> None:
    """Note that an agent got a new tmux pane, so every process's cached
    snapshot of agent panes (brindle.daemon.peer) stops clearing callers."""
    path = _panes_stamp()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = os.urandom(16).hex()
    tmp = path.with_name(f"{path.name}.{token}")
    tmp.write_text(token)
    os.replace(tmp, path)


def agent_panes_stamp() -> str:
    """Changes whenever :func:`agent_panes_changed` is called, in any process."""
    try:
        return _panes_stamp().read_text()
    except OSError:
        return ""


def agent_pids(agent_ids: set[str] | list[str], pane_pids: dict[str, list[int]] | None = None,
               procs: dict[int, Proc] | None = None, through_ancestors: bool = False) -> dict[str, set[int]]:
    """Process ids by agent. ``pane_pids``: agent id -> the pids of its tmux
    panes (taken before the window was closed). The caller's own ancestors are
    left out, so stopping an agent never signals the process doing it;
    ``through_ancestors`` (read-only discovery) walks through them instead, to
    find commands beside a hook running under the agent's CLI."""
    procs = table() if procs is None else procs
    skip = {os.getpid()} if through_ancestors else _ancestors(procs)
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)

    out: dict[str, set[int]] = {}
    for aid in set(agent_ids):
        roots = set()
        roots.update((pane_pids or {}).get(aid, []))
        for p in procs.values():
            if _names_agent(p, aid) and _home_matches(p, from_config=True):
                roots.add(p.pid)
            elif _is_brindle_mcp_for(p, aid) and _home_matches(p):
                roots.add(p.pid)
                parent = procs.get(p.ppid)
                if parent and is_spare(parent):
                    roots.add(parent.pid)   # a pre-started spare made for this agent
        found, queue = set(), [r for r in roots if r in procs]
        while queue:
            pid = queue.pop()
            if pid in found or pid in skip or _is_daemon(procs[pid]):
                continue
            found.add(pid)
            queue.extend(children.get(pid, []))
        if found:
            out[aid] = found
    return out


_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh"}
_PLUMBING = re.compile(r"claude|brindle|\bmcp\b|tmux|node_modules|\bnpm exec\b|caffeinate")
_CLI_NAMES = {"codex", "agy", "antigravity", "opencode"}
_LAUNCHERS = {"node", "python", "python3", "npx"}


def _is_cli(cmd: str) -> bool:
    """Whether the command is a provider CLI itself: its executable (or the
    script a launcher runs) is named for it, not just mentioned in arguments."""
    words = cmd.split()
    for word in words[:1] + (words[1:2] if words and os.path.basename(words[0]) in _LAUNCHERS else []):
        if os.path.splitext(os.path.basename(word))[0] in _CLI_NAMES:
            return True
    return False


def descendants(roots: list[int], procs: dict[int, Proc] | None = None) -> set[int]:
    """``roots`` and every process they started, whatever they run: for a
    pane nobody owns any more, whose CLI carries no agent id to find it by."""
    procs = table() if procs is None else procs
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    found: set[int] = set()
    queue = list(roots)
    while queue:
        pid = queue.pop()
        if pid in found:
            continue
        found.add(pid)
        queue.extend(children.get(pid, []))
    return found - _ancestors(procs)


def running_commands(agent_id: str, pane_pids: list[int] | None = None,
                     procs: dict[int, Proc] | None = None) -> list[Proc]:
    """Processes of this agent that look like a command it started (a test
    run, a build, a sleep), not its CLI, its MCP servers, hooks or a bare shell."""
    procs = table() if procs is None else procs
    pids = agent_pids([agent_id], {agent_id: pane_pids or []}, procs,
                      through_ancestors=True).get(agent_id, set())
    return [procs[pid] for pid in pids if pid in procs and is_command(procs[pid])]


def is_command(p: Proc, inside: str | None = None) -> bool:
    """Whether ``p`` looks like a command (a test run, a build, a dev server),
    not a provider CLI, its MCP servers, hooks or a bare shell. Only its
    command line counts, never its environment. ``inside``: a folder (a
    worktree) whose paths count by their last part alone, so a
    ``node_modules`` or a "brindle" in the folder's own path doesn't make
    what runs from it plumbing."""
    cmd = command_line(p)
    for folder in sorted({inside, os.path.realpath(inside)}, key=len, reverse=True) if inside else ():
        cmd = re.sub(re.escape(folder.rstrip(os.sep)) + r"(?:/(\S*))?",
                     lambda m: os.path.basename(m.group(1) or "") or ".", cmd)
    first = os.path.basename(cmd.split(None, 1)[0]) if cmd else ""
    return not (first.lstrip("-") in _SHELLS or _PLUMBING.search(cmd) or _is_cli(cmd))


def all_agent_ids(procs: dict[int, Proc] | None = None) -> set[str]:
    """Every agent id some process names in its command line or brindle MCP server."""
    procs = table() if procs is None else procs
    ids = set()
    for p in procs.values():
        if _home_matches(p, from_config=True):
            ids.update(re.findall(r'"BRINDLE_AGENT_ID": "([0-9a-f]+)"', p.text))
        if re.search(r"-m brindle mcp\b|\bbrindle mcp\b", p.text) and _home_matches(p):
            ids.update(re.findall(r"(?:^|\s)BRINDLE_AGENT_ID=([0-9a-f]+)", p.text))
    return ids


def _status(d: Path) -> dict[str, list[str]]:
    """``d/status`` (``/proc/<pid>/status``, readable by anyone) by field."""
    out = {}
    for line in (d / "status").read_text().splitlines():
        key, _, value = line.partition(":")
        out[key] = value.split()
    return out


# Processes of this user whose folder the kernel may refuse to show this user
# (systemd's user manager holds CAP_WAKE_ALARM, say) but which never live in a
# project folder, so a refusal of theirs keeps no worktree. By Name (as
# /proc/<pid>/status gives it), with why each qualifies.
NEVER_IN_A_PROJECT = {
    # The user's service manager (``systemd --user``): PID 1 starts it in /
    # for each logged-in user and it stays there; the services it starts are
    # processes of their own. It runs on nearly every Linux desktop and server.
    "systemd": "the user's service manager",
    # Both chdir to / when they daemonize, so they never hold a folder.
    "ssh-agent": "the SSH agent",
    "gpg-agent": "the GnuPG agent",
}


class Refused(OSError):
    """A process of this user whose folder can't be read: it could be in any
    worktree. ``name`` (its Name, or ``?``) and ``pid`` say which, so a
    refusal can name it."""

    def __init__(self, pid: int, name: str):
        super().__init__(f"the folder of process {name} (pid {pid}) can't be read")
        self.pid, self.name = pid, name


def _is_known_manager(name: str, status: dict[str, list[str]], argv0: str) -> bool:
    """Whether the process is one of NEVER_IN_A_PROJECT: systemd only as the
    user's manager (started by PID 1, argv[0] ``systemd``), not anything
    else that calls itself that."""
    if name not in NEVER_IN_A_PROJECT:
        return False
    return name != "systemd" or (status.get("PPid") == ["1"] and argv0 == "systemd")


def _argv0(d: Path) -> str:
    """The basename of argv[0] of the process at ``d``, or ``?``."""
    try:
        argv0 = (d / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
    except OSError:
        return "?"
    return os.path.basename(argv0) or "?"


def _excused(d: Path, ignore: frozenset[str] = frozenset()) -> bool:
    """Whether the process at ``d`` (``/proc/<pid>``), whose folder this user
    was refused, can be left out of ``cwds``:

    - it runs with another's ids: a uid or gid (real, effective, saved or
      filesystem) other than this user's. That is one thing the kernel
      refuses it for (ptrace_may_access), though ``d`` shows this user as its
      owner when only its effective uid is this user's: a system service like
      that is no more this user's than a root process, which ``cwds`` leaves
      out too;
    - it has this user's ids but is a known process that never lives in a
      project folder (NEVER_IN_A_PROJECT), or one the person excused by name
      (``ignore``: a repo's ``live_check_ignore``, matched against its Name
      or the basename of its argv[0]).

    Anything else refused (capabilities, say, non-dumpable, or a security
    module refused it), or whose ids can't be read, can't be told: False.
    Except for a known or excused process, it logs one warning saying what
    the process is (``_describe``)."""
    try:
        status = _status(d)
        uids, gids = [int(u) for u in status["Uid"]], [int(g) for g in status["Gid"]]
    except (OSError, KeyError, ValueError) as e:
        log.warning("brindle: a process's folder can't be read and nor can its ids (%s), so it counts "
                    "as in use: %s", type(e).__name__, _describe(d, {}))
        return False
    slots = ("real", "effective", "saved", "filesystem")
    differ = [f"{kind} {slot} {n}" for kind, ids, mine in (("uid", uids, os.getuid()), ("gid", gids, os.getgid()))
              for slot, n in zip(slots, ids) if n != mine]
    if differ:
        log.warning("brindle: a process's folder can't be read, but it runs with another's ids (%s), so it "
                    "isn't this user's: %s", ", ".join(differ), _describe(d, status))
        return True
    name, argv0 = " ".join(status.get("Name", [])), _argv0(d)
    if _is_known_manager(name, status, argv0):
        log.debug("brindle: %s (pid %s) has a folder that can't be read, but never runs in a project",
                  NEVER_IN_A_PROJECT[name], d.name)
        return True
    if name in ignore or argv0 in ignore:
        log.debug("brindle: %s (pid %s) has a folder that can't be read; live_check_ignore excuses it",
                  name, d.name)
        return True
    log.warning("brindle: a process with this user's ids has a folder that can't be read, so it counts "
                "as in use: %s", _describe(d, status))
    return False


def _name(d: Path) -> str:
    """The Name of the process at ``d``, or ``?``."""
    try:
        return " ".join(_status(d).get("Name", [])) or "?"
    except OSError:
        return "?"


def _describe(d: Path, status: dict[str, list[str]]) -> str:
    """What the process at ``d`` is, for a log: its pid, Name, State, parent
    (pid and Name), ids, CapEff, NoNewPrivs, Seccomp, start (seconds since
    boot) and the basename of argv[0]. Nothing else of its command line and
    never its environment. Whatever can't be read shows as ``?``."""
    def field(st: dict[str, list[str]], key: str) -> str:
        return " ".join(st.get(key, [])) or "?"

    ppid, start = field(status, "PPid"), "?"
    try:
        after = (d / "stat").read_text().rsplit(")", 1)[1].split()   # fields 3 on
        ppid = after[1] if ppid == "?" else ppid
        start = f"{int(after[19]) / os.sysconf('SC_CLK_TCK'):.2f}s"   # field 22, in clock ticks
    except (OSError, IndexError, ValueError):
        pass
    try:
        parent = field(_status(PROC / ppid), "Name") if ppid.isdigit() else "?"
    except OSError:
        parent = "?"
    argv0 = _argv0(d)
    return (f"pid {d.name}, Name {field(status, 'Name')}, State {field(status, 'State')}, PPid {ppid} "
            f"({parent}), Uid {field(status, 'Uid')}, Gid {field(status, 'Gid')}, "
            f"CapEff {field(status, 'CapEff')}, NoNewPrivs {field(status, 'NoNewPrivs')}, "
            f"Seccomp {field(status, 'Seccomp')}, started {start} after boot, argv[0] {argv0}")


def cwds(ignore: frozenset[str] | set[str] | list[str] = frozenset()) -> dict[int, str]:
    """The current folder of every process of this user, whatever started it
    (a detached dev server, a shell on any tmux server or terminal). Raises
    OSError when it can't be told: a caller deciding whether something is in
    use must then assume it is: one process of this user it can't read
    (``Refused``; not one that exited during the scan, one running with
    another's ids, a known service manager or agent, nor one named in
    ``ignore``: see ``_excused``) or an lsof that failed part way."""
    ignore = frozenset(ignore)
    proc = PROC
    if (proc / "self" / "cwd").exists():
        out: dict[int, str] = {}
        for d in proc.iterdir():
            if d.name.isdigit():
                try:
                    if d.stat().st_uid == os.getuid():
                        out[int(d.name)] = os.readlink(d / "cwd")
                except (FileNotFoundError, ProcessLookupError):
                    continue   # it exited meanwhile (or is a zombie): no folder to hold
                except PermissionError as e:
                    if not _excused(d, ignore):
                        raise Refused(int(d.name), _name(d)) from e
        return out
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        listing = spawn.read([lsof, "-w", "-n", "-P", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"],
                             timeout=PS_LIMIT)
    except (spawn.Busy, subprocess.SubprocessError) as e:
        raise OSError(f"lsof: {e}") from e
    if listing.returncode != 0:   # some process it couldn't look at: the rest isn't all
        raise OSError(f"lsof failed ({listing.stderr.strip() or listing.returncode})")
    out, listed = {}, []
    for line in listing.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            listed.append(int(line[1:]))
        elif line.startswith("n") and listed:
            out[listed[-1]] = line[1:]
    if os.getpid() not in out or set(listed) - set(out):   # this process has a folder too
        raise OSError(f"lsof left out a process's folder ({listing.stderr.strip() or 'no error'})")
    return out


def alive(pid: int) -> bool:
    """Whether ``pid`` is a running process. On Linux a process that has
    exited but isn't reaped yet (a zombie, e.g. an orphan waiting on its
    new parent) still answers kill(0); it runs nothing, so it isn't alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = (PROC / str(pid) / "stat").read_text()
    except OSError:   # no /proc (macOS), or it exited just now
        return True
    return stat.rpartition(")")[2].split()[:1] != ["Z"]


def terminate(pids: set[int] | list[int], grace: float = 3.0, owned: dict[int, str] | None = None) -> int:
    """SIGTERM, then SIGKILL whatever is left after ``grace`` seconds.
    Returns how many processes were signalled. ``owned`` (pid -> birth, from
    lineage): each signal goes only to a pid that is still the process its
    birth names, read for that pid alone just before its own signal is sent
    (birth_of), so a pid that came round again meanwhile (another's process,
    perhaps after an earlier signal of this batch ended the one it was) is
    spared."""
    def still(pid: int) -> bool:
        return owned is None or birth_of(pid) == owned.get(pid)

    sent = []
    for pid in pids:
        if not still(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            sent.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while sent and time.time() < deadline and any(alive(p) for p in sent):
        time.sleep(0.1)
    for pid in sent:
        if alive(pid) and still(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return len(sent)


def stop(agent_ids: set[str] | list[str], pane_pids: dict[str, list[int]] | None = None,
         grace: float = 3.0) -> int:
    """Stop every process belonging to these agents. Returns how many."""
    found = agent_pids(agent_ids, pane_pids)
    return terminate({p for pids in found.values() for p in pids}, grace)


# -- panes closed with their processes left for later --------------------------------
#
# Closing a pane ends the CLI in it, and what that CLI started in a session of
# its own (a Bash tool's shell, a background job) is left with launchd as its
# parent: no agent's process tree reaches it any more, and its command line
# names no agent. A close that doesn't stop the processes first (a pause that
# leaves them to a cull, a batch of pauses on the daemon's way out, stale panes
# settled) notes here what ran under the panes, and the cull ends whatever of
# it was left that way (handed_orphans).

HANDED = "closing-procs.json"
HANDED_MAX_AGE = 24 * 3600.0     # a record this old is dropped: its pid may be anyone's by now


def _handed_path() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / HANDED


def birth_of(pid: int) -> str | None:
    """The birth of the process ``pid`` is now (as lineage gives it), read
    for it alone; None when no process has that pid."""
    if (PROC / "self" / "stat").exists():
        try:
            boot = (PROC / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
        except OSError:
            boot = "?"
        try:
            after = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None
        return f"{boot}:{after[19]}" if len(after) > 19 else None
    started = spawn.read(["ps", "-o", "lstart=", "-p", str(int(pid))], timeout=PS_LIMIT).stdout.split()
    return " ".join(started) if len(started) == 5 else None


def lineage() -> dict[int, Proc]:
    """Every process with its parent, its command line and its birth (when
    the kernel started it: with the pid, who a process is), each process's
    three read together, so a parent and a command are never another
    process's that took the pid between two reads. A pid that comes round
    again has a later birth; a process that execs another program keeps its
    own. Linux: the start time in clock ticks since boot, with the boot's id,
    from one read of its stat (its command line is kept only if a second read
    finds the same birth); macOS: one ``ps`` listing, its start time to the
    second, a wall-clock date."""
    out: dict[int, Proc] = {}
    if (PROC / "self" / "stat").exists():
        try:
            boot = (PROC / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
        except OSError:
            boot = "?"
        for d in PROC.iterdir():
            if not d.name.isdigit():
                continue
            try:
                first = (d / "stat").read_text().rsplit(")", 1)[1].split()
                cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
                again = (d / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if len(first) > 19 and len(again) > 19 and first[19] == again[19]:   # field 22: starttime
                out[int(d.name)] = Proc(int(d.name), int(first[1]), cmd, cmd, f"{boot}:{first[19]}")
        return out
    listing = spawn.read(["ps", "-axww", "-o", "pid=,ppid=,lstart=,command="], timeout=PS_LIMIT).stdout
    for line in listing.splitlines():
        parts = line.split(None, 7)     # pid, ppid, then lstart's five words, then the command
        if len(parts) >= 7 and parts[0].isdigit() and parts[1].isdigit():
            cmd = parts[7] if len(parts) == 8 else ""
            out[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), cmd, cmd, " ".join(parts[2:7]))
    return out


def _tree(roots: list[int], procs: dict[int, Proc]) -> set[int]:
    """``roots`` and what they started, as :func:`descendants`, but never
    Claude Code's daemon or anything under it (the sessions it hosts are
    other agents' too), nor this process or its ancestors."""
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    skip, found, queue = _ancestors(procs), set(), [r for r in roots if r in procs]
    while queue:
        pid = queue.pop()
        if pid in found or pid in skip or _is_daemon(procs[pid]):
            continue
        found.add(pid)
        queue.extend(children.get(pid, []))
    return found


def _update_handed(change) -> object:
    """Apply ``change`` to the records ({pid: [birth, noted at]}) under
    an exclusive lock, written to a temporary file of its own and moved into
    place; returns what ``change`` returned."""
    import fcntl
    import json

    path = _handed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            records = {int(k): v for k, v in json.loads(path.read_text()).items()}
        except (OSError, ValueError, AttributeError):
            records = {}
        result = change(records)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}")
        try:
            tmp.write_text(json.dumps({str(k): v for k, v in records.items()}))
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    return result


def hand_off(roots: list[int], now: float | None = None) -> int:
    """Note ``roots`` (the programs of panes about to close) and everything
    under them, for the cull to end what the close leaves behind. Returns how
    many processes were noted."""
    if not roots:
        return 0
    seen = lineage()     # parent and birth from the same read: the tree is the noted processes'
    now = time.time() if now is None else now
    found = {pid: [seen[pid].birth, now] for pid in _tree(roots, seen) if seen[pid].birth}

    def add(records: dict) -> None:
        records.update(found)

    if found:
        _update_handed(add)
    return len(found)


def handed_orphans(now: float | None = None) -> dict[int, str]:
    """What closed panes left running (hand_off), to be ended at once
    (terminate with ``owned``), by pid with its birth: each noted process that is
    still the one noted (its birth) and whose noted parents lead to launchd
    (they lost the CLI that started them), with whatever it has started
    since (never Claude Code's daemon or what it hosts). Read as things are
    now, in one lineage read. What is returned is noted too, until a later
    look finds it gone; records of processes gone, or grown too old, are
    dropped."""
    if not _handed_path().exists():
        return {}
    now = time.time() if now is None else now

    def take(records: dict) -> dict[int, str]:
        current = lineage()
        for pid in [pid for pid, (birth, at) in records.items()
                    if pid not in current or current[pid].birth != birth or now - at > HANDED_MAX_AGE]:
            del records[pid]

        def orphaned(pid: int) -> bool:
            seen = set()
            while pid in records and pid not in seen:
                seen.add(pid)
                pid = current[pid].ppid
            return pid == 1

        out = {pid: current[pid].birth for pid in _tree([pid for pid in records if orphaned(pid)], current)
               if current[pid].birth}
        for pid, birth in out.items():
            records.setdefault(pid, [birth, now])   # started since the note: noted from here on
        return out

    return _update_handed(take)  # type: ignore[return-value]
