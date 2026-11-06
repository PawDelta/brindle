"""Git state built by the first test that needs it, then copied into later tests' ``tmp_path`` (the
absolute paths git records rewritten) instead of running git init, commit, push and worktree add again.
A module imports ``repo`` (overriding conftest's) and ``fast_host_tools``."""

import dataclasses
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import sh

_SAVED: dict[str, tuple[Path, str, object]] = {}   # key -> (copy, tmp_path it was built in, build's value)
_GIT_BIN: list[Path | None] = []


def _path_records(root: Path):
    """A worktree's ``.git`` file, a repo's config and its worktrees' ``gitdir`` files."""
    for top, dirs, files in os.walk(root):
        if ".git" in files:
            yield Path(top, ".git")
        if ".git" in dirs:
            dirs.remove(".git")
            yield Path(top, ".git", "config")
            yield from Path(top, ".git").glob("worktrees/*/gitdir")


def _copy(src: Path, dst: Path, names) -> None:
    for name in names:
        shutil.rmtree(dst / name, ignore_errors=True)
        # Not lock files: git's background maintenance can remove one mid-copy, and none belongs in a snapshot.
        shutil.copytree(src / name, dst / name, symlinks=True, ignore=shutil.ignore_patterns("*.lock"))


def _relocate(value, old: str, new: str):
    if isinstance(value, (str, Path)):
        return type(value)(str(value).replace(old, new))
    if isinstance(value, tuple):
        return tuple(_relocate(v, old, new) for v in value)
    if dataclasses.is_dataclass(value):
        return dataclasses.replace(value, **{f.name: getattr(value, f.name).replace(old, new) for f in
                                             dataclasses.fields(value) if isinstance(getattr(value, f.name), str)})
    return value


def restore_or_build(key: str, tmp_path: Path, factory, names, build, on_copy=lambda value: value):
    """Put the folders ``names`` of ``tmp_path`` in the state ``build()`` makes
    (the first time, saved under ``key``; then copied). Returns what ``build``
    returned: on a copy, moved to ``tmp_path`` and passed through ``on_copy``."""
    if key not in _SAVED:
        value = build()
        _SAVED[key] = (factory.mktemp("snapshot"), str(tmp_path), value)
        _copy(tmp_path, _SAVED[key][0], names)
        return value
    saved, old, value = _SAVED[key]
    _copy(saved, tmp_path, names)
    for record in _path_records(tmp_path):
        if record.is_file() and old in (text := record.read_text()):
            record.write_text(text.replace(old, str(tmp_path)))
    return on_copy(_relocate(value, old, str(tmp_path)))


def workspaces_once(key: str, db, tmp_path: Path, factory, build):
    """``build()`` makes a tuple of workspaces of ``repo``; a copy records their rows again, created now."""
    def record(made):
        made = tuple(dataclasses.replace(w, created_at=time.time()) for w in made)
        for w in made:
            db.add_workspace(w)
        return made

    return restore_or_build(key, tmp_path, factory, ("origin.git", "proj"), build, record)


@pytest.fixture(autouse=True)
def fast_host_tools(tmp_path_factory, monkeypatch):
    """git started directly, not through macOS's /usr/bin/git shim (an xcrun
    lookup on every call doubles its cost); ``ps`` (macOS; Linux reads /proc)
    shows just this process and docker no containers, as on a CI runner."""
    from brindle import spawn

    if not _GIT_BIN:
        _GIT_BIN.append(None)
        if sys.platform == "darwin" and shutil.which("git") == "/usr/bin/git":
            _GIT_BIN[0] = tmp_path_factory.mktemp("git-bin")
            (_GIT_BIN[0] / "git").symlink_to(sh("xcrun --find git", _GIT_BIN[0]))
    if _GIT_BIN[0] is not None:
        monkeypatch.setenv("PATH", f"{_GIT_BIN[0]}{os.pathsep}{os.environ.get('PATH', '')}")
    read, action, this = spawn.read, spawn.action, f"{os.getpid()} {os.getppid()} python -m pytest\n"
    monkeypatch.setattr(spawn, "read", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, this, "")
                        if argv and argv[0] == "ps" else read(argv, *a, **kw))
    monkeypatch.setattr(spawn, "action", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, "", "")
                        if argv and argv[0] == "docker" else action(argv, *a, **kw))


@pytest.fixture
def repo(tmp_path, tmp_path_factory):
    """conftest's ``repo`` (on ``main`` with one commit, pushed to a bare ``origin``), copied."""
    def build():
        (tmp_path / "proj").mkdir()
        for name, text in (("app.py", "print('hi')\n"), (".gitignore", ".env\n"), (".env", "SECRET=1\n")):
            (tmp_path / "proj" / name).write_text(text)
        origin = tmp_path / "origin.git"
        sh(f"git init -q --bare -b main {origin} && cd proj && git init -q -b main && git add -A && git commit -qm init"
           f" && git remote add origin {origin} && git push -q -u origin main && git remote set-head origin main", tmp_path)

    restore_or_build("repo", tmp_path, tmp_path_factory, ("origin.git", "proj"), build)
    return tmp_path / "proj"
