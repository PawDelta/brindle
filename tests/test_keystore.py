"""The shared secret store (backends) and the model-key layer on top of it."""
import os

import pytest

from brindle import keystore
from brindle.keystore import CredentialError

FAKE = "sk-ant-test-0123456789abcd"


def test_file_backend_refuses_loose_permissions(brindle_home):
    store = keystore.default_store("brindle-keys", "OPENAI_API_KEY")
    store.save({"value": "sk-test-1"})
    os.chmod(store.path, 0o644)
    with pytest.raises(CredentialError, match="permissions"):
        store.load()


def test_keychain_and_secret_service_keep_the_secret_off_argv(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(keystore.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or R())
    keystore.KeychainStore("brindle-keys", "OPENAI_API_KEY").save({"value": FAKE})
    keystore.SecretServiceStore("brindle-keys", "OPENAI_API_KEY").save({"value": FAKE})
    for argv, kw in calls:
        assert FAKE not in " ".join(argv) and keystore._encode({"value": FAKE}) in kw["input"]
    assert calls[0][0] == ["security", "-i"] and calls[1][0][0] == "secret-tool"


# -- names ---------------------------------------------------------------------------------------


def test_list_shows_only_the_last_four(brindle_home):
    keystore.set_key("ANTHROPIC_API_KEY", FAKE)
    assert keystore.list_keys() == {"ANTHROPIC_API_KEY": FAKE[-4:]}
    keystore.unset_key("ANTHROPIC_API_KEY")
    assert keystore.list_keys() == {}


def test_reading_asks_the_store_for_each_key_once_and_only_inside_it(brindle_home, monkeypatch):
    # GET /routing's learned picks check every candidate, each reading the same keys (a `security` run apiece).
    keystore.set_key("ANTHROPIC_API_KEY", FAKE)
    loads = []
    real = keystore.FileStore.load
    monkeypatch.setattr(keystore.FileStore, "load", lambda self: loads.append(1) or real(self))
    with keystore.reading():
        assert [keystore.get_key("ANTHROPIC_API_KEY") for _ in range(3)] == [FAKE] * 3
    assert len(loads) == 1
    keystore.get_key("ANTHROPIC_API_KEY")
    assert len(loads) == 2


# -- the panes -----------------------------------------------------------------------------------


def test_a_broken_store_gives_no_keys_and_no_error(brindle_home, monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "bogus")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert keystore.pane_keys({"ANTHROPIC_API_KEY"}) == {}
