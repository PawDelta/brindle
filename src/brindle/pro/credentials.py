"""Where brindle Pro keeps its credentials (entitlement + refresh token).

The backends (macOS keychain, Linux Secret Service, a 0600 file) live in
:mod:`brindle.keystore`; this keeps Pro's service name (``brindle-pro``) and
its file location, ``$BRINDLE_HOME/pro/credentials.json``.
``BRINDLE_PRO_CREDENTIAL_STORE=keychain|secret-service|file`` forces one.
Secrets are never logged.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - tests patch credentials.subprocess.run (the same module keystore uses)
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Lock

from brindle import keystore
from brindle.keystore import ACCOUNT, MAX_BLOB, CredentialError, _decode, _encode  # noqa: F401

SERVICE = "brindle-pro"
LABEL = "brindle Pro"


class KeychainStore(keystore.KeychainStore):
    def __init__(self, service: str = SERVICE, account: str = ACCOUNT) -> None:
        super().__init__(service, account, LABEL)


class SecretServiceStore(keystore.SecretServiceStore):
    def __init__(self, service: str = SERVICE, account: str = ACCOUNT) -> None:
        super().__init__(service, account, LABEL)


def file_store_dir() -> Path:
    """The directory the file backend keeps Pro's credentials in: ``$BRINDLE_HOME/pro``."""
    from brindle.config import brindle_home

    return brindle_home() / "pro"


class FileStore(keystore.FileStore):
    def __init__(self, directory: Path | None = None, account: str = ACCOUNT) -> None:
        super().__init__(directory if directory is not None else file_store_dir(),
                         "credentials" if account == ACCOUNT else account)


LEARNING_SECRET = "learning-secret"


def default_store(account: str = ACCOUNT):
    """The credential store for ``account`` (``default``: login credentials;
    ``learning-secret``: the per-install HMAC key for hosted learning, kept
    apart so logging out doesn't reset it)."""
    backend = keystore.backend_name()
    if backend == "keychain":
        return KeychainStore(account=account)
    if backend == "secret-service":
        return SecretServiceStore(account=account)
    return FileStore(account=account)


class _RequestCredentials:
    def __init__(self):
        self.lock = Lock()
        self.default = None
        self.records = {}


_request: ContextVar[_RequestCredentials | None] = ContextVar("request_credentials", default=None)


@contextmanager
def reading():
    """Share the Library sign-in across layers and worker threads of one request."""
    token = _request.set(_RequestCredentials())
    try:
        yield
    finally:
        _request.reset(token)


def request_store():
    request = _request.get()
    if request is None:
        return None
    with request.lock:
        if request.default is None:
            request.default = default_store()
        return request.default


def request_load(store):
    """Read the request's record. Refresh and mutations use the live store directly."""
    request = _request.get()
    if request is None or keystore.reading_uncached():
        return store.load()
    with request.lock:
        # Preserve identity even for a store with custom equality or no hash.
        key = id(store)
        if key not in request.records:
            request.records[key] = store, store.load()
        return request.records[key][1]


def forget_request_records() -> None:
    request = _request.get()
    if request is not None:
        with request.lock:
            request.records.clear()
