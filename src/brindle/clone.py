"""Cloning a repo for the desktop app's project picker: the user's own
``git clone``, with their git credentials and SSH keys exactly as a terminal
would use them. brindle never sees or stores a credential."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from brindle.service.errors import ServiceError

# Tests only: lets a file:// URL (a local bare repo) through. Production refuses it.
ALLOW_FILE_URLS = False

_SCP = re.compile(r"^git@[^\s/:@-][^\s/:@]*:[^\s:]\S*$")      # git@<host>:<path>, never a relative local path
_AUTH_FAILURE = re.compile(r"authentication failed|permission denied|could not read (?:username|password)|"
                           r"terminal prompts disabled|access denied|invalid username or password", re.I)
TAIL = 20


def validate_url(url: str) -> str:
    """``url`` if it is an https, ssh or git@ remote; never a local path, an
    option (a leading '-') or anything with whitespace or control characters."""
    if ALLOW_FILE_URLS and url.startswith("file://") and url.isprintable() and " " not in url:
        return url
    if url.isascii() and url.isprintable() and " " not in url:
        if _SCP.match(url):
            return url
        if url.startswith(("https://", "ssh://")):
            host = urlsplit(url).hostname
            if host and not host.startswith("-"):
                return url
    raise ServiceError("invalid", "url must be an https, ssh or git@host:path address of a repository")


def repo_name(url: str) -> str:
    name = re.split(r"[/:]", url.rstrip("/"))[-1].removesuffix(".git")
    if not name or name in (".", "..") or "\\" in name:
        raise ServiceError("invalid", f"can't tell the repository's name from {url!r}")
    return name


def destination(url: str, parent_dir: str) -> Path:
    """Where ``url`` would be cloned: refused unless ``parent_dir`` is a folder
    and nothing is at the destination yet."""
    parent = Path(parent_dir).expanduser()
    if not parent.is_dir():
        raise ServiceError("invalid", f"not a folder: {parent_dir}")
    dest = parent / repo_name(validate_url(url))
    if dest.exists() or dest.is_symlink():
        raise ServiceError("exists", f"{dest} already exists", 409)
    return dest


def clone(url: str, dest: Path, on_line: Callable[[str], None]) -> str:
    """Run ``git clone --progress`` into ``dest``, passing each progress line
    (git ends them with CR or LF) to ``on_line``. Returns the path; a failure
    raises a ServiceError carrying git's own message."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}      # no prompt to hang on: fail with git's message
    proc = subprocess.Popen(["git", "clone", "--progress", "--", url, str(dest)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    lines: list[str] = []
    buf = b""
    assert proc.stderr is not None
    while chunk := proc.stderr.read1(4096):
        parts = re.split(rb"[\r\n]", buf + chunk)
        buf = parts.pop()
        for part in parts:
            if text := part.decode(errors="replace").strip():
                lines.append(text)
                on_line(text)
    if text := buf.decode(errors="replace").strip():
        lines.append(text)
        on_line(text)
    if proc.wait() != 0:
        message = "\n".join(lines[-TAIL:]) or "git clone failed"
        code = "auth_failed" if _AUTH_FAILURE.search(message) else "clone_failed"
        raise ServiceError(code, message, 422)
    return str(dest)
