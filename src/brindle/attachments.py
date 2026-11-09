"""Bounded, credential-safe copies of files dropped onto an agent's pane."""

from __future__ import annotations

import base64
import binascii
import contextlib
import fnmatch
import os
import re
import secrets
import stat
import unicodedata
from pathlib import Path

from brindle import git, permissions, providers
from brindle.service.errors import ServiceError

MAX_BYTES = 10 * 1024 * 1024
SYSTEM_ALIASES = {Path(f"/{name}"): Path(f"/private/{name}") for name in ("tmp", "var", "etc")}

# Windows locations for the same CLI credentials and the profile's OS vaults.
_WINDOWS_CREDENTIAL_STORES = (
    "AppData/Roaming/gh", "AppData/Roaming/GitHub CLI", "AppData/Roaming/gcloud",
    "AppData/Roaming/gnupg", "AppData/Roaming/npm/npmrc", "AppData/Roaming/npm/etc/npmrc",
    "AppData/Roaming/Docker", "AppData/Local/Docker", ".gcm", ".codex/auth.json",
    "AppData/Local/Microsoft/Credentials", "AppData/Roaming/Microsoft/Credentials",
    "AppData/Local/Microsoft/Vault", "AppData/Roaming/Microsoft/Vault",
    "AppData/Roaming/Microsoft/Protect",
)


def request_limit() -> int:
    # Base64 plus the JSON envelope and a bounded filename/path.
    return 4 * ((MAX_BYTES + 2) // 3) + 4096


def _safe_name(name: str) -> str:
    # Keep extensions, strip traversal, control characters and CLI metacharacters.
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name.replace("\\", "/").split("/")[-1])
    return name.strip("._")[:160] or "attachment"


def _too_large(name: str, size: int) -> ServiceError:
    return ServiceError(
        "too_large", f"{name} is {size:,} bytes; the attachment limit is {MAX_BYTES:,} bytes.", 413,
    )


def _system_path(path: Path) -> Path:
    """Rewrite only a known root alias whose link names the expected system directory."""
    for alias, target in SYSTEM_ALIASES.items():
        if path.is_relative_to(alias):
            try:
                if stat.S_ISLNK(alias.lstat().st_mode) and alias.parent / alias.readlink() == target:
                    return target / path.relative_to(alias)
            except OSError:
                pass  # The descriptor walk will report an absent or unreadable source.
    return path


def _path_key(path: str | Path) -> str:
    # Fail closed for case and Unicode aliases even on case-sensitive volumes.
    # This changes comparisons only; the source walk still refuses symlinks.
    return unicodedata.normalize("NFD", str(path)).casefold()


def _denied(path: Path, worktree: Path) -> bool:
    key = _path_key(path)
    if any(fnmatch.fnmatchcase(key, _path_key(os.path.expanduser(rule.match)))
           for rule in permissions.CREDENTIAL_DENIES if rule.kind == "read"):
        return True
    denied = [*providers._codex_denied_reads(), *permissions.credential_paths()]
    # WSL drive mounts need not be /mnt: recognize the Users/<profile> suffix
    # at any mount root, including native/extended aliases from other clients.
    # Normalize only the comparison; the source still gets the no-symlink walk.
    windows_key = _path_key(os.path.normpath(str(path).replace("\\", "/")))
    windows_key = "/".join(part.rstrip(" .") for part in windows_key.split("/"))
    if profiles := list(re.finditer(r"(?:^|/)users/[^/]+(?=/|$)", windows_key)):
        stores = [*permissions._CREDENTIAL_DIRS, *permissions._CREDENTIAL_FILES,
                  *_WINDOWS_CREDENTIAL_STORES]
        home = _path_key(Path.home()) + "/"
        for name in denied:
            expanded = _path_key(os.path.expanduser(name))
            if expanded.startswith(home):
                stores.append(expanded[len(home):])
        for profile in profiles:
            relative = windows_key[profile.end():].lstrip("/")
            if any(relative == (store := _path_key(name)) or relative.startswith(store + "/")
                   for name in stores):
                return True
    # Include the credential-path checker's home-relative stores too, including
    # another account's home, so its fallback cannot miss an aliased spelling.
    homes = {str(Path.home())}
    if other := re.match(permissions._OTHER_HOME.pattern, str(path), re.IGNORECASE):
        homes.add(other.group())
    denied.extend(str(Path(home) / store) for home in homes
                  for store in (*permissions._CREDENTIAL_DIRS, *permissions._CREDENTIAL_FILES))
    for name in denied:
        root = _path_key(Path(name).expanduser().resolve())
        if key == root or key.startswith(root + "/"):
            return True
    request = permissions.Request("attachments", "read", "Read", worktree=str(worktree))
    part = permissions.ShellPart([], "", str(worktree))
    return permissions._credential_file(request, part, str(path))


class _SymlinkError(OSError):
    pass


def _open_component(component: str, flags: int, parent: int) -> int:
    try:
        return os.open(component, flags | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        # O_DIRECTORY reports ENOTDIR for a parent link on macOS and Linux.
        # Inspect only to classify the refusal; never open through that link.
        try:
            linked = stat.S_ISLNK(os.stat(component, dir_fd=parent, follow_symlinks=False).st_mode)
        except OSError:
            raise exc
        if linked:
            raise _SymlinkError from None
        raise


def _open_file(path: Path) -> int:
    """Walk with directory descriptors: neither a parent nor the file may be a symlink."""
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = _open_component(component, os.O_RDONLY | os.O_DIRECTORY, parent)
            os.close(parent)
            parent = child
        return _open_component(path.name, os.O_RDONLY | os.O_NONBLOCK, parent)
    finally:
        os.close(parent)


def _read_file(path: Path, name: str) -> bytes:
    try:
        fd = _open_file(path)
        try:
            meta = os.fstat(fd)
            if stat.S_ISDIR(meta.st_mode):
                raise ServiceError("directory", f"{name} is a folder; zip it before attaching it.")
            if not stat.S_ISREG(meta.st_mode):
                raise ServiceError("not_regular", f"{name} is not a regular file and cannot be attached.")
            if meta.st_nlink != 1:
                raise ServiceError("hardlink", f"{name} is a hard-linked file and cannot be attached.")
            if meta.st_size > MAX_BYTES:
                raise _too_large(name, meta.st_size)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise _too_large(name, max(len(data), os.fstat(fd).st_size))
            return data
        finally:
            os.close(fd)
    except _SymlinkError:
        raise ServiceError(
            "symlink", f"{name} is a link or is inside a linked folder and cannot be attached.",
        ) from None
    except FileNotFoundError:
        raise ServiceError(
            "missing", f"{name} could not be found; it may have been moved or deleted.",
        ) from None
    except PermissionError:
        raise ServiceError(
            "unreadable", f"{name} cannot be read because the operating system denied access.",
        ) from None
    except NotADirectoryError:
        raise ServiceError(
            "not_directory", f"{name} cannot be read because a parent is not a folder.",
        ) from None


def attach(worktree: str, provider: providers.Provider, body: dict) -> dict:
    """Copy one local file or clipboard payload and return the CLI's paste text."""
    if not provider.accepts_files:
        raise ServiceError("unsupported", "this agent's provider does not accept file attachments")
    local = set(body) == {"path"} and isinstance(body["path"], str) and bool(body["path"])
    clipboard = (set(body) == {"name", "data_base64"}
                 and isinstance(body["name"], str) and isinstance(body["data_base64"], str))
    if not local and not clipboard:
        raise ServiceError("invalid", "give path or name and data_base64")
    name = _safe_name(body["path"] if local else body["name"])
    root = Path(worktree).resolve()
    try:
        with contextlib.ExitStack() as stack:
            if local:
                source = Path(os.path.abspath(os.path.expanduser(body["path"])))
                rewritten = _system_path(source)
                if _denied(source, root) or _denied(rewritten, root):
                    raise ServiceError("forbidden", f"{name} is a credential file and cannot be attached.", 403)
                data = _read_file(rewritten, name)
            else:
                encoded = body["data_base64"]
                if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
                    size = len(encoded) // 4 * 3 - min(2, len(encoded) - len(encoded.rstrip("=")))
                    raise _too_large(name, size)
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise ServiceError("invalid", f"{name} could not be attached because its data is not valid base64.") from None
            if len(data) > MAX_BYTES:
                raise _too_large(name, len(data))
            destination = root / ".brindle" / "attachments"
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, directory)
            for component in (".brindle", "attachments"):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                stack.callback(os.close, directory)
            git.exclude(root, destination)
            final = f"{secrets.token_hex(16)}-{name}"
            temporary = f".upload-{secrets.token_hex(16)}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                # link is atomic and fails if the destination exists; replace would overwrite.
                os.link(temporary, final, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            finally:
                os.unlink(temporary, dir_fd=directory)
            path = str(destination / final)
            return {"paths": [path], "text": provider.format_attachment(path)}
    except (OSError, ValueError):
        # OS errors may contain paths; never expose source contents or internal details.
        raise ServiceError("invalid", f"{name} could not be copied; use a readable regular file.") from None
