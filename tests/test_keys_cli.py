"""`brindle keys set|list|unset`."""
from typer.testing import CliRunner

from brindle import keystore
from brindle.cli import app

FAKE = "sk-ant-test-0123456789wxyz"
runner = CliRunner()


def test_set_list_unset_round_trip(brindle_home):
    r = runner.invoke(app, ["keys", "set", "ANTHROPIC_API_KEY"], input=FAKE + "\n")
    assert r.exit_code == 0 and FAKE not in r.output
    assert keystore.get_key("ANTHROPIC_API_KEY") == FAKE
    r = runner.invoke(app, ["keys", "list"])
    assert r.exit_code == 0 and "ANTHROPIC_API_KEY" in r.output and "wxyz" in r.output
    assert FAKE not in r.output and FAKE[:-4] not in r.output
    r = runner.invoke(app, ["keys", "unset", "ANTHROPIC_API_KEY"])
    assert r.exit_code == 0 and keystore.get_key("ANTHROPIC_API_KEY") is None
    assert "ANTHROPIC_API_KEY" not in runner.invoke(app, ["keys", "list"]).output


def test_set_refuses_the_oauth_token_and_job_secrets(brindle_home):
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN", "BRINDLE_PRO_TOKEN", "GH_TOKEN", "PATH"):
        r = runner.invoke(app, ["keys", "set", name], input=FAKE + "\n")
        assert r.exit_code == 1, name
        assert FAKE not in r.output
        assert keystore.default_store("brindle-keys", name).load() is None


def test_unset_cannot_escape_the_key_directory(brindle_home):
    victim = brindle_home / "victim.json"
    brindle_home.mkdir(parents=True, exist_ok=True)
    victim.write_text("{}")
    for name in ("../victim", "../../victim", "a/b", 'A"B'):
        r = runner.invoke(app, ["keys", "unset", name])
        assert r.exit_code == 1, name
    assert victim.exists()


def test_an_empty_value_is_refused(brindle_home):
    r = runner.invoke(app, ["keys", "set", "OPENAI_API_KEY"], input="\n")
    assert r.exit_code == 1 and keystore.get_key("OPENAI_API_KEY") is None
