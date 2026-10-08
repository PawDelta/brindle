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
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Proc:
    pid: int
    ppid: int
    text: str    # command line, plus (on macOS) the environment ps appends


def table() -> dict[int, Proc]:
    """Every process of this user."""
    out: dict[int, Proc] = {}
    proc = Path("/proc")
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
            out[int(d.name)] = Proc(int(d.name), ppid, f"{cmd} {env}")
        return out
    # macOS (BSD ps): `e` appends each process's environment to its command.
    listing = subprocess.run(["ps", "xeww", "-o", "pid=,ppid=,command="],
                             capture_output=True, text=True).stdout
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), parts[2])
    return out


def _ancestors(procs: dict[int, Proc]) -> set[int]:
    out, pid = set(), os.getpid()
    while pid in procs and pid not in out:
        out.add(pid)
        pid = procs[pid].ppid
    out.add(os.getpid())
    return out


def _is_daemon(p: Proc) -> bool:
    return bool(re.search(r"\bclaude daemon\b|\bdaemon run\b", p.text.split(" BRINDLE_", 1)[0]))


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


def agent_pids(agent_ids: set[str] | list[str], pane_pids: dict[str, list[int]] | None = None,
               procs: dict[int, Proc] | None = None) -> dict[str, set[int]]:
    """Process ids by agent. ``pane_pids``: agent id -> the pids of its tmux
    panes (taken before the window was closed)."""
    procs = table() if procs is None else procs
    skip = _ancestors(procs)
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
                if parent and "bg-spare" in parent.text.split(" BRINDLE_", 1)[0]:
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


def running_commands(agent_id: str, pane_pids: list[int] | None = None,
                     procs: dict[int, Proc] | None = None) -> list[Proc]:
    """Processes of this agent that look like a command it started (a test
    run, a build, a sleep), not its CLI, its MCP servers, hooks or a bare shell."""
    procs = table() if procs is None else procs
    pids = agent_pids([agent_id], {agent_id: pane_pids or []}, procs).get(agent_id, set())
    out = []
    for pid in pids:
        p = procs.get(pid)
        if p is None:
            continue
        cmd = p.text.split(" BRINDLE_", 1)[0].strip()
        first = os.path.basename(cmd.split(None, 1)[0]) if cmd else ""
        if first.lstrip("-") in _SHELLS or _PLUMBING.search(cmd):
            continue
        out.append(p)
    return out


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
