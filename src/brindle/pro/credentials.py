"""Where brindle Pro keeps its credentials (entitlement + refresh token).

The backends (macOS keychain, Linux Secret Service, a 0600 file) live in
:mod:`brindle.keystore`; this keeps Pro's service name (``brindle-pro``) and
its file location, ``$BRINDLE_HOME/pro/credentials.json``.
``BRINDLE_PRO_CREDENTIAL_STORE=keychain|secret-service|file`` forces one.
Secrets are never logged.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - tests patch credentials.subprocess.run (the same module keystore uses)
from pathlib import Path

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


class FileStore(keystore.FileStore):
    def __init__(self, directory: Path | None = None, account: str = ACCOUNT) -> None:
        if directory is None:
            from brindle.config import brindle_home

            directory = brindle_home() / "pro"
        super().__init__(directory, "credentials" if account == ACCOUNT else account)


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
