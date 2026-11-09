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


def _too_large() -> ServiceError:
    return ServiceError("too_large", "attachment exceeds the file size limit", 413)


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


def _open_file(path: Path) -> int:
    """Walk with directory descriptors: neither a parent nor the file may be a symlink."""
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def attach(worktree: str, provider: providers.Provider, body: dict) -> dict:
    """Copy one local file or clipboard payload and return the CLI's paste text."""
    if not provider.accepts_files:
        raise ServiceError("unsupported", "this agent's provider does not accept file attachments")
    local = set(body) == {"path"} and isinstance(body["path"], str) and bool(body["path"])
    clipboard = (set(body) == {"name", "data_base64"}
                 and isinstance(body["name"], str) and isinstance(body["data_base64"], str))
    if not local and not clipboard:
        raise ServiceError("invalid", "give path or name and data_base64")
    root = Path(worktree).resolve()
    try:
        with contextlib.ExitStack() as stack:
            if local:
                source = Path(os.path.abspath(os.path.expanduser(body["path"])))
                if _denied(source, root):
                    raise ServiceError("forbidden", "credential files cannot be attached", 403)
                stream = stack.enter_context(os.fdopen(_open_file(source), "rb"))
                meta = os.fstat(stream.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                    raise ServiceError("invalid", "attach a regular file with no links")
                if meta.st_size > MAX_BYTES:
                    raise _too_large()
                data = stream.read(MAX_BYTES + 1)
                name = source.name
            else:
                encoded = body["data_base64"]
                if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
                    raise _too_large()
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise ServiceError("invalid", "attachment data is not valid base64") from None
                name = body["name"]
            if len(data) > MAX_BYTES:
                raise _too_large()
            # Keep extensions, strip traversal, control characters and CLI metacharacters.
            name = re.sub(r"[^A-Za-z0-9._-]", "_", name.replace("\\", "/").split("/")[-1])
            name = name.strip("._")[:160] or "attachment"
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
        raise ServiceError("invalid", "attachment could not be copied; use a readable regular file") from None
