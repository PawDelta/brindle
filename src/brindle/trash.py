"""Recoverable worktree removal. Cross-device moves fail closed here
(TrashUnavailable); git.remove_worktree then removes the checkout in place.

The checkout and its Git administration directory (including its
index) travel together. A private ref protects its commit while it is here.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from brindle import config, git

RETENTION = 30 * 86400
SIZE_CAP = 10 * 1024 ** 3


class TrashError(OSError):
    pass


class TrashUnavailable(TrashError):
    """The trash can't take this checkout at all (another volume or mount, or
    no exclusive rename): nothing was moved."""


# Errors of an exclusive rename that mean "not possible here", not "refused".
_UNSUPPORTED = {errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS}


def directory() -> Path:
    return config.brindle_home() / "trash"


@contextmanager
def _locked():
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _valid_metadata(data: object, ident: str) -> bool:
    """Validate the fields shared by listing, retention and restoration.

    Corrupt entries stay on disk for manual recovery. They must not break
    processing of intact entries, or supply invalid sizes to the cap.
    Missing required fields are handled by entries() alongside bad JSON.
    """
    if not isinstance(data, dict) or data["id"] != ident:
        return False
    for key in ("repo_root", "path"):
        if not isinstance(data[key], str) or not data[key]:
            return False
    for key in ("branch", "sha"):
        if data[key] is not None and not isinstance(data[key], str):
            return False
    removed_at = data["removed_at"]
    # Comparisons also reject NaN/infinity and huge JSON integers without
    # converting them to float (which can itself overflow).
    if type(removed_at) not in (int, float) or not 0 <= removed_at <= sys.float_info.max:
        return False
    size = data.get("size", 0)  # older entries are measured on read
    return type(size) is int and size >= 0


def entries() -> list[dict]:
    from brindle.disk import size_of

    result = []
    for p in directory().glob("*/entry.json"):
        if p.parent.is_symlink() or p.is_symlink():
            continue
        try:
            data = json.loads(p.read_text())
            if not _valid_metadata(data, p.parent.name) or not (p.parent / "tree").is_dir():
                continue
            # Entries trashed before sizes were stored are measured here.
            result.append({**data, "size": data["size"] if "size" in data else size_of(p.parent)})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda e: e["removed_at"])


def _move(source: Path, dest: Path) -> None:
    # Checking existence before rename is insufficient: even an empty directory
    # created after the check must survive. Unsupported filesystems fail closed.
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(dest), 4)  # RENAME_EXCL
    elif sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(dest), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise TrashUnavailable(errno.ENOTSUP, "atomic exclusive rename unavailable", str(dest))
    rename.restype = ctypes.c_int
    if rename(*args):
        error = ctypes.get_errno()
        kind = TrashUnavailable if error in _UNSUPPORTED else TrashError
        raise kind(error, os.strerror(error), str(dest))


def _plain_path(root: Path, path: Path) -> bool:
    """A path under root with no symlinks in any component below root."""
    if not path.is_relative_to(root):
        return False
    current = root
    for part in path.relative_to(root).parts:
        if part in {".", ".."}:
            return False
        current /= part
        if current.is_symlink():
            return False
    return True


def _repair_modules(tree: Path, original: Path, old_admin: Path, admin: Path, checkout: Path) -> None:
    """Relink initialized submodules, including nested ones, in the staged copy."""
    from brindle.disk import _replace

    pending = [(tree, admin)]
    while pending:
        parent, repository = pending.pop()
        args = ["--git-dir", str(repository), "--work-tree", str(parent)]
        index = git.out([*args, "ls-files", "--stage", "-z"], tree)
        head = git.out([*args, "ls-tree", "-r", "-z", "HEAD"], tree)
        # HEAD and saved submodule paths still identify initialized modules
        # after a staged removal. Unrelated nested repositories stay untouched.
        modules = {record.split("\t", 1)[1] for record in (index + head).split("\0")
                   if record.startswith("160000 ")}
        metadata = parent / ".gitmodules"
        if _plain_path(tree, metadata) and metadata.is_file():
            paths = git.run(["config", "--null", "--file", str(metadata), "--get-regexp",
                             r"^submodule\..*\.path$"], tree, check=False)
            if paths.returncode not in {0, 1}:
                raise TrashError(f"cannot read saved submodule paths: {metadata}")
            modules.update(record.split("\n", 1)[1] for record in paths.stdout.split("\0") if "\n" in record)
        for name in sorted(modules):
            folder = parent / name
            pointer = folder / ".git"
            if folder == parent or Path(name).is_absolute() or not _plain_path(tree, folder) or not pointer.exists():
                continue  # Uninitialized or replaced submodule.
            if pointer.is_symlink():
                raise TrashError(f"unsupported submodule Git pointer: {pointer}")
            relative = folder.relative_to(tree)
            if pointer.is_dir():
                target = pointer  # Older, embedded submodule administration.
                if not target.resolve().is_relative_to(tree.resolve()):
                    raise TrashError(f"submodule metadata is outside the staged worktree: {pointer}")
            else:
                text = pointer.read_text()
                if not text.startswith("gitdir: "):
                    raise TrashError(f"unsupported submodule Git pointer: {pointer}")
                target = (original / relative / text[8:].strip()).resolve()
                if not target.is_relative_to(admin):
                    if not target.is_relative_to(old_admin):
                        raise TrashError(f"submodule metadata is outside the saved worktree: {pointer}")
                    target = admin / target.relative_to(old_admin)
                _replace(pointer, lambda out: out.write(f"gitdir: {target}\n".encode()))
            boundary = tree if target.is_relative_to(tree) else admin
            if not _plain_path(boundary, target / "config"):
                raise TrashError(f"submodule configuration is outside staged administration: {target}")
            git.run(["config", "--file", str(target / "config"), "core.worktree", str(checkout / relative)], tree)
            pending.append((folder, target))


def _unpin(root: str | Path, ident: str) -> None:
    git.run(["update-ref", "--stdin"], root, check=False,
            input=f"delete refs/brindle-trash/{ident}\ndelete refs/brindle-trash-index/{ident}\n")


def put(root: str | Path, path: str | Path, *, force: bool = False) -> None:
    from brindle.disk import _replace, size_of

    source = Path(path).absolute()
    with _locked():
        if source.is_symlink() or not source.is_dir():
            raise TrashError(f"not a worktree directory: {source}")
        if source.stat().st_dev != directory().stat().st_dev:
            raise TrashUnavailable(f"trash is on another volume; original kept at {source}")
        original = source.stat()
        registered = next((w for w in git.list_worktrees(root)
                           if Path(w["worktree"]).resolve() == source.resolve()), None)
        if registered and "locked" in registered and not force:
            raise TrashError(f"worktree is locked: {source}")
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
                    raise TrashUnavailable("Git metadata and trash are on different volumes; original kept")
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
            # Measured once here, so retention never walks the saved checkouts.
            data["size"] = size_of(source) + (size_of(admin) if admin is not None else 0)
            _replace(dest / "entry.json", lambda out: out.write(json.dumps(data).encode()))
            current = source.stat()
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise TrashError(f"worktree changed during removal: {source}")
            if registered and not force and git.dirty_files(source):
                raise TrashError(f"uncommitted changes at {source}")
            if registered and not any(w.get("HEAD") == data["sha"] and w.get("branch") == data["branch"]
                                      and Path(w["worktree"]).resolve() == source.resolve()
                                      and (force or "locked" not in w)
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


def _check_restored_registration(admin: Path, dest: Path, entry: dict, registered: list[dict]) -> None:
    if (admin / "gitdir").read_text().strip() != str(dest / ".git") or not any(
        Path(w["worktree"]).resolve() == dest.resolve() and w.get("HEAD") == entry["sha"]
        and w.get("branch") == entry["branch"] and "locked" in w for w in registered
    ):
        raise TrashError("restore registration changed during preparation")


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
        temporary_lock = False
        published = False
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Keep the saved checkout untouched until its repaired copy has
            # been validated and published on the destination's volume.
            with tempfile.TemporaryDirectory(prefix=".brindle-restore-", dir=dest.parent) as staging:
                tree = Path(staging) / "tree"
                shutil.copytree(folder / "tree", tree, symlinks=True)
                if "common" in entry:
                    old_admin = (dest / (tree / ".git").read_text()[8:].strip()).resolve()
                    admin = Path(entry["common"]) / "worktrees" / f"trash-{ident}"
                    admin.parent.mkdir(parents=True, exist_ok=True)
                    # Prune must never see partial or unlocked administration
                    # pointing at the still-absent destination.
                    with tempfile.TemporaryDirectory(prefix=".brindle-admin-", dir=entry["common"]) as metadata:
                        prepared = Path(metadata) / "admin"
                        shutil.copytree(folder / "admin", prepared, symlinks=True)
                        temporary_lock = not (prepared / "locked").exists()
                        if temporary_lock:
                            _replace(prepared / "locked", lambda out: out.write(b"brindle restore in progress\n"))
                        _replace(prepared / "gitdir", lambda out: out.write((str(dest / ".git") + "\n").encode()))
                        _move(prepared, admin)
                        admin_created = True
                    _replace(tree / ".git", lambda out: out.write(f"gitdir: {admin}\n".encode()))
                    _repair_modules(tree, dest, old_admin, admin, tree)
                    git.run(["--git-dir", str(admin), "--work-tree", str(tree), "status", "--porcelain"], root)
                    _repair_modules(tree, dest, old_admin, admin, dest)
                # Re-read after copying, excluding only our own registration.
                registered = git.list_worktrees(root)
                if admin is not None:
                    _check_restored_registration(admin, dest, entry, registered)
                if any(branch and w.get("branch") == branch and Path(w["worktree"]).resolve() != dest.resolve()
                       for w in registered):
                    raise TrashError("restore branch is now in use")
                if branch and git.out(["rev-parse", "--verify", branch], root) != entry["sha"]:
                    raise TrashError("restore branch has moved")
                _move(tree, dest)
                published = True
                if admin is not None:
                    _check_restored_registration(admin, dest, entry, git.list_worktrees(root))
                    git.run(["status", "--porcelain"], dest)
                    if temporary_lock:
                        (admin / "locked").unlink()
        except BaseException:
            if admin_created and not published:
                shutil.rmtree(admin)
            raise
        shutil.rmtree(folder)
        _unpin(root, ident)
        return str(dest)


def retain(*, now: float | None = None, cap: int = SIZE_CAP, empty: bool = False) -> list[str]:
    removed = []
    now = time.time() if now is None else now
    with _locked():
        # Read once: put, restore and retention all hold this lock, so the
        # entries can't change under us. Oldest first, until under the cap.
        current = entries()
        total = sum(e["size"] for e in current)
        for entry in current:
            if not (empty or now - entry["removed_at"] > RETENTION or total > cap):
                continue
            shutil.rmtree(directory() / entry["id"])
            _unpin(entry["repo_root"], entry["id"])
            total -= entry["size"]
            removed.append(entry["id"])
    return removed


def empty() -> list[str]:
    return retain(empty=True)
