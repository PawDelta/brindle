"""Private files under ``$BRINDLE_HOME/pro``: 0600 files in a 0700 directory,
refused when group/world accessible, not the user's, or a symlink."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from brindle.pro.credentials import CredentialError, FileStore

_home: ContextVar[Path | None] = ContextVar("brindle_pro_home", default=None)


def pro_home() -> Path:
    """brindle's home for the Pro files: the one :func:`at_home` set for this
    thread or task, else ``brindle_home()``."""
    from brindle.config import brindle_home

    return _home.get() or brindle_home()


def home_set() -> bool:
    """Whether :func:`at_home` chose the home (so process-wide caches of the
    default home don't apply)."""
    return _home.get() is not None


@contextmanager
def at_home(home: Path) -> Iterator[None]:
    """Read and write the Pro files under ``home`` in this thread or task
    (brindle.pro.org_context resolves a context for another home); the
    environment is left alone."""
    token = _home.set(Path(home))
    try:
        yield
    finally:
        _home.reset(token)


def private_dir(create: bool = True) -> Path | None:
    """The private directory, made if missing. A read passes ``create=False``
    and gets None instead, so a lookup never creates brindle's home."""
    d = pro_home() / "pro"
    if not create and not os.path.lexists(d):
        return None
    if create:
        old = os.umask(0o077)
        try:
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        finally:
            os.umask(old)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise CredentialError(f"{d} is not a directory")
    FileStore._check(st, "brindle Pro directory", 0o700)
    return d


def read_private(path: Path, limit: int) -> bytes | None:
    """The file's bytes (at most ``limit``), or None if it doesn't exist."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CredentialError(f"cannot open {path.name}") from e
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CredentialError(f"{path.name} is not a regular file")
        FileStore._check(st, path.name, 0o600)
        return fh.read(limit)


def write_private(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, created 0600."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    old = os.umask(0o077)
    try:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.umask(old)
