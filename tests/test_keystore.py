"""The shared secret store (backends) and the model-key layer on top of it."""
import os
from subprocess import CompletedProcess, TimeoutExpired

import pytest
from typer.testing import CliRunner

from brindle import keys_cmds, keystore, spawn
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
    real_run = keystore.KeychainStore._run

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

    def fails_twice(fake_run, reads: int) -> None:
        now[0] += keystore.KEYCHAIN_READ_TTL
        monkeypatch.setattr(keystore.KeychainStore, "_run", fake_run)
        before = calls.count("find-generic-password")
        for _ in range(2):
            with pytest.raises(CredentialError) as exc:
                keystore.KeychainStore(service, account).load()
            assert FAKE not in str(exc.value)
        assert calls.count("find-generic-password") == before + reads

    def exits(code):
        return lambda self, args, stdin=None: calls.append(args[0]) or CompletedProcess(args, code, FAKE, FAKE)

    def unanswered(self, args, stdin=None):     # the dialog sat there until the read gave up
        calls.append(args[0])
        raise TimeoutExpired(args, 1)

    fails_twice(exits(1), 2)                    # no dialog came of it: nothing to hold back
    fails_twice(exits(128), 1)                  # a cancelled dialog is asked once, not at each poll...
    os.utime(keystore._refused_marker(), (0, 0))
    fails_twice(exits(128), 0)                  # ...however long the app stays open
    assert CliRunner().invoke(keys_cmds.app, ["unblock"]).exit_code == 0
    monkeypatch.setattr(keystore.KeychainStore, "_run", run)
    before = calls.count("find-generic-password")
    keystore.KeychainStore(service, account).load()
    assert calls.count("find-generic-password") == before + 1
    fails_twice(unanswered, 1)
    monkeypatch.setattr(keystore.KeychainStore, "_run", run)
    keystore.KeychainStore(service, account).save({"value": "replacement"})     # saving a key asks too
    assert keystore.KeychainStore(service, account).load() == {"value": "replacement"}

    started = []

    class Security:     # through the real spawn.read: the client goes away while the lookup waits (on a dialog, say)
        pid, returncode = 2 ** 30, None

        def __init__(self, argv, **kw):
            started.append(argv)

        def communicate(self, input=None, timeout=None):
            if not request.stopped:
                request.stop("the client went away")
                raise TimeoutExpired("security", 1)
            self.returncode = 0
            return keystore._encode({"value": "answered"}), ""

    now[0] += keystore.KEYCHAIN_READ_TTL
    with monkeypatch.context() as m, spawn.deadline(60) as request:
        m.setattr(spawn.subprocess, "Popen", Security)
        m.setattr(keystore.KeychainStore, "_run", real_run)
        for _ in range(2):     # it runs to its answer, so the next poll doesn't start it (and any dialog) over
            assert keystore.KeychainStore(service, account).load() == {"value": "answered"}
    assert len(started) == 1 and not keystore._refused_marker().exists()

    def no_room(self, args, stdin=None):
        raise spawn.Busy("no room for it")

    now[0] += keystore.KEYCHAIN_READ_TTL
    monkeypatch.setattr(keystore.KeychainStore, "_run", no_room)
    with pytest.raises(spawn.Busy):     # never started: nothing asked, so nothing held back
        keystore.KeychainStore(service, account).load()
    assert not keystore._refused_marker().exists()


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
