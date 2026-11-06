"""Recoverable worktree removal. Cross-device moves fail closed.

The checkout and its Git administration directory (including its
index) travel together. A private ref protects its commit while it is here.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from brindle import config, git

RETENTION = 30 * 86400
SIZE_CAP = 10 * 1024 ** 3


class TrashError(OSError):
    pass


def directory() -> Path:
    return config.brindle_home() / "trash"


@contextmanager
def _locked():
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def entries() -> list[dict]:
    from brindle.disk import size_of

    result = []
    for p in directory().glob("*/entry.json"):
        if p.parent.is_symlink() or p.is_symlink():
            continue
        try:
            data = json.loads(p.read_text())
            if data["id"] != p.parent.name or not (p.parent / "tree").is_dir():
                continue
            result.append({**data, "size": size_of(p.parent)})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda e: e["removed_at"])


def _move(source: Path, dest: Path) -> None:
    # Never use shutil.move: its copy/delete fallback can lose dirty files.
    source.rename(dest)


def _unpin(root: str | Path, ident: str) -> None:
    git.run(["update-ref", "--stdin"], root, check=False,
            input=f"delete refs/brindle-trash/{ident}\ndelete refs/brindle-trash-index/{ident}\n")


def put(root: str | Path, path: str | Path, *, force: bool = False) -> None:
    from brindle.disk import _replace

    source = Path(path).absolute()
    with _locked():
        if source.is_symlink() or not source.is_dir():
            raise TrashError(f"not a worktree directory: {source}")
        if source.stat().st_dev != directory().stat().st_dev:
            raise TrashError(f"trash is on another volume; original kept at {source}")
        original = source.stat()
        registered = next((w for w in git.list_worktrees(root)
                           if Path(w["worktree"]).resolve() == source.resolve()), None)
        if registered and not force and git.dirty_files(source):
            raise TrashError(f"uncommitted changes at {source}")
        ident = uuid.uuid4().hex
        dest = directory() / ident
        dest.mkdir(mode=0o700)
        ref = f"refs/brindle-trash/{ident}"
        data = {"id": ident, "repo_root": str(root), "path": str(source),
                "branch": registered.get("branch") if registered else None,
                "sha": registered.get("HEAD") if registered else None,
                "removed_at": time.time()}
        admin = None
        try:
            if registered:
                admin = Path(git.out(["rev-parse", "--absolute-git-dir"], source))
                common = Path(git.out(["rev-parse", "--path-format=absolute", "--git-common-dir"], source))
                if admin == common:
                    raise TrashError("the main checkout cannot be trashed")
                if admin.stat().st_dev != directory().stat().st_dev:
                    raise TrashError("Git metadata and trash are on different volumes; original kept")
                data["common"] = str(common)
                git.run(["update-ref", ref, data["sha"]], root)
                # The index is no longer in a registered checkout, so GC will
                # not see it. Protect every staged blob, even conflict stages.
                staged = git.out(["ls-files", "--stage", "-z"], source)
                records = []
                for i, record in enumerate(staged.split("\0")):
                    if not record:
                        continue
                    mode, oid, _stage = record.split("\t", 1)[0].split()
                    kind = "commit" if mode == "160000" else "tree" if mode == "040000" else "blob"
                    records.append(f"{mode} {kind} {oid}\t{i}\n")
                tree = git.run(["mktree", "--missing"], source, input="".join(records)).stdout.strip()
                git.run(["update-ref", f"refs/brindle-trash-index/{ident}", tree], root)
            _replace(dest / "entry.json", lambda out: out.write(json.dumps(data).encode()))
            current = source.stat()
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise TrashError(f"worktree changed during removal: {source}")
            if registered and not force and git.dirty_files(source):
                raise TrashError(f"uncommitted changes at {source}")
            if registered and not any(w.get("HEAD") == data["sha"] and w.get("branch") == data["branch"]
                                      and Path(w["worktree"]).resolve() == source.resolve()
                                      for w in git.list_worktrees(root)):
                raise TrashError(f"worktree changed during removal: {source}")
            _move(source, dest / "tree")
            if admin is not None:
                try:
                    if os.path.lexists(source):
                        raise TrashError(f"worktree path was reused during removal: {source}")
                    # Moving this one registration never touches another checkout,
                    # including one created at the original path during removal.
                    _move(admin, dest / "admin")
                except BaseException:
                    if not os.path.lexists(source):
                        _move(dest / "tree", source)
                    raise
        except BaseException:
            if not (dest / "tree").exists():
                shutil.rmtree(dest)
                _unpin(root, ident)
            raise


def restore(ident: str) -> str:
    from brindle.disk import _replace

    with _locked():
        entry = next((e for e in entries() if e["id"] == ident), None)
        if entry is None:
            raise TrashError(f"unknown trash entry: {ident}")
        dest = Path(entry["path"])
        root, branch = entry["repo_root"], entry["branch"]
        if os.path.lexists(dest):
            raise TrashError(f"restore path already exists: {dest}")
        registered = git.list_worktrees(root)
        if any(Path(w["worktree"]).resolve() == dest.resolve() or
               (branch and w.get("branch") == branch) for w in registered):
            raise TrashError("restore branch or path is in use")
        if branch:
            tip = git.run(["rev-parse", "--verify", branch], root, check=False)
            if tip.returncode == 0 and tip.stdout.strip() != entry["sha"]:
                raise TrashError("restore branch has moved")
            if tip.returncode:
                git.run(["update-ref", branch, entry["sha"], "0" * len(entry["sha"])], root)
        folder = directory() / ident
        admin = None
        admin_created = False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.mkdir()  # Reserve the destination; never merge into an existing folder.
        reservation = dest.lstat()
        try:
            if "common" in entry:
                admin = Path(entry["common"]) / "worktrees" / f"trash-{ident}"
                admin.parent.mkdir(parents=True, exist_ok=True)
                admin.mkdir()
                admin_created = True
                shutil.copytree(folder / "admin", admin, dirs_exist_ok=True, symlinks=True)
                _replace(admin / "gitdir", lambda out: out.write((str(dest / ".git") + "\n").encode()))
                _replace(folder / "tree" / ".git", lambda out: out.write(f"gitdir: {admin}\n".encode()))
            # Metadata copying can take time. Check again immediately before
            # moving the checkout, excluding only our reserved registration.
            if any(branch and w.get("branch") == branch and Path(w["worktree"]).resolve() != dest.resolve()
                   for w in git.list_worktrees(root)):
                raise TrashError("restore branch is now in use")
            if branch and git.out(["rev-parse", "--verify", branch], root) != entry["sha"]:
                raise TrashError("restore branch has moved")
            current = dest.lstat()
            if (current.st_dev, current.st_ino) != (reservation.st_dev, reservation.st_ino):
                raise TrashError("restore path is now in use")
            _move(folder / "tree", dest)
        except BaseException:
            if admin_created:
                shutil.rmtree(admin)
            if os.path.lexists(dest):
                current = dest.lstat()
                if (current.st_dev, current.st_ino) == (reservation.st_dev, reservation.st_ino):
                    dest.rmdir()
            raise
        shutil.rmtree(folder)
        _unpin(root, ident)
        return str(dest)


def retain(*, now: float | None = None, cap: int = SIZE_CAP, empty: bool = False) -> list[str]:
    removed = []
    now = time.time() if now is None else now
    with _locked():
        for candidate in entries():
            # Re-read after each removal: restore and retention share this lock.
            current = entries()
            entry = next((e for e in current if e["id"] == candidate["id"]), None)
            if entry is None or not (empty or now - entry["removed_at"] > RETENTION or
                                     sum(e["size"] for e in current) > cap):
                continue
            shutil.rmtree(directory() / entry["id"])
            _unpin(entry["repo_root"], entry["id"])
            removed.append(entry["id"])
    return removed


def empty() -> list[str]:
    return retain(empty=True)
