"""Native caller probes and the shared agent-pane snapshot, using fakes."""

import ctypes
import errno
import struct
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from brindle import procs
from brindle.daemon import peer, process_probe as probe


def test_caller_native_mac_probe_reads_one_pid_and_only_its_environment(monkeypatch):
    seen = []
    failure = [None]
    uid = [501]
    raw = struct.pack("i", 2) + b"/bin/curl\0\0curl\0BRINDLE_AGENT_ID=argument\0HOME=/u\0BRINDLE_AGENT_ID=w1\0"

    def pidinfo(pid, flavor, arg, buf, size):
        seen.append(pid)
        if failure[0]:
            ctypes.set_errno(failure[0])
            return 0
        ctypes.memmove(buf, struct.pack("4I16s8I", pid, 20, 0, 0, b"curl", 0, uid[0], 0, 501, 0, 501, 0, 0), size)
        return size

    def sysctl(mib, count, buf, size, new, new_size):
        if count == 2:
            buf._obj.value = len(raw)
        else:
            seen.append((mib[2], "environment"))
            ctypes.memmove(buf, raw, len(raw))
            size._obj.value = len(raw)
        return 0

    monkeypatch.setattr(probe, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(probe.os, "getuid", lambda: 501)
    monkeypatch.setattr(probe, "_libproc", lambda: SimpleNamespace(proc_pidinfo=pidinfo, sysctl=sysctl))
    caller = probe.process(30, environment=True)
    assert (caller.pid, caller.ppid, caller.text.strip()) == (30, 20, "HOME=/u BRINDLE_AGENT_ID=w1")
    assert probe.process(20).text == ""
    assert seen == [30, (30, "environment"), 20]
    uid[0] = 0
    assert probe.process(30, environment=True) is None
    failure[0] = errno.ESRCH
    assert probe.process(30) is None
    failure[0] = errno.EIO
    with pytest.raises(OSError):
        probe.process(30)


def test_caller_linux_probe_checks_owner_and_reads_one_process(monkeypatch, tmp_path):
    (tmp_path / "30").mkdir()
    (tmp_path / "30/stat").write_text("30 (name with ) spaces) S 20 0")
    (tmp_path / "30/environ").write_bytes(b"HOME=/u\0BRINDLE_AGENT_ID=w1\0")
    monkeypatch.setattr(probe, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(probe, "Path", lambda _: tmp_path)
    assert probe.process(30).ppid == 20
    assert "BRINDLE_AGENT_ID=w1" in probe.process(30, environment=True).text
    assert probe.process(99) is None
    monkeypatch.setattr(probe.os, "getuid", lambda: -1)
    assert probe.process(30) is None


def test_caller_pane_snapshot_proves_ownership_when_expired_but_never_clears(monkeypatch):
    clock, jobs = [100.0], []
    outcomes = [OSError("tmux busy"), {"w2": [30]}]
    def submit(fn):
        job = Future()
        jobs.append(job)
        outcome = outcomes.pop(0)
        job.set_exception(outcome) if isinstance(outcome, Exception) else job.set_result(outcome)
        return job
    monkeypatch.setattr(peer, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(peer, "_pane_cache", (100.0, procs.agent_panes_stamp(), {"w1": [20]}))
    monkeypatch.setattr(peer, "_pane_refresh", None)
    monkeypatch.setattr(peer, "_pane_executor", SimpleNamespace(submit=submit))
    clock[0] += peer.PANE_TTL + 1
    assert peer._pane_owners({20, 1}) == {"w1"} and not jobs
    with pytest.raises(OSError):
        peer._pane_owners({30})     # no fresh snapshot: never cleared
    assert peer._pane_owners({30}) == {"w2"}
    assert peer._pane_owners({31}) == set() and len(jobs) == 2


_CONFIG = '{"mcpServers": {"brindle": {"env": {"BRINDLE_AGENT_ID": "ab1", "BRINDLE_HOME": "/h"}}}}'
_TREE = {p.pid: p for p in [
    procs.Proc(300, 1, f"claude --mcp-config {_CONFIG} HOME=/u BRINDLE_AGENT_ID=ab1"),
    procs.Proc(400, 1, "claude daemon run HOME=/u BRINDLE_AGENT_ID=ab1"),   # started by ab1, detached
    procs.Proc(500, 400, f"claude --mcp-config {_CONFIG} HOME=/u"),   # ab1's session the daemon hosts
    procs.Proc(510, 500, "/bin/zsh -c make HOME=/u"),
    procs.Proc(511, 510, "curl --unix-socket daemon.sock HOME=/u"),
    procs.Proc(600, 400, "claude --bg-spare HOME=/u"),
    procs.Proc(601, 600, "python -m brindle mcp HOME=/u BRINDLE_AGENT_ID=ab2 BRINDLE_HOME=/h"),
    procs.Proc(610, 600, "curl --unix-socket daemon.sock HOME=/u"),
    procs.Proc(700, 1, "-zsh HOME=/u BRINDLE_AGENT_ID=ab3"),
    procs.Proc(710, 700, "curl --unix-socket daemon.sock HOME=/u"),   # dropped the variable
    procs.Proc(800, 400, "claude HOME=/u"),     # a person's session the daemon hosts
    procs.Proc(810, 800, "curl --unix-socket daemon.sock HOME=/u"),
    procs.Proc(720, 700, "claude daemon HOME=/u"),     # exec -a "claude daemon": not detached
    procs.Proc(730, 720, "curl --unix-socket daemon.sock HOME=/u"),
]}


@pytest.mark.parametrize("caller,agent", [
    (511, "ab1"),   # under a daemon-hosted session, outside any pane
    (610, "ab2"),   # under the pre-started spare that owns ab2's MCP server
    pytest.param(710, "ab3", marks=pytest.mark.variant("caller identity must resolve from an ancestor's environment without a pane")),
    (810, None),    # the daemon's environment belongs to whoever started it
    pytest.param(730, "ab3", marks=pytest.mark.variant("a daemon-like command line cannot hide the caller's owning ancestor")),
])
def test_caller_ancestry_finds_agents_without_a_pane(monkeypatch, caller, agent):
    monkeypatch.setenv("BRINDLE_HOME", "/h")
    monkeypatch.setattr(procs, "table", lambda: pytest.fail("caller read the full process table"))
    monkeypatch.setattr(probe, "process", lambda pid, **kw: _TREE.get(pid))
    monkeypatch.setattr(probe, "executable", lambda pid: "/opt/claude/versions/2.1.0")
    monkeypatch.setenv("BRINDLE_CLAUDE_BIN", "/opt/claude/versions/2.0.9")
    monkeypatch.setattr(probe, "children", lambda pid: [p.pid for p in _TREE.values() if p.ppid == pid],
                        raising=False)
    monkeypatch.setattr(peer, "_pane_cache", None)
    monkeypatch.setattr(peer, "_pane_refresh", None)
    def submit(fn):
        job = Future()
        job.set_result({})
        return job
    monkeypatch.setattr(peer, "_pane_executor", SimpleNamespace(submit=submit))
    reason = peer.agent_caller(caller)
    assert (reason and reason.rsplit(" ", 1)[-1]) == agent
