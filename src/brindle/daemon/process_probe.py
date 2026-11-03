"""Per-PID process probes for daemon caller authentication."""

from __future__ import annotations

import ctypes
import errno
import os
import struct
import sys
from functools import cache
from pathlib import Path

from brindle.procs import Proc


@cache
def _libproc():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                          ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                          ctypes.c_void_p, ctypes.c_size_t]
    lib.sysctl.restype = ctypes.c_int
    return lib


def _procargs(pid: int) -> tuple[bytes, bytes]:
    """This process's arguments and environment, from macOS KERN_PROCARGS2."""
    lib = _libproc()
    mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
    argmax = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    if lib.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "process probe failed")
    buf = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(len(buf))
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    if lib.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "process probe failed")
    raw = buf.raw[:size.value]
    argc = struct.unpack_from("i", raw)[0]
    offset = raw.index(b"\0", 4) + 1  # executable path, then padding
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    start = offset
    for _ in range(argc):
        offset = raw.index(b"\0", offset) + 1
    return raw[start:offset], raw[offset:]


def process(pid: int, *, environment: bool = False, command: bool = False) -> Proc | None:
    """Read one of this user's processes, never enumerate other processes.

    ``command`` and ``environment`` add its command line and environment to
    ``text``, in that order, as :func:`brindle.procs.table` does; without
    them it carries only the parent and owner. No subprocess or full
    command/environment table is involved.
    None means the process is gone or belongs to another user. Other probe
    failures propagate so the guard does not cache them as identity refusals.
    """
    if pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            # proc_bsdshortinfo (sys/proc_info.h): four uint32s, comm[16],
            # flags, uid, gid, ruid, rgid, svuid, svgid and a reserved uint32.
            info = ctypes.create_string_buffer(64)
            if _libproc().proc_pidinfo(pid, 13, 0, info, len(info)) != len(info):
                raise OSError(ctypes.get_errno(), "process probe failed")
            found, ppid = struct.unpack_from("II", info.raw)
            uid = struct.unpack_from("I", info.raw, 36)[0]
            if found != pid or uid != os.getuid():
                return None
            args, env = _procargs(pid) if environment or command else (b"", b"")
        else:
            path = Path("/proc") / str(pid)
            if path.stat().st_uid != os.getuid():
                return None
            stat = (path / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            args = (path / "cmdline").read_bytes() if command else b""
            env = (path / "environ").read_bytes() if environment else b""
        parts = ([args] if command else []) + ([env] if environment else [])
        return Proc(pid, ppid, " ".join(p.replace(b"\0", b" ").decode(errors="replace") for p in parts))
    except OSError as exc:
        if exc.errno in (errno.ESRCH, errno.ENOENT):
            return None
        raise


def children(pid: int) -> list[int]:
    """The pids whose parent is ``pid``, without reading any of their details."""
    if sys.platform == "darwin":
        lib = _libproc()
        lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.proc_listchildpids.restype = ctypes.c_int
        count = lib.proc_listchildpids(pid, None, 0)
        if count < 0:
            raise OSError(ctypes.get_errno(), "process probe failed")
        buf = (ctypes.c_int * (count + 64))()
        count = lib.proc_listchildpids(pid, buf, ctypes.sizeof(buf))
        if count < 0:
            raise OSError(ctypes.get_errno(), "process probe failed")
        return [p for p in buf[:count] if p > 0]
    task = Path("/proc") / str(pid) / "task"
    try:
        listed = [(t / "children").read_text().split() for t in task.iterdir()]
        return [int(p) for found in listed for p in found]
    except OSError:
        pass
    out = []
    for d in Path("/proc").iterdir():
        try:
            if d.name.isdigit() and int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]) == pid:
                out.append(int(d.name))
        except (OSError, IndexError, ValueError):
            continue
    return out
