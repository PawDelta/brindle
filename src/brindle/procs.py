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


@dataclass
class Proc:
    pid: int
    ppid: int
    text: str    # command line, plus the process's environment
    cmd: str | None = None   # the command line alone (None: a hand-made table's ``text`` is one)


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


def _not_this_users(d: Path) -> bool:
    """Whether the process at ``d`` (``/proc/<pid>``), whose folder this user
    was refused, runs with credentials this user doesn't have: a uid or gid
    (real, effective, saved or filesystem) other than this user's, or
    capabilities beyond this user's. Those are what the kernel refuses it
    for (ptrace_may_access), though ``d`` shows this user as its owner when
    only its effective uid is this user's: a system service like that
    (GitHub's Linux runner has one) is no more this user's than a root
    process, which ``cwds`` leaves out too. False (so: can't be told) when
    its credentials are this user's own, or can't be read."""
    try:
        theirs, mine = _status(d), _status(PROC / "self")
        if ({int(u) for u in theirs["Uid"]} != {os.getuid()}
                or {int(g) for g in theirs["Gid"]} != {os.getgid()}):
            return True
        return int(theirs["CapPrm"][0], 16) & ~int(mine["CapPrm"][0], 16) != 0
    except (OSError, KeyError, IndexError, ValueError):
        return False


def cwds() -> dict[int, str]:
    """The current folder of every process of this user, whatever started it
    (a detached dev server, a shell on any tmux server or terminal). Raises
    OSError when it can't be told: a caller deciding whether something is in
    use must then assume it is: one process of this user it can't read
    (not one that exited during the scan, nor one running with another's
    credentials: see ``_not_this_users``) or an lsof that failed part way."""
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
                except PermissionError:
                    if not _not_this_users(d):
                        raise
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pids: set[int] | list[int], grace: float = 3.0) -> int:
    """SIGTERM, then SIGKILL whatever is left after ``grace`` seconds.
    Returns how many processes were signalled."""
    sent = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            sent.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while sent and time.time() < deadline and any(alive(p) for p in sent):
        time.sleep(0.1)
    for pid in sent:
        if alive(pid):
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
