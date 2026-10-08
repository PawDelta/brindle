"""Where brindle keeps secrets: the OS keychain, or a 0600 file.

In order of preference:

* macOS: the login keychain, through ``security``. The secret is written via
  ``security -i`` on stdin, so it never appears on a command line.
* Linux: the Secret Service, through ``secret-tool`` (secret on stdin).
* Otherwise: a file under ``$BRINDLE_HOME`` (default ``~/.brindle``), created
  0600 inside a 0700 directory. Reading refuses a file or directory that is
  group/world accessible, not owned by the user, or a symlink.

``BRINDLE_PRO_CREDENTIAL_STORE=keychain|secret-service|file`` forces one.
Secrets are never logged. Values are JSON objects, stored base64 encoded.

This module is shared: brindle Pro keeps its sign-in here (service
``brindle-pro``), and ``brindle keys`` keeps model API keys (service
``brindle-keys``, one entry per variable).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ACCOUNT = "default"
MAX_BLOB = 64 * 1024
FORCE_ENV = "BRINDLE_PRO_CREDENTIAL_STORE"


class CredentialError(Exception):
    """The store couldn't be used safely. Never contains a secret."""


def _encode(creds: dict) -> str:
    return base64.b64encode(json.dumps(creds, separators=(",", ":")).encode()).decode("ascii")


def _decode(blob: str) -> dict | None:
    blob = blob.strip()
    if not blob:
        return None
    if len(blob) > MAX_BLOB:
        raise CredentialError("stored credentials are too large")
    try:
        creds = json.loads(base64.b64decode(blob, validate=True))
    except ValueError as e:
        raise CredentialError("stored credentials are corrupt") from e
    if not isinstance(creds, dict):
        raise CredentialError("stored credentials are corrupt")
    return creds


class KeychainStore:
    """macOS login keychain via the ``security`` CLI."""
    name = "keychain"

    def __init__(self, service: str, account: str = ACCOUNT, label: str = "brindle") -> None:
        self.service, self.account, self.label = service, account, label

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["security", *args], input=stdin, capture_output=True,
                              text=True, timeout=30, check=False)

    def load(self) -> dict | None:
        r = self._run(["find-generic-password", "-a", self.account, "-s", self.service, "-w"])
        if r.returncode == 44:   # errSecItemNotFound
            return None
        if r.returncode != 0:
            raise CredentialError(f"keychain read failed (security exit {r.returncode})")
        return _decode(r.stdout)

    def save(self, creds: dict) -> None:
        # The blob is base64 (no quotes, spaces or backslashes), so quoting is safe;
        # account, service and label are brindle's own fixed strings and variable names.
        line = (f'add-generic-password -U -a "{self.account}" -s "{self.service}" '
                f'-l "{self.label}" -w "{_encode(creds)}"\n')
        r = self._run(["-i"], stdin=line)
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            raise CredentialError(f"keychain write failed (security exit {r.returncode})")

    def delete(self) -> None:
        self._run(["delete-generic-password", "-a", self.account, "-s", self.service])


class SecretServiceStore:
    """The freedesktop Secret Service via ``secret-tool``."""
    name = "secret-service"

    def __init__(self, service: str, account: str = ACCOUNT, label: str = "brindle") -> None:
        self.attrs = ["service", service, "account", account]
        self.label = label

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["secret-tool", *args], input=stdin, capture_output=True,
                              text=True, timeout=30, check=False)

    def load(self) -> dict | None:
        r = self._run(["lookup", *self.attrs])
        if r.returncode != 0:
            if not r.stdout and not r.stderr:
                return None   # not found
            raise CredentialError(f"secret-tool lookup failed (exit {r.returncode})")
        return _decode(r.stdout)

    def save(self, creds: dict) -> None:
        r = self._run(["store", f"--label={self.label}", *self.attrs], stdin=_encode(creds))
        if r.returncode != 0:
            raise CredentialError(f"secret-tool store failed (exit {r.returncode})")

    def delete(self) -> None:
        self._run(["clear", *self.attrs])


class FileStore:
    """A 0600 file in a 0700 directory; refuses anything looser. The file is
    ``<directory>/<basename>.json``."""
    name = "file"

    def __init__(self, directory: Path, basename: str = "credentials") -> None:
        self.dir = Path(directory)
        self.basename = basename
        self.path = self.dir / f"{basename}.json"

    @staticmethod
    def _check(st: os.stat_result, what: str, mode: int) -> None:
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise CredentialError(f"{what} is not owned by the current user")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise CredentialError(
                f"refusing to use {what}: permissions {oct(stat.S_IMODE(st.st_mode))} "
                f"are looser than {oct(mode)}")

    def _check_dir(self) -> None:
        st = os.lstat(self.dir)
        if not stat.S_ISDIR(st.st_mode):
            raise CredentialError("credentials directory is not a directory")
        self._check(st, "credentials directory", 0o700)

    def load(self) -> dict | None:
        if not self.dir.exists():
            return None
        self._check_dir()
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as e:
            raise CredentialError("cannot open the credentials file") from e
        with os.fdopen(fd, "r", encoding="ascii", errors="replace") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise CredentialError("credentials file is not a regular file")
            self._check(st, "credentials file", 0o600)
            return _decode(fh.read(MAX_BLOB + 1))

    def save(self, creds: dict) -> None:
        old = os.umask(0o077)
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_dir()
            tmp = self.dir / f".{self.basename}.{os.getpid()}.tmp"
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    fh.write(_encode(creds))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        finally:
            os.umask(old)

    def delete(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def backend_name() -> str:
    """Which backend to use: ``keychain``, ``secret-service`` or ``file``."""
    forced = os.environ.get(FORCE_ENV, "").strip().lower()
    if forced in ("keychain", "secret-service", "file"):
        return forced
    if forced:
        raise CredentialError(f"unknown {FORCE_ENV} {forced!r}")
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    if sys.platform.startswith("linux") and shutil.which("secret-tool"):
        return "secret-service"
    return "file"


def default_store(service: str, account: str = ACCOUNT, *, label: str = "brindle",
                  directory: Path | None = None, basename: str | None = None):
    """The store for ``account`` under ``service``. The file backend keeps it
    in ``directory`` (default ``$BRINDLE_HOME/<service>``) as ``<basename>.json``
    (default ``account``)."""
    backend = backend_name()
    if backend == "keychain":
        return KeychainStore(service, account, label)
    if backend == "secret-service":
        return SecretServiceStore(service, account, label)
    if directory is None:
        from brindle.config import brindle_home

        directory = brindle_home() / service
    return FileStore(directory, basename or account)


# -- model API keys (`brindle keys`) -------------------------------------------------------------

KEYS_SERVICE = "brindle-keys"
# A Claude subscription session token: Anthropic's terms forbid third parties
# storing it, so brindle never does.
REFUSED = ("CLAUDE_CODE_OAUTH_TOKEN",)


class KeyNameError(CredentialError):
    """A key name brindle won't store."""


def _key_store(name: str):
    # Local invariant: the name becomes a file name and a quoted keychain argument.
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise KeyNameError("not a valid variable name")
    return default_store(KEYS_SERVICE, name, label="brindle key")


def known_names(repo_root: str | None = None) -> list[str]:
    """Every variable a provider reads or a profile's ``api_key_env`` names,
    less what brindle never stores (REFUSED, secrets.SECRET_ENV and GH_*)."""
    from brindle import profiles, providers, secrets

    names: set[str] = set()
    for group in (*secrets._CREDENTIALS.values(), *providers._ENV_AUTH.values()):
        names.update(group)
    try:
        names.update(p.api_key_env for p in profiles.list_profiles(repo_root) if p.api_key_env)
    except Exception:  # noqa: BLE001 - a broken profile file must not hide the built-in names
        pass
    return sorted(n for n in names if n not in REFUSED and not secrets.is_job_secret(n))


def check_name(name: str, repo_root: str | None = None) -> None:
    from brindle import secrets

    if name in REFUSED:
        raise KeyNameError(f"{name} is a Claude subscription session token; brindle does not store it "
                        "(sign in with `claude` instead, or use an API key)")
    if secrets.is_job_secret(name):
        raise KeyNameError(f"{name} is one of brindle's own secrets and is never stored as a model key")
    if name not in known_names(repo_root):
        raise KeyNameError(f"{name} is not a variable any provider or profile reads")


def set_key(name: str, value: str, repo_root: str | None = None) -> None:
    check_name(name, repo_root)
    value = value.strip()
    if not value:
        raise KeyNameError("empty key")
    _key_store(name).save({"value": value})


def get_key(name: str) -> str | None:
    rec = _key_store(name).load()
    value = rec.get("value") if rec else None
    return value if isinstance(value, str) and value else None


def unset_key(name: str) -> None:
    _key_store(name).delete()


def list_keys(repo_root: str | None = None) -> dict[str, str]:
    """Stored key names with their last four characters only."""
    out: dict[str, str] = {}
    for name in known_names(repo_root):
        try:
            value = get_key(name)
        except CredentialError:
            continue
        if value:
            out[name] = value[-4:]
    return out


def pane_keys(names, present=()) -> dict[str, str]:
    """The stored keys among ``names`` (an agent's own credential names) that
    aren't in ``present`` or the process environment: the environment beats
    the store. Never raises: a broken store just means no stored keys."""
    out: dict[str, str] = {}
    for name in sorted(names):
        if name in REFUSED or name in present or os.environ.get(name):
            continue
        try:
            value = get_key(name)
        except CredentialError:
            continue
        if value:
            out[name] = value
    return out
