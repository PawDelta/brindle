"""The shared secret store (backends) and the model-key layer on top of it."""
import os
import stat

import pytest

from brindle import keystore, secrets
from brindle.keystore import CredentialError

FAKE = "sk-ant-test-0123456789abcd"


def test_file_backend_round_trip(brindle_home):
    # BRINDLE_PRO_CREDENTIAL_STORE=file is set by the autouse fixture.
    assert isinstance(keystore.default_store("brindle-keys", "ANTHROPIC_API_KEY"), keystore.FileStore)
    store = keystore.default_store("brindle-keys", "ANTHROPIC_API_KEY")
    assert store.load() is None
    store.save({"value": FAKE})
    assert store.load() == {"value": FAKE}
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.dir).st_mode) == 0o700
    assert FAKE not in store.path.read_text()   # base64, not plain
    store.delete()
    assert store.load() is None


def test_entries_are_one_per_variable(brindle_home):
    a = keystore.default_store("brindle-keys", "ANTHROPIC_API_KEY")
    b = keystore.default_store("brindle-keys", "OPENAI_API_KEY")
    a.save({"value": "sk-ant-test-aaaa"})
    b.save({"value": "sk-test-bbbb"})
    assert a.path != b.path and a.load() == {"value": "sk-ant-test-aaaa"}


def test_file_backend_refuses_loose_permissions(brindle_home):
    store = keystore.default_store("brindle-keys", "OPENAI_API_KEY")
    store.save({"value": "sk-test-1"})
    os.chmod(store.path, 0o644)
    with pytest.raises(CredentialError, match="permissions"):
        store.load()


def test_unknown_backend_is_an_error(monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "bogus")
    with pytest.raises(CredentialError):
        keystore.default_store("brindle-keys", "X")


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

def test_the_oauth_token_is_refused(brindle_home):
    with pytest.raises(CredentialError, match="subscription"):
        keystore.set_key("CLAUDE_CODE_OAUTH_TOKEN", FAKE)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in keystore.known_names()
    assert keystore.default_store("brindle-keys", "CLAUDE_CODE_OAUTH_TOKEN").load() is None


@pytest.mark.parametrize("name", [*secrets.SECRET_ENV, "GH_TOKEN"])
def test_brindles_own_secrets_are_refused(brindle_home, name):
    with pytest.raises(CredentialError, match="never stored"):
        keystore.set_key(name, FAKE)


def test_unrelated_names_are_refused(brindle_home):
    with pytest.raises(CredentialError, match="not a variable"):
        keystore.set_key("PATH", "x")


def test_provider_and_profile_names_are_accepted(brindle_home, repo):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"):
        keystore.check_name(name)
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True)
    (d / "mine.md").write_text("---\nname: mine\nprovider: native\napi: openai\n"
                               "base_url: https://example.test/v1\nmodel: m\n"
                               "api_key_env: MY_MODEL_KEY\n---\nWork.\n")
    with pytest.raises(CredentialError):
        keystore.check_name("MY_MODEL_KEY")
    keystore.set_key("MY_MODEL_KEY", FAKE, str(repo))
    assert keystore.get_key("MY_MODEL_KEY") == FAKE


def test_list_shows_only_the_last_four(brindle_home):
    keystore.set_key("ANTHROPIC_API_KEY", FAKE)
    assert keystore.list_keys() == {"ANTHROPIC_API_KEY": FAKE[-4:]}
    keystore.unset_key("ANTHROPIC_API_KEY")
    assert keystore.list_keys() == {}


# -- the panes -----------------------------------------------------------------------------------

def test_the_environment_beats_the_store(brindle_home, monkeypatch):
    keystore.set_key("ANTHROPIC_API_KEY", FAKE)
    keystore.set_key("OPENAI_API_KEY", "sk-test-openai-1234")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert keystore.pane_keys({"ANTHROPIC_API_KEY"}) == {"ANTHROPIC_API_KEY": FAKE}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-exported")
    assert keystore.pane_keys({"ANTHROPIC_API_KEY"}) == {}
    # ...and so does what the launch itself sets for the pane
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert keystore.pane_keys({"ANTHROPIC_API_KEY"}, present={"ANTHROPIC_API_KEY": "x"}) == {}


def test_a_broken_store_gives_no_keys_and_no_error(brindle_home, monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_CREDENTIAL_STORE", "bogus")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert keystore.pane_keys({"ANTHROPIC_API_KEY"}) == {}


def test_free_tier_does_not_import_pro():
    import ast
    import pathlib

    for mod in (keystore, __import__("brindle.keys_cmds", fromlist=["x"])):
        for node in ast.walk(ast.parse(pathlib.Path(mod.__file__).read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or "", *[f"{node.module}.{a.name}" for a in node.names]]
                     if isinstance(node, ast.ImportFrom) else [])
            assert not any(n == "brindle.pro" or n.startswith("brindle.pro.") for n in names), mod.__name__
