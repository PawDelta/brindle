"""The shared secret store (backends) and the model-key layer on top of it."""
import os
from subprocess import CompletedProcess

import pytest

from brindle import keystore
from brindle.keystore import CredentialError

FAKE = "sk-ant-test-0123456789abcd"


@pytest.mark.parametrize("record,external", [
    ({"value": FAKE, "updated_at": 7}, None),
    ({"value": FAKE, "updated_at": 7}, {"value": "external-replacement"}),
    (None, None),
])
def test_keychain_read_cached(monkeypatch, record, external):
    from starlette.requests import Request
    from brindle import preflight, providers
    from brindle.daemon.routes_read import provider_preflight

    now, calls = [100.0], []
    stored = [record]

    def run(self, args, stdin=None):
        calls.append(args[0])
        if args[0] == "-i":
            stored[0] = {"value": "replacement"}
        elif args[0] == "delete-generic-password":
            stored[0] = None
        return CompletedProcess(args, 44 if stored[0] is None else 0,
                                keystore._encode(stored[0]) if stored[0] else "", "")

    monkeypatch.setattr(keystore.KeychainStore, "_run", run)
    monkeypatch.setattr(keystore.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(keystore, "_keychain_reads", {}, raising=False)
    monkeypatch.setenv(keystore.FORCE_ENV, "keychain")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(preflight, "turned_off", lambda provider: None)
    monkeypatch.setattr(providers, "which_cli", lambda exe: exe)
    monkeypatch.setattr(providers, "agy_uses_gemini_key", lambda: True)
    service, account = keystore.KEYS_SERVICE, "GEMINI_API_KEY"
    store = keystore.KeychainStore(service, account)
    assert store.load() == record
    # ProviderSignIn polls this real handler every three seconds.
    request = Request({"type": "http", "path_params": {"provider": "antigravity"}, "query_string": b""})
    for _ in range(2):
        provider_preflight.__wrapped__(None, request)
    assert calls.count("find-generic-password") == 1
    keystore.KeychainStore(service, "other-account").load()
    keystore.KeychainStore(service + "-other", account).load()
    assert calls.count("find-generic-password") == 3
    now[0] += keystore.KEYCHAIN_READ_TTL
    assert store.load() == record
    assert calls.count("find-generic-password") == 4
    stored[0] = external  # another process mutates the key without invalidating our cache
    assert not keystore.set_check(account, "verified", FAKE)
    assert stored[0] == external
    store.save({"value": "replacement"})
    assert keystore.KeychainStore(service, account).load() == {"value": "replacement"}
    assert calls.count("find-generic-password") == 6
    store.delete()
    assert keystore.KeychainStore(service, account).load() is None
    assert store.load() is None
    assert calls.count("find-generic-password") == 7
    now[0] += keystore.KEYCHAIN_READ_TTL
    monkeypatch.setattr(keystore.KeychainStore, "_run", lambda self, args, stdin=None:
                        calls.append(args[0]) or CompletedProcess(args, 1, FAKE, FAKE))
    for _ in range(2):
        with pytest.raises(CredentialError) as exc:
            store.load()
        assert FAKE not in str(exc.value)
    assert calls.count("find-generic-password") == 9


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


# -- the panes -----------------------------------------------------------------------------------


def test_a_broken_store_gives_no_keys_and_no_error(brindle_home, monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "bogus")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert keystore.pane_keys({"ANTHROPIC_API_KEY"}) == {}
