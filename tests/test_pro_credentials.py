"""brindle Pro's credentials keep their behavior on top of brindle.keystore."""
import os
import stat

import pytest

from brindle import keystore
from brindle.pro import credentials


def test_pro_uses_the_shared_backends_with_its_own_service(brindle_home):
    assert credentials.SERVICE == "brindle-pro"
    assert issubclass(credentials.KeychainStore, keystore.KeychainStore)
    assert issubclass(credentials.SecretServiceStore, keystore.SecretServiceStore)
    assert issubclass(credentials.FileStore, keystore.FileStore)
    assert credentials.KeychainStore().service == "brindle-pro"
    assert credentials.SecretServiceStore().attrs[:2] == ["service", "brindle-pro"]
    assert credentials.CredentialError is keystore.CredentialError


def test_file_round_trip_keeps_pro_location_and_modes(brindle_home):
    store = credentials.default_store()   # BRINDLE_PRO_CREDENTIAL_STORE=file
    assert isinstance(store, credentials.FileStore)
    assert store.path == brindle_home / "pro" / "credentials.json"
    store.save({"access_token": "tok-test-1"})
    assert store.load() == {"access_token": "tok-test-1"}
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    other = credentials.default_store(credentials.LEARNING_SECRET)
    assert other.path == brindle_home / "pro" / "learning-secret.json"
    store.delete()
    assert store.load() is None


def test_forced_backend_and_unknown_override(monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "keychain")
    assert isinstance(credentials.default_store(), credentials.KeychainStore)
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "bogus")
    with pytest.raises(credentials.CredentialError):
        credentials.default_store()
