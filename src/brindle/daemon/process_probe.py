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


def _procargs(pid: int) -> bytes:
    """Only this process's environment, from macOS KERN_PROCARGS2."""
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
    for _ in range(argc):
        offset = raw.index(b"\0", offset) + 1
    return raw[offset:]


def process(pid: int, *, environment: bool = False) -> Proc | None:
    """Read one of this user's processes, never enumerate other processes.

    Ancestors need only their parent and owner. Only the caller needs its
    environment; no subprocess or full command/environment table is involved.
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
            env = _procargs(pid) if environment else b""
        else:
            path = Path("/proc") / str(pid)
            if path.stat().st_uid != os.getuid():
                return None
            stat = (path / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            env = (path / "environ").read_bytes() if environment else b""
        return Proc(pid, ppid, env.replace(b"\0", b" ").decode(errors="replace"))
    except OSError as exc:
        if exc.errno in (errno.ESRCH, errno.ENOENT):
            return None
        raise
