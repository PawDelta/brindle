"""Native caller probes and the shared agent-pane snapshot, using fakes."""

import ctypes
import errno
import struct
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

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


def test_caller_pane_cache_shares_background_refresh_and_retains_agent_roots(monkeypatch):
    clock, jobs = [100.0], []
    def submit(fn):
        job = Future()
        jobs.append(job)
        if len(jobs) == 1:
            job.set_result({"w1": [20]})
        return job
    monkeypatch.setattr(peer, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(peer, "_pane_cache", None)
    monkeypatch.setattr(peer, "_pane_refresh", None)
    monkeypatch.setattr(peer, "_pane_executor", SimpleNamespace(submit=submit))
    assert peer._cached_agent_panes() == {"w1": [20]}
    clock[0] += peer.PANE_TTL + 1
    for _ in range(3):
        assert peer._cached_agent_panes() == {"w1": [20]}
    assert len(jobs) == 2
    jobs[-1].set_exception(OSError("unavailable"))
    assert peer._cached_agent_panes() == {"w1": [20]}
    jobs[-1].set_result({"w2": [40]})
    assert peer._cached_agent_panes() == {"w2": [40]}
    assert peer._cached_agent_panes() == {"w2": [40]} and len(jobs) == 3
